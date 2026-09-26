"""Clear references left dangling by the 2026-07-14 live-site force-delete.

A cleanup script (`bench execute exec(open('/tmp/cleanup_live_site_force_remaining.py'))`,
2026-07-14 17:28-17:31 site time) force-deleted test Items, practitioners, users and visits.
`force=True` skips Frappe's link check, so 95 rows kept naming records that no longer exist.
Frappe validates every link on save, historic rows included, so each parent became
unsaveable - PCE-2026-00021 among them, an open care episode that blocked Continue / New /
Close case for PET-00128.

THE PLAN IS AN EXPLICIT, AUDITED LIST - no pattern matching at run time. Each entry names the
row, the field and the dead value. An entry is applied only while all of these still hold:

    1. the target is absent from its table
    2. a Deleted Document for it exists, unrestored, created inside the run's window
    3. the field still holds exactly the dead value

so a second run changes nothing, and a target someone restores in the meantime is left alone.

WHY THE CONTROLLER IS BYPASSED. `doc.save()` on these parents throws the very
LinkValidationError being repaired, so writes go through `frappe.db.set_value` /
`frappe.db.delete`. That is safe here because nothing derived depends on the cleared fields:

    * the medication rows keep `medication` (the human-readable name, a live Medication),
      Pet Billable Item keeps `item_name`, the ledger keeps `medication`
    * episode and profile visit/doctor pointers are navigation snapshots that the next
      visit sync rewrites
    * the one derived value that DOES move - a visit's `total_billable_amount` when its
      billable rows are cancelled - is recomputed here with the controller's own rule
      (sum of non-Cancelled amounts)

and because every changed parent gets an Info Comment recording each old value, so the
trail a Version row would have kept is not lost.

WHY TEST-MEDICATION CHARGES ARE CANCELLED, not just cleared. A billable row without an
Item Code is refused by `_validate_billable_item_rows` unless it is Cancelled, and a
prescription row without a Medication Item is exactly what the visit's own
`_sync_prescribed_medications_billables` turns into a Cancelled charge on its next save. None
of these rows was ever billed (no Sales Invoice, no stock movement, no Stock Ledger Entry for
any deleted item). A row that is Billed or carries an invoice is refused and left untouched.

WHAT IT DELIBERATELY LEAVES (95 audited rows: 72 cleared, 2 removed, 21 left):

    * everything pointing at Item `Stool Test` (3 Labs, 3 Billable charges, 1 service
      template). It was a REAL lab service created by staff, deleted because its name
      contains "test"; Lab has no other field naming the test ordered. Restoring it is the
      owner's decision.
    * `mostafa test - 1` on submitted PetCareService-00208 and disabled CSBO-00017, and
      provider HCP-00015 on submitted PetCareService-00669 / -01487: submitted records that
      nothing re-saves, where the link is the only record of what was done or by whom.
    * `Medication.linked_item` on the 10 disabled test Medications: blanking it arms
      `Medication._resolve_linked_item`, which silently creates a fresh Item on the next save -
      bringing the test data back. The cost of leaving it: a visit save re-fills the cleared
      `medication_item` from that dead link, so the test draft visits VVT-2026-00093, -00098,
      -00099, -00101..-00108 still refuse to save ("Medication Item ... was not found"). They
      are Administrator test visits from 2026-07-09/10; which way to go is the owner's call.

VCS-2026-00115.vet_visit IS cleared: `_sync_status_with_visit` already treats a dead link as
blank, and visit conversion finds its visit by `Vet Visit.case_sheet` and rewrites the field.
"""

from __future__ import annotations

import frappe
from frappe.utils import flt

RUN_WINDOW = ("2026-07-14 17:28:00", "2026-07-14 17:32:00")
PATCH = "clear_force_deleted_test_links"

ITEM = "Item"
HCP = "Healthcare Practitioner"
USER = "User"
VISIT = "Vet Visit"

