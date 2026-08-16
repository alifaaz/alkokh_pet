from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, cstr, get_datetime

from pet_app.api.link_aliases import enrich_link_aliases, with_link_aliases
from pet_app.api.permissions import require_doctype_permission
from pet_app.api.response import fail, ok
from pet_app.api.healthcare.boarding import (
	VISIT_BOARDING_SUGGESTED_TYPE,
	can_cancel_visit_boarding,
	can_start_visit_boarding,
	checked_in_boarding_for_visit,
	visit_boarding_payload,
)
from pet_app.api.visit_referral import can_refer_visit, visit_referrals_payload
from pet_app.api.workspace import (
	_assert_record_access,
	_billing_snapshot,
	_visit_consult_requests,
	_visit_diagnoses,
	_visit_follow_up,
	_visit_orders,
	_linked_records_for_visit,
)
from pet_app.pet_app.doctype.pet_care_episode.pet_care_episode import ACTIVE_EPISODE_STATUSES
from pet_app.utils.case_assignment import (
	can_manage_episode_team,
	current_user_visit_practitioner,
	visit_practitioner,
)
from pet_app.utils.clinical_options import (
	ASSESSMENT_FINDING,
	CLIENT_OBSERVATION,
	OWNER_INSTRUCTION,
	clinical_catalogue_choices,
	clinical_options_payload,
	visit_clinical_payload,
)
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
		linked_records = _linked_records_for_visit(visit_doc.name)
		return ok(
			{
				"visit": _visit_payload(visit_doc),
				"case_sheet": _linked_doc_payload("Vet Case Sheet", visit_doc.get("case_sheet")),
				"pet": _linked_doc_payload("Pet", visit_doc.get("animal_patient")),
				"guardian": _linked_doc_payload("Guardian", visit_doc.get("guardian")),
				"medical_profile": _medical_profile_payload(visit_doc.get("animal_patient")),
				"active_episode": _active_episode_payload(visit_doc),
				"case_context": get_visit_case_context(visit_doc),
				"clinical_note": visit_clinical_payload(visit_doc),
				"clinical_options": clinical_options_payload(),
				"assessment_finding_options": clinical_catalogue_choices(ASSESSMENT_FINDING),
				"client_observation_options": clinical_catalogue_choices(CLIENT_OBSERVATION),
				"owner_instruction_options": clinical_catalogue_choices(OWNER_INSTRUCTION),
				"active_plan_items": _plan_items(visit_doc),
				"plan_items": _visit_plan_items(visit_doc),
				"diagnoses": _visit_diagnoses(visit_doc),
				"orders": _visit_orders(visit_doc, linked_records=linked_records),
				"linked_records": linked_records,
				"medications": _medication_rows(visit_doc),
				"billables": [_billable_row(row) for row in _active_billable_rows(visit_doc)],
				"cancelled_billables": [_billable_row(row) for row in _cancelled_billable_rows(visit_doc)],
				"followups": _visit_follow_up(visit_doc),
				"consults": _visit_consult_requests(visit_doc),
				"referrals": visit_referrals_payload(visit_doc),
				"boarding": visit_boarding_payload(visit_doc),
				"billing": _billing_snapshot(visit_doc),
				"permissions": _workbench_permissions(visit_doc),
			}
		)
	except Exception as exc:
		return _error_response(exc)


def _active_episode_payload(visit_doc) -> dict:
	return _linked_doc_payload("Pet Care Episode", _active_episode_name_for_visit(visit_doc))


def _visit_chose_wellness(visit_doc) -> bool:
	return cstr(visit_doc.get("doctor_case_choice")).strip().lower() == "wellness"


def _active_episode_name_for_visit(visit_doc) -> str | None:
	episode_name = visit_doc.get("care_episode")
	if episode_name:
		return episode_name
	# "wellness" is an explicit "this visit has no clinical case", so it must not
	# inherit the pet's open episode - the write path already nulled care_episode
	# on purpose. The pet-level fallback below stays for visits that simply have
	# not chosen yet (case_choice_required), which is what it was there for.
	if _visit_chose_wellness(visit_doc):
		return None
	if not visit_doc.get("animal_patient"):
		return None
	return frappe.db.get_value(
		"Pet Care Episode",
		{"pet": visit_doc.animal_patient, "episode_status": ["in", list(ACTIVE_EPISODE_STATUSES)]},
		"name",
		order_by="modified desc",
	)


def _select_options(doctype: str, fieldname: str) -> list[str]:
	field = frappe.get_meta(doctype).get_field(fieldname)
	if not field:
		return []
	return [option for option in cstr(field.options).splitlines() if option]


def _plan_items(visit_doc) -> list[dict]:
	filters = {}
	if visit_doc.get("care_episode"):
		filters["care_episode"] = visit_doc.care_episode
	elif _visit_chose_wellness(visit_doc):
		# Same guard as _active_episode_name_for_visit: a wellness visit has no case,
		# so the pet-level fallback would list another case's open items as its own.
		return []
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


