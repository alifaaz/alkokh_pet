from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cstr, getdate, now_datetime, nowdate

from pet_app.api.response import fail, ok, standardize_response
from pet_app.api.link_aliases import with_link_aliases


OPEN_FOLLOW_UP_STATUSES = ("Requested", "Scheduled", "Contacted", "Missed")


@frappe.whitelist()
def list_due_follow_ups(date_from=None, date_to=None, doctor=None, guardian=None, pet=None, status=None, limit=50):
	try:
		filters = {"docstatus": ["<", 2], "follow_up_required": 1}
		if doctor:
			filters["doctor"] = doctor
		if guardian:
			filters["guardian"] = guardian
		if pet:
			filters["animal_patient"] = pet
		if status:
			filters["follow_up_status"] = status
		else:
			filters["follow_up_status"] = ["in", OPEN_FOLLOW_UP_STATUSES]
		if date_from:
			filters["follow_up_date"] = [">=", getdate(date_from)]
		if date_to:
			if "follow_up_date" in filters and isinstance(filters["follow_up_date"], list):
				filters["follow_up_date"] = ["between", [getdate(date_from), getdate(date_to)]]
			else:
				filters["follow_up_date"] = ["<=", getdate(date_to)]
		if not date_to and not date_from:
			filters["follow_up_date"] = ["<=", getdate(nowdate())]
		limit = max(min(int(limit or 50), 200), 1)
		rows = frappe.get_all(
			"Vet Visit",
			filters=filters,
			fields=[
				"name",
				"animal_patient",
				"guardian",
				"customer",
				"doctor",
				"visit_datetime",
				"follow_up_status",
				"follow_up_reason",
				"follow_up_preferred_date",
				"follow_up_date",
				"follow_up_appointment_id",
				"follow_up_contacted_at",
				"follow_up_contacted_by",
				"follow_up_contact_note",
				"missed_reason",
			],
			order_by="follow_up_date asc, modified desc",
			limit_page_length=limit,
			ignore_permissions=True,
		)
		follow_ups = [dict(row) for row in rows]
		_enrich_follow_up_display_names(follow_ups)
		return ok({"follow_ups": follow_ups}, meta={"total": len(rows)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
@standardize_response
def mark_follow_up_contacted(visit=None, note=None, data=None, **kwargs):
	return _update_follow_up(visit, data, kwargs, status="Contacted", note=note)


@frappe.whitelist(methods=["POST"])
@standardize_response
def mark_follow_up_missed(visit=None, missed_reason=None, data=None, **kwargs):
	return _update_follow_up(visit, data, kwargs, status="Missed", missed_reason=missed_reason)


@frappe.whitelist(methods=["POST"])
def reschedule_follow_up(visit=None, follow_up_date=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		doc = _get_visit(visit or payload.get("visit"))
		new_date = follow_up_date or payload.get("follow_up_date") or payload.get("date")
		if not new_date:
			return fail(_("Follow-up date is required."), code="VALIDATION_ERROR")
		doc.follow_up_required = 1
		doc.follow_up_date = getdate(new_date)
		if doc.meta.has_field("follow_up_preferred_date"):
			doc.follow_up_preferred_date = getdate(new_date)
		doc.follow_up_status = "Scheduled"
		if doc.meta.has_field("follow_up_contact_note") and payload.get("note"):
			doc.follow_up_contact_note = payload.get("note")
		doc.flags.ignore_billing_lock = True
		doc.save(ignore_permissions=True)
		return ok({"follow_up": _follow_up_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


def _update_follow_up(visit, data, kwargs, *, status, note=None, missed_reason=None):
	try:
		payload = _payload(data, kwargs)
		doc = _get_visit(visit or payload.get("visit"))
		doc.follow_up_required = 1
		doc.follow_up_status = status
		if status == "Contacted":
			doc.follow_up_contacted_at = now_datetime()
			doc.follow_up_contacted_by = frappe.session.user
			doc.follow_up_contact_note = note or payload.get("note") or payload.get("follow_up_contact_note")
		if status == "Missed":
			doc.missed_reason = missed_reason or payload.get("missed_reason") or payload.get("reason")
		doc.flags.ignore_billing_lock = True
		doc.save(ignore_permissions=True)
		return ok({"follow_up": _follow_up_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


def _get_visit(name):
	name = cstr(name).strip()
	if not name:
		frappe.throw(_("Visit is required."))
	doc = frappe.get_doc("Vet Visit", name)
	doc.check_permission("write")
	return doc


def _follow_up_payload(doc) -> dict:
	payload = {
		"visit": doc.name,
		"pet": doc.animal_patient,
		"guardian": doc.guardian,
		"doctor": doc.doctor,
		"status": doc.follow_up_status,
		"reason": doc.get("follow_up_reason"),
		"preferred_date": doc.get("follow_up_preferred_date"),
		"date": doc.get("follow_up_date"),
		"appointment_id": doc.get("follow_up_appointment_id"),
		"contacted_at": doc.get("follow_up_contacted_at"),
		"contacted_by": doc.get("follow_up_contacted_by"),
		"contact_note": doc.get("follow_up_contact_note"),
		"missed_reason": doc.get("missed_reason"),
	}
	return with_link_aliases(payload, pet_field="pet", guardian_field="guardian", doctor_field="doctor", include_provider=False)


def _enrich_follow_up_display_names(follow_ups: list[dict]) -> None:
	pet_ids = sorted({cstr(row.get("animal_patient")).strip() for row in follow_ups if row.get("animal_patient")})
	guardian_ids = sorted({cstr(row.get("guardian")).strip() for row in follow_ups if row.get("guardian")})
	doctor_ids = sorted({cstr(row.get("doctor")).strip() for row in follow_ups if row.get("doctor")})

	pet_names = {}
	if pet_ids:
		pet_names = {
			row.name: row.pet_name
			for row in frappe.get_all("Pet", filters={"name": ["in", pet_ids]}, fields=["name", "pet_name"])
		}

	guardian_names = {}
	if guardian_ids:
		guardian_names = {
			row.name: row.full_name
			for row in frappe.get_all("Guardian", filters={"name": ["in", guardian_ids]}, fields=["name", "full_name"])
		}

	doctor_names = {}
	if doctor_ids:
		doctor_names = {
			row.name: row.practitioner_name
			for row in frappe.get_all(
				"Healthcare Practitioner",
				filters={"name": ["in", doctor_ids]},
				fields=["name", "practitioner_name"],
			)
		}

	for row in follow_ups:
		pet_id = row.get("animal_patient")
		guardian_id = row.get("guardian")
		doctor_id = row.get("doctor")
		row["pet"] = pet_id
		row["pet_id"] = pet_id
		row["pet_name"] = pet_names.get(pet_id) or pet_id
		row["guardian_id"] = guardian_id
		row["guardian_name"] = guardian_names.get(guardian_id) or guardian_id
		row["doctor_name"] = doctor_names.get(doctor_id) or doctor_id


def _payload(data, kwargs) -> dict:
	if isinstance(data, str) and data:
		return json.loads(data)
	if isinstance(data, dict):
		return data
	return kwargs or {}


def _error_response(exc):
	if isinstance(exc, frappe.PermissionError):
		return fail(_("Not permitted"), code="PERMISSION_ERROR")
	return fail(cstr(exc), code=getattr(exc, "exc_type", None) or exc.__class__.__name__)
