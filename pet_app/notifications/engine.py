from __future__ import annotations

import json
from datetime import datetime, timedelta

import frappe
from frappe import _
from frappe.utils import cint, cstr, get_datetime, now_datetime

from pet_app.notifications.channels.dummy import DummyChannel
from pet_app.notifications.channels.onesignal import OneSignalPushChannel
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

# Which side a queued template send came from. Our vocabulary, not Meta's.
TEMPLATE_SOURCE_LOCAL = "local"
TEMPLATE_SOURCE_META = "meta"


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
	push_user=None,
	push_title=None,
	push_body=None,
	push_url=None,
	push_data=None,
	template_source=None,
	meta_template=None,
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

		# Template addressing, in strict precedence order:
		#
		#   1. meta_template   - an explicitly addressed mirror row
		#   2. template_key    - an explicitly named local template
		#   3. the send-target registry, keyed by event_key
		#   4. the LOCAL event_key fallback inside resolve_template (engine.py:392)
		#
		# 3 and 4 both key off event_key and mean different things. It is a column on
		# the local template doctype AND the registry's key, and the two now share one
		# argument name. The registry sits above the local lookup deliberately: a local
		# row that later acquires a colliding event_key must not quietly outrank a
		# registered event and change which template an operator's button sends.
		# _registry_send_target returns None for anything explicitly addressed, so 1 and
		# 2 keep winning and no existing caller changes.
		registry_target = (
			_registry_send_target(event_key, template_key, meta_template, source_name)
			if channel == "WhatsApp"
			else None
		)
		if registry_target:
			template_source = TEMPLATE_SOURCE_META
			meta_template = registry_target.meta_template
			# The registry supplies the source doctype - it is read from the mirror row,
			# which is what the slot map was validated against. The caller supplies only
			# the record name. Resolved before the context is built, or a caller sending
			# a source_name without a source_doctype would get no context at all.
			source_doctype = registry_target.source_doctype

		if source_doctype and source_name:
			context = build_document_context(source_doctype, source_name, context)

		# A mirror-direct send names a Pet App WhatsApp Meta Template and has no local
		# template at all - not an unbound one, none. Everything below that would have
		# come from the local row is taken from the mirror row instead.
		meta_row = _resolve_meta_template_row(template_source, meta_template) if channel == "WhatsApp" else None
		template = None if meta_row else resolve_template(event_key=event_key, template_key=template_key, channel=channel)
		account = resolve_whatsapp_account(template=template, settings=settings) if channel == "WhatsApp" else None
		phone = normalize_phone(
			recipient_phone(recipient_type, recipient_name, to_phone),
			settings.get("default_country_code"),
		)
		email = recipient_email(recipient_type, recipient_name, to_email)
		push_user = _resolve_push_user(recipient_type, recipient_name, push_user) if channel == "Push" else push_user
		if channel == "WhatsApp" and not phone:
			return api_error(_("Recipient phone is required."), code="VALIDATION_ERROR")
		if channel == "Email" and not email:
			return api_error(_("Recipient email is required."), code="VALIDATION_ERROR")
		if channel == "Push" and not push_user:
			return api_error(_("Push notification user is required."), code="VALIDATION_ERROR")
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
			idempotency_key = _manual_idempotency_key(event_key, recipient_type, recipient_name, phone or push_user)
		else:
			idempotency_key = _idempotency_key(event_key, recipient_type, recipient_name, source_doctype, source_name, template.template_key if template else template_key, phone or push_user)
			existing = frappe.db.get_value("Pet App Notification Queue", {"idempotency_key": idempotency_key}, "name")
			if existing:
				return api_success({"queue": queue_payload(frappe.get_doc("Pet App Notification Queue", existing))}, meta={"duplicate": True})

		category = _queue_category(template, meta_row)
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
		rendered_preview = render_preview(template, context, mask_sensitive=bool(cint(settings.get("mask_sensitive_values")))) if template else _meta_body_preview(meta_row)
		status = "Queued" if _channel_enabled(channel, settings) else "Skipped"
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
				"template_key": None if meta_row else (template.template_key if template else template_key),
				"template_name": meta_row.template_name if meta_row else (template.template_name if template else None),
				"language": meta_row.language if meta_row else (template.language if template else settings.get("default_language")),
				"template_source": TEMPLATE_SOURCE_META if meta_row else TEMPLATE_SOURCE_LOCAL,
				"meta_template": meta_row.name if meta_row else None,
				"context_json": json.dumps(masked_context, default=str),
				"rendered_preview": rendered_preview,
				"source_doctype": source_doctype,
				"source_name": source_name,
				"source_title": _source_title(source_doctype, source_name),
				"scheduled_at": scheduled_at,
				"queued_at": now_datetime(),
				"provider": "OneSignal" if channel == "Push" else account.provider if account else None,
				"provider_account": account.name if account else None,
				"conversation": conversation,
				"action_request": action_request,
				"message_type": message_type,
				"interactive_json": json.dumps(interactive, default=str) if interactive else None,
				"media_file": media_file or (template.get("media_file") if template else None),
				"delivery_mode": "Meta Template" if meta_row else (template.get("delivery_mode") if template else None),
				"push_user": push_user,
				"push_title": push_title,
				"push_body": push_body,
				"push_url": _absolute_url(push_url),
				"push_data_json": json.dumps(push_data or {}, default=str) if push_data else None,
				"idempotency_key": idempotency_key,
				"dedupe_key": idempotency_key,
				"manual": cint(manual),
			}
		)
		doc.insert(ignore_permissions=True)
		return api_success({"queue": queue_payload(doc)}, meta={"scheduled": str(scheduled_at)})
	except Exception as exc:
		return _error_response(exc)


