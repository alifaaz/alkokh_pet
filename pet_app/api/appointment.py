from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr, getdate, now_datetime

from pet_app.api.link_aliases import with_link_aliases
from pet_app.api.permissions import require_doctype_permission
from pet_app.api.response import fail, ok
from pet_app.api.workspace import _case_sheet_from_appointment, _coerce_dict
from pet_app.utils.guardian_customer import get_or_create_customer_from_guardian

ACTIVE_QUEUE_STATUSES = {"Checked In", "Waiting", "Called", "In Consultation"}


@frappe.whitelist(methods=["POST"])
def check_in_appointment(appointment_id=None, name=None, payload=None, **kwargs):
	try:
		require_doctype_permission("Pet Queue Ticket", "create")
		payload = _coerce_dict(payload)
		payload.update({key: value for key, value in kwargs.items() if value is not None})

		appointment_name = cstr(appointment_id or name or payload.get("appointment") or payload.get("appointment_id")).strip()
		if not appointment_name:
			return fail(_("Appointment is required."), code="VALIDATION_ERROR")
		if not frappe.db.exists("Appointment", appointment_name):
			return fail(_("Appointment {0} was not found.").format(appointment_name), code="NOT_FOUND")

		appointment = frappe.get_doc("Appointment", appointment_name)
		linked_visit = appointment.get("custom_linked_visit_id")
		if linked_visit and frappe.db.exists("Vet Visit", linked_visit):
			visit = frappe.get_doc("Vet Visit", linked_visit)
			ticket = get_or_create_queue_ticket(
				appointment=appointment.name,
				case_sheet=visit.case_sheet,
				visit=visit.name,
				guardian=visit.guardian,
				customer=visit.customer,
				pet=visit.animal_patient,
				doctor=payload.get("practitioner") or payload.get("doctor") or visit.doctor,
				priority=payload.get("priority") or visit.get("priority"),
				room=payload.get("room"),
				branch=payload.get("branch"),
			)
			return ok(
				{
					"appointment": appointment.name,
					"case_sheet": visit.case_sheet,
					"visit": visit.name,
					"queue_ticket": _ticket_payload(ticket),
				}
			)

		case_sheet = _case_sheet_from_appointment(appointment, payload)
		ticket = get_or_create_queue_ticket(
			appointment=appointment.name,
			case_sheet=case_sheet.name,
			guardian=case_sheet.guardian,
			customer=case_sheet.customer,
			pet=case_sheet.animal_patient,
			doctor=payload.get("practitioner") or payload.get("doctor"),
			priority=payload.get("priority") or case_sheet.priority,
			room=payload.get("room"),
			branch=payload.get("branch"),
		)
		return ok(
			{
				"appointment": appointment.name,
				"case_sheet": case_sheet.name,
				"queue_ticket": _ticket_payload(ticket),
			}
		)
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def create_walkin_case_sheet(payload=None, **kwargs):
	try:
		require_doctype_permission("Vet Case Sheet", "create")
		require_doctype_permission("Pet Queue Ticket", "create")
		payload = _coerce_dict(payload)
		payload.update({key: value for key, value in kwargs.items() if value is not None})

		pet = cstr(payload.get("pet") or payload.get("pet_id") or payload.get("animal_patient")).strip()
		guardian = cstr(payload.get("guardian") or payload.get("guardian_id")).strip()
		if not pet or not guardian:
			return fail(_("Pet and Guardian are required."), code="VALIDATION_ERROR")
		if not frappe.db.exists("PetGuardian", {"pet_id": pet, "guardian_id": guardian}):
			return fail(_("Pet is not linked to Guardian."), code="PERMISSION_DENIED")

		customer = payload.get("customer") or get_or_create_customer_from_guardian(guardian)
		case_sheet = frappe.get_doc(
			{
				"doctype": "Vet Case Sheet",
				"status": "Waiting Practitioner",
				"priority": payload.get("priority") or "Normal",
				"guardian": guardian,
				"customer": customer,
				"animal_patient": pet,
				"chief_complaint": payload.get("chief_complaint") or "Checkup",
				"intake_notes": payload.get("intake_notes") or payload.get("note"),
				"weight": payload.get("weight"),
			}
		)
		case_sheet.insert(ignore_permissions=True)
		ticket = get_or_create_queue_ticket(
			case_sheet=case_sheet.name,
			guardian=guardian,
			customer=case_sheet.customer,
			pet=pet,
			doctor=payload.get("practitioner") or payload.get("doctor"),
			priority=case_sheet.priority,
			room=payload.get("room"),
			branch=payload.get("branch"),
		)
		return ok({"case_sheet": case_sheet.name, "queue_ticket": _ticket_payload(ticket)})
	except Exception as exc:
		return _error_response(exc)


