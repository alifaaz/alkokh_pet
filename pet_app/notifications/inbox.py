from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import frappe
import requests
from frappe.integrations.utils import make_get_request
from frappe.utils import cint, cstr, get_datetime, now_datetime
from frappe.utils.data import convert_utc_to_system_timezone
from frappe.utils.file_manager import save_file
from frappe.utils.password import get_decrypted_password

from pet_app.notifications.context import normalize_phone


SESSION_HOURS = 24


def resolve_account(phone_number_id=None):
	name = None
	if phone_number_id:
		name = frappe.db.get_value(
			"Pet App WhatsApp Account", {"phone_number_id": phone_number_id, "enabled": 1}, "name"
		)
	if not name:
		name = frappe.db.get_single_value("Pet App Notification Settings", "default_whatsapp_account")
	if not name:
		name = frappe.db.get_value("Pet App WhatsApp Account", {"is_default": 1, "enabled": 1}, "name")
	return frappe.get_doc("Pet App WhatsApp Account", name) if name else None


def get_or_create_conversation(phone, account=None, *, inbound=False):
	account = account or resolve_account()
	if not account:
		frappe.throw("WhatsApp account is not configured.")
	normalized = normalize_phone(phone, _default_country_code())
	if not normalized:
		frappe.throw("A valid WhatsApp phone number is required.")
	key = hashlib.sha1(f"{account.name}|{normalized}".encode()).hexdigest()
	name = frappe.db.get_value("Pet App WhatsApp Conversation", {"conversation_key": key}, "name")
	if name:
		conversation = frappe.get_doc("Pet App WhatsApp Conversation", name)
	else:
		guardian = _guardian_for_phone(normalized)
		conversation = frappe.get_doc(
			{
				"doctype": "Pet App WhatsApp Conversation",
				"conversation_key": key,
				"provider_account": account.name,
				"normalized_phone": normalized,
				"guardian": guardian,
				"customer": frappe.db.get_value("Guardian", guardian, "customer_id") if guardian else None,
				"display_name": frappe.db.get_value("Guardian", guardian, "full_name") if guardian else normalized,
				"status": "Open",
			}
		).insert(ignore_permissions=True)
	if inbound:
		conversation.last_inbound_at = now_datetime()
		conversation.session_expires_at = now_datetime() + timedelta(hours=SESSION_HOURS)
	return conversation


def session_is_open(conversation) -> bool:
	if isinstance(conversation, str):
		conversation = frappe.get_doc("Pet App WhatsApp Conversation", conversation)
	return bool(
		conversation.last_inbound_at
		and conversation.session_expires_at
		and get_datetime(conversation.session_expires_at) > now_datetime()
		and conversation.status != "Blocked"
	)


def record_inbound_message(event, account=None):
	account = account or resolve_account(event.get("phone_number_id"))
	conversation = get_or_create_conversation(event.get("from_phone"), account, inbound=True)
	message_at = _message_datetime(event.get("timestamp"))
	doc = frappe.get_doc(
		{
			"doctype": "Pet App WhatsApp Message",
			"conversation": conversation.name,
			"direction": "Inbound",
			"message_type": event.get("message_type") or "Text",
			"body": event.get("message") or event.get("interactive_title"),
			"caption": event.get("caption"),
			"provider_media_id": event.get("provider_media_id"),
			"provider_message_id": event.get("provider_message_id"),
			"interactive_id": event.get("interactive_id"),
			"interactive_title": event.get("interactive_title"),
			"status": "Received",
			"message_at": message_at,
			"raw_json": json.dumps(event.get("raw_message") or event, default=str),
		}
	).insert(ignore_permissions=True)

	preview = doc.body or doc.caption or f"[{doc.message_type}]"
	conversation.last_inbound_at = message_at or now_datetime()
	conversation.session_expires_at = (message_at or now_datetime()) + timedelta(hours=SESSION_HOURS)
	conversation.last_message_preview = cstr(preview)[:500]
	conversation.last_message_direction = "Inbound"
	conversation.unread_count = cint(conversation.unread_count) + 1
	conversation.save(ignore_permissions=True)

	notify_inbox_users(conversation, doc)
	if doc.provider_media_id and account and account.provider == "Meta Cloud API":
		frappe.enqueue(
			"pet_app.notifications.inbox.download_inbound_media",
			queue="short",
			enqueue_after_commit=True,
			message_name=doc.name,
			account_name=account.name,
			filename=event.get("filename"),
		)
	return doc, conversation