def queue_push_notification(
	user,
	title,
	body=None,
	url=None,
	data=None,
	event_key="push.manual",
	source_doctype=None,
	source_name=None,
	send_after=None,
	priority="default",
	idempotency_key=None,
	manual=False,
):
	return queue_notification(
		event_key=event_key,
		recipient_type="User",
		recipient_name=user,
		channel="Push",
		source_doctype=source_doctype,
		source_name=source_name,
		send_after=send_after,
		priority=priority,
		idempotency_key=idempotency_key,
		manual=manual,
		push_user=user,
		push_title=title,
		push_body=body or title,
		push_url=url,
		push_data=data or {},
	)


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

		# A mirror-direct row has no local template to resolve, and resolve_template
		# would throw "Notification template was not found." before the dispatch
		# branch in _send_queue_message was ever reached. Branch around it rather
		# than making the local path tolerate a missing template: the local path
		# below is byte-identical to what it was.
		if doc.get("meta_template"):
			template = None
			channel = _channel_for_account(doc)
		else:
			template = resolve_template(event_key=doc.event_key, template_key=doc.template_key, channel=doc.channel)
			channel = _channel_for(doc, template)
		context = _queue_context(doc)
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
		message = None
		if doc.channel == "WhatsApp":
			from pet_app.notifications.inbox import record_outbound_message

			message = record_outbound_message(doc, response, message_type=sent_type)
		if doc.channel == "WhatsApp" and doc.get("action_request"):
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
			"push_user": queue_doc.get("push_user"),
			"event_datetime": now_datetime(),
			"message": message if message is not None else (queue_doc.get("push_body") or queue_doc.rendered_preview),
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
	if channel == "Push":
		return None
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
		# The reason a failed row failed. _mark_failed always writes both underlying
		# fields, but neither was ever exposed here, so a failure reached the composer
		# as status "Failed" with nothing to show. Named error_code / error_message
		# because that is what the frontend reads; the doctype fields keep their own
		# provider_* names.
		"error_code": doc.get("provider_error_code"),
		"error_message": doc.get("provider_error_message"),
		"retry_count": doc.retry_count,
		"idempotency_key": doc.idempotency_key,
		"conversation": doc.get("conversation"),
		"action_request": doc.get("action_request"),
		"message_type": doc.get("message_type"),
		"delivery_mode": doc.get("delivery_mode"),
		"template_source": doc.get("template_source"),
		"meta_template": doc.get("meta_template"),
		"push_user": doc.get("push_user"),
		"push_title": doc.get("push_title"),
		"push_body": doc.get("push_body"),
		"push_url": doc.get("push_url"),
	}


