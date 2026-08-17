"""Read-only: which live room-stay rows disagree with the catalogue, and why.

Nothing in the system warned when a charge stopped matching the catalogue, and that
silence hid two unrelated faults on the same day - four templates saved with
`animal_type = All` so every dog priced as a cat, and gunicorn workers still holding a
pre-Stage-3b module image so new rows were written by deleted code. Both were found by a
person noticing a number on a screen. This is the check that would have found either in
seconds.

It changes NOTHING. No writes, no saves, no side effects - it is safe to run against a
live site at any time, and safe to schedule.

The hard part is not spotting a mismatch, it is saying which mismatches matter. A rate a
member of staff typed is legitimate and is never overwritten, so reporting it as an error
would train everyone to ignore this report. The distinction drawn here:

    item_mismatch   the row names a DIFFERENT ITEM than the catalogue prices for this
                    pet and boarding type. Staff adjust rates, not item codes - this is
                    the signature of a stale write, and it is what BRD-00090 looked like.
    legacy_key      the row's linked_service_id is keyed on boarding type, a shape only
                    pre-Stage-3b code produced. Definitive proof of a stale worker.
    unresolvable    the catalogue cannot price this pet at all - an ambiguous or missing
                    template. Blocks new charges.
    missing_pet     the row does not say which animal it is for.
    rate_adjusted   right item, different price. Reported as INFORMATION, not a fault:
                    this is what a deliberate adjustment looks like.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr, flt

from pet_app.api.response import standardize_response
from pet_app.utils.boarding_pricing import resolve_boarding_rate

ROOM_STAY_SERVICE_PREFIX = "boarding_room_stay"

# Findings that mean something is wrong, as opposed to something being deliberate.
FAULT_KINDS = ("item_mismatch", "legacy_key", "unresolvable", "missing_pet")


@frappe.whitelist()
@standardize_response
def room_stay_rate_drift(include_billed=0, include_adjusted=1, limit=1000):
	"""Compare every live room-stay charge against what the catalogue would price it at.

	`include_billed` off by default: rows on submitted bookings are frozen against issued
	invoices and must never be changed, so listing them by default would fill the report
	with findings nobody may act on. Turn it on to audit history.
	"""
	include_billed = int(include_billed or 0)
	include_adjusted = int(include_adjusted or 1)

	rows = frappe.db.sql(
		"""
		SELECT i.name AS row_name, i.parent AS boarding, i.pet, i.item_code, i.rate, i.qty,
		       i.status, i.linked_service_id, i.note,
		       b.record_status, b.docstatus, b.sales_invoice, b.boarding_type AS booking_type,
		       o.boarding_type AS occupant_type, p.animal_type, p.pet_name
		FROM `tabPet Billable Item` i
		JOIN `tabPet Boarding` b ON b.name = i.parent
		LEFT JOIN `tabPet Boarding Occupant` o
		       ON o.parent = b.name AND o.parenttype = 'Pet Boarding' AND o.pet = i.pet
		LEFT JOIN `tabPet` p ON p.name = i.pet
		WHERE i.parenttype = 'Pet Boarding'
		  AND i.item_type = 'Room Stay'
		  AND i.status != 'Cancelled'
		  AND (%(include_billed)s = 1 OR (b.docstatus = 0 AND IFNULL(b.sales_invoice, '') = ''))
		ORDER BY b.creation DESC
		LIMIT %(limit)s
		""",
		{"include_billed": include_billed, "limit": int(limit)},
		as_dict=True,
	)

	findings = []
	checked = 0
	for row in rows:
		checked += 1
		finding = _classify(row)
		if not finding:
			continue
		if finding["kind"] == "rate_adjusted" and not include_adjusted:
			continue
		findings.append(finding)

	faults = [f for f in findings if f["kind"] in FAULT_KINDS]
	return {
		"checked": checked,
		"faults": len(faults),
		"adjusted": len([f for f in findings if f["kind"] == "rate_adjusted"]),
		"clean": not faults,
		"findings": findings,
	}


def _classify(row) -> dict | None:
	pet = cstr(row.pet).strip()
	if not pet:
		return _finding(row, "missing_pet", _("Charge does not say which animal it is for."))

	boarding_type = cstr(row.occupant_type).strip() or cstr(row.booking_type).strip()
	try:
		resolved = resolve_boarding_rate(pet, boarding_type)
	except Exception as exc:
		return _finding(row, "unresolvable", cstr(exc))

	# Written by code that keyed the row on boarding type rather than on the pet. Only
	# pre-Stage-3b code did that, so this is a deployment fact rather than a pricing one -
	# but it is reported WITH the price comparison, because the useful question about a
	# batch of stale rows is which of them are also charging the wrong amount.
	linked = cstr(row.linked_service_id).strip()
	if linked and linked.startswith(f"{ROOM_STAY_SERVICE_PREFIX}:") and not linked.endswith(f":{pet}"):
		mispriced = (
			cstr(row.item_code).strip() != resolved.item_code or flt(row.rate) != flt(resolved.rate)
		)
		return _finding(
			row,
			"legacy_key",
			_("Written by pre-Stage-3b code (key {0}); {1}. Delete the row so it is rebuilt.").format(
				linked,
				_("price is also wrong - catalogue says {0} at {1}").format(resolved.item_code, resolved.rate)
				if mispriced
				else _("price still matches the catalogue"),
			),
			expected_item=resolved.item_code,
			expected_rate=resolved.rate,
			mispriced=mispriced,
		)

	if cstr(row.item_code).strip() != resolved.item_code:
		return _finding(
			row,
			"item_mismatch",
			_("Charged as {0} but the catalogue prices {1} {2} boarding as {3} at {4}.").format(
				cstr(row.item_code), cstr(row.animal_type), boarding_type, resolved.item_code, resolved.rate
			),
			expected_item=resolved.item_code,
			expected_rate=resolved.rate,
		)

	if flt(row.rate) != flt(resolved.rate):
		return _finding(
			row,
			"rate_adjusted",
			_("Right item, price set to {0} where the catalogue says {1}. Left alone - a rate somebody set is never overwritten.").format(
				flt(row.rate), resolved.rate
			),
			expected_item=resolved.item_code,
			expected_rate=resolved.rate,
		)
	return None


def _finding(row, kind: str, message: str, *, expected_item=None, expected_rate=None, mispriced=None) -> dict:
	return {
		"kind": kind,
		"severity": "fault" if kind in FAULT_KINDS else "info",
		"boarding": row.boarding,
		"row": row.row_name,
		"pet": row.pet,
		"pet_name": row.pet_name,
		"animal_type": row.animal_type,
		"boarding_type": cstr(row.occupant_type).strip() or cstr(row.booking_type).strip(),
		"record_status": row.record_status,
		"charged_item": row.item_code,
		"charged_rate": flt(row.rate),
		"expected_item": expected_item,
		"expected_rate": expected_rate,
		"mispriced": mispriced,
		"message": message,
	}