# (doctype, row name, fieldname, target doctype, dead value)
CLEAR = [
	# Test medication Items: prescription rows keep `medication`.
	*[
		("Vet Visit Medication Item", row, "medication_item", ITEM, dead)
		for dead, rows in {
			"TST-001": ("10audr0i9d", "4jthfvidi4", "iiqmhkps41", "ndnkfv4fv4"),
			"TEST_DOSE_OPTION_KETAMINE_INJECTION": ("1msonfspn0", "450m33vg4q", "bi2foeu9fq", "emflpscp7l"),
			"TEST_DOSE_OPTION_NORMAL_PILL": ("5360piql27", "b6u960n83b", "bi5qaiv3ld"),
			"TEST_DOSE_OPTION_NOS_WHOLE": ("b6tcu8v4lr", "bi3pbs8ptv"),
			"TEST_DOSE_QTY_PILL_bcf068f9": ("f4rhkrcqi5",),
			"TEST_DOSE_QTY_SCALE_55ac5d2c": ("b7ntmgh1ag", "gmsdglm6p8", "j935oiv78u"),
			"TEST_ITEM_UNIT_NORMAL_NO_DEFAULT_DISPENSE": ("1mp6t52k85",),
		}.items()
		for row in rows
	],
	# Pet Care Episode Medication keeps `medication`.
	("Pet Care Episode Medication", "1plr0er1cl", "medication_item", ITEM, "TST-001"),
	("Pet Care Episode Medication", "4hq4bksb6j", "medication_item", ITEM, "TST-001"),
	("Pet Care Episode Medication", "ndn6u3t0r5", "medication_item", ITEM, "TST-001"),
	("Pet Care Episode Medication", "gmss3qfepe", "medication_item", ITEM, "TEST_DOSE_QTY_SCALE_55ac5d2c"),
	# Ledger row: stock_qty 0, no Stock Entry - a status record, it keeps `medication`.
	("Medication Dispense Ledger", "MDL-00002", "medication_item", ITEM, "TST-001"),
	# Open episodes pointing at a deleted test practitioner / deleted visits.
	("Pet Care Episode", "PCE-2026-00025", "primary_doctor", HCP, "HCP-00015"),
	*[("Pet Care Episode", "PCE-2026-00025", f, VISIT, "VVT-2026-00110") for f in ("opened_visit", "current_visit", "last_visit", "opened_from_name")],
	*[("Pet Care Episode", "PCE-2026-00030", f, VISIT, "VVT-2026-00124") for f in ("opened_visit", "current_visit", "last_visit", "opened_from_name")],
	("Pet Care Episode Medication", "bt97le9q9j", "source_visit", VISIT, "VVT-2026-00124"),
	("Vet Case Sheet", "VCS-2026-00115", "vet_visit", VISIT, "VVT-2026-00124"),
	# Pet Medical Profile snapshot pointers.
	("Pet Medical Profile", "PET-01134", "current_doctor", HCP, "HCP-00015"),
	("Pet Medical Profile", "PET-01134", "last_doctor", HCP, "HCP-00015"),
	*[("Pet Medical Profile", "PET-01134", f, VISIT, "VVT-2026-00111") for f in ("current_visit", "last_visit", "last_completed_visit", "last_synced_from_name")],
	*[("Pet Medical Profile", "PET-01188", f, VISIT, "VVT-2026-00124") for f in ("current_visit", "last_visit", "last_synced_from_name")],
	*[("Pet Medical Profile", pet, f, HCP, "HCP-00027") for pet in ("PET-01199", "PET-01202") for f in ("current_doctor", "last_doctor")],
	*[("Pet Medical Profile", "PET-01203", f, HCP, "HCP-00026") for f in ("current_doctor", "last_doctor")],
	("Pet Medical Profile", "PET-01213", "last_completed_visit", VISIT, "VVT-2026-00134"),
	# Deleted test users.
	("Guardian", "GUARDIAN-00282", "user_id", USER, "testmobile@alkokh.com"),
	("Guardian", "GUARDIAN-00285", "user_id", USER, "07799999999@petapp.local"),
	("Vet Visit Vital Sign", "hkfd75cj4f", "recorded_by", USER, "cor@test.mn"),
	("Rating", "RATING-2026-00558", "rated_by", USER, "cor@test.mn"),
]

# Charges for deleted test medication Items: item_code cleared above-style, and cancelled.
CANCEL_BILLABLE = {
	"TST-001": ("10aqo8uclr", "4jtsgivtnj", "iiql55g55h", "ndno2iadq4"),
	"TEST_DOSE_OPTION_KETAMINE_INJECTION": ("1ms49rk04u", "45021cpvrd", "bi2p48ddpm", "emf6n5if85"),
	"TEST_DOSE_OPTION_NORMAL_PILL": ("536a872g9r", "b6uococs6f", "bi522kqadr"),
	"TEST_DOSE_OPTION_NOS_WHOLE": ("b6tbtg2nq6", "bi33o7au83"),
	"TEST_DOSE_QTY_PILL_bcf068f9": ("f4rbng3ad1",),
	"TEST_DOSE_QTY_SCALE_55ac5d2c": ("b7nviv6lvq", "gmso5g6vhf", "j93h0b6tat"),
	"TEST_ITEM_UNIT_NORMAL_NO_DEFAULT_DISPENSE": ("1mpluucsb6",),
}