def _mark_failed(queue_name, exc):
	try:
		doc = frappe.get_doc("Pet App Notification Queue", queue_name)
		settings = get_settings()
		doc.status = "Failed"
		doc.failed_at = now_datetime()
		doc.provider_error_code = getattr(exc, "exc_type", None) or exc.__class__.__name__
		doc.provider_error_message = cstr(exc)
		if _is_structural_failure(exc):
			# A pre-flight refusal: the template has no approved Meta binding. No Graph
			# call was made and no later attempt can succeed without someone changing
			# the binding, so a retry would only re-refuse on a timer. Park the row at
			# max_retries, which is where retry_failed_notifications' `retry_count <
			# max_retries` filter stops selecting it, and leave next_retry_at unset.
			# (Unset alone is not enough: the sweeper only skips a row whose
			# next_retry_at is in the future, so a null would be retried immediately.)
			doc.retry_count = max(cint(doc.retry_count), cint(settings.get("max_retries") or 3))
			doc.next_retry_at = None
		else:
			doc.retry_count = cint(doc.retry_count) + 1
			doc.next_retry_at = now_datetime() + timedelta(minutes=cint(settings.get("retry_after_minutes") or 5))
		doc.save(ignore_permissions=True)
		if doc.channel == "WhatsApp":
			from pet_app.notifications.inbox import record_failed_outbound_message

			record_failed_outbound_message(doc, doc.provider_error_message)
		if doc.channel == "WhatsApp" and doc.get("action_request"):
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


def _registry_send_target(event_key, template_key, meta_template, source_name):
	"""The registered send target for an event, or None if the event does not apply.

	Returns None - leaving every existing caller untouched - when the send is already
	addressed explicitly, or when event_key is not a registered event. manual_whatsapp_reply
	is not registered, so it falls straight through to the behaviour it has today.

	Refuses, rather than returning None, when a registered event arrives without a
	source record: the event resolves to a template whose slots are filled from a
	document, and with no document there is nothing to fill them from. Refusing here
	means it happens at queue time, before a row exists and long before Graph.
	"""
	from pet_app.notifications.send_targets import SEND_TARGETS, SendTargetError, resolve_send_target

	if cstr(meta_template).strip() or cstr(template_key).strip():
		return None
	event = cstr(event_key).strip()
	if event not in SEND_TARGETS:
		return None

	target = resolve_send_target(event)
	if not cstr(source_name).strip():
		raise SendTargetError(
			_(
				"Event {0} sends template {1}, whose message is filled from a {2} record. "
				"Send it with the {2} it is about - source_name is required."
			).format(target.event, target.template_name, target.source_doctype or _("source")),
			code="SEND_TARGET_SOURCE_NAME_REQUIRED",
			details={
				"event": target.event,
				"template_name": target.template_name,
				"meta_template": target.meta_template,
				"source_doctype": target.source_doctype,
			},
		)
	return target