def record_outbound_message(queue, response, *, message_type=None, body=None):
	if not queue.get("conversation"):
		return None
	existing = frappe.db.get_value("Pet App WhatsApp Message", {"notification_queue": queue.name}, "name")
	if existing:
		return frappe.get_doc("Pet App WhatsApp Message", existing)
	doc = frappe.get_doc(
		{
			"doctype": "Pet App WhatsApp Message",
			"conversation": queue.conversation,
			"direction": "Outbound",
			"message_type": message_type or queue.get("message_type") or "Text",
			"body": body if body is not None else queue.rendered_preview,
			"file": queue.get("media_file"),
			"provider_message_id": response.get("provider_message_id"),
			"status": "Sent",
			"message_at": now_datetime(),
			"notification_queue": queue.name,
			"action_request": queue.get("action_request"),
			"source_doctype": queue.source_doctype,
			"source_name": queue.source_name,
			"raw_json": json.dumps(response.get("payload") or {}, default=str),
		}
	).insert(ignore_permissions=True)
	conversation = frappe.get_doc("Pet App WhatsApp Conversation", queue.conversation)
	conversation.last_outbound_at = doc.message_at
	conversation.last_message_preview = cstr(doc.body or f"[{doc.message_type}]")[:500]
	conversation.last_message_direction = "Outbound"
	conversation.save(ignore_permissions=True)
	if queue.get("action_request"):
		frappe.db.set_value("Pet App WhatsApp Action Request", queue.action_request, "outbound_message", doc.name)
	return doc


def record_failed_outbound_message(queue, error):
	if isinstance(queue, str):
		queue = frappe.get_doc("Pet App Notification Queue", queue)
	if not queue.get("conversation"):
		return None
	existing = frappe.db.get_value("Pet App WhatsApp Message", {"notification_queue": queue.name}, "name")
	if existing:
		doc = frappe.get_doc("Pet App WhatsApp Message", existing)
		doc.status = "Failed"
		doc.raw_json = json.dumps({"error": cstr(error)}, default=str)
		doc.save(ignore_permissions=True)
		return doc
	doc = frappe.get_doc(
		{
			"doctype": "Pet App WhatsApp Message",
			"conversation": queue.conversation,
			"direction": "Outbound",
			"message_type": queue.get("message_type") or "Text",
			"body": queue.rendered_preview,
			"file": queue.get("media_file"),
			"status": "Failed",
			"message_at": now_datetime(),
			"notification_queue": queue.name,
			"action_request": queue.get("action_request"),
			"source_doctype": queue.source_doctype,
			"source_name": queue.source_name,
			"raw_json": json.dumps({"error": cstr(error)}, default=str),
		}
	).insert(ignore_permissions=True)
	conversation = frappe.get_doc("Pet App WhatsApp Conversation", queue.conversation)
	conversation.last_outbound_at = doc.message_at
	conversation.last_message_preview = cstr(doc.body or "[Failed WhatsApp message]")[:500]
	conversation.last_message_direction = "Outbound"
	conversation.save(ignore_permissions=True)
	return doc


