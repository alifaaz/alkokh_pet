from __future__ import annotations

import frappe
from frappe.utils.password import get_decrypted_password

from pet_app.notifications.channels.base import BaseNotificationChannel
from pet_app.notifications.renderer import template_components


class WhatsAppMetaChannel(BaseNotificationChannel):
	def send_template(self, *, to_phone: str, template, context: dict | None = None, queue=None):
		payload = self.build_template_payload(to_phone=to_phone, template=template, context=context or {})
		if self.settings and self.settings.get("dry_run"):
			return {"provider_message_id": f"dry-run-{queue.name if queue else frappe.generate_hash(length=10)}", "status": "sent", "payload": payload}

		token = get_decrypted_password("Pet App WhatsApp Account", self.account.name, "access_token", raise_exception=False)
		if not token:
			frappe.throw("WhatsApp access token is not configured.")
		version = self.account.graph_api_version or "v20.0"
		url = f"https://graph.facebook.com/{version}/{self.account.phone_number_id}/messages"
		response = frappe.make_post_request(url, headers={"Authorization": f"Bearer {token}"}, json=payload)
		message_id = ((response or {}).get("messages") or [{}])[0].get("id")
		return {"provider_message_id": message_id, "status": "sent", "payload": payload, "response": response}

	def send_text(self, *, to_phone: str, message: str, queue=None):
		payload = {"messaging_product": "whatsapp", "to": to_phone, "type": "text", "text": {"body": message}}
		if self.settings and self.settings.get("dry_run"):
			return {"provider_message_id": f"dry-run-{queue.name if queue else frappe.generate_hash(length=10)}", "status": "sent", "payload": payload}
		token = get_decrypted_password("Pet App WhatsApp Account", self.account.name, "access_token", raise_exception=False)
		if not token:
			frappe.throw("WhatsApp access token is not configured.")
		version = self.account.graph_api_version or "v20.0"
		url = f"https://graph.facebook.com/{version}/{self.account.phone_number_id}/messages"
		response = frappe.make_post_request(url, headers={"Authorization": f"Bearer {token}"}, json=payload)
		message_id = ((response or {}).get("messages") or [{}])[0].get("id")
		return {"provider_message_id": message_id, "status": "sent", "payload": payload, "response": response}

	def build_template_payload(self, *, to_phone: str, template, context: dict | None = None) -> dict:
		return {
			"messaging_product": "whatsapp",
			"to": to_phone,
			"type": "template",
			"template": {
				"name": template.template_name,
				"language": {"code": template.language or self.account.default_language or "en"},
				"components": template_components(template, context or {}, mask_sensitive=False),
			},
		}
