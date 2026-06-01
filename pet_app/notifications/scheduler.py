from __future__ import annotations

import json

import frappe
from frappe.utils import get_datetime, now_datetime

from pet_app.notifications.engine import queue_notification
from pet_app.utils.api_response import api_success


def enqueue_due_reminders(limit=None):
	if not frappe.db.exists("DocType", "Pet App Reminder"):
		return api_success({"queued": []}, meta={"total": 0})
	settings = frappe.get_single("Pet App Notification Settings") if frappe.db.exists("DocType", "Pet App Notification Settings") else None
	batch_size = int(limit or (settings.reminder_batch_size if settings else 100) or 100)
	rows = frappe.get_all(
		"Pet App Reminder",
		filters={"status": "Scheduled", "send_at": ["<=", now_datetime()]},
		fields=["name"],
		order_by="send_at asc, creation asc",
		limit_page_length=batch_size,
		ignore_permissions=True,
	)
	queued = []
	for row in rows:
		reminder = frappe.get_doc("Pet App Reminder", row.name)
		if reminder.notification_queue and frappe.db.exists("Pet App Notification Queue", reminder.notification_queue):
			reminder.status = "Queued"
			reminder.save(ignore_permissions=True)
			queued.append(reminder.notification_queue)
			continue
		context = json.loads(reminder.context_json) if reminder.context_json else {}
		result = queue_notification(
			event_key=f"reminder.{reminder.reminder_type}",
			recipient_type="Guardian" if reminder.guardian else "Customer" if reminder.customer else "Manual",
			recipient_name=reminder.guardian or reminder.customer,
			context=context,
			source_doctype=reminder.source_doctype,
			source_name=reminder.source_name,
			template_key=reminder.template_key,
			channel=reminder.channel,
			send_after=reminder.send_at,
			idempotency_key=reminder.dedupe_key or reminder.name,
		)
		if result.get("ok"):
			reminder.notification_queue = result["data"]["queue"]["name"]
			reminder.status = "Queued"
			reminder.save(ignore_permissions=True)
			queued.append(reminder.notification_queue)
	return api_success({"queued": queued}, meta={"total": len(queued)})


def create_daily_reminders():
	return enqueue_due_reminders()


def cleanup_old_webhook_events(days=30):
	if not frappe.db.exists("DocType", "Pet App WhatsApp Webhook Event"):
		return
	from frappe.utils import add_days

	for name in frappe.get_all(
		"Pet App WhatsApp Webhook Event",
		filters={"creation": ["<", add_days(now_datetime(), -int(days or 30))]},
		pluck="name",
		ignore_permissions=True,
	):
		frappe.delete_doc("Pet App WhatsApp Webhook Event", name, force=True, ignore_permissions=True)

