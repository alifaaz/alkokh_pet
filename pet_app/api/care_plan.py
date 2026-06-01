from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, cstr, get_datetime, getdate, now_datetime, nowdate

from pet_app.api.link_aliases import enrich_link_aliases, with_link_aliases
from pet_app.api.permissions import get_user_roles, require_doctype_permission, user_has_full_access
from pet_app.api.response import fail, ok
from pet_app.api.workspace import _assert_record_access, _has_field, _stamp_appointment_conversion
from pet_app.utils.medical_profile import (
	set_visit_case_choice,
	sync_follow_up_from_visit,
	sync_treatment_from_visit,
	update_profile_for_visit,
)
from pet_app.utils.practitioner import get_practitioner_for_user
from pet_app.workflows import clinical_state


ACTIVE_PLAN_STATUSES = {"Planned", "Scheduled", "In Progress", "Overdue"}
PLAN_TERMINAL_STATUSES = {"Done", "Cancelled", "Converted To Visit"}
APPOINTMENT_PLAN_TYPES = {"Follow-up Visit", "Lab Recheck", "Imaging Recheck", "Procedure", "Vaccination"}
MEDICATION_PAYLOAD_KEYS = {"medication", "medication_item", "qty", "dosage", "frequency", "duration_days", "instructions"}
CLINICAL_ROLES = {"Doctor", "Physician", "Healthcare", "Healthcare Practitioner", "Healthcare Administrator"}
GUARDIAN_ROLES = {"Guardian", "Guardians", "Pet"}