def _visit_plan_items(visit_doc) -> list[dict]:
	rows = frappe.get_all(
		"Pet Care Plan Item",
		filters={"source_visit": visit_doc.name},
		fields=["*"],
		order_by="due_date asc, priority desc, modified desc",
		ignore_permissions=True,
	)
	items = [_visit_plan_item_payload(row) for row in rows]
	enrich_link_aliases(items, pet_field="pet", guardian_field="guardian", doctor_field="doctor", include_provider=False)
	return items


def _visit_plan_item_payload(row) -> dict:
	item = dict(row)
	item["item_type"] = item.get("item_type") or item.get("plan_type")
	item["owner_instructions"] = item.get("owner_instructions") or item.get("instructions")
	item["due_datetime"] = item.get("due_datetime") or _plan_due_datetime(item)
	item["linked_doctype"] = item.get("linked_doctype") or None
	item["linked_name"] = item.get("linked_name") or None
	return item


def _plan_due_datetime(item: dict) -> str | None:
	if not item.get("due_date") or not item.get("due_time"):
		return None
	try:
		due_date = item.get("due_date")
		due_time = item.get("due_time")
		return cstr(get_datetime(f"{due_date} {due_time}"))
	except Exception:
		return None


def _medical_profile_payload(pet: str | None) -> dict:
	if not pet:
		return {}
	name = frappe.db.get_value("Pet Medical Profile", {"pet": pet}, "name")
	return _linked_doc_payload("Pet Medical Profile", name)


def _visit_payload(doc) -> dict:
	payload = _doc_payload(doc)
	for legacy_fieldname in ("assessment", "doctor_notes", "instructions", "differential_diagnosis"):
		payload.pop(legacy_fieldname, None)
	clinical_note = visit_clinical_payload(doc)
	payload.update(
		{
			"assessment_findings": clinical_note["assessment_findings"],
			"assessment_note": clinical_note["assessment_note"],
			"client_observations": clinical_note["client_observations"],
			"doctor_note": clinical_note["doctor_note"],
			"owner_instruction_items": clinical_note["owner_instruction_items"],
			"owner_instruction_note": clinical_note["owner_instruction_note"],
			"clinical_note": clinical_note,
		}
	)
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
			"dispense_uom": row.get("dispense_uom"),
			"stock_uom": row.get("stock_uom"),
			"conversion_factor": row.get("conversion_factor"),
			"rate": row.get("rate"),
			"amount": row.get("amount"),
			"dosage": row.get("dosage"),
			"frequency": row.get("frequency"),
			"duration_days": row.get("duration_days"),
			"instructions": row.get("instructions"),
			"warehouse": row.get("warehouse"),
			"dispense_status": row.get("dispense_status"),
			"dispensed_qty": row.get("dispensed_qty"),
			"return_qty": row.get("return_qty"),
			"dispensed_by": row.get("dispensed_by"),
			"dispensed_at": row.get("dispensed_at"),
			"returned_by": row.get("returned_by"),
			"returned_at": row.get("returned_at"),
			"batch_no": row.get("batch_no"),
			"expiry_date": row.get("expiry_date"),
			"quantity_modified_by": row.get("quantity_modified_by"),
			"quantity_modified_at": row.get("quantity_modified_at"),
			"rate_modified_by": row.get("rate_modified_by"),
			"rate_modified_at": row.get("rate_modified_at"),
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
	checked_in_boarding = checked_in_boarding_for_visit(visit_doc.name)
	can_write_visit = (not billed and not cancelled and not checked_in_boarding) and _can_doctype("Vet Visit", "write")
	can_update_follow_up = (not billed and not checked_in_boarding) and _can_doctype("Vet Visit", "write")
	visit_doctor = visit_practitioner(visit_doc)
	is_visit_doctor = bool(current_user_visit_practitioner(visit_doc))
	team_episode = _active_episode_name_for_visit(visit_doc)
	can_manage_team = bool(team_episode and can_manage_episode_team(None, visit_doc))
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
		"can_refer_visit": can_refer_visit(visit_doc),
		"can_add_doctor": can_manage_team,
		"can_remove_doctor": can_manage_team,
		"is_visit_doctor": is_visit_doctor,
		"visit_practitioner": visit_doctor,
		"team_episode": team_episode,
		"can_start_boarding": can_start_visit_boarding(visit_doc),
		# What start_visit_boarding will record if the operator expresses no preference.
		# Sent so the UI can present it as a pre-selected choice rather than apply it unseen.
		"suggested_boarding_type": VISIT_BOARDING_SUGGESTED_TYPE,
		"can_cancel_boarding": can_cancel_visit_boarding(visit_doc),
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
