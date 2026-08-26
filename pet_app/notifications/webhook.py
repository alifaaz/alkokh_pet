from __future__ import annotations

import json

import frappe
from frappe.utils import cstr, now_datetime

from pet_app.notifications.consent import opt_out_phone
from pet_app.notifications.engine import create_log
from pet_app.notifications.inbox import outbound_status_advances, record_inbound_message, resolve_account, update_outbound_status
from pet_app.utils.api_response import api_error, api_success


OPT_OUT_WORDS = {"STOP", "ايقاف", "الغاء", "إلغاء"}
STATUS_MAP = {"sent": "Sent", "delivered": "Delivered", "read": "Read", "failed": "Failed"}

# Meta names each webhook subscription in change["field"]. Until now nothing read it,
# so every non-messaging field - template approvals included - fell through to the
# "raw" catch-all and was stored and dropped. This one is handled; anything else still
# reaches the catch-all, but now says so in processing_error instead of vanishing.
TEMPLATE_STATUS_FIELD = "message_template_status_update"


def handle_whatsapp_webhook(payload):
	try:
		events = _extract_events(payload or {})
		processed = []
		for event in events:
			if _already_processed(event):
				processed.append({"duplicate": True, **event})
				continue
			doc = _store_event(event, payload)
			processing_error = _apply_event(event)
			doc.processed = 1
			doc.processed_at = now_datetime()
			# _apply_event returns a note when it understood the event but could not
			# act on it, or did not recognise it at all. Recording that beats the old
			# silent drop: the row is already stored, it just never said why nothing
			# happened. None from every existing branch, so their behaviour is unchanged.
			if processing_error:
				doc.processing_error = cstr(processing_error)[:500]
				event = {**event, "processing_error": doc.processing_error}
			doc.save(ignore_permissions=True)
			processed.append(event)
		return api_success({"events": processed}, meta={"total": len(processed)})
	except Exception as exc:
		frappe.logger("pet_app.whatsapp_webhook").error(
			"WhatsApp webhook processing failed\n%s", frappe.get_traceback()
		)
		return api_error(str(exc), code=getattr(exc, "exc_type", None) or exc.__class__.__name__)


