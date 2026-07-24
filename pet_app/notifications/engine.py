from __future__ import annotations

import json
from datetime import datetime, timedelta

import frappe
from frappe import _
from frappe.utils import cint, cstr, get_datetime, now_datetime

from pet_app.notifications.channels.dummy import DummyChannel
from pet_app.notifications.channels.whatsapp_meta import WhatsAppMetaChannel
from pet_app.notifications.consent import assert_consent_allowed
from pet_app.notifications.context import (
	build_document_context,
	coerce_context,
	mask_phone,
	mask_sensitive_context,
	normalize_phone,
	recipient_email,
	recipient_phone,
)
from pet_app.notifications.rate_limit import assert_rate_limit_allowed
from pet_app.notifications.renderer import render_preview
from pet_app.utils.api_response import api_error, api_success


TERMINAL_STATUSES = {"Sent", "Delivered", "Read", "Cancelled", "Skipped"}


def queue_notification(
	event_key,
	recipient_type,
	recipient_name=None,
	to_phone=None,
	context=None,
	source_doctype=None,
	source_name=None,
	template_key=None,
	channel="WhatsApp",
	send_after=None,
	priority="default",
	idempotency_key=None,
	manual=False,
	to_email=None,
	conversation=None,
	action_request=None,
	message_type="Text",
	interactive=None,
	media_file=None,
):
	try:
		if getattr(frappe.flags, "pet_app_whatsapp_simulation", False):
			return api_error(
				_("Notifications cannot be queued during WhatsApp rule simulation."),
				code="SIMULATION_SIDE_EFFECT_BLOCKED",
			)
		_ensure_schema()
		settings = get_settings()
		context = coerce_context(context)
		if source_doctype and source_name:
			context = build_document_context(source_doctype, source_name, context)
		template = resolve_template(event_key=event_key, template_key=template_key, channel=channel)
		account = resolve_whatsapp_account(template=template, settings=settings) if channel == "WhatsApp" else None
		phone = normalize_phone(
			recipient_phone(recipient_type, recipient_name, to_phone),
			settings.get("default_country_code"),
		)
		email = recipient_email(recipient_type, recipient_name, to_email)
		if channel == "WhatsApp" and not phone:
			return api_error(_("Recipient phone is required."), code="VALIDATION_ERROR")
		if channel == "Email" and not email:
			return api_error(_("Recipient email is required."), code="VALIDATION_ERROR")
		if channel == "WhatsApp" and not conversation:
			from pet_app.notifications.inbox import get_or_create_conversation

			conversation = get_or_create_conversation(phone, account).name

		# A deliberate manual click must always dispatch. Automated triggers (and
		# any caller that passes an explicit idempotency_key, e.g. offline replay)
		# keep dedup; a bare manual send gets a unique key each time so it never
		# collides with a prior send and gets silently swallowed.
		if idempotency_key:
			existing = frappe.db.get_value("Pet App Notification Queue", {"idempotency_key": idempotency_key}, "name")
			if existing:
				return api_success({"queue": queue_payload(frappe.get_doc("Pet App Notification Queue", existing))}, meta={"duplicate": True})
		elif cint(manual):
			idempotency_key = _manual_idempotency_key(event_key, recipient_type, recipient_name, phone)
		else:
			idempotency_key = _idempotency_key(event_key, recipient_type, recipient_name, source_doctype, source_name, template.template_key if template else template_key, phone)
			existing = frappe.db.get_value("Pet App Notification Queue", {"idempotency_key": idempotency_key}, "name")
			if existing:
				return api_success({"queue": queue_payload(frappe.get_doc("Pet App Notification Queue", existing))}, meta={"duplicate": True})

		category = template.category if template else "Utility"
		if category == "Marketing" and not cint(settings.get("allow_marketing_messages")):
			return api_error(_("Marketing messages are disabled."), code="MARKETING_DISABLED")
		assert_consent_allowed(
			channel=channel,
			category=category,
			recipient_type=recipient_type,
			recipient_name=recipient_name,
			phone=phone,
			require_opt_in=bool(cint(settings.get("require_opt_in")) or cint(template.get("requires_opt_in") if template else 0)),
		)
		assert_rate_limit_allowed(channel=channel, recipient_type=recipient_type, recipient_name=recipient_name)

		scheduled_at = get_datetime(send_after) if send_after else now_datetime()
		if _quiet_hours_delay_required(settings, template, priority):
			scheduled_at = _next_quiet_hours_end(settings)

		masked_context = mask_sensitive_context(context) if cint(settings.get("mask_sensitive_values")) else context
		rendered_preview = render_preview(template, context, mask_sensitive=bool(cint(settings.get("mask_sensitive_values")))) if template else ""
		status = "Queued" if cint(settings.get("enabled")) else "Skipped"
		doc = frappe.get_doc(
			{
				"doctype": "Pet App Notification Queue",
				"event_key": event_key,
				"channel": channel,
				"status": status,
				"priority": priority,
				"recipient_type": recipient_type,
				"recipient_name": recipient_name,
				"to_phone": phone,
				"to_email": email,
				"template_key": template.template_key if template else template_key,
				"template_name": template.template_name if template else None,
				"language": template.language if template else settings.get("default_language"),
				"context_json": json.dumps(masked_context, default=str),
				"rendered_preview": rendered_preview,
				"source_doctype": source_doctype,
				"source_name": source_name,
				"source_title": _source_title(source_doctype, source_name),
				"scheduled_at": scheduled_at,
				"queued_at": now_datetime(),
				"provider": account.provider if account else None,
				"provider_account": account.name if account else None,
				"conversation": conversation,
				"action_request": action_request,
				"message_type": message_type,
				"interactive_json": json.dumps(interactive, default=str) if interactive else None,
				"media_file": media_file or (template.get("media_file") if template else None),
				"delivery_mode": template.get("delivery_mode") if template else None,
				"idempotency_key": idempotency_key,
				"dedupe_key": idempotency_key,
				"manual": cint(manual),
			}
		)
		doc.insert(ignore_permissions=True)
		return api_success({"queue": queue_payload(doc)}, meta={"scheduled": str(scheduled_at)})
	except Exception as exc:
		return _error_response(exc)


