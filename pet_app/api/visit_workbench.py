from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, cstr

from pet_app.api.link_aliases import enrich_link_aliases, with_link_aliases
from pet_app.api.permissions import require_doctype_permission
from pet_app.api.response import fail, ok
from pet_app.api.workspace import (
	_assert_record_access,
	_billing_snapshot,
	_visit_consult_requests,
	_visit_diagnoses,
	_visit_follow_up,
	_visit_orders,
)
from pet_app.pet_app.doctype.pet_care_episode.pet_care_episode import ACTIVE_EPISODE_STATUSES
from pet_app.utils.medical_profile import get_visit_case_context
from pet_app.workflows import clinical_state


@frappe.whitelist()
def get_visit_workbench(visit=None, visit_id=None, name=None):
	try:
		visit_name = cstr(visit or visit_id or name).strip()
		if not visit_name:
			return fail(_("Visit is required."), code="VALIDATION_ERROR")
		if not frappe.db.exists("Vet Visit", visit_name):
			return fail(_("Visit {0} was not found.").format(visit_name), code="NOT_FOUND")

		require_doctype_permission("Vet Visit", "read")
		_assert_record_access("Vet Visit", visit_name)

		visit_doc = frappe.get_doc("Vet Visit", visit_name)
		return ok(
			{
				"visit": _visit_payload(visit_doc),
				"case_sheet": _linked_doc_payload("Vet Case Sheet", visit_doc.get("case_sheet")),
				"pet": _linked_doc_payload("Pet", visit_doc.get("animal_patient")),
				"guardian": _linked_doc_payload("Guardian", visit_doc.get("guardian")),
				"medical_profile": _medical_profile_payload(visit_doc.get("animal_patient")),
				"active_episode": _active_episode_payload(visit_doc),
				"case_context": get_visit_case_context(visit_doc),
				"active_plan_items": _plan_items(visit_doc),
					"diagnoses": _visit_diagnoses(visit_doc),
					"orders": _visit_orders(visit_doc),
					"medications": _medication_rows(visit_doc),
					"billables": [_billable_row(row) for row in _active_billable_rows(visit_doc)],
					"cancelled_billables": [_billable_row(row) for row in _cancelled_billable_rows(visit_doc)],
					"followups": _visit_follow_up(visit_doc),
					"consults": _visit_consult_requests(visit_doc),
					"billing": _billing_snapshot(visit_doc),
					"permissions": _workbench_permissions(visit_doc),
			}
		)
	except Exception as exc:
		return _error_response(exc)


def _active_episode_payload(visit_doc) -> dict:
	episode_name = visit_doc.get("care_episode")
	if not episode_name and visit_doc.get("animal_patient"):
		episode_name = frappe.db.get_value(
			"Pet Care Episode",
			{"pet": visit_doc.animal_patient, "episode_status": ["in", list(ACTIVE_EPISODE_STATUSES)]},
			"name",
			order_by="modified desc",
		)
	return _linked_doc_payload("Pet Care Episode", episode_name)


def _plan_items(visit_doc) -> list[dict]:
	filters = {}
	if visit_doc.get("care_episode"):
		filters["care_episode"] = visit_doc.care_episode
	else:
		filters["pet"] = visit_doc.animal_patient
	filters["status"] = ["not in", ["Done", "Cancelled", "Converted To Visit"]]
	rows = frappe.get_all(
		"Pet Care Plan Item",
		filters=filters,
		fields=["*"],
		order_by="due_date asc, priority desc, modified desc",
		ignore_permissions=True,
	)
	items = [dict(row) for row in rows]
	enrich_link_aliases(items, pet_field="pet", guardian_field="guardian", doctor_field="doctor", include_provider=False)
	return items


def _medical_profile_payload(pet: str | None) -> dict:
	if not pet:
		return {}
	name = frappe.db.get_value("Pet Medical Profile", {"pet": pet}, "name")
	return _linked_doc_payload("Pet Medical Profile", name)


def _visit_payload(doc) -> dict:
	payload = _doc_payload(doc)
	pet_id = doc.get("animal_patient")
	guardian_id = doc.get("guardian")
	doctor_id = doc.get("doctor")
	payload["pet"] = pet_id
	payload["pet_id"] = pet_id
	payload["pet_name"] = frappe.db.get_value("Pet", pet_id, "pet_name") if pet_id else None
	payload["guardian_id"] = guardian_id
	payload["guardian_name"] = payload.get("guardian_name") or (
		frappe.db.get_value("Guardian", guardian_id, "full_name") if guardian_id else None
	)
	payload["doctor_name"] = payload.get("doctor_name") or (
		frappe.db.get_value("Healthcare Practitioner", doctor_id, "practitioner_name") if doctor_id else None
	)
	return payload


