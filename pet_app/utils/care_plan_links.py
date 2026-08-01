from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr, get_datetime, getdate, nowdate


TERMINAL_PLAN_ITEM_STATUSES = {"Done", "Cancelled", "Converted To Visit"}

REQUIRED_PLAN_LINKS = {
	"Medication": {
		"doctype": "Vet Visit Medication Item",
		"missing_message": "Add a prescribed medication to this visit before adding a Medication plan item.",
	},
	"Injection": {
		"doctype": "Vet Visit Medication Item",
		"missing_message": "Add a prescribed medication to this visit before adding an Injection plan item.",
	},
	"Lab Recheck": {
		"doctype": "Lab",
		"missing_message": "Add a lab order to this visit before adding a Lab Recheck plan item.",
	},
	"Imaging Recheck": {
		"doctype": "Imaging",
		"missing_message": "Add an imaging order to this visit before adding an Imaging Recheck plan item.",
	},
	"Procedure": {
		"doctype": "Pet Procedure",
		"missing_message": "Add a procedure to this visit before adding a Procedure plan item.",
	},
}

ALLOWED_LINK_DOCTYPES = {target["doctype"] for target in REQUIRED_PLAN_LINKS.values()}


APPOINTMENT_CANCELLED_STATUS = "Cancelled"
APPOINTMENT_CLOSED_STATUS = "Closed"
APPOINTMENT_TERMINAL_STATUSES = {APPOINTMENT_CANCELLED_STATUS, APPOINTMENT_CLOSED_STATUS}


def appointment_status_options() -> set[str]:
	if not frappe.db.exists("DocType", "Appointment"):
		return set()
	field = frappe.get_meta("Appointment").get_field("status")
	if not field:
		return set()
	return {cstr(option).strip() for option in cstr(field.options).splitlines() if cstr(option).strip()}


def appointment_cancel_status() -> str:
	return APPOINTMENT_CANCELLED_STATUS if APPOINTMENT_CANCELLED_STATUS in appointment_status_options() else APPOINTMENT_CLOSED_STATUS


def appointment_has_field(fieldname: str) -> bool:
	return bool(frappe.db.exists("DocType", "Appointment") and frappe.get_meta("Appointment").has_field(fieldname))


def plan_appointment(plan):
	if not frappe.db.exists("DocType", "Appointment"):
		return None
	appointment_name = cstr(plan.get("appointment")).strip()
	if appointment_name and frappe.db.exists("Appointment", appointment_name):
		return frappe.get_doc("Appointment", appointment_name)
	if appointment_has_field("custom_care_plan_item"):
		name = frappe.db.get_value(
			"Appointment",
			{"custom_care_plan_item": plan.name},
			"name",
			order_by="creation desc",
		)
		if name:
			return frappe.get_doc("Appointment", name)
	return None


def scheduled_time_for_plan(plan):
	due_date = plan.get("due_date") or nowdate()
	due_time = cstr(plan.get("due_time") or "09:00:00").strip() or "09:00:00"
	return get_datetime(f"{getdate(due_date)} {due_time}")


def refresh_plan_appointment_status_field(plan, appointment=None) -> None:
	appointment = appointment or plan_appointment(plan)
	if appointment and plan.meta.has_field("appointment") and not plan.get("appointment"):
		plan.appointment = appointment.name
	if plan.meta.has_field("appointment_status"):
		plan.appointment_status = appointment.get("status") if appointment else None


def sync_linked_plan_appointment_schedule(plan):
	appointment = plan_appointment(plan)
	if not appointment:
		return None

	changed = False
	if not appointment.get("custom_linked_visit_id"):
		scheduled_time = scheduled_time_for_plan(plan)
		if cstr(appointment.get("scheduled_time")) != cstr(scheduled_time):
			appointment.scheduled_time = scheduled_time
			changed = True
	if appointment_has_field("custom_care_plan_item") and not appointment.get("custom_care_plan_item"):
		appointment.custom_care_plan_item = plan.name
		changed = True
	if changed:
		appointment.save(ignore_permissions=True)
	return appointment


def _append_appointment_note(appointment, note: str | None) -> bool:
	note = cstr(note).strip()
	if not note:
		return False
	current = cstr(appointment.get("customer_details")).strip()
	if note in current:
		return False
	appointment.customer_details = "\n".join(part for part in [current, note] if part)
	return True


def cancel_linked_plan_appointment(plan, *, reason: str | None = None, clear_plan_link: bool = False):
	appointment = plan_appointment(plan)
	if not appointment:
		return None

	changed = False
	cancel_status = appointment_cancel_status()
	if appointment.get("status") != cancel_status:
		appointment.status = cancel_status
		changed = True
	if appointment_has_field("custom_care_plan_item"):
		if clear_plan_link and appointment.get("custom_care_plan_item"):
			appointment.custom_care_plan_item = None
			changed = True
		elif not clear_plan_link and not appointment.get("custom_care_plan_item"):
			appointment.custom_care_plan_item = plan.name
			changed = True
	if _append_appointment_note(appointment, reason):
		changed = True
	if changed:
		appointment.save(ignore_permissions=True)
	return appointment