def send_whatsapp_template(
	to_phone=None,
	template_key=None,
	recipient_type="Manual",
	recipient_name=None,
	context=None,
	event_key="manual.whatsapp",
	source_doctype=None,
	source_name=None,
	idempotency_key=None,
):
	return queue_notification(
		event_key=event_key,
		recipient_type=recipient_type,
		recipient_name=recipient_name,
		to_phone=to_phone,
		context=context,
		source_doctype=source_doctype,
		source_name=source_name,
		template_key=template_key,
		channel="WhatsApp",
		idempotency_key=idempotency_key,
		manual=True,
	)


def send_whatsapp_otp(
	guardian=None,
	phone=None,
	otp=None,
	event_key="auth.otp",
	context=None,
	idempotency_key=None,
	process_now=True,
):
	settings = get_settings()
	context = {**(coerce_context(context)), "otp": otp}
	result = queue_notification(
		event_key=event_key,
		recipient_type="Guardian" if guardian else "Manual",
		recipient_name=guardian,
		to_phone=phone,
		context=context,
		template_key=settings.get("otp_template") or "auth_otp",
		channel="WhatsApp",
		priority="urgent",
		idempotency_key=idempotency_key,
	)
	if result.get("ok") and process_now:
		_overrides = getattr(frappe.flags, "pet_app_notification_context", None) or {}
		_overrides[result["data"]["queue"]["name"]] = context
		frappe.flags.pet_app_notification_context = _overrides
		process_notification_queue(result["data"]["queue"]["name"])
		queue = frappe.get_doc("Pet App Notification Queue", result["data"]["queue"]["name"])
		result["data"]["queue"] = queue_payload(queue)
	return result