def send_conversation_message(conversation_name, message=None, file_name=None, *, action_request=None):
	if getattr(frappe.flags, "pet_app_whatsapp_simulation", False):
		from pet_app.notifications.designer import DesignerError

		raise DesignerError(
			"Messages cannot be sent during WhatsApp rule simulation.",
			"SIMULATION_SIDE_EFFECT_BLOCKED",
		)
	conversation = frappe.get_doc("Pet App WhatsApp Conversation", conversation_name)
	if not session_is_open(conversation):
		frappe.throw("The WhatsApp 24-hour service window is closed. Send an approved Meta template first.")
	from pet_app.notifications.consent import assert_consent_allowed
	from pet_app.notifications.rate_limit import assert_rate_limit_allowed

	recipient_type = "Guardian" if conversation.guardian else "Manual"
	recipient_name = conversation.guardian or conversation.normalized_phone
	assert_consent_allowed(
		channel="WhatsApp",
		category="Service",
		recipient_type=recipient_type,
		recipient_name=recipient_name,
		phone=conversation.normalized_phone,
	)
	assert_rate_limit_allowed(channel="WhatsApp", recipient_type=recipient_type, recipient_name=recipient_name)
	account = frappe.get_doc("Pet App WhatsApp Account", conversation.provider_account)
	channel = _channel(account)
	if file_name:
		from frappe.utils.file_manager import get_file

		file_doc = frappe.get_doc("File", file_name)
		file_doc.check_permission("read")
		resolved_name, content = get_file(file_doc.file_url)
		media_type = _media_type(file_doc.file_name or resolved_name)
		response = channel.send_media(
			to_phone=conversation.normalized_phone,
			media_type=media_type,
			file_name=resolved_name,
			content=content,
			caption=message,
		)
		message_type = media_type.title()
	else:
		if not cstr(message).strip():
			frappe.throw("Message is required.")
		response = channel.send_text(to_phone=conversation.normalized_phone, message=cstr(message).strip())
		message_type = "Text"
	doc = frappe.get_doc(
		{
			"doctype": "Pet App WhatsApp Message",
			"conversation": conversation.name,
			"direction": "Outbound",
			"message_type": message_type,
			"body": cstr(message).strip(),
			"file": file_name,
			"provider_message_id": response.get("provider_message_id"),
			"status": "Sent",
			"message_at": now_datetime(),
			"action_request": action_request,
			"raw_json": json.dumps(response.get("payload") or {}, default=str),
		}
	).insert(ignore_permissions=True)
	conversation.last_outbound_at = doc.message_at
	conversation.last_message_preview = cstr(message or f"[{message_type}]")[:500]
	conversation.last_message_direction = "Outbound"
	conversation.save(ignore_permissions=True)
	return doc


def update_outbound_status(provider_message_id, status):
	row = frappe.db.get_value(
		"Pet App WhatsApp Message",
		{"provider_message_id": provider_message_id},
		["name", "status"],
		as_dict=True,
	)
	if not row or not outbound_status_advances(row.status, status):
		return
	values = {"status": status}
	if status == "Read":
		values["read_at"] = now_datetime()
	frappe.db.set_value("Pet App WhatsApp Message", row.name, values)


def outbound_status_advances(current, incoming):
	rank = {"Sent": 1, "Delivered": 2, "Read": 3, "Failed": 4}
	return rank.get(cstr(incoming), 0) >= rank.get(cstr(current), 0)


def mark_conversation_read(conversation_name):
	conversation = frappe.get_doc("Pet App WhatsApp Conversation", conversation_name)
	conversation.unread_count = 0
	conversation.save(ignore_permissions=True)
	frappe.db.set_value(
		"Pet App WhatsApp Message",
		{"conversation": conversation.name, "direction": "Inbound", "read_at": ["is", "not set"]},
		{"read_at": now_datetime()},
	)
	return conversation


def notify_inbox_users(conversation, message, *, subject=None, notification_type="Alert"):
	settings = frappe.get_single("Pet App Notification Settings")
	raw_users = cstr(settings.get("whatsapp_inbox_notification_users"))
	users = {value.strip() for value in raw_users.replace(",", "\n").splitlines() if value.strip()}
	if not users:
		users = {"Administrator"}
	subject = subject or f"WhatsApp from {conversation.display_name or conversation.normalized_phone}: {cstr(message.body or message.caption or message.message_type)[:80]}"
	for user in users:
		if not frappe.db.exists("User", user):
			continue
		frappe.get_doc(
			{
				"doctype": "Notification Log",
				"for_user": user,
				"from_user": "Administrator",
				"subject": subject,
				"document_type": "Pet App WhatsApp Conversation",
				"document_name": conversation.name,
				"type": notification_type,
			}
		).insert(ignore_permissions=True)


