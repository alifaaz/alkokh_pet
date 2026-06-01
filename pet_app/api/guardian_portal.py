from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, cstr, now_datetime

from pet_app.api.link_aliases import enrich_link_aliases, guardian_display_map, with_link_aliases
from pet_app.api.response import fail, ok


@frappe.whitelist()
def get_my_pets_dashboard():
	try:
		guardian = _current_guardian()
		guardian_name = guardian_display_map([guardian]).get(guardian) or guardian
		pets = _guardian_pets(guardian)
		data = []
		for pet in pets:
			data.append(
				{
					"pet": pet,
					"pet_id": pet.get("name"),
					"pet_name": pet.get("pet_name") or pet.get("name"),
					"guardian_id": guardian,
					"guardian_name": guardian_name,
					"latest_visit": _latest_visit(pet["name"]),
					"upcoming_appointments": _appointments(guardian, pet["name"], future_only=True, limit=3),
					"active_boarding": _active_boarding(guardian, pet["name"]),
					"open_invoices": _invoices(guardian, outstanding_only=True, limit=5),
					"death_record": _owner_safe_death_record(pet["name"]),
				}
			)
		return ok({"guardian": guardian, "guardian_id": guardian, "guardian_name": guardian_name, "pets": data}, meta={"total": len(data)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_pet_medical_timeline(pet=None, pet_id=None, limit=50):
	try:
		guardian = _current_guardian()
		pet_name = cstr(pet or pet_id).strip()
		_assert_pet_access(guardian, pet_name)
		events = []
		events.extend(_visit_events(pet_name, limit))
		events.extend(_released_diagnostic_events("Lab", pet_name, limit))
		events.extend(_released_diagnostic_events("Imaging", pet_name, limit))
		events.extend(_procedure_events(pet_name, limit))
		events.extend(_vaccination_events(pet_name, limit))
		events.extend(_deworming_events(pet_name, limit))
		death = _owner_safe_death_record(pet_name)
		if death:
			events.append(
				{
					"type": "death",
					"source_doctype": "Pet Death Record",
					"name": death["name"],
					"at": death["death_datetime"],
					"status": death["status"],
					"summary": death["guardian_visible_summary"],
					"certificate_issued": death["certificate_issued"],
				}
			)
		events = sorted(events, key=lambda row: cstr(row.get("at")), reverse=True)[: int(limit or 50)]
		return ok({"events": events}, meta={"total": len(events)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_pet_documents(pet=None, pet_id=None):
	try:
		guardian = _current_guardian()
		pet_name = cstr(pet or pet_id).strip()
		_assert_pet_access(guardian, pet_name)
		documents = []
		for doctype in ("Pet Vaccination Record", "Pet Deworming Record", "Pet Death Record"):
			if not frappe.db.exists("DocType", doctype):
				continue
			rows = frappe.get_all(
				doctype,
				filters={"pet": pet_name},
				fields=["name", "modified", "certificate_file"] if doctype == "Pet Death Record" else ["name", "modified"],
				order_by="modified desc",
				ignore_permissions=True,
			)
			for row in rows:
				if doctype == "Pet Death Record" and not row.get("certificate_file"):
					continue
				documents.append({"source_doctype": doctype, "name": row.name, "file": row.get("certificate_file"), "modified": row.modified})
		for doctype in ("Lab", "Imaging"):
			for row in _released_diagnostic_rows(doctype, pet_name, fields=["name", "modified"]):
				documents.append({"source_doctype": doctype, "name": row.name, "modified": row.modified})
		return ok({"documents": documents}, meta={"total": len(documents)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_upcoming_appointments(limit=20):
	try:
		guardian = _current_guardian()
		rows = _appointments(guardian, None, future_only=True, limit=int(limit or 20))
		return ok({"appointments": rows}, meta={"total": len(rows)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_guardian_invoices(outstanding_only=0, limit=50):
	try:
		guardian = _current_guardian()
		rows = _invoices(guardian, outstanding_only=cint(outstanding_only), limit=int(limit or 50))
		return ok({"invoices": rows}, meta={"total": len(rows)})
	except Exception as exc:
		return _error_response(exc)


def _current_guardian() -> str:
	guardian = frappe.db.get_value("Guardian", {"user_id": frappe.session.user}, "name")
	if not guardian:
		frappe.throw(_("No Guardian is linked to the current user."), frappe.PermissionError)
	return guardian


def _assert_pet_access(guardian: str, pet: str):
	if not pet or not frappe.db.exists("PetGuardian", {"guardian_id": guardian, "pet_id": pet}):
		frappe.throw(_("Not permitted"), frappe.PermissionError)


def _guardian_pets(guardian: str) -> list[dict]:
	links = frappe.get_all("PetGuardian", filters={"guardian_id": guardian}, pluck="pet_id", ignore_permissions=True)
	if not links:
		return []
	rows = frappe.get_all(
		"Pet",
		filters={"name": ["in", links]},
		fields=["name", "pet_name", "animal_species", "animal_type", "breed", "gender", "weight", "pet_image", "is_deceased", "death_date"],
		order_by="pet_name asc",
		ignore_permissions=True,
	)
	pets = [dict(row) for row in rows]
	enrich_link_aliases(pets, pet_field="name", include_guardian=False, include_doctor=False, include_provider=False)
	return pets


def _latest_visit(pet: str) -> dict:
	row = frappe.db.get_value(
		"Vet Visit",
		{"animal_patient": pet},
		["name", "visit_datetime", "status", "diagnosis", "follow_up_date", "follow_up_status"],
		as_dict=True,
		order_by="visit_datetime desc, creation desc",
	)
	if not row:
		return {}
	return dict(row)


def _visit_events(pet: str, limit: int) -> list[dict]:
	rows = frappe.get_all(
		"Vet Visit",
		filters={"animal_patient": pet},
		fields=["name", "visit_datetime", "status", "diagnosis", "follow_up_date", "follow_up_status", "modified"],
		order_by="visit_datetime desc, creation desc",
		limit_page_length=limit,
		ignore_permissions=True,
	)
	return [
		{
			"type": "visit",
			"source_doctype": "Vet Visit",
			"name": row.name,
			"at": row.visit_datetime or row.modified,
			"status": row.status,
			"summary": row.diagnosis,
			"follow_up_date": row.follow_up_date,
			"follow_up_status": row.follow_up_status,
		}
		for row in rows
	]


def _released_diagnostic_rows(doctype: str, pet: str, fields=None):
	fields = fields or _diagnostic_fields(doctype)
	return frappe.get_all(
		doctype,
		filters={"pet": pet, "status": "Released", "result_visibility": "Guardian Visible"},
		fields=fields,
		order_by="released_at desc, modified desc",
		ignore_permissions=True,
	)


def _diagnostic_fields(doctype: str) -> list[str]:
	base = ["name", "visit", "status", "care_service", "released_at", "modified"]
	if doctype == "Lab":
		return [*base, "result"]
	if doctype == "Imaging":
		return [*base, "report", "image"]
	return base


def _released_diagnostic_events(doctype: str, pet: str, limit: int) -> list[dict]:
	if not frappe.db.exists("DocType", doctype):
		return []
	rows = _released_diagnostic_rows(doctype, pet)[:limit]
	return [
		{
			"type": doctype.lower(),
			"source_doctype": doctype,
			"name": row.name,
			"visit": row.visit,
			"at": row.released_at or row.modified,
			"status": row.status,
			"summary": row.get("result") or row.get("report") or row.care_service,
			"image": row.get("image"),
		}
		for row in rows
	]


def _procedure_events(pet: str, limit: int) -> list[dict]:
	if not frappe.db.exists("DocType", "Pet Procedure"):
		return []
	rows = frappe.get_all(
		"Pet Procedure",
		filters={"pet": pet, "status": ["in", ["Completed", "Closed"]]},
		fields=["name", "visit", "procedure_template", "status", "completed_at", "closed_at", "modified", "aftercare_instructions"],
		order_by="modified desc",
		limit_page_length=limit,
		ignore_permissions=True,
	)
	return [
		{
			"type": "procedure",
			"source_doctype": "Pet Procedure",
			"name": row.name,
			"visit": row.visit,
			"at": row.closed_at or row.completed_at or row.modified,
			"status": row.status,
			"summary": row.procedure_template,
			"aftercare_instructions": row.aftercare_instructions,
		}
		for row in rows
	]


def _vaccination_events(pet: str, limit: int) -> list[dict]:
	return _preventive_events("Pet Vaccination Record", pet, "vaccination", "vaccine_name", limit)


def _deworming_events(pet: str, limit: int) -> list[dict]:
	return _preventive_events("Pet Deworming Record", pet, "deworming", "medication_name", limit)


def _preventive_events(doctype, pet, event_type, summary_field, limit):
	if not frappe.db.exists("DocType", doctype):
		return []
	rows = frappe.get_all(
		doctype,
		filters={"pet": pet},
		fields=["name", "visit", summary_field, "administered_on", "next_due_date", "reminder_status"],
		order_by="administered_on desc, modified desc",
		limit_page_length=limit,
		ignore_permissions=True,
	)
	return [
		{
			"type": event_type,
			"source_doctype": doctype,
			"name": row.name,
			"visit": row.visit,
			"at": row.administered_on,
			"status": row.reminder_status,
			"summary": row.get(summary_field),
			"next_due_date": row.next_due_date,
		}
		for row in rows
	]


def _appointments(guardian: str, pet: str | None, future_only: bool, limit: int) -> list[dict]:
	filters = {"custom_guardian": guardian, "status": ["not in", ["Cancelled", "Closed"]]}
	if pet:
		filters["custom_pet"] = pet
	if future_only:
		filters["scheduled_time"] = [">=", now_datetime()]
	rows = frappe.get_all(
		"Appointment",
		filters=filters,
		fields=["name", "status", "scheduled_time", "customer_details", "custom_appointment_type", "custom_pet", "custom_guardian", "custom_doctor"],
		order_by="scheduled_time asc",
		limit_page_length=limit,
		ignore_permissions=True,
	)
	appointments = [dict(row) for row in rows]
	enrich_link_aliases(appointments, pet_field="custom_pet", guardian_field="custom_guardian", doctor_field="custom_doctor", include_provider=False)
	return appointments


def _active_boarding(guardian: str, pet: str) -> dict:
	if not frappe.db.exists("DocType", "Pet Boarding"):
		return {}
	row = frappe.db.get_value(
		"Pet Boarding",
		{"guardian": guardian, "pet": pet, "record_status": ["in", ["Reserved", "Checked In"]]},
		["name", "service_room", "pet", "guardian", "record_status", "check_in", "check_out"],
		as_dict=True,
		order_by="modified desc",
	)
	if not row:
		return {}
	payload = dict(row)
	enrich_link_aliases([payload], pet_field="pet", guardian_field="guardian", include_doctor=False, include_provider=False)
	return payload


def _invoices(guardian: str, outstanding_only=False, limit=50) -> list[dict]:
	customer = frappe.db.get_value("Guardian", guardian, "customer_id")
	if not customer:
		return []
	filters = {"customer": customer, "docstatus": ["<", 2]}
	if outstanding_only:
		filters["outstanding_amount"] = [">", 0]
	rows = frappe.get_all(
		"Sales Invoice",
		filters=filters,
		fields=["name", "posting_date", "due_date", "status", "grand_total", "outstanding_amount", "docstatus"],
		order_by="posting_date desc, modified desc",
		limit_page_length=limit,
		ignore_permissions=True,
	)
	return [dict(row) for row in rows]


def _owner_safe_death_record(pet: str) -> dict:
	if not frappe.db.exists("DocType", "Pet Death Record"):
		return {}
	name = frappe.db.get_value("Pet Death Record", {"pet": pet, "status": ["!=", "Cancelled"]}, "name")
	if not name:
		return {}
	doc = frappe.get_doc("Pet Death Record", name)
	payload = {
		"name": doc.name,
		"pet": doc.pet,
		"death_datetime": doc.death_datetime,
		"death_reason_category": doc.death_reason_category,
		"guardian_visible_summary": doc.guardian_visible_summary,
		"status": doc.status,
		"certificate_issued": cint(doc.certificate_issued),
		"certificate_no": doc.certificate_no,
		"certificate_file": doc.certificate_file,
	}
	return with_link_aliases(payload, pet_field="pet", include_guardian=False, include_doctor=False, include_provider=False)


def _error_response(exc):
	if isinstance(exc, frappe.PermissionError):
		return fail(_("Not permitted"), code="PERMISSION_ERROR")
	return fail(cstr(exc), code=getattr(exc, "exc_type", None) or exc.__class__.__name__)