def process_notification_queue(queue_name):
	try:
		_ensure_schema()
		doc = frappe.get_doc("Pet App Notification Queue", queue_name)
		if doc.status in TERMINAL_STATUSES:
			return api_success({"queue": queue_payload(doc)}, meta={"already_processed": True})
		if doc.scheduled_at and get_datetime(doc.scheduled_at) > now_datetime():
			return api_success({"queue": queue_payload(doc)}, meta={"scheduled": True})
		before_status = doc.status
		doc.status = "Processing"
		doc.processing_at = now_datetime()
		doc.save(ignore_permissions=True)

		template = resolve_template(event_key=doc.event_key, template_key=doc.template_key, channel=doc.channel)
		context = _queue_context(doc)
		channel = _channel_for(doc, template)
		response, sent_type, action_delivery_stage = _send_queue_message(doc, template, context, channel)
		if response is None:
			return api_success({"queue": queue_payload(doc)}, meta={"waiting_for_session": True})

		doc.provider_message_id = response.get("provider_message_id")
		doc.provider_response_json = json.dumps(response, default=str)
		doc.status = "Sent"
		doc.sent_at = now_datetime()
		doc.failed_at = None
		doc.provider_error_code = None
		doc.provider_error_message = None
		doc.save(ignore_permissions=True)
		create_log(doc, status="Sent")
		from pet_app.notifications.inbox import record_outbound_message

		message = record_outbound_message(doc, response, message_type=sent_type)
		if doc.get("action_request"):
			frappe.db.set_value(
				"Pet App WhatsApp Action Request",
				doc.action_request,
				{
					"notification_queue": doc.name,
					"delivery_stage": action_delivery_stage,
					"outbound_message": message.name if message else None,
					"status": "Waiting Reply",
					"error_message": None,
				},
			)
		return api_success({"queue": queue_payload(doc)}, meta={"previous_status": before_status})
	except Exception as exc:
		return _mark_failed(queue_name, exc)


def retry_failed_notifications(limit=100):
	_ensure_schema()
	settings = get_settings()
	max_retries = cint(settings.get("max_retries") or 3)
	rows = frappe.get_all(
		"Pet App Notification Queue",
		filters={"status": ["in", ["Failed", "Retry Scheduled"]], "retry_count": ["<", max_retries]},
		fields=["name", "next_retry_at"],
		order_by="next_retry_at asc, modified asc",
		limit_page_length=limit,
		ignore_permissions=True,
	)
	processed = []
	for row in rows:
		if row.next_retry_at and get_datetime(row.next_retry_at) > now_datetime():
			continue
		processed.append(process_notification_queue(row.name))
	return api_success({"processed": processed}, meta={"total": len(processed)})


def create_log(queue_doc, status=None, message=None, details=None):
	log = frappe.get_doc(
		{
			"doctype": "Pet App Notification Log",
			"queue": queue_doc.name,
			"event_key": queue_doc.event_key,
			"channel": queue_doc.channel,
			"status": status or queue_doc.status,
			"recipient_type": queue_doc.recipient_type,
			"recipient_name": queue_doc.recipient_name,
			"to_phone_masked": mask_phone(queue_doc.to_phone),
			"template_key": queue_doc.template_key,
			"source_doctype": queue_doc.source_doctype,
			"source_name": queue_doc.source_name,
			"provider_message_id": queue_doc.provider_message_id,
			"event_datetime": now_datetime(),
			"message": message if message is not None else queue_doc.rendered_preview,
			"details_json": json.dumps(details or {}, default=str),
		}
	)
	log.insert(ignore_permissions=True)
	return log


def get_settings() -> dict:
	if not frappe.db.exists("DocType", "Pet App Notification Settings"):
		return {"enabled": 0, "dry_run": 1, "default_country_code": "964", "default_language": "en"}
	doc = frappe.get_single("Pet App Notification Settings")
	return doc.as_dict()


def resolve_template(event_key=None, template_key=None, channel="WhatsApp"):
	if not frappe.db.exists("DocType", "Pet App WhatsApp Template"):
		return None
	name = None
	if template_key:
		name = frappe.db.get_value("Pet App WhatsApp Template", {"template_key": template_key, "enabled": 1}, "name")
	if not name and event_key:
		name = frappe.db.get_value("Pet App WhatsApp Template", {"event_key": event_key, "enabled": 1}, "name")
	if not name:
		frappe.throw(_("Notification template was not found."))
	return frappe.get_doc("Pet App WhatsApp Template", name)


