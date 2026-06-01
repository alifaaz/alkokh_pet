from __future__ import annotations

import json

import frappe
from frappe.utils import now_datetime

from pet_app.notifications.consent import opt_out_phone
from pet_app.notifications.engine import create_log
from pet_app.utils.api_response import api_error, api_success


OPT_OUT_WORDS = {"STOP", "ايقاف", "الغاء", "إلغاء"}
STATUS_MAP = {"sent": "Sent", "delivered": "Delivered", "read": "Read", "failed": "Failed"}


def handle_whatsapp_webhook(payload):
	try:
		events = _extract_events(payload or {})
		processed = []
		for event in events:
			if _already_processed(event):
				processed.append({"duplicate": True, **event})
				continue
			doc = _store_event(event, payload)
			_apply_event(event)
			doc.processed = 1
			doc.processed_at = now_datetime()
			doc.save(ignore_permissions=True)
			processed.append(event)
		return api_success({"events": processed}, meta={"total": len(processed)})
	except Exception as exc:
		return api_error(str(exc), code=getattr(exc, "exc_type", None) or exc.__class__.__name__)


def _extract_events(payload):
	events = []
	for entry in payload.get("entry") or []:
		for change in entry.get("changes") or []:
			value = change.get("value") or {}
			metadata = value.get("metadata") or {}
			phone_number_id = metadata.get("phone_number_id")
			for status in value.get("statuses") or []:
				events.append(
					{
						"event_type": "status",
						"provider_message_id": status.get("id"),
						"status": status.get("status"),
						"timestamp": status.get("timestamp"),
						"phone_number_id": phone_number_id,
						"to_phone": status.get("recipient_id"),
					}
				)
			for message in value.get("messages") or []:
				body = ((message.get("text") or {}).get("body") or "").strip()
				events.append(
					{
						"event_type": "message",
						"provider_message_id": message.get("id"),
						"status": "received",
						"timestamp": message.get("timestamp"),
						"phone_number_id": phone_number_id,
						"from_phone": message.get("from"),
						"message": body,
					}
				)
	if not events:
		events.append({"event_type": "raw", "payload": payload})
	return events


def _already_processed(event):
	provider_message_id = event.get("provider_message_id")
	if not provider_message_id:
		return False
	return bool(
		frappe.db.exists(
			"Pet App WhatsApp Webhook Event",
			{
				"provider_message_id": provider_message_id,
				"event_type": event.get("event_type"),
				"status": event.get("status"),
				"timestamp": event.get("timestamp"),
				"processed": 1,
			},
		)
	)


def _store_event(event, payload):
	return frappe.get_doc(
		{
			"doctype": "Pet App WhatsApp Webhook Event",
			"event_type": event.get("event_type"),
			"provider_message_id": event.get("provider_message_id"),
			"phone_number_id": event.get("phone_number_id"),
			"from_phone": event.get("from_phone"),
			"to_phone": event.get("to_phone"),
			"status": event.get("status"),
			"timestamp": event.get("timestamp"),
			"payload_json": json.dumps(payload, default=str),
		}
	).insert(ignore_permissions=True)


def _apply_event(event):
	if event.get("event_type") == "message":
		body = (event.get("message") or "").strip().upper()
		if body in OPT_OUT_WORDS:
			opt_out_phone(event.get("from_phone"), channel="WhatsApp")
		return
	provider_message_id = event.get("provider_message_id")
	if not provider_message_id:
		return
	queue_name = frappe.db.get_value("Pet App Notification Queue", {"provider_message_id": provider_message_id}, "name")
	if not queue_name:
		return
	queue = frappe.get_doc("Pet App Notification Queue", queue_name)
	status = STATUS_MAP.get(event.get("status"), queue.status)
	queue.status = status
	if status == "Delivered":
		queue.delivered_at = now_datetime()
	elif status == "Read":
		queue.read_at = now_datetime()
	elif status == "Failed":
		queue.failed_at = now_datetime()
		queue.provider_error_message = event.get("error") or queue.provider_error_message
	queue.save(ignore_permissions=True)
	create_log(queue, status=status, details=event)