def _linked_doc_payload(doctype: str, name: str | None) -> dict:
	if not name or not frappe.db.exists(doctype, name):
		return {}
	payload = frappe.get_doc(doctype, name).as_dict(no_nulls=False)
	if doctype in {"Vet Visit", "Vet Case Sheet"}:
		return with_link_aliases(payload, pet_field="animal_patient", guardian_field="guardian", doctor_field="doctor", include_provider=False)
	if doctype == "Pet Medical Profile":
		return with_link_aliases(payload, pet_field="pet", guardian_field="primary_guardian", include_doctor=False, include_provider=False)
	if doctype == "Pet Care Episode":
		return with_link_aliases(payload, pet_field="pet", guardian_field="guardian", include_doctor=False, include_provider=False)
	return payload


def _doc_payload(doc) -> dict:
	return doc.as_dict(no_nulls=False)


def _medication_rows(visit_doc) -> list[dict]:
	return [
		{
			"name": row.name,
			"medication": row.get("medication"),
			"medication_item": row.get("medication_item"),
			"qty": row.get("qty"),
			"rate": row.get("rate"),
			"amount": row.get("amount"),
			"dosage": row.get("dosage"),
			"frequency": row.get("frequency"),
			"duration_days": row.get("duration_days"),
			"instructions": row.get("instructions"),
			"dispense_status": row.get("dispense_status"),
		}
		for row in visit_doc.get("prescribed_medications") or []
	]


def _billable_row(row) -> dict:
	return {
		"name": row.name,
		"item_name": row.get("item_name"),
		"item_code": row.get("item_code"),
		"item_type": row.get("item_type"),
		"qty": row.get("qty"),
		"rate": row.get("rate"),
		"amount": row.get("amount"),
		"status": row.get("status"),
		"linked_service_id": row.get("linked_service_id"),
		"linked_doctype": row.get("linked_doctype"),
		"linked_name": row.get("linked_name"),
		"order_id": row.get("order_id"),
		"note": row.get("note"),
	}


def _active_billable_rows(visit_doc) -> list:
	return [row for row in visit_doc.get("billable_items") or [] if not _is_cancelled_billable(row)]


def _cancelled_billable_rows(visit_doc) -> list:
	return [row for row in visit_doc.get("billable_items") or [] if _is_cancelled_billable(row)]


def _is_cancelled_billable(row) -> bool:
	return cstr(row.get("status")).strip() == "Cancelled"


def _workbench_permissions(visit_doc) -> dict:
	billed = clinical_state.is_billed_visit(visit_doc)
	cancelled = cstr(visit_doc.get("status")) == "Cancelled"
	can_write_visit = (not billed and not cancelled) and _can_doctype("Vet Visit", "write")
	can_update_follow_up = (not billed) and _can_doctype("Vet Visit", "write")
	return {
		"can_start_consultation": can_write_visit,
		"can_set_case_choice": can_write_visit,
		"can_save_clinical_note": can_write_visit,
		"can_save_diagnoses": can_write_visit,
		"can_create_orders": can_write_visit,
		"can_complete_case": can_write_visit,
		"can_request_follow_up": can_update_follow_up and _can_doctype("Appointment", "create"),
		"can_add_plan_item": (not billed and not cancelled) and _can_doctype("Pet Care Plan Item", "create"),
		"can_schedule_plan_item": _can_doctype("Pet Care Plan Item", "write") and _can_doctype("Appointment", "create"),
		"can_convert_plan_item_to_visit": _can_doctype("Pet Care Plan Item", "write") and _can_doctype("Vet Visit", "create"),
		"is_billed": cint(billed),
		"is_cancelled": cint(cancelled),
	}


def _can_doctype(doctype: str, ptype: str) -> bool:
	try:
		return bool(frappe.has_permission(doctype, ptype=ptype))
	except Exception:
		return False


def _error_response(exc: Exception) -> dict:
	code = "PERMISSION_DENIED" if isinstance(exc, frappe.PermissionError) else "ERROR"
	return fail(cstr(exc), code=code, details=frappe.get_traceback())