def resolve_whatsapp_account(template=None, settings=None):
	settings = settings or get_settings()
	account_name = template.get("provider_account") if template else None
	account_name = account_name or settings.get("default_whatsapp_account")
	if not account_name:
		account_name = frappe.db.get_value("Pet App WhatsApp Account", {"is_default": 1, "enabled": 1}, "name")
	if not account_name:
		frappe.throw(_("WhatsApp account is not configured."))
	return frappe.get_doc("Pet App WhatsApp Account", account_name)


def queue_payload(doc) -> dict:
	return {
		"name": doc.name,
		"event_key": doc.event_key,
		"channel": doc.channel,
		"status": doc.status,
		"recipient_type": doc.recipient_type,
		"recipient_name": doc.recipient_name,
		"to_phone": doc.to_phone,
		"to_email": doc.to_email,
		"template_key": doc.template_key,
		"template_name": doc.template_name,
		"rendered_preview": doc.rendered_preview,
		"scheduled_at": doc.scheduled_at,
		"sent_at": doc.sent_at,
		"delivered_at": doc.delivered_at,
		"read_at": doc.read_at,
		"provider": doc.provider,
		"provider_account": doc.provider_account,
		"provider_message_id": doc.provider_message_id,
		"retry_count": doc.retry_count,
		"idempotency_key": doc.idempotency_key,
		"conversation": doc.get("conversation"),
		"action_request": doc.get("action_request"),
		"message_type": doc.get("message_type"),
		"delivery_mode": doc.get("delivery_mode"),
	}


def _mark_failed(queue_name, exc):
	try:
		doc = frappe.get_doc("Pet App Notification Queue", queue_name)
		settings = get_settings()
		doc.status = "Failed"
		doc.failed_at = now_datetime()
		doc.provider_error_code = getattr(exc, "exc_type", None) or exc.__class__.__name__
		doc.provider_error_message = cstr(exc)
		doc.retry_count = cint(doc.retry_count) + 1
		doc.next_retry_at = now_datetime() + timedelta(minutes=cint(settings.get("retry_after_minutes") or 5))
		doc.save(ignore_permissions=True)
		from pet_app.notifications.inbox import record_failed_outbound_message

		record_failed_outbound_message(doc, doc.provider_error_message)
		if doc.get("action_request"):
			from pet_app.notifications.actions import mark_action_failed

			mark_action_failed(
				doc.action_request,
				doc.provider_error_message,
				details={"notification_queue": doc.name, "error_code": doc.provider_error_code},
			)
		create_log(doc, status="Failed", message=doc.provider_error_message)
		return api_error(cstr(exc), code=doc.provider_error_code, details={"queue": queue_payload(doc)})
	except Exception:
		return _error_response(exc)


def _channel_for(queue_doc, template=None):
	if queue_doc.channel != "WhatsApp":
		return DummyChannel(settings=get_settings())
	account = resolve_whatsapp_account(template=template, settings=get_settings())
	if account.provider == "Meta Cloud API":
		return WhatsAppMetaChannel(account=account, settings=get_settings())
	return DummyChannel(account=account, settings=get_settings())


