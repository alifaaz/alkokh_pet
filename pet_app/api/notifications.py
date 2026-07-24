from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, cstr, now_datetime

from pet_app.api.permissions import require_doctype_permission
from pet_app.api.response import fail, ok, standardize_response
from pet_app.api.link_aliases import enrich_link_aliases, with_link_aliases
from pet_app.notifications import engine
from pet_app.notifications import actions as whatsapp_actions
from pet_app.notifications import designer as whatsapp_designer
from pet_app.notifications import inbox as whatsapp_inbox
from pet_app.notifications.context import list_template_variables
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
			fields=["name", "template_key", "enabled", "template_name", "language", "category", "event_key", "source_doctype", "recipient_type", "delivery_mode", "priority"],
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
		if payload.get("source_doctype"):
			whatsapp_designer.get_source_schema(payload["source_doctype"])
			require_doctype_permission(payload["source_doctype"], "read")
		name = payload.get("name") or frappe.db.get_value("Pet App WhatsApp Template", {"template_key": payload.get("template_key")}, "name")
		require_doctype_permission("Pet App WhatsApp Template", "write" if name else "create")
		doc = frappe.get_doc("Pet App WhatsApp Template", name) if name else frappe.new_doc("Pet App WhatsApp Template")
		content_candidate = doc.as_dict()
		content_candidate.update(payload)
		whatsapp_designer.validate_template_content(
			content_candidate,
			content_candidate.get("source_doctype"),
		)
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
		process_now = payload.pop("process_now", 1)
		payload["manual"] = True
		result = engine.queue_notification(**payload)
		if result.get("ok") and cint(process_now):
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
	return api_error(
		cstr(exc),
		code=getattr(exc, "code", None) or getattr(exc, "exc_type", None) or exc.__class__.__name__,
		details=getattr(exc, "details", None),
	)


def _require_whatsapp_designer_access():
	user = frappe.session.user
	if not user or user == "Guest":
		raise frappe.PermissionError
	roles = set(frappe.get_roles(user) or [])
	allowed_roles = {"System Manager", "Healthcare Administrator"}
	if frappe.db.exists("DocType", "Pet App Notification Settings"):
		meta = frappe.get_meta("Pet App Notification Settings")
		if meta.has_field("whatsapp_full_access_role"):
			configured_role = frappe.db.get_single_value("Pet App Notification Settings", "whatsapp_full_access_role")
			if configured_role:
				allowed_roles.add(configured_role)
	if user != "Administrator" and not roles.intersection(allowed_roles):
		raise frappe.PermissionError


