from __future__ import annotations

import hashlib

import frappe
from frappe.utils import add_days, getdate, now_datetime, nowdate


def enqueue_due_reminders():
	if not frappe.db.exists("DocType", "Pet Reminder"):
		return
	_enqueue_preventive("Pet Vaccination Record", "Vaccination Due", "vaccine_name")
	_enqueue_preventive("Pet Deworming Record", "Deworming Due", "medication_name")
	_enqueue_follow_ups()
	_enqueue_invoice_due()


def send_due_reminders(limit=100):
	if not frappe.db.exists("DocType", "Pet Reminder"):
		return
	from pet_app.api.notifications import send_manual_reminder

	rows = frappe.get_all(
		"Pet Reminder",
		filters={"status": ["in", ["Pending", "Queued"]], "due_date": ["<=", nowdate()]},
		fields=["name"],
		order_by="due_date asc, creation asc",
		limit_page_length=limit,
		ignore_permissions=True,
	)
	for row in rows:
		send_manual_reminder(reminder=row.name)


def _enqueue_preventive(doctype, reminder_type, summary_field):
	if not frappe.db.exists("DocType", doctype):
		return
	rows = frappe.get_all(
		doctype,
		filters={"next_due_date": ["between", [nowdate(), add_days(nowdate(), 7)]], "reminder_enabled": 1},
		fields=["name", "pet", "guardian", "next_due_date", summary_field],
		ignore_permissions=True,
	)
	for row in rows:
		_upsert_reminder(reminder_type, row.pet, row.guardian, row.next_due_date, doctype, row.name, row.get(summary_field))


def _enqueue_follow_ups():
	rows = frappe.get_all(
		"Vet Visit",
		filters={"follow_up_required": 1, "follow_up_date": ["between", [nowdate(), add_days(nowdate(), 3)]], "follow_up_status": ["in", ["Requested", "Scheduled"]]},
		fields=["name", "animal_patient", "guardian", "follow_up_date", "follow_up_reason"],
		ignore_permissions=True,
	)
	for row in rows:
		_upsert_reminder("Follow-up Due", row.animal_patient, row.guardian, row.follow_up_date, "Vet Visit", row.name, row.follow_up_reason)


def _enqueue_invoice_due():
	rows = frappe.get_all(
		"Sales Invoice",
		filters={"docstatus": 1, "outstanding_amount": [">", 0], "due_date": ["<=", add_days(nowdate(), 3)]},
		fields=["name", "customer", "due_date", "outstanding_amount"],
		ignore_permissions=True,
	)
	for row in rows:
		guardian = frappe.db.get_value("Guardian", {"customer_id": row.customer}, "name")
		if guardian:
			_upsert_reminder("Invoice Due", None, guardian, row.due_date, "Sales Invoice", row.name, row.outstanding_amount)


def _upsert_reminder(reminder_type, pet, guardian, due_date, reference_doctype, reference_name, note=None):
	key = _key(reminder_type, reference_doctype, reference_name)
	if frappe.db.exists("Pet Reminder", {"idempotency_key": key}):
		return
	frappe.get_doc(
		{
			"doctype": "Pet Reminder",
			"reminder_type": reminder_type,
			"pet": pet,
			"guardian": guardian,
			"due_date": getdate(due_date),
			"status": "Pending",
			"channel": "In App",
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"idempotency_key": key,
			"note": note,
		}
	).insert(ignore_permissions=True)


def _key(*parts) -> str:
	return hashlib.sha1("|".join(str(part or "") for part in parts).encode()).hexdigest()

