from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cstr, now_datetime

from pet_app.api.permissions import require_doctype_permission
from pet_app.api.response import fail, ok, standardize_response
from pet_app.api.link_aliases import enrich_link_aliases, with_link_aliases
from pet_app.notifications import engine
from pet_app.notifications.scheduler import enqueue_due_reminders as _enqueue_due_reminders
from pet_app.utils.api_response import api_error, api_success


@frappe.whitelist()
def get_notification_settings():
	try:
		require_doctype_permission("Pet App Notification Settings", "read")
		return api_success({"settings": engine.get_settings()})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def update_notification_settings(data=None, **kwargs):
	try:
		require_doctype_permission("Pet App Notification Settings", "write")
		payload = _payload(data, kwargs)
		doc = frappe.get_single("Pet App Notification Settings")
		for key, value in payload.items():
			if doc.meta.has_field(key):
				doc.set(key, value)
		doc.save(ignore_permissions=True)
		return api_success({"settings": doc.as_dict()})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def test_whatsapp_account(account=None):
	try:
		require_doctype_permission("Pet App WhatsApp Account", "write")
		name = account or frappe.db.get_value("Pet App WhatsApp Account", {"is_default": 1}, "name")
		doc = frappe.get_doc("Pet App WhatsApp Account", name)
		doc.last_health_check_at = now_datetime()
		doc.last_error = None if doc.enabled else _("Account is disabled.")
		doc.save(ignore_permissions=True)
		return api_success({"account": _doc_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def list_templates(event_key=None, enabled=None):
	try:
		require_doctype_permission("Pet App WhatsApp Template", "read")
		filters = {}
		if event_key:
			filters["event_key"] = event_key
		if enabled is not None:
			filters["enabled"] = int(enabled)
		rows = frappe.get_all(
			"Pet App WhatsApp Template",
			filters=filters,
			fields=["name", "template_key", "enabled", "template_name", "language", "category", "event_key", "recipient_type", "priority"],
			order_by="template_key asc",
			ignore_permissions=True,
		)
		return api_success({"templates": [dict(row) for row in rows]}, meta={"total": len(rows)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_template(template_key=None, name=None):
	try:
		require_doctype_permission("Pet App WhatsApp Template", "read")
		doc = _get_template(template_key or name)
		return api_success({"template": _doc_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def save_template(data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		name = payload.get("name") or frappe.db.get_value("Pet App WhatsApp Template", {"template_key": payload.get("template_key")}, "name")
		require_doctype_permission("Pet App WhatsApp Template", "write" if name else "create")
		doc = frappe.get_doc("Pet App WhatsApp Template", name) if name else frappe.new_doc("Pet App WhatsApp Template")
		for key, value in payload.items():
			if doc.meta.has_field(key):
				doc.set(key, value)
		doc.save(ignore_permissions=True)
		return api_success({"template": _doc_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def preview_template(template_key=None, context=None, data=None, **kwargs):
	try:
		require_doctype_permission("Pet App WhatsApp Template", "read")
		payload = _payload(data, kwargs)
		doc = _get_template(template_key or payload.get("template_key"))
		from pet_app.notifications.renderer import render_preview

		return api_success({"preview": render_preview(doc, _payload(context, {}) or payload.get("context") or {})})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
@standardize_response
def queue_notification(data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		if payload.get("manual"):
			require_doctype_permission("Pet App Notification Queue", "create")
		return engine.queue_notification(**payload)
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
@standardize_response
def send_manual_notification(data=None, **kwargs):
	try:
		require_doctype_permission("Pet App Notification Queue", "create")
		payload = _payload(data, kwargs)
		payload["manual"] = True
		result = engine.queue_notification(**payload)
		if result.get("ok") and payload.get("process_now", 1):
			return engine.process_notification_queue(result["data"]["queue"]["name"])
		return result
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
@standardize_response
def retry_notification(queue=None, name=None):
	try:
		require_doctype_permission("Pet App Notification Queue", "write")
		doc = frappe.get_doc("Pet App Notification Queue", queue or name)
		doc.status = "Queued"
		doc.save(ignore_permissions=True)
		return engine.process_notification_queue(doc.name)
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def cancel_notification(queue=None, name=None):
	try:
		require_doctype_permission("Pet App Notification Queue", "write")
		doc = frappe.get_doc("Pet App Notification Queue", queue or name)
		doc.status = "Cancelled"
		doc.save(ignore_permissions=True)
		return api_success({"queue": engine.queue_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def list_notification_queue(status=None, limit=50):
	try:
		require_doctype_permission("Pet App Notification Queue", "read")
		filters = {}
		if status:
			filters["status"] = status
		rows = frappe.get_all(
			"Pet App Notification Queue",
			filters=filters,
			fields=["name", "event_key", "channel", "status", "recipient_type", "recipient_name", "template_key", "scheduled_at", "sent_at", "provider_message_id"],
			order_by="creation desc",
			limit_page_length=int(limit or 50),
			ignore_permissions=True,
		)
		return api_success({"queue": [dict(row) for row in rows]}, meta={"total": len(rows)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_notification_queue(queue=None, name=None):
	try:
		require_doctype_permission("Pet App Notification Queue", "read")
		doc = frappe.get_doc("Pet App Notification Queue", queue or name)
		return api_success({"queue": engine.queue_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def list_notification_logs(queue=None, guardian=None, pet=None, limit=50):
	try:
		if frappe.db.exists("DocType", "Pet App Notification Log"):
			require_doctype_permission("Pet App Notification Log", "read")
			filters = {}
			if queue:
				filters["queue"] = queue
			rows = frappe.get_all(
				"Pet App Notification Log",
				filters=filters,
				fields=["name", "queue", "event_key", "channel", "status", "recipient_type", "recipient_name", "to_phone_masked", "template_key", "event_datetime", "message"],
				order_by="event_datetime desc, creation desc",
				limit_page_length=int(limit or 50),
				ignore_permissions=True,
			)
			return api_success({"logs": [dict(row) for row in rows]}, meta={"total": len(rows)})
		require_doctype_permission("Pet App Notification Queue", "read")
		return ok({"logs": []}, meta={"total": 0})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_notification_stats():
	try:
		require_doctype_permission("Pet App Notification Queue", "read")
		rows = frappe.db.sql(
			"""
			select status, count(*) as total
			from `tabPet App Notification Queue`
			group by status
			""",
			as_dict=True,
		)
		return api_success({"stats": [dict(row) for row in rows]})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def create_reminder(data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		doc = frappe.get_doc({"doctype": "Pet App Reminder", **payload})
		if not doc.send_at:
			doc.send_at = doc.due_datetime or now_datetime()
		doc.status = doc.status or "Scheduled"
		doc.save(ignore_permissions=True)
		return api_success({"reminder": _doc_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def cancel_reminder(reminder=None, name=None):
	try:
		doc = frappe.get_doc("Pet App Reminder", reminder or name)
		doc.status = "Cancelled"
		doc.cancelled_at = now_datetime()
		doc.save(ignore_permissions=True)
		return api_success({"reminder": _doc_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def list_reminders(status=None, limit=50):
	try:
		filters = {}
		if status:
			filters["status"] = status
		rows = frappe.get_all(
			"Pet App Reminder",
			filters=filters,
			fields=["name", "reminder_type", "status", "channel", "guardian", "pet", "source_doctype", "source_name", "due_datetime", "send_at", "notification_queue"],
			order_by="send_at asc, creation asc",
			limit_page_length=int(limit or 50),
			ignore_permissions=True,
		)
		reminders = [dict(row) for row in rows]
		enrich_link_aliases(reminders, pet_field="pet", guardian_field="guardian", include_doctor=False, include_provider=False)
		return api_success({"reminders": reminders}, meta={"total": len(rows)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
@standardize_response
def enqueue_due_reminders():
	return _enqueue_due_reminders()


@frappe.whitelist(methods=["POST"])
@standardize_response
def send_manual_reminder(reminder=None, guardian=None, pet=None, reminder_type=None, message=None, channel="In App", data=None, **kwargs):
	"""Compatibility wrapper for the older Pet Reminder API."""
	try:
		payload = _payload(data, kwargs)
		reminder_name = cstr(reminder or payload.get("reminder")).strip()
		reminder_doc = frappe.get_doc("Pet Reminder", reminder_name) if reminder_name and frappe.db.exists("Pet Reminder", reminder_name) else None
		guardian = guardian or payload.get("guardian") or (reminder_doc.guardian if reminder_doc else None)
		pet = pet or payload.get("pet") or (reminder_doc.pet if reminder_doc else None)
		if channel != "WhatsApp":
			return _send_legacy_reminder(reminder_doc, guardian, pet, reminder_type, message, channel, payload)
		result = engine.queue_notification(
			event_key=payload.get("event_key") or f"legacy_reminder.{reminder_type or (reminder_doc.reminder_type if reminder_doc else 'Manual')}",
			recipient_type="Guardian" if guardian else "Manual",
			recipient_name=guardian,
			to_phone=payload.get("to_phone"),
			context={"message": message or payload.get("message"), "pet": pet},
			source_doctype=reminder_doc.reference_doctype if reminder_doc else payload.get("reference_doctype"),
			source_name=reminder_doc.reference_name if reminder_doc else payload.get("reference_name"),
			template_key=payload.get("template_key") or "auth_otp" if channel == "WhatsApp" else payload.get("template_key"),
			channel="WhatsApp" if channel == "WhatsApp" else "In App",
			idempotency_key=payload.get("idempotency_key") or (f"{reminder_name}:{channel}" if reminder_name else None),
			manual=True,
		)
		if result.get("ok") and channel == "WhatsApp":
			result = engine.process_notification_queue(result["data"]["queue"]["name"])
		if reminder_doc:
			reminder_doc.status = "Sent"
			reminder_doc.save(ignore_permissions=True)
		return result
	except Exception as exc:
		return _error_response(exc)


def _send_legacy_reminder(reminder_doc, guardian, pet, reminder_type, message, channel, payload):
	if not guardian:
		return fail(_("Guardian is required."), code="VALIDATION_ERROR")
	reminder_name = reminder_doc.name if reminder_doc else None
	resolved_type = reminder_type or payload.get("reminder_type") or (reminder_doc.reminder_type if reminder_doc else "Manual")
	template = _legacy_template(resolved_type, channel)
	subject = payload.get("subject") or (template.subject if template else resolved_type)
	body = message or payload.get("message") or (template.body if template else resolved_type)
	log = frappe.get_doc(
		{
			"doctype": "Pet Notification Log",
			"reminder": reminder_name,
			"template": template.name if template else None,
			"guardian": guardian,
			"pet": pet,
			"channel": channel,
			"recipient": _legacy_recipient(guardian, channel),
			"subject": subject,
			"message": body,
			"status": "Sent",
			"sent_at": now_datetime(),
			"reference_doctype": reminder_doc.reference_doctype if reminder_doc else payload.get("reference_doctype"),
			"reference_name": reminder_doc.reference_name if reminder_doc else payload.get("reference_name"),
			"idempotency_key": payload.get("idempotency_key") or (f"{reminder_name}:{channel}" if reminder_name else None),
		}
	)
	try:
		log.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		log = frappe.get_doc("Pet Notification Log", {"idempotency_key": log.idempotency_key})
	if reminder_doc:
		reminder_doc.status = "Sent"
		reminder_doc.save(ignore_permissions=True)
	notification = {key: log.get(key) for key in ("name", "reminder", "guardian", "pet", "channel", "recipient", "subject", "message", "status", "sent_at")}
	return ok({"notification": with_link_aliases(notification, pet_field="pet", guardian_field="guardian", include_doctor=False, include_provider=False)})


def _legacy_template(reminder_type, channel):
	name = frappe.db.get_value("Pet Notification Template", {"reminder_type": reminder_type, "channel": channel, "active": 1}, "name")
	return frappe.get_doc("Pet Notification Template", name) if name else None


def _legacy_recipient(guardian, channel):
	return frappe.db.get_value("Guardian", guardian, "email_id" if channel == "Email" else "phone") or guardian


def _get_template(name):
	if not name:
		frappe.throw(_("Template is required."))
	docname = frappe.db.get_value("Pet App WhatsApp Template", {"template_key": name}, "name") or name
	return frappe.get_doc("Pet App WhatsApp Template", docname)


def _doc_payload(doc) -> dict:
	return {field.fieldname: doc.get(field.fieldname) for field in doc.meta.fields}


def _payload(data, kwargs) -> dict:
	if isinstance(data, str) and data:
		return json.loads(data)
	if isinstance(data, dict):
		return data
	return kwargs or {}


def _error_response(exc):
	if isinstance(exc, frappe.PermissionError):
		return api_error(_("Not permitted"), code="PERMISSION_ERROR")
	return api_error(cstr(exc), code=getattr(exc, "exc_type", None) or exc.__class__.__name__)