def _send_queue_message(doc, template, context, channel):
	from frappe.utils.file_manager import get_file
	from pet_app.notifications.inbox import session_is_open

	delivery_mode = cstr(doc.get("delivery_mode") or (template.get("delivery_mode") if template else None) or "Meta Template")
	conversation_open = bool(doc.get("conversation") and session_is_open(doc.conversation))
	if template and delivery_mode == "App Styled" and not conversation_open:
		doc.status = "Waiting For Session"
		doc.save(ignore_permissions=True)
		if doc.get("action_request"):
			frappe.db.set_value("Pet App WhatsApp Action Request", doc.action_request, "delivery_stage", "Awaiting Session")
		return None, "Text", "Awaiting Session"
	if template and delivery_mode == "Hybrid" and not conversation_open:
		fallback_key = template.get("fallback_template_key")
		if not fallback_key:
			doc.status = "Waiting For Session"
			doc.save(ignore_permissions=True)
			if doc.get("action_request"):
				frappe.db.set_value("Pet App WhatsApp Action Request", doc.action_request, "delivery_stage", "Awaiting Session")
			return None, "Text", "Awaiting Session"
		fallback = resolve_template(template_key=fallback_key)
		fallback_channel = _channel_for(doc, fallback)
		return (
			fallback_channel.send_template(to_phone=doc.to_phone, template=fallback, context=context, queue=doc),
			"Text",
			"Awaiting Session",
		)
	if template and delivery_mode == "Meta Template":
		return channel.send_template(to_phone=doc.to_phone, template=template, context=context, queue=doc), "Text", "Interactive Sent"

	message = doc.rendered_preview or doc.event_key
	interactive = json.loads(doc.interactive_json) if doc.get("interactive_json") else None
	if interactive:
		return (
			channel.send_interactive(to_phone=doc.to_phone, message=message, interactive=interactive, queue=doc),
			"Interactive",
			"Interactive Sent",
		)
	if doc.get("media_file"):
		file_doc = frappe.get_doc("File", doc.media_file)
		file_name, content = get_file(file_doc.file_url)
		media_type = cstr((template.get("media_type") if template else None) or doc.get("message_type") or "Document").lower()
		return channel.send_media(
			to_phone=doc.to_phone,
			media_type=media_type,
			file_name=file_name,
			content=content,
			caption=message,
			queue=doc,
		), media_type.title(), "Interactive Sent"
	return channel.send_text(to_phone=doc.to_phone, message=message, queue=doc), "Text", "Interactive Sent"


def _queue_context(doc) -> dict:
	overrides = getattr(frappe.flags, "pet_app_notification_context", None) or {}
	if doc.name in overrides:
		return overrides[doc.name]
	if not doc.context_json:
		return {}
	return json.loads(doc.context_json)


def _source_title(source_doctype, source_name):
	if source_doctype and source_name and frappe.db.exists(source_doctype, source_name):
		return f"{source_doctype} {source_name}"
	return None


def _quiet_hours_delay_required(settings, template, priority) -> bool:
	if cstr(priority).lower() in {"urgent", "high"}:
		return False
	if template and cint(template.get("allow_during_quiet_hours")):
		return False
	if not cint(settings.get("respect_quiet_hours")):
		return False
	start = _time_value(settings.get("quiet_hours_start"))
	end = _time_value(settings.get("quiet_hours_end"))
	if not start or not end:
		return False
	now_time = now_datetime().time()
	if start <= end:
		return start <= now_time <= end
	return now_time >= start or now_time <= end


def _next_quiet_hours_end(settings):
	end = _time_value(settings.get("quiet_hours_end"))
	now = now_datetime()
	target = datetime.combine(now.date(), end)
	if target <= now:
		target += timedelta(days=1)
	return target


def _time_value(value):
	if not value:
		return None
	if hasattr(value, "hour"):
		return value
	return datetime.strptime(cstr(value), "%H:%M:%S" if len(cstr(value).split(":")) == 3 else "%H:%M").time()


def _idempotency_key(*parts) -> str:
	import hashlib

	return hashlib.sha1("|".join(cstr(part) for part in parts).encode()).hexdigest()


def _manual_idempotency_key(*parts) -> str:
	"""Unique-per-click key for manual sends so they never dedup-collide.

	The queue's idempotency_key column is unique; a manual click is a deliberate
	action and must always dispatch, so we append a fresh hash to the context.
	"""
	return f"manual:{_idempotency_key(*parts)}:{frappe.generate_hash(length=12)}"


def _ensure_schema():
	if not frappe.db.exists("DocType", "Pet App Notification Queue"):
		frappe.throw(_("Notification engine schema is missing. Run bench migrate."))


def _error_response(exc):
	if isinstance(exc, frappe.PermissionError):
		return api_error(_("Not permitted"), code="PERMISSION_ERROR")
	return api_error(cstr(exc), code=getattr(exc, "exc_type", None) or exc.__class__.__name__)