def close_linked_plan_appointment(plan, *, reason: str | None = None):
	appointment = plan_appointment(plan)
	if not appointment:
		return None

	changed = False
	if appointment.get("status") not in APPOINTMENT_TERMINAL_STATUSES:
		appointment.status = APPOINTMENT_CLOSED_STATUS
		changed = True
	if appointment_has_field("custom_care_plan_item") and not appointment.get("custom_care_plan_item"):
		appointment.custom_care_plan_item = plan.name
		changed = True
	if _append_appointment_note(appointment, reason):
		changed = True
	if changed:
		appointment.save(ignore_permissions=True)
	return appointment


def enrich_plan_appointment_payload(data: dict) -> dict:
	if not data or not frappe.db.exists("DocType", "Appointment"):
		return data

	appointment_name = cstr(data.get("appointment")).strip()
	row = None
	if appointment_name and frappe.db.exists("Appointment", appointment_name):
		row = frappe.db.get_value("Appointment", appointment_name, ["name", "status", "scheduled_time"], as_dict=True)
	elif data.get("name") and appointment_has_field("custom_care_plan_item"):
		row = frappe.db.get_value(
			"Appointment",
			{"custom_care_plan_item": data.get("name")},
			["name", "status", "scheduled_time"],
			as_dict=True,
			order_by="creation desc",
		)

	if row:
		data["appointment"] = row.name
		data["appointment_status"] = row.status
		data["appointment_scheduled_time"] = cstr(row.scheduled_time) if row.scheduled_time else None
	elif appointment_name:
		data["appointment_status"] = None
		data["appointment_scheduled_time"] = None
	return data


def plan_requires_link(plan_type: str | None) -> bool:
	return cstr(plan_type).strip() in REQUIRED_PLAN_LINKS


def required_link_message(plan_type: str | None) -> str:
	target = REQUIRED_PLAN_LINKS.get(cstr(plan_type).strip())
	return target["missing_message"] if target else "Linked record is required for this plan item."


def validate_plan_item_link_target(plan, *, require_link: bool = False) -> None:
	plan_type = cstr(plan.get("plan_type")).strip()
	linked_doctype = cstr(plan.get("linked_doctype")).strip()
	linked_name = cstr(plan.get("linked_name")).strip()

	if not linked_doctype and not linked_name:
		if require_link:
			frappe.throw(_(required_link_message(plan_type)))
		return

	if not linked_doctype or not linked_name:
		frappe.throw(_("Linked DocType and Linked Name are both required."))

	if linked_doctype not in ALLOWED_LINK_DOCTYPES:
		frappe.throw(
			_("Linked DocType {0} is not allowed for treatment plan items.").format(
				frappe.bold(linked_doctype)
			)
		)

	target = REQUIRED_PLAN_LINKS.get(plan_type)
	if not target:
		frappe.throw(_("{0} plan items do not accept a linked clinical record.").format(frappe.bold(plan_type)))

	expected_doctype = target["doctype"]
	if linked_doctype != expected_doctype:
		frappe.throw(
			_("{0} plan items must link to {1} records.").format(
				frappe.bold(plan_type), frappe.bold(expected_doctype)
			)
		)

	source_visit = cstr(plan.get("source_visit")).strip()
	if not source_visit:
		frappe.throw(_("Source Visit is required before linking a treatment plan item."))

	if linked_doctype == "Vet Visit Medication Item":
		row = frappe.db.get_value(
			"Vet Visit Medication Item",
			linked_name,
			["parent", "parenttype", "parentfield"],
			as_dict=True,
		)
		if not row:
			frappe.throw(_("Linked medication row {0} was not found.").format(frappe.bold(linked_name)))
		if row.parent != source_visit or row.parenttype != "Vet Visit" or row.parentfield != "prescribed_medications":
			frappe.throw(_("Medication plan item must link to a prescribed medication on this visit."))
		return

	linked_visit = frappe.db.get_value(linked_doctype, linked_name, "visit")
	if not linked_visit:
		frappe.throw(
			_("Linked {0} {1} was not found.").format(
				frappe.bold(linked_doctype), frappe.bold(linked_name)
			)
		)
	if linked_visit != source_visit:
		frappe.throw(
			_("{0} plan item must link to a {1} record on this visit.").format(
				frappe.bold(plan_type), frappe.bold(linked_doctype)
			)
		)


def assert_no_active_plan_items_linked_to(linked_doctype: str, linked_name: str, *, action: str = "remove") -> None:
	linked_doctype = cstr(linked_doctype).strip()
	linked_name = cstr(linked_name).strip()
	if not linked_doctype or not linked_name or not frappe.db.exists("DocType", "Pet Care Plan Item"):
		return

	items = frappe.get_all(
		"Pet Care Plan Item",
		filters={
			"linked_doctype": linked_doctype,
			"linked_name": linked_name,
			"status": ["not in", list(TERMINAL_PLAN_ITEM_STATUSES)],
		},
		fields=["name", "plan_type", "title", "status"],
		limit=5,
		ignore_permissions=True,
	)
	if not items:
		return

	first = items[0]
	label = cstr(first.get("title") or first.get("plan_type") or first.get("name")).strip()
	frappe.throw(
		_(
			"Cannot {0} {1} {2} because active treatment plan item {3} links to it. "
			"Cancel or relink the plan item first."
		).format(
			action,
			frappe.bold(linked_doctype),
			frappe.bold(linked_name),
			frappe.bold(label),
		)
	)
