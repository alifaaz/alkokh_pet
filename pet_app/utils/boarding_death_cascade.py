"""Death cascade for pets that die while boarded.

When a death is reported against a ``Pet Boarding`` record (``source_doctype ==
"Pet Boarding"``) the front desk expects the system to settle the boarding in one
shot: mark the pet deceased, close the boarding to a terminal checked-out state,
stop room-day accrual at the death datetime, finalise the boarding invoice,
close any still-open clinical documents for that pet, and keep the death record
linked back to the boarding.

Everything here is written to be idempotent and defensive: re-running the
cascade for an already-deceased pet / already-closed boarding must not error,
duplicate work, or double-cancel anything.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr, flt, get_datetime, getdate

from pet_app.pet_app.doctype.pet_boarding.pet_boarding import CLOSED_BOARDING_STATUSES


# Lab / Imaging order statuses that still represent open / pending work.
OPEN_LAB_STATUSES = ("Ordered", "Sample Collected", "In Progress", "Result Entered")
OPEN_IMAGING_STATUSES = ("Ordered", "Scheduled", "In Progress", "Reported")
# PetCareService statuses that are still open (cancellable on death).
OPEN_CARE_SERVICE_STATUSES = ("Pending", "Scheduled", "In Progress", "Ordered", "Active")
# Vet Visit / Vet Case Sheet open statuses.
OPEN_VET_VISIT_STATUSES = ("Draft", "In Progress", "Follow-up Needed")
OPEN_CASE_SHEET_STATUSES = ("Draft", "Waiting Practitioner", "In Consultation")


def run_boarding_death_cascade(death_doc) -> dict:
	"""Run the full boarding death cascade for ``death_doc``.

	``death_doc`` is an inserted ``Pet Death Record`` whose ``source_doctype`` is
	``Pet Boarding``. Returns a small summary dict describing what changed (handy
	for audit logging / tests). Never raises for the idempotent paths -- a
	missing/already-closed boarding is tolerated.
	"""
	summary = {
		"pet_marked_deceased": False,
		"boarding_closed": False,
		"boarding_already_closed": False,
		"invoice": None,
		"closed_clinical_docs": [],
	}

	pet = death_doc.pet
	boarding_name = death_doc.source_name
	if not pet:
		return summary

	death_datetime = get_datetime(death_doc.death_datetime) if death_doc.death_datetime else None

	# 1. Pet -> deceased (after this the pet is no longer bookable/boardable).
	summary["pet_marked_deceased"] = _mark_pet_deceased(pet, death_doc, death_datetime)

	# 2 + 5. Close the boarding and settle its billing.
	if boarding_name and frappe.db.exists("Pet Boarding", boarding_name):
		boarding_result = _close_boarding(boarding_name, death_doc, death_datetime)
		summary.update(boarding_result)

	# 3. Close linked open clinical docs for the pet.
	summary["closed_clinical_docs"] = _close_open_clinical_docs(pet, boarding_name, death_doc)

	return summary


def _mark_pet_deceased(pet: str, death_doc, death_datetime) -> bool:
	"""Set the pet to the deceased state. Idempotent."""
	row = frappe.db.get_value(
		"Pet", pet, ["is_deceased", "status", "pet_status", "death_record"], as_dict=True
	)
	if not row:
		return False

	meta = frappe.get_meta("Pet")
	updates = {}
	if meta.has_field("is_deceased"):
		updates["is_deceased"] = 1
	if meta.has_field("status"):
		updates["status"] = "Deceased"
	if meta.has_field("pet_status"):
		# Existing finalize flow stamps pet_status="Deceased" via set_value (which
		# bypasses Select validation); mirror that so both flows agree.
		updates["pet_status"] = "Deceased"
	if meta.has_field("death_record") and not row.get("death_record"):
		updates["death_record"] = death_doc.name
	if meta.has_field("death_date") and death_datetime:
		updates["death_date"] = death_datetime.date()

	# Nothing left to change -> already deceased; treat as no-op.
	already = bool(row.get("is_deceased")) or row.get("status") == "Deceased"
	frappe.db.set_value("Pet", pet, updates, update_modified=True)
	return not already


def _close_boarding(boarding_name: str, death_doc, death_datetime) -> dict:
	"""Transition the boarding to a closed/checked-out terminal state and settle
	billing up to the death datetime. Idempotent."""
	result = {
		"boarding_closed": False,
		"boarding_already_closed": False,
		"invoice": None,
	}

	boarding = frappe.get_doc("Pet Boarding", boarding_name)
	meta = boarding.meta

	# Always (re)link the death record + outcome flags, even if already closed.
	link_updates = {}
	if meta.has_field("death_record") and boarding.get("death_record") != death_doc.name:
		link_updates["death_record"] = death_doc.name
	if meta.has_field("death_during_boarding") and not boarding.get("death_during_boarding"):
		link_updates["death_during_boarding"] = 1
	if meta.has_field("boarding_outcome") and boarding.get("boarding_outcome") != "Death":
		link_updates["boarding_outcome"] = "Death"

	# If the boarding is already in a terminal state (or submitted), do not
	# re-close or re-invoice -- just ensure the death linkage is stamped.
	already_closed = boarding.record_status in CLOSED_BOARDING_STATUSES or boarding.docstatus != 0
	if already_closed:
		if link_updates:
			frappe.db.set_value("Pet Boarding", boarding.name, link_updates, update_modified=True)
		result["boarding_already_closed"] = True
		return result

	# Stop accrual: cap the stay at the death datetime so no room-day charges
	# accumulate past the death.
	if death_datetime:
		boarding.check_out = death_datetime
	elif not boarding.check_out:
		boarding.check_out = boarding.get("check_in") or death_doc.reported_datetime

	# Settle billing up to the death datetime (mirrors the normal checkout flow,
	# but only when there is something to bill -- a pet that died while merely
	# Reserved has no room-days to charge).
	invoice_name = _settle_boarding_billing(boarding, death_doc)
	result["invoice"] = invoice_name

	for key, value in link_updates.items():
		boarding.set(key, value)

	boarding.record_status = "Checked Out"
	boarding.status = "Closed"
	boarding.workflow_state = "Closed"
	if boarding.meta.has_field("checkout_notes"):
		boarding.checkout_notes = _death_checkout_note(boarding, death_doc)

	boarding.flags.ignore_permissions = True
	boarding.save(ignore_permissions=True)
	boarding.add_comment(
		"Comment",
		_("Boarding closed due to pet death (Death Record {0}) by {1}.").format(
			death_doc.name, frappe.session.user
		),
	)
	frappe.logger("pet_app.boarding").info(
		{
			"event": "BOARDING_DEATH_CLOSED",
			"boarding": boarding.name,
			"death_record": death_doc.name,
			"sales_invoice": invoice_name,
			"user": frappe.session.user,
		}
	)

	if boarding.meta.is_submittable and boarding.docstatus == 0 and boarding.sales_invoice:
		boarding.submit()

	result["boarding_closed"] = True
	return result


def _settle_boarding_billing(boarding, death_doc) -> str | None:
	"""Finalise the boarding invoice up to the death datetime.

	Reuses the existing boarding checkout invoice machinery. Returns the Sales
	Invoice name, or ``None`` when there is nothing billable (e.g. the pet died
	while only Reserved). Idempotent: an already-invoiced boarding is left alone.
	"""
	if boarding.sales_invoice:
		return boarding.sales_invoice

	# Local import avoids a circular import (boarding.py imports this module's
	# package siblings indirectly).
	from pet_app.api.healthcare.boarding import (
		_build_sales_invoice_items,
		_ensure_room_stay_billable_item,
	)
	from pet_app.api.sales import _set_optional_guardian_reference
	from pet_app.utils.guardian_customer import get_or_create_customer_from_guardian

	boarding.customer = boarding.customer or get_or_create_customer_from_guardian(boarding.guardian)

	# Only auto-add the room-stay line if the pet was actually checked in --
	# a Reserved boarding has no room-days to charge.
	if boarding.get("check_in"):
		_ensure_room_stay_billable_item(boarding)
		boarding.run_method("_apply_billable_item_amounts")
		boarding.run_method("_compute_stay_days")
		boarding.run_method("_compute_totals")

	invoice_items = _build_sales_invoice_items(boarding)
	if not invoice_items:
		# Nothing to bill -> close without an invoice; staff can settle manually.
		boarding.billing_status = "Unbilled"
		return None

	if not boarding.customer:
		# Cannot invoice without a customer; close unbilled rather than erroring.
		boarding.billing_status = "Unbilled"
		return None

	invoice = frappe.get_doc(
		{
			"doctype": "Sales Invoice",
			"customer": boarding.customer,
			"posting_date": getdate(boarding.check_out),
			"due_date": getdate(boarding.check_out),
			"items": invoice_items,
			"remarks": _("Pet Boarding {0} settled on pet death (Death Record {1}).").format(
				boarding.name, death_doc.name
			),
		}
	)
	_set_optional_guardian_reference(invoice, boarding.guardian)
	invoice.flags.from_custom_flow = True
	invoice.insert(ignore_permissions=True)
	invoice.add_comment(
		"Comment",
		_("Boarding death settlement invoice created from Pet Boarding {0} by {1}.").format(
			boarding.name, frappe.session.user
		),
	)

	for row in boarding.billable_items or []:
		if row.status != "Cancelled":
			row.status = "Billed"

	boarding.sales_invoice = invoice.name
	boarding.billing_status = "Invoiced"
	return invoice.name


def _death_checkout_note(boarding, death_doc) -> str:
	parts = [_("Closed on pet death (Death Record {0}).").format(death_doc.name)]
	if death_doc.death_datetime:
		parts.append(_("Death datetime: {0}.").format(cstr(death_doc.death_datetime)))
	reason = death_doc.death_reason_category or death_doc.death_reason
	if reason:
		parts.append(_("Reason: {0}.").format(cstr(reason)))
	existing = cstr(boarding.get("checkout_notes")).strip()
	note = " ".join(parts)
	return f"{existing}\n{note}".strip() if existing else note


def _close_open_clinical_docs(pet: str, boarding_name: str | None, death_doc) -> list[dict]:
	"""Cancel/close still-open clinical docs tied to the pet (and boarding where a
	link exists). Uses status guards so nothing already-closed is touched and
	history is preserved (no deletes)."""
	closed: list[dict] = []

	# Lab / Imaging orders -> Cancelled.
	closed += _cancel_orders("Lab", pet, OPEN_LAB_STATUSES, death_doc)
	closed += _cancel_orders("Imaging", pet, OPEN_IMAGING_STATUSES, death_doc)

	# PetCareService -> Cancelled (carries its own death_record link).
	closed += _cancel_care_services(pet, death_doc)

	# Open Vet Visit / Vet Case Sheet for the pet -> closed terminal state.
	closed += _close_vet_visits(pet, death_doc)
	closed += _close_case_sheets(pet, death_doc)

	return closed


def _cancel_orders(doctype: str, pet: str, open_statuses: tuple, death_doc) -> list[dict]:
	if not frappe.db.exists("DocType", doctype):
		return []
	meta = frappe.get_meta(doctype)
	if not meta.has_field("pet") or not meta.has_field("status"):
		return []

	rows = frappe.get_all(
		doctype,
		filters={"pet": pet, "status": ["in", list(open_statuses)], "docstatus": ["<", 2]},
		pluck="name",
		ignore_permissions=True,
	)
	closed = []
	for name in rows:
		updates = {"status": "Cancelled"}
		if meta.has_field("death_record"):
			updates["death_record"] = death_doc.name
		frappe.db.set_value(doctype, name, updates, update_modified=True)
		closed.append({"doctype": doctype, "name": name})
	return closed


def _cancel_care_services(pet: str, death_doc) -> list[dict]:
	doctype = "PetCareService"
	if not frappe.db.exists("DocType", doctype):
		return []
	meta = frappe.get_meta(doctype)
	if not meta.has_field("pet_id") or not meta.has_field("status"):
		return []

	rows = frappe.get_all(
		doctype,
		filters={
			"pet_id": pet,
			"status": ["in", list(OPEN_CARE_SERVICE_STATUSES)],
			"docstatus": ["<", 2],
		},
		pluck="name",
		ignore_permissions=True,
	)
	closed = []
	for name in rows:
		updates = {"status": "Cancelled"}
		if meta.has_field("death_record"):
			updates["death_record"] = death_doc.name
		if meta.has_field("cancellation_reason"):
			updates["cancellation_reason"] = _("Pet deceased (Death Record {0}).").format(death_doc.name)
		if meta.has_field("cancelled_at"):
			updates["cancelled_at"] = death_doc.reported_datetime
		if meta.has_field("cancelled_by"):
			updates["cancelled_by"] = frappe.session.user
		frappe.db.set_value(doctype, name, updates, update_modified=True)
		closed.append({"doctype": doctype, "name": name})
	return closed


def _close_vet_visits(pet: str, death_doc) -> list[dict]:
	doctype = "Vet Visit"
	if not frappe.db.exists("DocType", doctype):
		return []
	meta = frappe.get_meta(doctype)
	if not meta.has_field("animal_patient") or not meta.has_field("status"):
		return []

	rows = frappe.get_all(
		doctype,
		filters={
			"animal_patient": pet,
			"status": ["in", list(OPEN_VET_VISIT_STATUSES)],
			"docstatus": ["<", 2],
		},
		pluck="name",
		ignore_permissions=True,
	)
	closed = []
	for name in rows:
		# Do not collide with the source visit if the death was reported from one.
		frappe.db.set_value(doctype, name, {"status": "Cancelled"}, update_modified=True)
		closed.append({"doctype": doctype, "name": name})
	return closed


def _close_case_sheets(pet: str, death_doc) -> list[dict]:
	doctype = "Vet Case Sheet"
	if not frappe.db.exists("DocType", doctype):
		return []
	meta = frappe.get_meta(doctype)
	if not meta.has_field("animal_patient") or not meta.has_field("status"):
		return []

	rows = frappe.get_all(
		doctype,
		filters={
			"animal_patient": pet,
			"status": ["in", list(OPEN_CASE_SHEET_STATUSES)],
			"docstatus": ["<", 2],
		},
		pluck="name",
		ignore_permissions=True,
	)
	closed = []
	for name in rows:
		frappe.db.set_value(doctype, name, {"status": "Closed"}, update_modified=True)
		closed.append({"doctype": doctype, "name": name})
	return closed