# Assignment rows naming the deleted test practitioner "prov" (user cor@test.mn). The row has
# no name field of its own, so a blanked row would say nothing; it is removed instead.
REMOVE = [
	("Pet Care Episode Assigned Practitioner", "32b8ug1vdg", "practitioner", HCP, "HCP-00015"),
	("Pet Care Episode Assigned Practitioner", "p872ohmhml", "practitioner", HCP, "HCP-00015"),
]


def execute():
	notes: dict[tuple[str, str], list[str]] = {}
	visits_to_total: set[str] = set()
	target_ok: dict[tuple[str, str], bool] = {}

	def dead(target_dt, value):
		key = (target_dt, value)
		if key not in target_ok:
			target_ok[key] = not frappe.db.exists(target_dt, value) and bool(
				frappe.db.exists(
					"Deleted Document",
					{
						"deleted_doctype": target_dt,
						"deleted_name": value,
						"restored": 0,
						"creation": ["between", RUN_WINDOW],
					},
				)
			)
		return target_ok[key]

	def row_state(doctype, row, fieldname):
		fields = [fieldname, "parent", "parenttype"] if frappe.get_meta(doctype).istable else [fieldname]
		return frappe.db.get_value(doctype, row, fields, as_dict=True)

	def note(doctype, row, state, text):
		parent = (state.get("parenttype"), state.get("parent")) if state.get("parent") else (doctype, row)
		notes.setdefault(parent, []).append(text)

	for doctype, row, fieldname, target_dt, value in CLEAR:
		state = row_state(doctype, row, fieldname)
		if not state or state.get(fieldname) != value or not dead(target_dt, value):
			continue
		frappe.db.set_value(doctype, row, fieldname, None, update_modified=False)
		note(doctype, row, state, f"{doctype} {row}: {fieldname} was '{value}' (deleted {target_dt})")

	for value, rows in CANCEL_BILLABLE.items():
		if not dead(ITEM, value):
			continue
		for row in rows:
			state = frappe.db.get_value(
				"Pet Billable Item", row, ["item_code", "status", "sales_invoice", "parent", "parenttype"], as_dict=True
			)
			if not state or state.item_code != value:
				continue
			if state.status == "Billed" or state.sales_invoice:
				frappe.log_error(
					f"{PATCH}: Pet Billable Item {row} is billed ({state.sales_invoice}); left untouched.", PATCH
				)
				continue
			frappe.db.set_value(
				"Pet Billable Item", row, {"item_code": None, "status": "Cancelled"}, update_modified=False
			)
			note(
				"Pet Billable Item",
				row,
				state,
				f"Pet Billable Item {row}: item_code was '{value}' (deleted Item), status {state.status} -> Cancelled",
			)
			if state.parenttype == "Vet Visit":
				visits_to_total.add(state.parent)

	for doctype, row, fieldname, target_dt, value in REMOVE:
		state = row_state(doctype, row, fieldname)
		if not state or state.get(fieldname) != value or not dead(target_dt, value):
			continue
		frappe.db.delete(doctype, {"name": row, fieldname: value})
		note(doctype, row, state, f"{doctype} row {row} removed: {fieldname} was '{value}' (deleted {target_dt})")

	for visit in sorted(visits_to_total):
		total = sum(
			flt(r.amount)
			for r in frappe.get_all(
				"Pet Billable Item",
				filters={"parent": visit, "parenttype": "Vet Visit", "status": ["!=", "Cancelled"]},
				fields=["amount"],
			)
		)
		frappe.db.set_value("Vet Visit", visit, "total_billable_amount", total, update_modified=False)

	for (parenttype, parent), lines in notes.items():
		frappe.get_doc(
			{
				"doctype": "Comment",
				"comment_type": "Info",
				"reference_doctype": parenttype,
				"reference_name": parent,
				"content": "Cleared references to records force-deleted on 2026-07-14 "
				f"(patch {PATCH}):<br>" + "<br>".join(frappe.utils.escape_html(line) for line in lines),
			}
		).insert(ignore_permissions=True)

	print(f"{PATCH}: {sum(len(v) for v in notes.values())} change(s) on {len(notes)} document(s)")