@frappe.whitelist()
def list_whatsapp_template_variables(source_doctype=None):
	try:
		_require_whatsapp_designer_access()
		require_doctype_permission("Pet App WhatsApp Template", "read")
		if source_doctype:
			whatsapp_designer.get_source_schema(source_doctype)
			require_doctype_permission(source_doctype, "read")
		return api_success({"variables": list_template_variables(source_doctype)}, meta={"source_doctype": source_doctype})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_whatsapp_designer_schema(source_doctype=None):
	try:
		_require_whatsapp_designer_access()
		require_doctype_permission("Pet App WhatsApp Action Rule", "read")
		require_doctype_permission("Pet App WhatsApp Template", "read")
		if source_doctype:
			require_doctype_permission(source_doctype, "read")
		schema = whatsapp_designer.get_designer_schema(source_doctype)
		if not source_doctype:
			schema["source_tables"] = [
				row for row in schema["source_tables"] if frappe.has_permission(row["value"], ptype="read")
			]
		return api_success({"schema": schema})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def list_whatsapp_action_rules(source_doctype=None, enabled=None):
	try:
		_require_whatsapp_designer_access()
		require_doctype_permission("Pet App WhatsApp Action Rule", "read")
		if source_doctype:
			whatsapp_designer.get_source_schema(source_doctype)
			require_doctype_permission(source_doctype, "read")
		filters = {}
		if source_doctype:
			filters["source_doctype"] = source_doctype
		if enabled is not None:
			filters["enabled"] = cint(enabled)
		names = frappe.get_all(
			"Pet App WhatsApp Action Rule",
			filters=filters,
			pluck="name",
			order_by="rule_name asc",
			ignore_permissions=True,
		)
		rules = []
		for name in names:
			doc = frappe.get_doc("Pet App WhatsApp Action Rule", name)
			if doc.source_doctype in whatsapp_designer.SOURCE_REGISTRY and frappe.has_permission(doc.source_doctype, ptype="read"):
				rules.append(whatsapp_designer.canonical_rule(doc))
		return api_success({"rules": rules}, meta={"total": len(rules)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def save_whatsapp_action_rule(data=None, **kwargs):
	try:
		_require_whatsapp_designer_access()
		payload = _payload(data, kwargs)
		if isinstance(payload.get("rule"), dict):
			payload = payload["rule"]
		name = payload.get("name") or payload.get("rule_name")
		existing = frappe.db.exists("Pet App WhatsApp Action Rule", name) if name else None
		require_doctype_permission("Pet App WhatsApp Action Rule", "write" if existing else "create")
		doc = frappe.get_doc("Pet App WhatsApp Action Rule", existing) if existing else frappe.new_doc("Pet App WhatsApp Action Rule")
		normalized = whatsapp_designer.raise_for_invalid_rule(payload, doc if existing else None)
		require_doctype_permission(normalized["source_doctype"], "read")
		require_doctype_permission("Pet App WhatsApp Template", "read")
		for key in whatsapp_designer.RULE_FIELDS:
			if key != "name" and doc.meta.has_field(key):
				doc.set(key, normalized.get(key))
		for key, value in whatsapp_designer.serialize_rule_sections(normalized).items():
			doc.set(key, value)
		doc.save(ignore_permissions=True)
		return api_success({"rule": whatsapp_designer.canonical_rule(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def validate_whatsapp_action_rule(data=None, rule=None, **kwargs):
	try:
		_require_whatsapp_designer_access()
		require_doctype_permission("Pet App WhatsApp Action Rule", "read")
		payload = _payload(data, kwargs)
		candidate = _payload(rule, {}) if rule else payload.get("rule") or payload
		source_doctype = cstr(candidate.get("source_doctype")).strip()
		if source_doctype in whatsapp_designer.SOURCE_REGISTRY:
			require_doctype_permission(source_doctype, "read")
		require_doctype_permission("Pet App WhatsApp Template", "read")
		return api_success({"validation": whatsapp_designer.validate_rule(candidate)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def simulate_whatsapp_action_rule(data=None, rule=None, source_name=None, **kwargs):
	try:
		_require_whatsapp_designer_access()
		require_doctype_permission("Pet App WhatsApp Action Rule", "read")
		require_doctype_permission("Pet App WhatsApp Template", "read")
		payload = _payload(data, kwargs)
		candidate = _payload(rule, {}) if rule else payload.get("rule") or payload
		source_name = source_name or payload.get("source_name")
		source_doctype = cstr(candidate.get("source_doctype")).strip()
		whatsapp_designer.get_source_schema(source_doctype)
		if not source_name or not frappe.db.exists(source_doctype, source_name):
			raise whatsapp_designer.DesignerError(_("Source record was not found."), "SOURCE_RECORD_NOT_FOUND")
		try:
			require_doctype_permission(source_doctype, "read")
			source_doc = frappe.get_doc(source_doctype, source_name)
			if not source_doc.has_permission("read"):
				raise frappe.PermissionError
		except frappe.PermissionError:
			raise whatsapp_designer.DesignerError(_("You cannot read this source record."), "SOURCE_READ_FORBIDDEN")
		return api_success({"simulation": whatsapp_designer.simulate_rule(candidate, source_name)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def send_actionable_whatsapp_message(
	template_key=None,
	source_doctype=None,
	source_name=None,
	recipient=None,
	context=None,
	action_rule=None,
	data=None,
	**kwargs,
):
	try:
		payload = _payload(data, kwargs)
		template_key = template_key or payload.get("template_key")
		source_doctype = source_doctype or payload.get("source_doctype")
		source_name = source_name or payload.get("source_name")
		recipient = recipient or payload.get("recipient")
		action_rule = action_rule or payload.get("action_rule")
		require_doctype_permission("Pet App WhatsApp Action Request", "create")
		require_doctype_permission(source_doctype, "read")
		filters = {"enabled": 1, "source_doctype": source_doctype}
		if action_rule:
			filters["name"] = action_rule
		elif template_key:
			filters["template_key"] = template_key
		name = frappe.db.get_value("Pet App WhatsApp Action Rule", filters, "name", order_by="modified desc")
		if not name:
			frappe.throw(_("No active WhatsApp action rule matches this request."))
		rule = frappe.get_doc("Pet App WhatsApp Action Rule", name)
		source = frappe.get_doc(source_doctype, source_name)
		request = whatsapp_actions.create_action_request(
			rule,
			source,
			recipient=recipient,
			context=_payload(context, {}) if context else None,
			process_now=True,
		)
		return api_success({"action": _doc_payload(request)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def list_pending_whatsapp_actions(status=None, limit=50):
	try:
		require_doctype_permission("Pet App WhatsApp Action Request", "read")
		filters = {"status": status} if status else {"status": ["in", ["Waiting Reply", "Matched", "Pending Review", "Needs Review"]]}
		rows = frappe.get_all(
			"Pet App WhatsApp Action Request",
			filters=filters,
			fields=[
				"name", "rule", "status", "delivery_stage", "conversation", "source_doctype", "source_name",
				"recipient_phone", "response_key", "response_value", "expires_at", "creation",
			],
			order_by="creation desc",
			limit_page_length=cint(limit) or 50,
			ignore_permissions=True,
		)
		return api_success({"actions": [dict(row) for row in rows]}, meta={"total": len(rows)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def approve_whatsapp_action(action_request=None, note=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		name = action_request or payload.get("action_request") or payload.get("name")
		require_doctype_permission("Pet App WhatsApp Action Request", "write")
		doc = frappe.get_doc("Pet App WhatsApp Action Request", name)
		if doc.status not in {"Pending Review", "Needs Review", "Matched"}:
			frappe.throw(_("Only matched or review-pending actions can be approved."))
		if doc.status == "Needs Review":
			doc = whatsapp_actions.select_review_response(doc, payload.get("response_key"))
		doc = whatsapp_actions.execute_action(doc, approved_by=frappe.session.user, approval_note=note or payload.get("note"))
		return api_success({"action": _doc_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def reject_whatsapp_action(action_request=None, note=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		name = action_request or payload.get("action_request") or payload.get("name")
		require_doctype_permission("Pet App WhatsApp Action Request", "write")
		doc = whatsapp_actions.reject_action(name, note or payload.get("note"))
		return api_success({"action": _doc_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def list_whatsapp_conversations(status=None, limit=50):
	try:
		require_doctype_permission("Pet App WhatsApp Conversation", "read")
		filters = {"status": status} if status else {}
		rows = frappe.get_all(
			"Pet App WhatsApp Conversation",
			filters=filters,
			fields=[
				"name", "display_name", "normalized_phone", "guardian", "customer", "status", "last_inbound_at",
				"last_outbound_at", "session_expires_at", "unread_count", "last_message_preview", "last_message_direction",
			],
			order_by="modified desc",
			limit_page_length=cint(limit) or 50,
			ignore_permissions=True,
		)
		for row in rows:
			row["session_open"] = whatsapp_inbox.session_is_open(row)
		return api_success({"conversations": [dict(row) for row in rows]}, meta={"total": len(rows)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_whatsapp_conversation(conversation=None, limit=100):
	try:
		require_doctype_permission("Pet App WhatsApp Conversation", "read")
		doc = frappe.get_doc("Pet App WhatsApp Conversation", conversation)
		messages = frappe.get_all(
			"Pet App WhatsApp Message",
			filters={"conversation": doc.name},
			fields=[
				"name", "direction", "message_type", "body", "caption", "file", "provider_message_id", "interactive_id",
				"interactive_title", "status", "message_at", "read_at", "action_request", "source_doctype", "source_name",
			],
			order_by="message_at asc, creation asc",
			limit_page_length=cint(limit) or 100,
			ignore_permissions=True,
		)
		actions = frappe.get_all(
			"Pet App WhatsApp Action Request",
			filters={"conversation": doc.name},
			fields=[
				"name", "rule", "status", "delivery_stage", "source_doctype", "source_name",
				"response_key", "response_value", "result_doctype", "result_name", "error_message",
			],
			order_by="creation asc",
			ignore_permissions=True,
		)
		for action in actions:
			rule_config = frappe.db.get_value("Pet App WhatsApp Action Rule", action.rule, "response_config_json")
			action["response_options"] = (_payload(rule_config, {}).get("options") or []) if rule_config else []
		return api_success(
			{
				"conversation": {**_doc_payload(doc), "session_open": whatsapp_inbox.session_is_open(doc)},
				"messages": [dict(row) for row in messages],
				"actions": [dict(row) for row in actions],
			}
		)
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def reply_whatsapp_conversation(conversation=None, message=None, file=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		conversation = conversation or payload.get("conversation")
		message = message if message is not None else payload.get("message")
		file = file or payload.get("file")
		require_doctype_permission("Pet App WhatsApp Message", "create")
		doc = whatsapp_inbox.send_conversation_message(conversation, message, file)
		return api_success({"message": _doc_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def mark_whatsapp_conversation_read(conversation=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		conversation = conversation or payload.get("conversation")
		require_doctype_permission("Pet App WhatsApp Conversation", "write")
		doc = whatsapp_inbox.mark_conversation_read(conversation)
		return api_success({"conversation": _doc_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def download_whatsapp_media(message=None):
	try:
		require_doctype_permission("Pet App WhatsApp Message", "read")
		doc = frappe.get_doc("Pet App WhatsApp Message", message)
		if not doc.file:
			frappe.throw(_("This message has no downloaded attachment."))
		file_doc = frappe.get_doc("File", doc.file)
		return api_success({"file": {"name": file_doc.name, "file_name": file_doc.file_name, "file_url": file_doc.file_url, "is_private": file_doc.is_private}})
	except Exception as exc:
		return _error_response(exc)
