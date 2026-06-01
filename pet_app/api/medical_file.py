from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cstr

from pet_app.api.link_aliases import enrich_link_aliases, with_link_aliases
from pet_app.api.permissions import get_user_roles, user_has_full_access
from pet_app.api.response import fail, ok
from pet_app.pet_app.doctype.pet_medical_profile.pet_medical_profile import ensure_pet_medical_profile


CLINICAL_READ_ROLES = {
	"Doctor",
	"Physician",
	"Healthcare",
	"Healthcare Practitioner",
	"Healthcare Administrator",
	"System Manager",
	"Pet App Admin",
}
CLINICAL_WRITE_ROLES = CLINICAL_READ_ROLES
GUARDIAN_READ_ROLES = {"Guardian", "Guardians", "Pet"}
PROFILE_UPDATE_FIELDS = {
	"microchip_no",
	"allergies",
	"chronic_conditions",
	"special_alerts",
	"diet_notes",
	"behavior_notes",
	"vaccination_notes",
	"deworming_notes",
}


@frappe.whitelist()
def get_pet_medical_summary(pet=None, pet_id=None):
	try:
		pet_name = cstr(pet or pet_id).strip()
		if not pet_name:
			return fail(_("Pet is required."), code="VALIDATION_ERROR")
		_assert_pet_medical_access(pet_name)
		profile_name = ensure_pet_medical_profile(pet_name, _primary_guardian_for_pet(pet_name))
		profile = frappe.get_doc("Pet Medical Profile", profile_name)
		latest_visit = _latest_visit_for_pet(pet_name)
		latest_vitals = _latest_vitals_for_pet(pet_name)
		return ok(
			{
				"profile": _profile_payload(profile),
				"latest_visit": latest_visit,
				"latest_vitals": latest_vitals,
			}
		)
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def update_pet_medical_profile(pet=None, pet_id=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		pet_name = cstr(pet or pet_id or payload.get("pet") or payload.get("pet_id")).strip()
		if not pet_name:
			return fail(_("Pet is required."), code="VALIDATION_ERROR")
		_assert_pet_medical_write_access(pet_name)
		profile_name = ensure_pet_medical_profile(pet_name, _primary_guardian_for_pet(pet_name))
		profile = frappe.get_doc("Pet Medical Profile", profile_name)
		changed = False
		for fieldname in PROFILE_UPDATE_FIELDS:
			if fieldname in payload:
				profile.set(fieldname, payload.get(fieldname))
				changed = True
		if changed:
			profile.save(ignore_permissions=True)
		return ok({"profile": _profile_payload(profile)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_pet_medical_timeline(pet=None, pet_id=None, limit=50):
	try:
		pet_name = cstr(pet or pet_id).strip()
		if not pet_name:
			return fail(_("Pet is required."), code="VALIDATION_ERROR")
		_assert_pet_medical_access(pet_name)
		limit = max(min(int(limit or 50), 200), 1)
		events = []
		events.extend(_visit_events(pet_name, limit))
		events.extend(_case_sheet_events(pet_name, limit))
		events.extend(_linked_clinical_events(pet_name, limit))
		events = sorted(events, key=lambda item: cstr(item.get("at")), reverse=True)[:limit]
		return ok({"events": events}, meta={"total": len(events)})
	except Exception as exc:
		return _error_response(exc)


def _assert_pet_medical_access(pet: str):
	if not frappe.db.exists("Pet", pet):
		frappe.throw(_("Pet {0} was not found.").format(frappe.bold(pet)))
	user = frappe.session.user
	roles = get_user_roles(user)
	if user_has_full_access(user) or roles & CLINICAL_READ_ROLES:
		return
	if roles & GUARDIAN_READ_ROLES:
		guardian = frappe.db.get_value("Guardian", {"user_id": user}, "name")
		if guardian and frappe.db.exists("PetGuardian", {"guardian_id": guardian, "pet_id": pet}):
			return
	frappe.throw(_("Not permitted"), frappe.PermissionError)


def _assert_pet_medical_write_access(pet: str):
	if not frappe.db.exists("Pet", pet):
		frappe.throw(_("Pet {0} was not found.").format(frappe.bold(pet)))
	user = frappe.session.user
	roles = get_user_roles(user)
	if user_has_full_access(user) or roles & CLINICAL_WRITE_ROLES:
		return
	frappe.throw(_("Not permitted"), frappe.PermissionError)


def _profile_payload(profile) -> dict:
	payload = {
		"name": profile.name,
		"pet": profile.pet,
		"primary_guardian": profile.primary_guardian,
		"microchip_no": profile.microchip_no,
		"allergies": profile.allergies,
		"chronic_conditions": profile.chronic_conditions,
		"special_alerts": profile.special_alerts,
		"diet_notes": profile.diet_notes,
		"behavior_notes": profile.behavior_notes,
		"vaccination_notes": profile.vaccination_notes,
		"deworming_notes": profile.deworming_notes,
		"last_weight": profile.last_weight,
		"last_visit": profile.last_visit,
		"active": profile.active,
	}
	return with_link_aliases(payload, pet_field="pet", guardian_field="primary_guardian", include_doctor=False, include_provider=False)


def _latest_visit_for_pet(pet: str) -> dict:
	row = frappe.db.get_value(
		"Vet Visit",
		{"animal_patient": pet},
		["name", "visit_datetime", "status", "doctor", "animal_patient", "guardian", "diagnosis", "weight"],
		as_dict=True,
		order_by="visit_datetime desc, creation desc",
	)
	if not row:
		return {}
	payload = dict(row)
	enrich_link_aliases([payload], pet_field="animal_patient", guardian_field="guardian", doctor_field="doctor", include_provider=False)
	return payload


def _latest_vitals_for_pet(pet: str) -> dict:
	visit_names = frappe.get_all("Vet Visit", filters={"animal_patient": pet}, pluck="name", ignore_permissions=True)
	if not visit_names:
		return {}
	if frappe.db.exists("DocType", "Vet Visit Vital Sign"):
		row = frappe.db.get_value(
			"Vet Visit Vital Sign",
			{"parent": ["in", visit_names], "parenttype": "Vet Visit"},
			[
				"parent",
				"recorded_at",
				"temperature",
				"heart_rate",
				"respiratory_rate",
				"weight",
				"body_condition_score",
				"pain_score",
			],
			as_dict=True,
			order_by="recorded_at desc, idx desc",
		)
		if row:
			return dict(row)
	visit = _latest_visit_for_pet(pet)
	if not visit:
		return {}
	values = frappe.db.get_value(
		"Vet Visit",
		visit["name"],
		["name", "temperature", "heart_rate", "respiratory_rate", "weight"],
		as_dict=True,
	)
	return dict(values) if values else {}


def _visit_events(pet: str, limit: int) -> list[dict]:
	rows = frappe.get_all(
		"Vet Visit",
		filters={"animal_patient": pet},
		fields=["name", "visit_datetime", "status", "doctor", "animal_patient", "guardian", "diagnosis", "modified"],
		order_by="visit_datetime desc, creation desc",
		limit_page_length=limit,
		ignore_permissions=True,
	)
	events = [
		{
			"type": "visit",
			"source_doctype": "Vet Visit",
			"name": row.name,
			"pet": row.animal_patient,
			"guardian": row.guardian,
			"at": row.visit_datetime or row.modified,
			"status": row.status,
			"doctor": row.doctor,
			"summary": row.diagnosis,
		}
		for row in rows
	]
	enrich_link_aliases(events, pet_field="pet", guardian_field="guardian", doctor_field="doctor", include_provider=False)
	return events


def _case_sheet_events(pet: str, limit: int) -> list[dict]:
	rows = frappe.get_all(
		"Vet Case Sheet",
		filters={"animal_patient": pet},
		fields=["name", "case_sheet_date", "status", "chief_complaint", "modified"],
		order_by="case_sheet_date desc, creation desc",
		limit_page_length=limit,
		ignore_permissions=True,
	)
	return [
		{
			"type": "case_sheet",
			"source_doctype": "Vet Case Sheet",
			"name": row.name,
			"at": row.case_sheet_date or row.modified,
			"status": row.status,
			"summary": row.chief_complaint,
		}
		for row in rows
	]


def _linked_clinical_events(pet: str, limit: int) -> list[dict]:
	events = []
	visit_names = frappe.get_all("Vet Visit", filters={"animal_patient": pet}, pluck="name", ignore_permissions=True)
	events.extend(_diagnosis_events(visit_names, limit))
	events.extend(_medication_events(visit_names, limit))
	events.extend(_follow_up_events(pet, limit))
	events.extend(_addendum_events(visit_names, limit))
	events.extend(_invoice_events(visit_names, limit))
	for doctype in ("Lab", "Imaging"):
		rows = frappe.get_all(
			doctype,
			filters={"pet": pet},
			fields=["name", "visit", "status", "care_service", "modified"],
			order_by="modified desc",
			limit_page_length=limit,
			ignore_permissions=True,
		)
		for row in rows:
			events.append(
				{
					"type": doctype.lower(),
					"source_doctype": doctype,
					"name": row.name,
					"visit": row.visit,
					"at": row.modified,
					"status": row.status,
					"summary": row.care_service,
				}
			)
	if frappe.db.exists("DocType", "Pet Procedure"):
		rows = frappe.get_all(
			"Pet Procedure",
			filters={"pet": pet},
			fields=["name", "visit", "status", "procedure_template", "modified"],
			order_by="modified desc",
			limit_page_length=limit,
			ignore_permissions=True,
		)
		for row in rows:
			events.append(
				{
					"type": "procedure",
					"source_doctype": "Pet Procedure",
					"name": row.name,
					"visit": row.visit,
					"at": row.modified,
					"status": row.status,
					"summary": row.procedure_template,
				}
			)
	events.extend(_record_events("Pet Vaccination Record", pet, "vaccination", "vaccine_name", "administered_on", limit))
	events.extend(_record_events("Pet Deworming Record", pet, "deworming", "medication_name", "administered_on", limit))
	events.extend(_boarding_events(pet, limit))
	events.extend(_death_events(pet, limit))
	return events


def _diagnosis_events(visit_names: list[str], limit: int) -> list[dict]:
	if not visit_names or not frappe.db.exists("DocType", "Visit Diagnosis"):
		return []
	rows = frappe.get_all(
		"Visit Diagnosis",
		filters={"parent": ["in", visit_names], "parenttype": "Vet Visit"},
		fields=["name", "parent", "disease", "diagnosis_text", "severity", "is_primary", "modified"],
		order_by="modified desc",
		limit_page_length=limit,
		ignore_permissions=True,
	)
	return [
		{
			"type": "diagnosis",
			"source_doctype": "Visit Diagnosis",
			"name": row.name,
			"visit": row.parent,
			"at": row.modified,
			"status": "Primary" if row.is_primary else None,
			"summary": row.diagnosis_text or row.disease,
			"severity": row.severity,
		}
		for row in rows
	]


def _medication_events(visit_names: list[str], limit: int) -> list[dict]:
	if not visit_names or not frappe.db.exists("DocType", "Vet Visit Medication Item"):
		return []
	rows = frappe.get_all(
		"Vet Visit Medication Item",
		filters={"parent": ["in", visit_names], "parenttype": "Vet Visit"},
		fields=["name", "parent", "medication", "medication_item", "qty", "dispense_status", "dispensed_qty", "modified"],
		order_by="modified desc",
		limit_page_length=limit,
		ignore_permissions=True,
	)
	return [
		{
			"type": "medication",
			"source_doctype": "Vet Visit Medication Item",
			"name": row.name,
			"visit": row.parent,
			"at": row.modified,
			"status": row.get("dispense_status"),
			"summary": row.medication or row.medication_item,
			"qty": row.qty,
			"dispensed_qty": row.get("dispensed_qty"),
		}
		for row in rows
	]


def _follow_up_events(pet: str, limit: int) -> list[dict]:
	rows = frappe.get_all(
		"Vet Visit",
		filters={"animal_patient": pet, "follow_up_required": 1},
		fields=["name", "follow_up_date", "follow_up_status", "follow_up_reason", "follow_up_appointment_id", "modified"],
		order_by="follow_up_date desc, modified desc",
		limit_page_length=limit,
		ignore_permissions=True,
	)
	return [
		{
			"type": "follow_up",
			"source_doctype": "Vet Visit",
			"name": row.name,
			"visit": row.name,
			"at": row.follow_up_date or row.modified,
			"status": row.follow_up_status,
			"summary": row.follow_up_reason,
			"appointment": row.follow_up_appointment_id,
		}
		for row in rows
	]


def _addendum_events(visit_names: list[str], limit: int) -> list[dict]:
	if not visit_names or not frappe.db.exists("DocType", "Vet Visit Addendum"):
		return []
	rows = frappe.get_all(
		"Vet Visit Addendum",
		filters={"visit": ["in", visit_names]},
		fields=["name", "visit", "addendum_datetime", "addendum_type", "reason", "modified"],
		order_by="addendum_datetime desc, creation desc",
		limit_page_length=limit,
		ignore_permissions=True,
	)
	return [
		{
			"type": "addendum",
			"source_doctype": "Vet Visit Addendum",
			"name": row.name,
			"visit": row.visit,
			"at": row.addendum_datetime or row.modified,
			"status": row.addendum_type,
			"summary": row.reason,
		}
		for row in rows
	]


def _invoice_events(visit_names: list[str], limit: int) -> list[dict]:
	if not visit_names:
		return []
	visits = frappe.get_all(
		"Vet Visit",
		filters={"name": ["in", visit_names], "sales_invoice": ["is", "set"]},
		fields=["name", "sales_invoice", "billing_status", "modified"],
		limit_page_length=limit,
		ignore_permissions=True,
	)
	events = []
	for visit in visits:
		invoice = frappe.db.get_value(
			"Sales Invoice",
			visit.sales_invoice,
			["name", "posting_date", "status", "grand_total", "outstanding_amount"],
			as_dict=True,
		)
		if not invoice:
			continue
		events.append(
			{
				"type": "invoice",
				"source_doctype": "Sales Invoice",
				"name": invoice.name,
				"visit": visit.name,
				"at": invoice.posting_date or visit.modified,
				"status": invoice.status or visit.billing_status,
				"summary": invoice.grand_total,
				"outstanding_amount": invoice.outstanding_amount,
			}
		)
	return events


def _record_events(doctype: str, pet: str, event_type: str, summary_field: str, date_field: str, limit: int) -> list[dict]:
	if not frappe.db.exists("DocType", doctype):
		return []
	rows = frappe.get_all(
		doctype,
		filters={"pet": pet},
		fields=["name", "visit", "pet", "doctor", summary_field, date_field, "next_due_date", "reminder_status", "modified"],
		order_by=f"{date_field} desc, modified desc",
		limit_page_length=limit,
		ignore_permissions=True,
	)
	events = [
		{
			"type": event_type,
			"source_doctype": doctype,
			"name": row.name,
			"visit": row.visit,
			"pet": row.pet,
			"at": row.get(date_field) or row.modified,
			"status": row.get("reminder_status"),
			"summary": row.get(summary_field),
			"doctor": row.doctor,
			"next_due_date": row.get("next_due_date"),
		}
		for row in rows
	]
	enrich_link_aliases(events, pet_field="pet", doctor_field="doctor", include_guardian=False, include_provider=False)
	return events


def _boarding_events(pet: str, limit: int) -> list[dict]:
	if not frappe.db.exists("DocType", "Pet Boarding"):
		return []
	rows = frappe.get_all(
		"Pet Boarding",
		filters={"pet": pet},
		fields=["name", "pet", "guardian", "record_status", "status", "check_in", "check_out", "service_room", "billing_status", "sales_invoice", "modified"],
		order_by="modified desc",
		limit_page_length=limit,
		ignore_permissions=True,
	)
	events = [
		{
			"type": "boarding",
			"source_doctype": "Pet Boarding",
			"name": row.name,
			"pet": row.pet,
			"guardian": row.guardian,
			"at": row.check_out or row.check_in or row.modified,
			"status": row.record_status or row.status,
			"summary": row.service_room,
			"billing_status": row.billing_status,
			"invoice": row.sales_invoice,
		}
		for row in rows
	]
	enrich_link_aliases(events, pet_field="pet", guardian_field="guardian", include_doctor=False, include_provider=False)
	return events


def _death_events(pet: str, limit: int) -> list[dict]:
	if not frappe.db.exists("DocType", "Pet Death Record"):
		return []
	rows = frappe.get_all(
		"Pet Death Record",
		filters={"pet": pet, "status": ["!=", "Cancelled"]},
		fields=[
			"name",
			"pet",
			"guardian",
			"death_datetime",
			"death_reason",
			"death_reason_category",
			"guardian_visible_summary",
			"source_doctype",
			"source_name",
			"status",
			"certificate_issued",
			"certificate_no",
			"certificate_file",
			"modified",
		],
		order_by="death_datetime desc, creation desc",
		limit_page_length=limit,
		ignore_permissions=True,
	)
	events = [
		{
			"type": "death",
			"source_doctype": "Pet Death Record",
			"name": row.name,
			"pet": row.pet,
			"guardian": row.guardian,
			"at": row.death_datetime or row.modified,
			"status": row.status,
			"summary": row.guardian_visible_summary or row.death_reason_category or row.death_reason,
			"death_reason": row.death_reason,
			"death_reason_category": row.death_reason_category,
			"source": {"doctype": row.source_doctype, "name": row.source_name} if row.source_doctype else {},
			"certificate_issued": row.certificate_issued,
			"certificate_no": row.certificate_no,
			"certificate_file": row.certificate_file,
		}
		for row in rows
	]
	enrich_link_aliases(events, pet_field="pet", guardian_field="guardian", include_doctor=False, include_provider=False)
	return events


def _primary_guardian_for_pet(pet: str) -> str | None:
	return frappe.db.get_value("PetGuardian", {"pet_id": pet, "role": "primary_owner"}, "guardian_id") or frappe.db.get_value(
		"PetGuardian", {"pet_id": pet}, "guardian_id"
	)


def _payload(data, kwargs) -> dict:
	if isinstance(data, str) and data:
		return json.loads(data)
	if isinstance(data, dict):
		return data
	return kwargs or {}


def _error_response(exc: Exception) -> dict:
	code = "PERMISSION_DENIED" if isinstance(exc, frappe.PermissionError) else "ERROR"
	return fail(cstr(exc), code=code, details=frappe.get_traceback())
