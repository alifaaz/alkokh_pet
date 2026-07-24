from __future__ import annotations

import json
from datetime import datetime, timedelta

import frappe
from frappe import _
from frappe.utils import cint, cstr, get_datetime, getdate, now_datetime

from pet_app.api.link_aliases import enrich_link_aliases, with_link_aliases
from pet_app.api.response import fail, ok, standardize_response
from pet_app.utils.guardian_customer import get_or_create_customer_from_guardian
from pet_app.utils.mortality import validate_not_deceased
from pet_app.utils.offline import run_idempotent


@frappe.whitelist()
def get_available_slots(date=None, doctor=None, practitioner=None, room=None, service_type="Visit", duration_minutes=None):
	try:
		doctor = practitioner or doctor
		target_date = getdate(date)
		duration = int(duration_minutes or _duration_minutes(service_type) or 30)
		windows = _availability_windows(target_date, doctor=doctor, room=room)
		slots = []
		for start, end in windows:
			cursor = start
			while cursor + timedelta(minutes=duration) <= end:
				if not _has_conflict(cursor, duration, doctor=doctor, room=room):
					slots.append({"start": cursor, "end": cursor + timedelta(minutes=duration), "doctor": doctor, "practitioner": doctor, "room": room})
				cursor += timedelta(minutes=duration)
		enrich_link_aliases(slots, doctor_field="doctor", include_pet=False, include_guardian=False, include_provider=False)
		return ok({"slots": slots}, meta={"total": len(slots), "duration_minutes": duration})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
@standardize_response
def book_appointment(data=None, **kwargs):
	payload = _payload(data, kwargs)
	return run_idempotent("pet_app.api.scheduling.book_appointment", payload, lambda: _book_appointment(payload), idempotency_key=payload.get("idempotency_key"))