def download_inbound_media(message_name, account_name, filename=None):
	message = frappe.get_doc("Pet App WhatsApp Message", message_name)
	account = frappe.get_doc("Pet App WhatsApp Account", account_name)
	token = get_decrypted_password("Pet App WhatsApp Account", account.name, "access_token", raise_exception=False)
	if not token:
		message.media_error = "WhatsApp access token is not configured."
		message.save(ignore_permissions=True)
		return
	try:
		version = account.graph_api_version or "v20.0"
		metadata = make_get_request(
			f"https://graph.facebook.com/{version}/{message.provider_media_id}",
			headers={"Authorization": f"Bearer {token}"},
		)
		response = requests.get(metadata["url"], headers={"Authorization": f"Bearer {token}"}, timeout=30)
		response.raise_for_status()
		resolved_name = filename or f"whatsapp-{message.provider_media_id}{_extension(metadata.get('mime_type'))}"
		file_doc = save_file(resolved_name, response.content, message.doctype, message.name, is_private=1)
		message.file = file_doc.name
		message.media_error = None
		message.save(ignore_permissions=True)
	except Exception as exc:
		message.media_error = cstr(exc)[:500]
		message.save(ignore_permissions=True)
		frappe.log_error(frappe.get_traceback(), "WhatsApp inbound media download")


def _guardian_for_phone(normalized):
	candidates = {normalized, f"+{normalized}"}
	country = _default_country_code()
	if country and normalized.startswith(country):
		candidates.add("0" + normalized[len(country):])
	return frappe.db.get_value("Guardian", {"phone": ["in", list(candidates)]}, "name")


def _default_country_code():
	return frappe.db.get_single_value("Pet App Notification Settings", "default_country_code") or "964"


def _message_datetime(value):
	"""Convert a WhatsApp webhook epoch timestamp (UTC seconds) to a naive
	datetime in Frappe's system timezone.

	WhatsApp sends `timestamp` as Unix epoch seconds in UTC. Using the bare
	``datetime.fromtimestamp`` relies on the server's OS timezone (UTC here),
	which stores inbound times shifted by the system-timezone offset relative
	to everything else (which uses ``now_datetime()``). Convert through the
	system timezone so inbound times match the WhatsApp app and outbound rows.
	"""
	try:
		if not value:
			return now_datetime()
		utc_dt = datetime.fromtimestamp(int(value), tz=timezone.utc)
		return convert_utc_to_system_timezone(utc_dt).replace(tzinfo=None)
	except (TypeError, ValueError, OSError):
		return now_datetime()


def _extension(mime_type):
	return {
		"image/jpeg": ".jpg",
		"image/png": ".png",
		"application/pdf": ".pdf",
		"audio/ogg": ".ogg",
		"video/mp4": ".mp4",
	}.get(cstr(mime_type).split(";")[0], "")


def _channel(account):
	from pet_app.notifications.channels.dummy import DummyChannel
	from pet_app.notifications.channels.whatsapp_meta import WhatsAppMetaChannel
	from pet_app.notifications.engine import get_settings

	settings = get_settings()
	return WhatsAppMetaChannel(account=account, settings=settings) if account.provider == "Meta Cloud API" else DummyChannel(account=account, settings=settings)


def _media_type(file_name):
	extension = cstr(file_name).lower().rsplit(".", 1)[-1]
	if extension in {"jpg", "jpeg", "png", "webp"}:
		return "image"
	if extension in {"mp4", "3gp"}:
		return "video"
	if extension in {"aac", "amr", "mp3", "m4a", "ogg", "opus"}:
		return "audio"
	return "document"