@frappe.whitelist(methods=["POST"])
def add_plan_item_from_visit(visit=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		visit_name = cstr(visit or payload.get("visit") or payload.get("source_visit")).strip()
		if not visit_name:
			return fail(_("Visit is required."), code="VALIDATION_ERROR")

		require_doctype_permission("Pet Care Plan Item", "create")
		_assert_record_access("Vet Visit", visit_name, write=True, action="add_plan_item")

		visit_doc = frappe.get_doc("Vet Visit", visit_name)
		_validate_visit_can_accept_plan(visit_doc)
		episode_name = visit_doc.get("care_episode")
		if not episode_name:
			return fail(_("Choose New Case or Continue Case before adding care plan items."), code="CASE_CHOICE_REQUIRED")

		plan_type = cstr(payload.get("plan_type") or payload.get("type") or "Other").strip() or "Other"
		requires_appointment = cint(payload.get("requires_appointment"))
		if plan_type == "Follow-up Visit":
			requires_appointment = 1

		plan = frappe.get_doc(
			{
				"doctype": "Pet Care Plan Item",
				"pet": visit_doc.animal_patient,
				"guardian": visit_doc.guardian,
				"customer": visit_doc.customer,
				"care_episode": episode_name,
				"source_visit": visit_doc.name,
				"doctor": visit_doc.doctor,
				"plan_type": plan_type,
				"title": payload.get("title") or plan_type,
				"description": payload.get("description"),
				"instructions": payload.get("instructions"),
				"start_date": payload.get("start_date") or nowdate(),
				"due_date": payload.get("due_date"),
				"due_time": payload.get("due_time"),
				"end_date": payload.get("end_date"),
				"frequency": payload.get("frequency"),
				"duration_days": payload.get("duration_days"),
				"requires_appointment": requires_appointment,
				"requires_reminder": cint(payload.get("requires_reminder")),
				"status": payload.get("status") or "Planned",
				"priority": payload.get("priority") or "Normal",
				"created_from_action": payload.get("created_from_action") or "add_plan_item_from_visit",
				"source_doctype": "Vet Visit",
				"source_name": visit_doc.name,
			}
		)
		plan.insert(ignore_permissions=True)

		if plan.plan_type == "Medication":
			_sync_medication_to_visit(visit_doc, plan, payload)
		if cint(plan.requires_reminder):
			_create_reminder_for_plan(plan)

		_sync_episode_profile_for_plan(plan)
		return _workbench_response(visit_doc.name, plan=plan)
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def update_plan_item(plan_item=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		name = cstr(plan_item or payload.get("plan_item") or payload.get("name")).strip()
		if not name:
			return fail(_("Plan item is required."), code="VALIDATION_ERROR")

		require_doctype_permission("Pet Care Plan Item", "write")
		plan = frappe.get_doc("Pet Care Plan Item", name)
		_assert_plan_access(plan, write=True)
		if plan.status in {"Cancelled", "Converted To Visit"}:
			return fail(_("This plan item can no longer be edited."), code="INVALID_STATE")

		for fieldname in _plan_update_fields():
			if fieldname in payload and plan.meta.has_field(fieldname):
				plan.set(fieldname, payload.get(fieldname))
		if plan.plan_type == "Follow-up Visit":
			plan.requires_appointment = 1
		plan.save(ignore_permissions=True)
		_sync_episode_profile_for_plan(plan)
		return ok({"plan_item": _doc_payload(plan)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def cancel_plan_item(plan_item=None, reason=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		name = cstr(plan_item or payload.get("plan_item") or payload.get("name")).strip()
		if not name:
			return fail(_("Plan item is required."), code="VALIDATION_ERROR")

		require_doctype_permission("Pet Care Plan Item", "write")
		plan = frappe.get_doc("Pet Care Plan Item", name)
		_assert_plan_access(plan, write=True)
		if plan.status != "Cancelled":
			plan.status = "Cancelled"
			plan.completion_note = reason or payload.get("reason") or plan.completion_note
			plan.save(ignore_permissions=True)
		_sync_episode_profile_for_plan(plan)
		return ok({"plan_item": _doc_payload(plan)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def complete_plan_item(plan_item=None, note=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		name = cstr(plan_item or payload.get("plan_item") or payload.get("name")).strip()
		if not name:
			return fail(_("Plan item is required."), code="VALIDATION_ERROR")

		require_doctype_permission("Pet Care Plan Item", "write")
		plan = frappe.get_doc("Pet Care Plan Item", name)
		_assert_plan_access(plan, write=True)
		if plan.status != "Done":
			plan.status = "Done"
			plan.completed_on = plan.completed_on or now_datetime()
			plan.completed_by = plan.completed_by or frappe.session.user
			plan.completion_note = note or payload.get("note") or plan.completion_note
			plan.save(ignore_permissions=True)
		_sync_episode_profile_for_plan(plan)
		return ok({"plan_item": _doc_payload(plan)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def schedule_plan_item_appointment(plan_item=None, appointment_data=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		name = cstr(plan_item or payload.get("plan_item") or payload.get("name")).strip()
		if not name:
			return fail(_("Plan item is required."), code="VALIDATION_ERROR")

		require_doctype_permission("Pet Care Plan Item", "write")
		require_doctype_permission("Appointment", "create")
		plan = frappe.get_doc("Pet Care Plan Item", name)
		_assert_plan_access(plan, write=True)
		if not cint(plan.requires_appointment):
			return fail(_("This plan item does not require an appointment."), code="VALIDATION_ERROR")

		appointment_payload = _payload(appointment_data, {})
		appointment_payload.update(_payload(payload.get("appointment_data"), {}))
		appointment = _get_or_create_plan_appointment(plan, appointment_payload)
		plan.appointment = appointment.name
		plan.appointment_status = appointment.get("status") or "Scheduled"
		if plan.status == "Planned":
			plan.status = "Scheduled"
		plan.save(ignore_permissions=True)
		_sync_episode_profile_for_plan(plan)
		return ok({"plan_item": _doc_payload(plan), "appointment": _doc_payload(appointment)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def convert_plan_item_to_visit(plan_item=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		name = cstr(plan_item or payload.get("plan_item") or payload.get("name")).strip()
		if not name:
			return fail(_("Plan item is required."), code="VALIDATION_ERROR")

		require_doctype_permission("Pet Care Plan Item", "write")
		require_doctype_permission("Vet Case Sheet", "create")
		require_doctype_permission("Vet Visit", "create")
		plan = frappe.get_doc("Pet Care Plan Item", name)
		_assert_plan_access(plan, write=True)

		if plan.converted_visit and frappe.db.exists("Vet Visit", plan.converted_visit):
			return ok({"plan_item": _doc_payload(plan), "visit": plan.converted_visit, "idempotent": True})

		appointment = _plan_appointment(plan)
		linked_visit = appointment.get("custom_linked_visit_id") if appointment and _has_field("Appointment", "custom_linked_visit_id") else None
		if linked_visit and frappe.db.exists("Vet Visit", linked_visit):
			_mark_plan_converted(plan, linked_visit, appointment)
			return ok({"plan_item": _doc_payload(plan), "visit": linked_visit, "idempotent": True})

		visit = _create_visit_from_plan(plan, payload, appointment)
		_mark_plan_converted(plan, visit.name, appointment)
		update_profile_for_visit(visit, clinical_status="In Consultation", episode_status="Under Diagnosis")
		return ok({"plan_item": _doc_payload(plan), "visit": visit.name, "case_sheet": visit.case_sheet})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def list_due_plan_items(filters=None, data=None, limit_start=0, limit_page_length=50, **kwargs):
	try:
		require_doctype_permission("Pet Care Plan Item", "read")
		payload = _payload(filters or data, kwargs)
		db_filters = {"status": ["in", list(ACTIVE_PLAN_STATUSES)]}
		if payload.get("pet"):
			db_filters["pet"] = payload.get("pet")
		if payload.get("guardian"):
			db_filters["guardian"] = payload.get("guardian")
		if payload.get("doctor"):
			db_filters["doctor"] = payload.get("doctor")
		if payload.get("care_episode"):
			db_filters["care_episode"] = payload.get("care_episode")
		if payload.get("plan_type"):
			db_filters["plan_type"] = payload.get("plan_type")
		if payload.get("date_to") or payload.get("due_date"):
			db_filters["due_date"] = ["<=", getdate(payload.get("date_to") or payload.get("due_date"))]

		limit_start = max(cint(limit_start), 0)
		limit_page_length = max(min(cint(limit_page_length or payload.get("limit") or 50), 200), 1)
		names = frappe.get_all(
			"Pet Care Plan Item",
			filters=db_filters,
			pluck="name",
			order_by="due_date asc, priority desc, modified desc",
			limit_start=limit_start,
			limit_page_length=limit_page_length,
			ignore_permissions=True,
		)
		items = []
		for item_name in names:
			plan = frappe.get_doc("Pet Care Plan Item", item_name)
			_assert_plan_access(plan)
			items.append(_doc_payload(plan))
		_enrich_due_plan_item_display_names(items)
		total = frappe.db.count("Pet Care Plan Item", db_filters)
		return ok({"items": items}, meta={"total": total})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_pet_active_plan(pet=None, pet_id=None):
	try:
		pet_name = cstr(pet or pet_id).strip()
		if not pet_name:
			return fail(_("Pet is required."), code="VALIDATION_ERROR")
		_assert_pet_access(pet_name)
		rows = frappe.get_all(
			"Pet Care Plan Item",
			filters={"pet": pet_name, "status": ["in", list(ACTIVE_PLAN_STATUSES)]},
			fields=["*"],
			order_by="due_date asc, priority desc, modified desc",
			ignore_permissions=True,
		)
		items = [dict(row) for row in rows]
		enrich_link_aliases(items, pet_field="pet", guardian_field="guardian", doctor_field="doctor", include_provider=False)
		return ok({"items": items})
	except Exception as exc:
		return _error_response(exc)


def _validate_visit_can_accept_plan(visit):
	if cstr(visit.get("status")) == "Cancelled":
		frappe.throw(_("Cancelled visits cannot accept care plan items."))
	clinical_state.assert_visit_not_billed(visit)


def _sync_medication_to_visit(visit, plan, payload: dict):
	if not visit.meta.has_field("prescribed_medications"):
		return
	medication_payload = _coerce_medication_payload(payload)
	if not medication_payload:
		return
	key = (
		cstr(medication_payload.get("medication_item")),
		cstr(medication_payload.get("medication")),
		cstr(medication_payload.get("dosage")),
		cstr(medication_payload.get("frequency")),
		cstr(medication_payload.get("instructions")),
	)
	for row in visit.get("prescribed_medications") or []:
		existing_key = (
			cstr(row.get("medication_item")),
			cstr(row.get("medication")),
			cstr(row.get("dosage")),
			cstr(row.get("frequency")),
			cstr(row.get("instructions")),
		)
		if key == existing_key:
			return
	row_data = {fieldname: medication_payload.get(fieldname) for fieldname in MEDICATION_PAYLOAD_KEYS if medication_payload.get(fieldname) is not None}
	row_data.setdefault("qty", medication_payload.get("qty") or 1)
	row_data.setdefault("instructions", medication_payload.get("instructions") or plan.instructions)
	visit.append("prescribed_medications", row_data)
	visit.save(ignore_permissions=True)
	sync_treatment_from_visit(visit)


def _coerce_medication_payload(payload: dict) -> dict:
	medication = payload.get("medication")
	if isinstance(medication, str):
		return {"medication_item": medication}
	if isinstance(medication, dict):
		result = dict(medication)
	else:
		result = {}
	for key in MEDICATION_PAYLOAD_KEYS:
		if key in payload and key not in result:
			result[key] = payload.get(key)
	if result.get("medication_item") or result.get("medication"):
		return result
	return {}


def _create_reminder_for_plan(plan):
	if not frappe.db.exists("DocType", "Pet Reminder") or not plan.due_date:
		return None
	key = f"care-plan::{plan.name}"
	existing = frappe.db.get_value("Pet Reminder", {"idempotency_key": key}, "name")
	if existing:
		plan.reminder = existing
		plan.reminder_status = frappe.db.get_value("Pet Reminder", existing, "status")
		plan.save(ignore_permissions=True)
		return existing
	reminder = frappe.get_doc(
		{
			"doctype": "Pet Reminder",
			"reminder_type": _reminder_type(plan),
			"pet": plan.pet,
			"guardian": plan.guardian,
			"due_date": plan.due_date,
			"status": "Pending",
			"channel": "In App",
			"reference_doctype": "Pet Care Plan Item",
			"reference_name": plan.name,
			"idempotency_key": key,
			"note": plan.title,
		}
	)
	reminder.insert(ignore_permissions=True)
	plan.reminder = reminder.name
	plan.reminder_status = reminder.status
	plan.save(ignore_permissions=True)
	return reminder.name


def _reminder_type(plan) -> str:
	if plan.plan_type == "Follow-up Visit":
		return "Follow-up Due"
	if plan.plan_type == "Vaccination":
		return "Vaccination Due"
	if plan.plan_type == "Deworming":
		return "Deworming Due"
	return "Follow-up Due"


def _sync_episode_profile_for_plan(plan):
	if plan.source_visit and frappe.db.exists("Vet Visit", plan.source_visit):
		visit = frappe.get_doc("Vet Visit", plan.source_visit)
		if not clinical_state.is_billed_visit(visit):
			if plan.plan_type == "Follow-up Visit":
				if plan.due_date:
					visit.follow_up_required = 1
					visit.follow_up_date = plan.due_date
					if _has_field("Vet Visit", "follow_up_preferred_date"):
						visit.follow_up_preferred_date = plan.due_date
				if plan.appointment and _has_field("Vet Visit", "follow_up_appointment_id"):
					visit.follow_up_appointment_id = plan.appointment
				if _has_field("Vet Visit", "follow_up_status"):
					visit.follow_up_status = "Scheduled" if plan.appointment else "Requested"
				visit.flags.ignore_billing_lock = True
				visit.save(ignore_permissions=True)
				sync_follow_up_from_visit(visit)
			else:
				update_profile_for_visit(visit, plan_status="Active")

	if plan.care_episode and frappe.db.exists("Pet Care Episode", plan.care_episode):
		episode = frappe.get_doc("Pet Care Episode", plan.care_episode)
		if plan.status not in PLAN_TERMINAL_STATUSES:
			episode.episode_status = "Follow-up Scheduled" if plan.plan_type == "Follow-up Visit" else _episode_status_for_plan(plan)
		if plan.due_date and plan.plan_type == "Follow-up Visit":
			episode.next_follow_up_date = plan.due_date
			episode.requires_follow_up = 1
			episode.follow_up_status = "Scheduled" if plan.appointment else "Requested"
		if plan.title and not episode.treatment_summary and plan.plan_type not in {"Follow-up Visit", "Lab Recheck", "Imaging Recheck"}:
			episode.treatment_summary = plan.title
		episode.save(ignore_permissions=True)

	if plan.pet and frappe.db.exists("Pet Medical Profile", {"pet": plan.pet}):
		profile_name = frappe.db.get_value("Pet Medical Profile", {"pet": plan.pet}, "name")
		updates = {
			"active_care_episode": plan.care_episode,
			"treatment_plan_status": _profile_plan_status(plan),
			"current_clinical_status": _profile_clinical_status(plan),
			"current_case_status": _profile_case_status(plan),
			"next_follow_up_date": plan.due_date if plan.plan_type == "Follow-up Visit" else None,
			"follow_up_status": "Scheduled" if plan.appointment else ("Requested" if plan.plan_type == "Follow-up Visit" else None),
			"last_synced_from_doctype": "Pet Care Plan Item",
			"last_synced_from_name": plan.name,
			"last_synced_at": now_datetime(),
		}
		_set_existing_values("Pet Medical Profile", profile_name, updates)


def _episode_status_for_plan(plan) -> str:
	if plan.plan_type in {"Lab Recheck", "Imaging Recheck", "Procedure"}:
		return "Pending Diagnostics"
	if plan.plan_type == "Monitoring":
		return "Monitoring"
	return "Under Treatment"


def _profile_plan_status(plan) -> str:
	if plan.status == "Done":
		return "Completed"
	if plan.status == "Cancelled":
		return "Cancelled"
	if plan.status == "Scheduled" and plan.plan_type == "Follow-up Visit":
		return "Waiting Follow-up"
	return "Active"


def _profile_clinical_status(plan) -> str:
	if plan.status == "Done":
		return "Stable"
	if plan.plan_type == "Follow-up Visit":
		return "Follow-up Scheduled" if plan.appointment else "Monitoring"
	if plan.plan_type in {"Lab Recheck", "Imaging Recheck"}:
		return "Pending Lab / Imaging"
	if plan.plan_type == "Monitoring":
		return "Monitoring"
	return "Under Treatment"


def _profile_case_status(plan) -> str:
	if plan.status == "Done":
		return "No Active Case"
	if plan.plan_type == "Follow-up Visit":
		return "Follow-up Scheduled"
	if plan.plan_type in {"Lab Recheck", "Imaging Recheck"}:
		return "Pending Diagnostics"
	if plan.plan_type == "Monitoring":
		return "Monitoring"
	return "Under Treatment"


def _get_or_create_plan_appointment(plan, payload: dict):
	existing = _plan_appointment(plan)
	if existing:
		_update_plan_appointment(existing, plan, payload)
		return existing

	scheduled_time = payload.get("scheduled_time") or _scheduled_time_for_plan(plan)
	contact = _appointment_contact_for_plan(plan)
	appointment_data = {
		"doctype": "Appointment",
		"status": payload.get("status") or "Open",
		"scheduled_time": scheduled_time,
		"customer_name": payload.get("customer_name") or contact["name"],
		"customer_phone_number": payload.get("customer_phone_number") or contact.get("phone"),
		"customer_email": payload.get("customer_email") or contact["email"],
		"customer_details": payload.get("customer_details") or plan.description or plan.instructions or plan.title,
	}
	for fieldname, value in {
		"custom_appointment_type": "follow_up" if plan.plan_type == "Follow-up Visit" else "visit",
		"custom_pet": plan.pet,
		"custom_guardian": plan.guardian,
		"custom_customer": plan.customer,
		"custom_follow_up_of_visit_id": plan.source_visit if plan.plan_type == "Follow-up Visit" else None,
		"custom_care_plan_item": plan.name,
	}.items():
		if value and _has_field("Appointment", fieldname):
			appointment_data[fieldname] = value
	appointment = frappe.get_doc(appointment_data)
	appointment.insert(ignore_permissions=True)
	return appointment


def _update_plan_appointment(appointment, plan, payload: dict):
	changed = False
	if payload.get("scheduled_time") and not appointment.get("custom_linked_visit_id"):
		appointment.scheduled_time = payload.get("scheduled_time")
		changed = True
	if _has_field("Appointment", "custom_care_plan_item") and not appointment.get("custom_care_plan_item"):
		appointment.custom_care_plan_item = plan.name
		changed = True
	if changed:
		appointment.save(ignore_permissions=True)


def _plan_appointment(plan):
	if plan.appointment and frappe.db.exists("Appointment", plan.appointment):
		return frappe.get_doc("Appointment", plan.appointment)
	if _has_field("Appointment", "custom_care_plan_item"):
		name = frappe.db.get_value("Appointment", {"custom_care_plan_item": plan.name}, "name", order_by="creation desc")
		if name:
			return frappe.get_doc("Appointment", name)
	return None


def _scheduled_time_for_plan(plan):
	due_date = plan.due_date or nowdate()
	due_time = cstr(plan.due_time or "09:00:00").strip() or "09:00:00"
	return get_datetime(f"{getdate(due_date)} {due_time}")


def _appointment_contact_for_plan(plan) -> dict:
	guardian = frappe.db.get_value("Guardian", plan.guardian, ["full_name", "phone", "email_id"], as_dict=True) if plan.guardian else None
	name = (guardian or {}).get("full_name") or plan.customer or plan.guardian or plan.pet or plan.name
	email = (guardian or {}).get("email_id") or f"care-plan-{plan.name.lower()}@pet-app.local"
	return {"name": name, "phone": (guardian or {}).get("phone"), "email": email}


def _create_visit_from_plan(plan, payload: dict, appointment=None):
	doctor = payload.get("doctor") or payload.get("practitioner") or plan.doctor or get_practitioner_for_user()
	case_sheet_data = {
		"doctype": "Vet Case Sheet",
		"status": "Waiting Practitioner",
		"priority": _visit_priority(plan.priority),
		"guardian": plan.guardian,
		"customer": plan.customer,
		"animal_patient": plan.pet,
		"chief_complaint": "Follow-up" if plan.plan_type == "Follow-up Visit" else "Other",
		"intake_notes": plan.description or plan.instructions or plan.title,
	}
	if appointment and _has_field("Vet Case Sheet", "appointment"):
		case_sheet_data["appointment"] = appointment.name
	case_sheet = frappe.get_doc(case_sheet_data)
	case_sheet.insert(ignore_permissions=True)
	visit_data = {
		"doctype": "Vet Visit",
		"case_sheet": case_sheet.name,
		"guardian": plan.guardian,
		"customer": plan.customer,
		"animal_patient": plan.pet,
		"doctor": doctor,
		"priority": _visit_priority(plan.priority),
		"status": "In Progress",
		"visit_type": "Follow-up" if plan.plan_type == "Follow-up Visit" else "Consultation",
		"case_summary": plan.description or plan.instructions,
	}
	if _has_field("Vet Visit", "care_episode"):
		visit_data["care_episode"] = plan.care_episode
	if _has_field("Vet Visit", "appointment") and appointment:
		visit_data["appointment"] = appointment.name
	if _has_field("Vet Visit", "follow_up_of_visit_id") and plan.source_visit:
		visit_data["follow_up_of_visit_id"] = plan.source_visit
	visit = frappe.get_doc(visit_data)
	visit.insert(ignore_permissions=True)
	frappe.db.set_value("Vet Case Sheet", case_sheet.name, {"vet_visit": visit.name, "status": "In Consultation"}, update_modified=False)
	if plan.care_episode:
		set_visit_case_choice(visit, "continue_case", episode=plan.care_episode, note=_("Visit created from care plan item {0}.").format(plan.name))
	else:
		update_profile_for_visit(visit, clinical_status="In Consultation")
	return visit


def _mark_plan_converted(plan, visit_name: str, appointment=None):
	plan.converted_to_visit = 1
	plan.converted_visit = visit_name
	plan.status = "Converted To Visit"
	if appointment:
		plan.appointment = appointment.name
		plan.appointment_status = appointment.get("status")
		_stamp_appointment_conversion(appointment.name, linked_visit_id=visit_name, target="Visit")
	plan.save(ignore_permissions=True)


def _visit_priority(priority: str | None) -> str:
	if priority == "Important":
		return "Urgent"
	return priority if priority in {"Low", "Normal", "Urgent", "Emergency"} else "Normal"


def _assert_plan_access(plan, write=False):
	if plan.source_visit and frappe.db.exists("Vet Visit", plan.source_visit):
		_assert_record_access("Vet Visit", plan.source_visit, write=write, action="care_plan")
		return
	_assert_pet_access(plan.pet, write=write)


def _assert_pet_access(pet: str, write=False):
	if not pet or not frappe.db.exists("Pet", pet):
		frappe.throw(_("Pet {0} was not found.").format(frappe.bold(pet)))
	user = frappe.session.user
	roles = get_user_roles(user)
	if user_has_full_access(user) or roles & CLINICAL_ROLES:
		return
	guardian = frappe.db.get_value("Guardian", {"user_id": user}, "name")
	if not write and guardian and frappe.db.exists("PetGuardian", {"guardian_id": guardian, "pet_id": pet}):
		return
	if write:
		require_doctype_permission("Pet Care Plan Item", "write")
	else:
		require_doctype_permission("Pet Care Plan Item", "read")


def _set_existing_values(doctype: str, name: str, values: dict):
	meta = frappe.get_meta(doctype)
	updates = {fieldname: value for fieldname, value in values.items() if value is not None and meta.has_field(fieldname)}
	if updates:
		frappe.db.set_value(doctype, name, updates, update_modified=False)


def _plan_update_fields() -> set[str]:
	return {
		"plan_type",
		"title",
		"description",
		"instructions",
		"start_date",
		"due_date",
		"due_time",
		"end_date",
		"frequency",
		"duration_days",
		"requires_appointment",
		"requires_reminder",
		"priority",
		"status",
	}


def _doc_payload(doc) -> dict:
	return with_link_aliases(doc.as_dict(no_nulls=False))


def _enrich_due_plan_item_display_names(items: list[dict]) -> None:
	enrich_link_aliases(items, pet_field="pet", guardian_field="guardian", doctor_field="doctor", include_provider=False)


def _workbench_response(visit_name: str, plan=None) -> dict:
	from pet_app.api.visit_workbench import get_visit_workbench

	response = get_visit_workbench(visit_name)
	if response.get("ok") and plan:
		response["data"]["created_plan_item"] = _doc_payload(plan)
	return response


def _payload(data, kwargs) -> dict:
	if isinstance(data, str) and data:
		return json.loads(data)
	if isinstance(data, dict):
		return data
	return kwargs or {}


def _error_response(exc: Exception) -> dict:
	code = "PERMISSION_DENIED" if isinstance(exc, frappe.PermissionError) else getattr(exc, "code", None) or "ERROR"
	return fail(cstr(exc), code=code, details=frappe.get_traceback())
