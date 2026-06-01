from __future__ import annotations

import json

import frappe
from frappe.utils import cstr

from pet_app.notifications.webhook import handle_whatsapp_webhook
from pet_app.utils.api_response import api_error, api_success


@frappe.whitelist(allow_guest=True)
def webhook(**kwargs):
	method = getattr(frappe.request, "method", "GET") if getattr(frappe, "request", None) else "GET"
	if method == "GET":
		mode = frappe.form_dict.get("hub.mode") or frappe.form_dict.get("hub_mode")
		token = frappe.form_dict.get("hub.verify_token") or frappe.form_dict.get("hub_verify_token")
		challenge = frappe.form_dict.get("hub.challenge") or frappe.form_dict.get("hub_challenge")
		if mode == "subscribe" and _verify_token(token):
			return challenge
		frappe.local.response.http_status_code = 403
		return "Forbidden"
	try:
		payload = frappe.local.form_dict or {}
		if getattr(frappe.request, "data", None):
			payload = json.loads(frappe.request.data)
		return handle_whatsapp_webhook(payload)
	except Exception as exc:
		return api_error(cstr(exc), code=getattr(exc, "exc_type", None) or exc.__class__.__name__)


def _verify_token(token) -> bool:
	if not token:
		return False
	expected = frappe.db.get_value("Pet App WhatsApp Account", {"enabled": 1}, "verify_token")
	if expected and expected == token:
		return True
	single = frappe.db.get_single_value("Pet App Notification Settings", "default_whatsapp_account")
	if single:
		account = frappe.get_doc("Pet App WhatsApp Account", single)
		return account.get_password("verify_token", raise_exception=False) == token
	return False