def _book_appointment(payload):
	try:
		pet = payload.get("pet") or payload.get("pet_id")
		guardian = payload.get("guardian") or payload.get("guardian_id")
		validate_not_deceased(pet, doctype="Appointment")
		if not pet or not guardian:
			return fail(_("Pet and Guardian are required."), code="VALIDATION_ERROR")
		if not frappe.db.exists("PetGuardian", {"pet_id": pet, "guardian_id": guardian}):
			return fail(_("Pet is not linked to Guardian."), code="PERMISSION_ERROR")
		start = get_datetime(payload.get("scheduled_time") or payload.get("start"))
		duration = int(payload.get("duration_minutes") or _duration_minutes(payload.get("service_type") or "Visit") or 30)
		doctor = payload.get("practitioner") or payload.get("doctor")
		room = payload.get("room")
		if _has_conflict(start, duration, doctor=doctor, room=room):
			return fail(_("Requested appointment slot is no longer available."), code="CONFLICT")
		guardian_doc = frappe.get_doc("Guardian", guardian)
		customer = payload.get("customer") or get_or_create_customer_from_guardian(guardian)
		doc = frappe.get_doc(
			{
				"doctype": "Appointment",
				"status": "Open",
				"scheduled_time": start,
				"customer_name": guardian_doc.full_name,
				"customer_phone_number": guardian_doc.phone,
				"customer_email": guardian_doc.email_id,
				"customer_details": payload.get("note") or payload.get("customer_details"),
				"custom_appointment_type": payload.get("appointment_type") or "visit",
				"custom_pet": pet,
				"custom_guardian": guardian,
				"custom_customer": customer,
			}
		)
		_set_optional(doc, "custom_doctor", doctor)
		_set_optional(doc, "custom_room", room)
		_set_optional(doc, "custom_duration_minutes", duration)
		_set_optional(doc, "custom_client_request_id", payload.get("client_request_id"))
		_set_optional(doc, "custom_idempotency_key", payload.get("idempotency_key"))
		doc.insert(ignore_permissions=True)
		return ok({"appointment": _appointment_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def reschedule_appointment(appointment=None, scheduled_time=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		doc = frappe.get_doc("Appointment", appointment or payload.get("appointment"))
		start = get_datetime(scheduled_time or payload.get("scheduled_time") or payload.get("start"))
		duration = int(payload.get("duration_minutes") or doc.get("custom_duration_minutes") or 30)
		doctor = payload.get("practitioner") or payload.get("doctor") or doc.get("custom_doctor")
		room = payload.get("room") or doc.get("custom_room")
		if _has_conflict(start, duration, doctor=doctor, room=room, exclude=doc.name):
			return fail(_("Requested appointment slot is no longer available."), code="CONFLICT")
		doc.scheduled_time = start
		_set_optional(doc, "custom_doctor", doctor)
		_set_optional(doc, "custom_room", room)
		_set_optional(doc, "custom_duration_minutes", duration)
		doc.save(ignore_permissions=True)
		return ok({"appointment": _appointment_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def cancel_appointment(appointment=None, reason=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		doc = frappe.get_doc("Appointment", appointment or payload.get("appointment"))
		doc.status = "Cancelled"
		if payload.get("death_record") and doc.meta.has_field("custom_death_record"):
			doc.custom_death_record = payload.get("death_record")
		if reason or payload.get("reason"):
			doc.customer_details = "\n".join(part for part in [doc.customer_details, reason or payload.get("reason")] if part)
		doc.save(ignore_permissions=True)
		return ok({"appointment": _appointment_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def list_appointments(
	guardian=None,
	guardian_id=None,
	customer=None,
	customer_id=None,
	pet=None,
	pet_id=None,
	status=None,
	date_from=None,
	date_to=None,
	future_only=0,
	limit=50,
):
	try:
		requested_guardian = cstr(guardian or guardian_id).strip()
		requested_customer = cstr(customer or customer_id).strip()
		current_guardian = _current_user_guardian()
		if current_guardian and not _can_filter_all_appointments():
			if requested_guardian and requested_guardian != current_guardian:
				return fail(_("Not permitted"), code="PERMISSION_ERROR")
			requested_guardian = current_guardian
			customer_from_guardian = frappe.db.get_value("Guardian", current_guardian, "customer_id")
			if requested_customer and customer_from_guardian and requested_customer != customer_from_guardian:
				return fail(_("Not permitted"), code="PERMISSION_ERROR")

		filters = {"status": ["not in", ["Cancelled", "Closed"]]}
		if requested_guardian:
			filters["custom_guardian"] = requested_guardian
		if requested_customer:
			filters["custom_customer"] = requested_customer
		if pet or pet_id:
			filters["custom_pet"] = cstr(pet or pet_id).strip()
		if status:
			filters["status"] = cstr(status)
		if cint(future_only):
			filters["scheduled_time"] = [">=", now_datetime()]
		if date_from and date_to:
			filters["scheduled_time"] = ["between", [f"{getdate(date_from)} 00:00:00", f"{getdate(date_to)} 23:59:59"]]
		elif date_from:
			filters["scheduled_time"] = [">=", f"{getdate(date_from)} 00:00:00"]
		elif date_to:
			filters["scheduled_time"] = ["<=", f"{getdate(date_to)} 23:59:59"]

		rows = frappe.get_all(
			"Appointment",
			filters=filters,
			fields=[
				"name",
				"status",
				"scheduled_time",
				"customer_name",
				"customer_details",
				"custom_appointment_type",
				"custom_pet",
				"custom_guardian",
				"custom_customer",
				"custom_doctor",
				"custom_room",
				"custom_duration_minutes",
			],
			order_by="scheduled_time asc",
			limit_page_length=int(limit or 50),
			ignore_permissions=True,
		)
		appointments = [dict(row) for row in rows]
		enrich_link_aliases(appointments, pet_field="custom_pet", guardian_field="custom_guardian", doctor_field="custom_doctor", include_provider=False)
		return ok({"appointments": appointments}, meta={"total": len(rows)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_doctor_calendar(doctor=None, practitioner=None, date_from=None, date_to=None):
	try:
		doctor = practitioner or doctor
		filters = {"status": ["not in", ["Cancelled", "Closed"]]}
		if doctor:
			filters["custom_doctor"] = doctor
		if date_from and date_to:
			filters["scheduled_time"] = ["between", [f"{getdate(date_from)} 00:00:00", f"{getdate(date_to)} 23:59:59"]]
		rows = frappe.get_all(
			"Appointment",
			filters=filters,
			fields=["name", "status", "scheduled_time", "customer_name", "custom_pet", "custom_guardian", "custom_doctor", "custom_room", "custom_duration_minutes"],
			order_by="scheduled_time asc",
			ignore_permissions=True,
		)
		appointments = [dict(row) for row in rows]
		enrich_link_aliases(appointments, pet_field="custom_pet", guardian_field="custom_guardian", doctor_field="custom_doctor", include_provider=False)
		return ok({"appointments": appointments}, meta={"total": len(rows)})
	except Exception as exc:
		return _error_response(exc)


def _availability_windows(target_date, doctor=None, room=None):
	day = target_date.strftime("%A")
	windows = []
	if doctor and frappe.db.exists("DocType", "Practitioner Availability"):
		rows = frappe.get_all(
			"Practitioner Availability",
			filters={"practitioner": doctor, "active": 1},
			fields=["date", "day_of_week", "start_time", "end_time"],
			ignore_permissions=True,
		)
		windows.extend(_rows_to_windows(rows, target_date, day))
	if not windows and frappe.db.exists("DocType", "Clinic Working Hours"):
		rows = frappe.get_all(
			"Clinic Working Hours",
			filters={"active": 1},
			fields=["date", "day_of_week", "start_time", "end_time"],
			ignore_permissions=True,
		)
		windows.extend(_rows_to_windows(rows, target_date, day))
	if not windows:
		windows.append((datetime.combine(target_date, datetime.strptime("09:00", "%H:%M").time()), datetime.combine(target_date, datetime.strptime("17:00", "%H:%M").time())))
	return windows


def _rows_to_windows(rows, target_date, day):
	windows = []
	for row in rows:
		if row.date and getdate(row.date) != target_date:
			continue
		if not row.date and row.day_of_week and row.day_of_week != day:
			continue
		windows.append((get_datetime(f"{target_date} {row.start_time}"), get_datetime(f"{target_date} {row.end_time}")))
	return windows


def _has_conflict(start, duration, doctor=None, room=None, exclude=None):
	end = start + timedelta(minutes=duration)
	filters = {"status": ["not in", ["Cancelled", "Closed"]], "scheduled_time": ["between", [start - timedelta(hours=12), end + timedelta(hours=12)]]}
	if exclude:
		filters["name"] = ["!=", exclude]
	rows = frappe.get_all(
		"Appointment",
		filters=filters,
		fields=["name", "scheduled_time", "custom_doctor", "custom_room", "custom_duration_minutes"],
		ignore_permissions=True,
	)
	for row in rows:
		if doctor and row.get("custom_doctor") != doctor:
			continue
		if room and row.get("custom_room") != room:
			continue
		if not doctor and not room:
			continue
		row_start = get_datetime(row.scheduled_time)
		row_end = row_start + timedelta(minutes=int(row.get("custom_duration_minutes") or 30))
		if start < row_end and end > row_start:
			return True
	return False


def _duration_minutes(service_type):
	if frappe.db.exists("DocType", "Service Duration Rules"):
		value = frappe.db.get_value("Service Duration Rules", {"service_type": service_type, "active": 1}, "duration_minutes")
		if value:
			return value
	return 30


def _appointment_payload(doc) -> dict:
	payload = {
		"name": doc.name,
		"status": doc.status,
		"scheduled_time": doc.scheduled_time,
		"pet": doc.get("custom_pet"),
		"guardian": doc.get("custom_guardian"),
		"customer": doc.get("custom_customer"),
		"doctor": doc.get("custom_doctor"),
		"practitioner": doc.get("custom_doctor"),
		"room": doc.get("custom_room"),
		"duration_minutes": doc.get("custom_duration_minutes"),
	}
	return with_link_aliases(payload, pet_field="pet", guardian_field="guardian", doctor_field="doctor", include_provider=False)


def _current_user_guardian() -> str | None:
	if not frappe.session.user or frappe.session.user == "Guest":
		return None
	return frappe.db.get_value("Guardian", {"user_id": frappe.session.user}, "name")


def _can_filter_all_appointments() -> bool:
	roles = set(frappe.get_roles(frappe.session.user) or [])
	return bool({"System Manager", "Healthcare Administrator", "Receptionist", "Healthcare Receptionist"} & roles)


def _set_optional(doc, fieldname, value):
	if value is not None and doc.meta.has_field(fieldname):
		doc.set(fieldname, value)


def _payload(data, kwargs):
	if isinstance(data, str) and data:
		return json.loads(data)
	if isinstance(data, dict):
		return data
	return kwargs or {}


def _error_response(exc):
	return fail(cstr(exc), code=getattr(exc, "exc_type", None) or exc.__class__.__name__)
