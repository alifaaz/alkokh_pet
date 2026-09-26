"""Turn due clinical dates into Pet Reminder rows, once per date.

Two numbers govern the preventive sweep, and they answer different questions.

`PREVENTIVE_LEAD_DAYS` is how far AHEAD a row is created. It is not when the guardian
hears about it: `send_due_reminders` only sends once `Pet Reminder.due_date` has arrived,
so a longer lead creates the row earlier, it does not message earlier. What it buys is
resilience - the row already exists before the due date, so a scheduler that is down ON
the due date still sends as soon as it comes back.

`PREVENTIVE_OVERDUE_DAYS` is how far BACK the sweep looks. Without it a record whose date
passed while the scheduler was stopped is never picked up at all: the window moves past it
and nothing ever creates its reminder. That is exactly what the 8-day outage did. Bounded
rather than unlimited on purpose - past a quarter overdue this is not a reminder any more,
it is a recall campaign, and it should be someone's deliberate decision rather than a
side effect of restarting the scheduler.
"""

from __future__ import annotations

import hashlib

import frappe
from frappe.utils import add_days, getdate, now_datetime, nowdate

# 30 days ahead: enough notice for a guardian to book an annual vaccination, and it spans
# a monthly clinic cadence so a record is seen at least once before it falls due.
PREVENTIVE_LEAD_DAYS = 30

# 90 days back: covers any realistic scheduler outage (the last one was 8 days) plus a
# quarter of drift, without sweeping years of history into the queue on first run.
PREVENTIVE_OVERDUE_DAYS = 90


def enqueue_due_reminders():
	if not frappe.db.exists("DocType", "Pet Reminder"):
		return
	_enqueue_preventive()
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


# One doctype now holds both, so this is one query where it used to be two. Which reminder
# a row raises comes from the row's own `kind` rather than from which table it was read out
# of - the same fact, stated once by the record instead of twice by the caller.
PREVENTIVE_DOCTYPE = "Preventive Care Record"
PREVENTIVE_REMINDER_TYPES = {"Vaccination": "Vaccination Due", "Deworming": "Deworming Due"}


def _enqueue_preventive():
	if not frappe.db.exists("DocType", PREVENTIVE_DOCTYPE):
		return
	rows = frappe.get_all(
		PREVENTIVE_DOCTYPE,
		filters={
			"next_due_date": [
				"between",
				[add_days(nowdate(), -PREVENTIVE_OVERDUE_DAYS), add_days(nowdate(), PREVENTIVE_LEAD_DAYS)],
			],
			"reminder_enabled": 1,
			# A cancelled dose was not given, so it owes no next one. The doctypes this
			# replaces had no status at all and so could not express this; every row they
			# held was implicitly a dose that happened.
			"status": ["!=", "Cancelled"],
		},
		fields=["name", "pet", "guardian", "next_due_date", "kind", "medication_name"],
		ignore_permissions=True,
	)
	for row in rows:
		reminder_type = PREVENTIVE_REMINDER_TYPES.get(row.kind)
		if not reminder_type:
			# A row whose kind is neither - impossible through the controller, which refuses
			# to save one. Logged rather than guessed at: a reminder of the wrong type is
			# worse than one nobody sent.
			frappe.log_error(
				title="PREVENTIVE_REMINDER_UNKNOWN_KIND",
				message=f"{PREVENTIVE_DOCTYPE} {row.name} has kind {row.kind!r}; no reminder raised.",
			)
			continue
		_upsert_reminder(
			reminder_type,
			row.pet,
			row.guardian,
			row.next_due_date,
			PREVENTIVE_DOCTYPE,
			row.name,
			row.medication_name,
		)


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
	due_date = getdate(due_date)
	key = _key(reminder_type, reference_doctype, reference_name, due_date)

	# The date is part of the key so a record can be reminded again for its NEXT due date,
	# and so moving a date produces a new reminder instead of nothing. Before this there was
	# exactly one reminder per source record for all time.
	if frappe.db.exists("Pet Reminder", {"idempotency_key": key}):
		return

	# Transitional: every row written before the key changed was hashed without the date, so
	# on the first run after this ships the new key would miss them and insert a second row
	# for every record still inside a window. Checking the old shape too keeps that from
	# happening. The cost is bounded and temporary - for those rows only, moving the date
	# will not raise a new reminder until the legacy row leaves the window. Safe to delete
	# once no pre-change rows remain in any sweep window.
	if frappe.db.exists("Pet Reminder", {"idempotency_key": _key(reminder_type, reference_doctype, reference_name)}):
		return
	frappe.get_doc(
		{
			"doctype": "Pet Reminder",
			"reminder_type": reminder_type,
			"pet": pet,
			"guardian": guardian,
			"due_date": due_date,
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