def _resolve_meta_template_row(template_source, meta_template):
	"""The mirror row a caller asked to send, or None for the ordinary local path.

	Either naming the source explicitly or just passing a meta_template selects the
	mirror; asking for the mirror source without naming a row is an error rather
	than a silent fall back to the local path.
	"""
	wants_meta = cstr(template_source).strip().lower() == TEMPLATE_SOURCE_META or bool(meta_template)
	if not wants_meta:
		return None

	from pet_app.notifications.meta_templates import MIRROR_DOCTYPE, MetaTemplateNotSendable

	# Refuse before doing anything if the columns are not there yet. Frappe silently
	# drops unknown fieldnames on insert, so without this the queue row would lose
	# its meta_template, miss the dispatch branch, and fall through to a free-form
	# text send carrying the template's raw {{1}} body - a real message, to a real
	# number, in any open window.
	if not frappe.get_meta("Pet App Notification Queue").has_field("meta_template"):
		raise MetaTemplateNotSendable(
			_("Sending a Meta template directly needs a schema update. Run bench migrate first."),
			code="META_TEMPLATE_SCHEMA_MISSING",
		)

	name = cstr(meta_template).strip()
	if not name:
		raise MetaTemplateNotSendable(
			_("A Meta template must be selected when sending from the Meta template list."),
			code="META_TEMPLATE_NOT_SELECTED",
		)
	row = frappe.db.get_value(
		MIRROR_DOCTYPE, name, ["name", "template_name", "language", "category", "components_json"], as_dict=True
	)
	if not row:
		raise MetaTemplateNotSendable(
			_("Meta template {0} is not in the local mirror. Run a sync from the Meta Templates tab.").format(name),
			code="META_TEMPLATE_NOT_IN_MIRROR",
		)
	# status / missing_on_meta are checked at send time, in the same place the local
	# path checks them, so a template approved between queueing and sending is not
	# refused for a state it has since left.
	return row


def _queue_category(template, meta_row=None):
	"""The category the consent and marketing gates should see.

	A mirror row carries Meta's vocabulary (UTILITY), while those gates are written
	against the local one (Utility), so it is translated back through the category
	map rather than compared raw - otherwise a MARKETING template would slip past
	allow_marketing_messages simply because the casing differs.
	"""
	if meta_row:
		from pet_app.notifications.meta_templates import local_category_for

		return local_category_for(meta_row.get("category")) or "Utility"
	return template.category if template else "Utility"


def _meta_body_preview(meta_row):
	"""The mirror template's BODY text, for the queue preview and the inbox bubble.

	Taken verbatim, not rendered: Meta's text contains {{1}}-style placeholders that
	are not Jinja, and running them through the renderer would strip them.
	"""
	if not meta_row:
		return ""
	try:
		components = json.loads(meta_row.get("components_json") or "[]")
	except (TypeError, ValueError):
		return ""
	for component in components:
		if cstr(component.get("type")).upper() == "BODY":
			return cstr(component.get("text"))
	return ""


def _is_structural_failure(exc) -> bool:
	"""True for a refusal that no retry can turn into a success.

	The Meta-binding pre-flight check, and a send-target registry refusal: an
	unmapped template does not become mapped, and an unregistered event does not
	become registered, by trying again five minutes later.
	"""
	from pet_app.notifications.meta_templates import MetaTemplateNotSendable
	from pet_app.notifications.send_targets import SendTargetError

	return isinstance(exc, (MetaTemplateNotSendable, SendTargetError))


def _channel_for(queue_doc, template=None):
	if queue_doc.channel == "Push":
		return OneSignalPushChannel(settings=get_settings())
	if queue_doc.channel != "WhatsApp":
		return DummyChannel(settings=get_settings())
	account = resolve_whatsapp_account(template=template, settings=get_settings())
	if account.provider == "Meta Cloud API":
		return WhatsAppMetaChannel(account=account, settings=get_settings())
	return DummyChannel(account=account, settings=get_settings())