def _extract_events(payload):
	events = []
	for entry in payload.get("entry") or []:
		for change in entry.get("changes") or []:
			value = change.get("value") or {}
			metadata = value.get("metadata") or {}
			phone_number_id = metadata.get("phone_number_id")
			field = cstr(change.get("field"))

			if field == TEMPLATE_STATUS_FIELD:
				events.append(_template_status_event(value, entry, field))
				continue

			for status in value.get("statuses") or []:
				errors = status.get("errors") or []
				error = errors[0] if errors else {}
				events.append(
					{
						"event_type": "status",
						"provider_message_id": status.get("id"),
						"status": status.get("status"),
						"timestamp": status.get("timestamp"),
						"phone_number_id": phone_number_id,
						"to_phone": status.get("recipient_id"),
						"error": error.get("message") or error.get("title"),
						"error_code": error.get("code"),
					}
				)
			for message in value.get("messages") or []:
				events.append(_message_event(message, phone_number_id))
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
	account = resolve_account(event.get("phone_number_id"))
	return frappe.get_doc(
		{
			"doctype": "Pet App WhatsApp Webhook Event",
			"provider_account": account.name if account else None,
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
	"""Act on one extracted event.

	Returns None when the event was handled, or a short note when it was not - the
	caller stores that note in processing_error rather than dropping it.
	"""
	if event.get("event_type") == "template_status":
		return _apply_template_status(event)
	if event.get("event_type") == "raw":
		return _unhandled_note(event)
	if event.get("event_type") == "message":
		message, conversation = record_inbound_message(event)
		body = (event.get("message") or "").strip().upper()
		if body in OPT_OUT_WORDS:
			opt_out_phone(event.get("from_phone"), channel="WhatsApp")
			conversation.status = "Blocked"
			conversation.save(ignore_permissions=True)
			return
		from pet_app.notifications.actions import process_inbound_action

		process_inbound_action(message, conversation)
		return
	provider_message_id = event.get("provider_message_id")
	if not provider_message_id:
		return
	status = STATUS_MAP.get(event.get("status"))
	if not status:
		return
	update_outbound_status(provider_message_id, status)
	queue_name = frappe.db.get_value("Pet App Notification Queue", {"provider_message_id": provider_message_id}, "name")
	if not queue_name:
		return
	queue = frappe.get_doc("Pet App Notification Queue", queue_name)
	if not outbound_status_advances(queue.status, status):
		return
	queue.status = status
	if status == "Delivered":
		queue.delivered_at = now_datetime()
	elif status == "Read":
		queue.read_at = now_datetime()
	elif status == "Failed":
		queue.failed_at = now_datetime()
		# A delivery failure arriving by webhook must never land as a bare "Failed"
		# with nothing to explain it. This row was Sent moments ago, which means
		# process_notification_queue cleared provider_error_code and
		# provider_error_message; if Meta's status payload carries no errors[] entry,
		# both would stay empty and the UI would show a failure with no reason.
		# _extract_events already captures Meta's code - it was simply never stored.
		error_code = event.get("error_code")
		queue.provider_error_code = (
			(f"META_{error_code}" if error_code else None)
			or queue.provider_error_code
			or "META_DELIVERY_FAILED"
		)
		queue.provider_error_message = (
			event.get("error")
			or queue.provider_error_message
			or "Meta reported this message as failed and gave no reason."
		)
	queue.save(ignore_permissions=True)
	create_log(queue, status=status, details=event)
	if status == "Failed" and queue.get("action_request"):
		from pet_app.notifications.actions import mark_action_failed

		mark_action_failed(queue.action_request, queue.provider_error_message, details=event)


def _template_status_event(value, entry, field):
	"""Flatten a message_template_status_update change into an event.

	Meta puts the new status in ``event`` and the template's identity in
	``message_template_id`` / ``_name`` / ``_language``. Every value is carried
	through untouched - this function reads key names, never values.
	"""
	return {
		"event_type": "template_status",
		"field": field,
		"meta_template_id": cstr(value.get("message_template_id")),
		"template_name": cstr(value.get("message_template_name")),
		"language": cstr(value.get("message_template_language")),
		# Verbatim. Whatever Meta calls the new state is what gets stored.
		"status": value.get("event"),
		"rejected_reason": value.get("reason"),
		"waba_id": entry.get("id"),
		"timestamp": entry.get("time"),
		"raw_value": value,
	}


def _apply_template_status(event):
	"""Push a template approval result onto the mirror row.

	No row match is not an error: a template can be approved on Meta before this
	site has ever synced it, and inventing a mirror row from a status payload would
	create one with no components and no raw_json. The event is recorded instead, so
	the gap is visible, and the next sync fills it in properly.
	"""
	from pet_app.notifications.meta_templates import apply_status_update

	matched = apply_status_update(
		meta_template_id=event.get("meta_template_id"),
		template_name=event.get("template_name"),
		language=event.get("language"),
		status=event.get("status"),
		rejected_reason=event.get("rejected_reason"),
	)
	if matched:
		return None
	return (
		f"No Pet App WhatsApp Meta Template row matched template_id="
		f"{cstr(event.get('meta_template_id')) or '?'} name={cstr(event.get('template_name')) or '?'} "
		f"language={cstr(event.get('language')) or '?'}; status {cstr(event.get('status')) or '?'} not applied. "
		f"Run sync_meta_templates to pull the template in."
	)


def _unhandled_note(event):
	"""Describe a webhook change this app does not handle, for the stored row."""
	fields = sorted(
		{
			cstr(change.get("field"))
			for entry in (event.get("payload") or {}).get("entry") or []
			for change in entry.get("changes") or []
			if change.get("field")
		}
	)
	if fields:
		return f"Unhandled webhook field(s): {', '.join(fields)}. Stored only; no handler for this event type."
	return "Webhook payload carried no messages, statuses or recognised field. Stored only."


def _message_event(message, phone_number_id):
	message_type = cstr(message.get("type") or "text").lower()
	body = cstr((message.get("text") or {}).get("body")).strip()
	interactive_id = None
	interactive_title = None
	caption = None
	provider_media_id = None
	filename = None

	if message_type == "interactive":
		interactive = message.get("interactive") or {}
		reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
		interactive_id = reply.get("id")
		interactive_title = reply.get("title")
		body = interactive_title or body
		resolved_type = "Interactive"
	elif message_type in {"image", "document", "video", "audio", "sticker"}:
		media = message.get(message_type) or {}
		provider_media_id = media.get("id")
		caption = media.get("caption")
		filename = media.get("filename")
		body = caption or body
		resolved_type = message_type.title()
	elif message_type == "location":
		location = message.get("location") or {}
		body = json.dumps({key: location.get(key) for key in ("latitude", "longitude", "name", "address")}, default=str)
		resolved_type = "Location"
	elif message_type == "contacts":
		body = json.dumps(message.get("contacts") or [], default=str)
		resolved_type = "Contacts"
	elif message_type == "text":
		resolved_type = "Text"
	else:
		resolved_type = "Unsupported"

	return {
		"event_type": "message",
		"provider_message_id": message.get("id"),
		"status": "received",
		"timestamp": message.get("timestamp"),
		"phone_number_id": phone_number_id,
		"from_phone": message.get("from"),
		"message_type": resolved_type,
		"message": body,
		"caption": caption,
		"provider_media_id": provider_media_id,
		"filename": filename,
		"interactive_id": interactive_id,
		"interactive_title": interactive_title,
		"raw_message": message,
	}