def get_or_create_queue_ticket(
	*,
	appointment: str | None = None,
	case_sheet: str | None = None,
	visit: str | None = None,
	guardian: str | None = None,
	customer: str | None = None,
	pet: str | None = None,
	doctor: str | None = None,
	priority: str | None = None,
	room: str | None = None,
	branch: str | None = None,
):
	filters = None
	if appointment:
		filters = {"appointment": appointment, "status": ["in", list(ACTIVE_QUEUE_STATUSES)]}
	elif visit:
		filters = {"visit": visit, "status": ["in", list(ACTIVE_QUEUE_STATUSES)]}
	elif case_sheet:
		filters = {"case_sheet": case_sheet, "status": ["in", list(ACTIVE_QUEUE_STATUSES)]}
	if filters:
		existing = frappe.db.get_value("Pet Queue Ticket", filters, "name")
		if existing:
			ticket = frappe.get_doc("Pet Queue Ticket", existing)
			updates = {
				"case_sheet": case_sheet,
				"visit": visit,
				"guardian": guardian,
				"customer": customer,
				"pet": pet,
				"doctor": doctor,
				"priority": priority,
				"room": room,
				"branch": branch,
			}
			changed = False
			for fieldname, value in updates.items():
				if value and not ticket.get(fieldname):
					ticket.set(fieldname, value)
					changed = True
			if ticket.status in {"No Show", "Cancelled"}:
				ticket.status = "Checked In"
				ticket.checked_in_at = ticket.checked_in_at or now_datetime()
				changed = True
			if changed:
				ticket.save(ignore_permissions=True)
			return ticket

	ticket = frappe.get_doc(
		{
			"doctype": "Pet Queue Ticket",
			"queue_date": getdate(),
			"appointment": appointment,
			"case_sheet": case_sheet,
			"visit": visit,
			"guardian": guardian,
			"customer": customer,
			"pet": pet,
			"doctor": doctor,
			"priority": priority or "Normal",
			"room": room,
			"branch": branch,
			"status": "Checked In",
			"checked_in_at": now_datetime(),
		}
	)
	ticket.insert(ignore_permissions=True)
	return ticket


def _ticket_payload(ticket) -> dict:
	if isinstance(ticket, str):
		ticket = frappe.get_doc("Pet Queue Ticket", ticket)
	payload = {
		"name": ticket.name,
		"ticket_no": ticket.ticket_no,
		"queue_date": ticket.queue_date,
		"status": ticket.status,
		"priority": ticket.priority,
		"appointment": ticket.appointment,
		"case_sheet": ticket.case_sheet,
		"visit": ticket.visit,
		"guardian": ticket.guardian,
		"customer": ticket.customer,
		"pet": ticket.pet,
		"doctor": ticket.doctor,
		"room": ticket.room,
		"branch": ticket.branch,
		"checked_in_at": ticket.checked_in_at,
		"called_at": ticket.called_at,
		"started_at": ticket.started_at,
		"completed_at": ticket.completed_at,
		"no_show_at": ticket.no_show_at,
		"cancelled_at": ticket.cancelled_at,
	}
	return with_link_aliases(payload, pet_field="pet", guardian_field="guardian", doctor_field="doctor", include_provider=False)


def _error_response(exc: Exception) -> dict:
	code = "PERMISSION_DENIED" if isinstance(exc, frappe.PermissionError) else "ERROR"
	return fail(cstr(exc), code=code, details=frappe.get_traceback())