def _channel_for_account(queue_doc):
	"""Channel for a mirror-direct row, from the queue row's own account snapshot.

	Deliberately not _channel_for with a null template. That one derives the account
	from the local template and falls back to the settings default, which happens to
	be right here only because this site has exactly one account - with two, the
	fallback could send from an account the composer never chose. The queue row
	recorded what was resolved when the send was composed, and that is the only
	answer this function will accept.

	The mirror row's own provider_account is deliberately not consulted either: it
	records where the template lives, not what the send resolved to.
	"""
	from pet_app.notifications.meta_templates import MetaTemplateNotSendable

	account_name = cstr(queue_doc.get("provider_account")).strip()
	if not account_name:
		raise MetaTemplateNotSendable(
			_(
				"This send has no WhatsApp account recorded on it, so there is nothing to "
				"send it from. Re-send it from the composer."
			),
			code="META_TEMPLATE_NO_ACCOUNT",
			details={"queue": queue_doc.name},
		)
	if not frappe.db.exists("Pet App WhatsApp Account", account_name):
		raise MetaTemplateNotSendable(
			_("WhatsApp account {0} no longer exists, so this send cannot be delivered.").format(account_name),
			code="META_TEMPLATE_ACCOUNT_MISSING",
			details={"queue": queue_doc.name, "provider_account": account_name},
		)

	account = frappe.get_doc("Pet App WhatsApp Account", account_name)
	if account.provider != "Meta Cloud API":
		# The mirror only ever describes templates on a Meta Cloud API WABA, and no
		# other channel implements send_meta_template. Refusing here beats an
		# AttributeError, which would be retried three times as a transient fault.
		raise MetaTemplateNotSendable(
			_(
				"WhatsApp account {0} uses provider {1}, which cannot send Meta templates "
				"directly. Send from a Meta Cloud API account."
			).format(account_name, account.provider),
			code="META_TEMPLATE_PROVIDER_UNSUPPORTED",
			details={"queue": queue_doc.name, "provider": cstr(account.provider)},
		)
	return WhatsAppMetaChannel(account=account, settings=get_settings())


def _send_queue_message(doc, template, context, channel):
	from frappe.utils.file_manager import get_file
	from pet_app.notifications.inbox import session_is_open

	if doc.channel == "Push":
		data = json.loads(doc.push_data_json) if doc.get("push_data_json") else {}
		return (
			channel.send_push(
				user=doc.push_user,
				title=doc.push_title or doc.event_key,
				body=doc.push_body or doc.rendered_preview or doc.event_key,
				url=doc.push_url,
				data=data,
				queue=doc,
			),
			"Push",
			"Complete",
		)

	# Mirror-direct: the queue row names a Meta template, so there is no local row to
	# consult and no delivery_mode to interpret. Sent straight from the mirror, with
	# the same pre-flight gate applied inside build_meta_template_payload. Placed
	# ahead of the delivery_mode fork so that fork is reached only by local sends and
	# keeps behaving exactly as it did.
	if doc.get("meta_template"):
		return (
			channel.send_meta_template(
				to_phone=doc.to_phone, meta_template=doc.meta_template, context=context, queue=doc
			),
			"Text",
			"Interactive Sent",
		)

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


def _channel_enabled(channel, settings) -> bool:
	if not cint(settings.get("enabled")):
		return False
	if channel == "Push":
		return bool(cint(settings.get("onesignal_enabled")))
	return True


def _resolve_push_user(recipient_type, recipient_name=None, push_user=None):
	if push_user:
		return push_user if frappe.db.exists("User", push_user) else None
	if not recipient_name:
		return None
	if recipient_type == "User":
		return recipient_name if frappe.db.exists("User", recipient_name) else None
	if recipient_type == "Guardian":
		return frappe.db.get_value("Guardian", recipient_name, "user_id")
	if recipient_type == "Customer":
		guardian = frappe.db.get_value("Guardian", {"customer_id": recipient_name}, "name")
		return frappe.db.get_value("Guardian", guardian, "user_id") if guardian else None
	if recipient_type == "Doctor":
		return frappe.db.get_value("Healthcare Practitioner", recipient_name, "user_id")
	return None


def _absolute_url(url):
	text = cstr(url).strip()
	if not text:
		return None
	if text.startswith(("http://", "https://")):
		return text
	if not text.startswith("/"):
		text = f"/{text}"
	return frappe.utils.get_url(text)


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
