from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, cstr, getdate, now_datetime

from pet_app.api.appointment import _ticket_payload
from pet_app.api.permissions import require_doctype_permission, require_restriction_value
from pet_app.api.response import fail, ok


@frappe.whitelist()
def list_queue(queue_date=None, status=None, doctor=None, practitioner=None, branch=None, limit_start=0, limit_page_length=50):
	try:
		doctor = practitioner or doctor
		require_doctype_permission("Pet Queue Ticket", "read")
		filters = {"queue_date": getdate(queue_date) if queue_date else getdate()}
		if status:
			statuses = [cstr(value).strip() for value in cstr(status).split(",") if cstr(value).strip()]
			filters["status"] = ["in", statuses] if len(statuses) > 1 else statuses[0]
		if doctor:
			require_restriction_value("practitioner", doctor)
			filters["doctor"] = doctor
		if branch:
			require_restriction_value("branch", branch)
			filters["branch"] = branch

		limit_start = max(cint(limit_start), 0)
		limit_page_length = max(min(cint(limit_page_length or 50), 200), 1)
		names = frappe.get_all(
			"Pet Queue Ticket",
			filters=filters,
			pluck="name",
			order_by=_queue_order_by(),
			limit_start=limit_start,
			limit_page_length=limit_page_length,
			ignore_permissions=True,
		)
		total = frappe.db.count("Pet Queue Ticket", filters)
		return ok({"items": [_ticket_payload(name) for name in names]}, meta={"total": total})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def call_next(queue_date=None, doctor=None, practitioner=None, room=None, branch=None):
	try:
		doctor = practitioner or doctor
		require_doctype_permission("Pet Queue Ticket", "write")
		if doctor:
			require_restriction_value("practitioner", doctor)
		if branch:
			require_restriction_value("branch", branch)

		filters = {
			"queue_date": getdate(queue_date) if queue_date else getdate(),
			"status": ["in", ["Checked In", "Waiting"]],
		}
		if doctor:
			filters["doctor"] = ["in", [doctor, ""]]
		if branch:
			filters["branch"] = branch

		name = frappe.db.get_value("Pet Queue Ticket", filters, "name", order_by=_queue_order_by())
		if not name:
			return fail(_("No waiting queue ticket was found."), code="NOT_FOUND")

		ticket = frappe.get_doc("Pet Queue Ticket", name)
		ticket.status = "Called"
		ticket.called_at = ticket.called_at or now_datetime()
		if doctor and not ticket.doctor:
			ticket.doctor = doctor
		if room:
			ticket.room = room
		if branch and not ticket.branch:
			ticket.branch = branch
		ticket.save(ignore_permissions=True)
		return ok(_ticket_payload(ticket))
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def mark_no_show(ticket=None, ticket_no=None, name=None):
	try:
		require_doctype_permission("Pet Queue Ticket", "write")
		ticket_name = _resolve_ticket(ticket or name, ticket_no)
		if not ticket_name:
			return fail(_("Queue ticket is required."), code="VALIDATION_ERROR")
		if not frappe.db.exists("Pet Queue Ticket", ticket_name):
			return fail(_("Queue ticket was not found."), code="NOT_FOUND")

		doc = frappe.get_doc("Pet Queue Ticket", ticket_name)
		doc.status = "No Show"
		doc.no_show_at = doc.no_show_at or now_datetime()
		doc.save(ignore_permissions=True)
		_update_appointment_status(doc.appointment, "No Show")
		return ok(_ticket_payload(doc))
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def complete_queue_ticket(ticket=None, ticket_no=None, name=None):
	try:
		require_doctype_permission("Pet Queue Ticket", "write")
		ticket_name = _resolve_ticket(ticket or name, ticket_no)
		if not ticket_name:
			return fail(_("Queue ticket is required."), code="VALIDATION_ERROR")
		if not frappe.db.exists("Pet Queue Ticket", ticket_name):
			return fail(_("Queue ticket was not found."), code="NOT_FOUND")

		doc = frappe.get_doc("Pet Queue Ticket", ticket_name)
		doc.status = "Completed"
		doc.completed_at = doc.completed_at or now_datetime()
		doc.save(ignore_permissions=True)
		_update_appointment_status(doc.appointment, "Closed")
		return ok(_ticket_payload(doc))
	except Exception as exc:
		return _error_response(exc)


def _queue_order_by() -> str:
	return "priority asc, checked_in_at asc, creation asc"


def _resolve_ticket(name: str | None = None, ticket_no: str | None = None) -> str | None:
	name = cstr(name).strip()
	if name:
		return name
	ticket_no = cstr(ticket_no).strip()
	if ticket_no:
		return frappe.db.get_value("Pet Queue Ticket", {"ticket_no": ticket_no}, "name")
	return None


def _update_appointment_status(appointment: str | None, preferred_status: str):
	if not appointment or not frappe.db.exists("Appointment", appointment):
		return
	meta = frappe.get_meta("Appointment")
	if not meta.has_field("status"):
		return
	field = meta.get_field("status")
	options = {cstr(value).strip() for value in cstr(field.options).splitlines() if cstr(value).strip()}
	status = preferred_status
	if status not in options:
		if preferred_status == "No Show" and "Cancelled" in options:
			status = "Cancelled"
		elif "Closed" in options:
			status = "Closed"
		elif "Cancelled" in options:
			status = "Cancelled"
		else:
			return
	frappe.db.set_value("Appointment", appointment, "status", status, update_modified=True)


def _error_response(exc: Exception) -> dict:
	code = "PERMISSION_DENIED" if isinstance(exc, frappe.PermissionError) else "ERROR"
	return fail(cstr(exc), code=code, details=frappe.get_traceback())
