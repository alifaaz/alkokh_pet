from __future__ import annotations

import hashlib
import hmac
import json

import frappe
from frappe.utils import cstr
from werkzeug.wrappers import Response

from pet_app.notifications.webhook import handle_whatsapp_webhook
from pet_app.utils.api_response import api_error


@frappe.whitelist(allow_guest=True)
def webhook(**kwargs):
	method = getattr(frappe.request, "method", "GET") if getattr(frappe, "request", None) else "GET"
	if method == "GET":
		mode = frappe.form_dict.get("hub.mode") or frappe.form_dict.get("hub_mode")
		token = frappe.form_dict.get("hub.verify_token") or frappe.form_dict.get("hub_verify_token")
		challenge = frappe.form_dict.get("hub.challenge") or frappe.form_dict.get("hub_challenge")
		if mode == "subscribe" and _verify_token(token):
			return Response(cstr(challenge), status=200, content_type="text/plain")
		return Response("Forbidden", status=403, content_type="text/plain")
	try:
		raw_body = getattr(frappe.request, "data", None) or b""
		if not _verify_signature(raw_body, frappe.get_request_header("X-Hub-Signature-256")):
			return Response("Forbidden", status=403, content_type="text/plain")
		payload = frappe.local.form_dict or {}
		if raw_body:
			payload = json.loads(raw_body)
		return handle_whatsapp_webhook(payload)
	except Exception as exc:
		return api_error(cstr(exc), code=getattr(exc, "exc_type", None) or exc.__class__.__name__)


def _verify_token(token) -> bool:
	if not token:
		return False

	for account_name in frappe.get_all(
		"Pet App WhatsApp Account",
		filters={"enabled": 1},
		pluck="name",
		ignore_permissions=True,
	):
		account = frappe.get_doc("Pet App WhatsApp Account", account_name)
		if _account_verify_token(account) == token:
			return True

	single = frappe.db.get_single_value("Pet App Notification Settings", "default_whatsapp_account")
	if single:
		account = frappe.get_doc("Pet App WhatsApp Account", single)
		return _account_verify_token(account) == token
	return False


def _account_verify_token(account) -> str | None:
	token = account.get_password("verify_token", raise_exception=False)
	if token:
		return token
	raw_token = account.get("verify_token")
	if raw_token and not account.is_dummy_password(raw_token):
		return raw_token
	return None


def _verify_signature(raw_body, signature) -> bool:
	secrets = []
	for account_name in frappe.get_all(
		"Pet App WhatsApp Account",
		filters={"enabled": 1},
		pluck="name",
		ignore_permissions=True,
	):
		secret = frappe.get_doc("Pet App WhatsApp Account", account_name).get_password(
			"app_secret", raise_exception=False
		)
		if secret:
			secrets.append(secret)
	if not secrets:
		return True
	if not signature or not cstr(signature).startswith("sha256="):
		return False
	body = raw_body if isinstance(raw_body, bytes) else cstr(raw_body).encode()
	return any(
		hmac.compare_digest(cstr(signature), f"sha256={hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()}")
		for secret in secrets
	)
