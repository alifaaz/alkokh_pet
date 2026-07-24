from __future__ import annotations

import mimetypes

import frappe
import requests
from frappe.utils.password import get_decrypted_password

from pet_app.notifications.channels.base import BaseNotificationChannel
from pet_app.notifications.renderer import template_components


class WhatsAppMetaAPIError(frappe.ValidationError):
	def __init__(self, message, error_code=None, details=None):
		super().__init__(message)
		self.exc_type = f"META_{error_code}" if error_code else "META_API_ERROR"
		self.details = details or {}


class WhatsAppMetaChannel(BaseNotificationChannel):
	def send_template(self, *, to_phone: str, template, context: dict | None = None, queue=None):
		payload = self.build_template_payload(to_phone=to_phone, template=template, context=context or {})
		if self.settings and self.settings.get("dry_run"):
			return {"provider_message_id": f"dry-run-{queue.name if queue else frappe.generate_hash(length=10)}", "status": "sent", "payload": payload}

		response = self._post_json("messages", payload)
		message_id = ((response or {}).get("messages") or [{}])[0].get("id")
		return {"provider_message_id": message_id, "status": "sent", "payload": payload, "response": response}

	def send_text(self, *, to_phone: str, message: str, queue=None):
		payload = {"messaging_product": "whatsapp", "to": to_phone, "type": "text", "text": {"body": message}}
		return self._send_payload(payload, queue)

	def send_interactive(self, *, to_phone: str, message: str, interactive: dict, queue=None):
		kind = interactive.get("type") or "Buttons"
		options = interactive.get("options") or []
		if kind == "Buttons":
			if len(options) > 3:
				frappe.throw("WhatsApp reply buttons support at most three options.")
			content = {
				"type": "button",
				"body": {"text": message},
				"action": {
					"buttons": [
						{"type": "reply", "reply": {"id": row["id"], "title": row["label"][:20]}}
						for row in options
					]
				},
			}
		else:
			if len(options) > 10:
				frappe.throw("WhatsApp lists support at most ten options.")
			content = {
				"type": "list",
				"body": {"text": message},
				"action": {
					"button": (interactive.get("button_text") or "Choose")[:20],
					"sections": [
						{
							"title": (interactive.get("section_title") or "Options")[:24],
							"rows": [
								{
									"id": row["id"],
									"title": row["label"][:24],
									**({"description": row["description"][:72]} if row.get("description") else {}),
								}
								for row in options
							],
						}
					],
				},
			}
		if interactive.get("footer"):
			content["footer"] = {"text": interactive["footer"][:60]}
		payload = {"messaging_product": "whatsapp", "to": to_phone, "type": "interactive", "interactive": content}
		return self._send_payload(payload, queue)

	def send_media(self, *, to_phone: str, media_type: str, file_name: str, content: bytes, caption=None, queue=None):
		media_type = (media_type or "document").lower()
		if self.settings and self.settings.get("dry_run"):
			media_id = f"dry-run-media-{frappe.generate_hash(length=10)}"
		else:
			media_id = self._upload_media(file_name, content)
		media = {"id": media_id}
		if caption and media_type != "audio":
			media["caption"] = caption
		if media_type == "document":
			media["filename"] = file_name
		payload = {"messaging_product": "whatsapp", "to": to_phone, "type": media_type, media_type: media}
		result = self._send_payload(payload, queue)
		result["provider_media_id"] = media_id
		return result

	def build_template_payload(self, *, to_phone: str, template, context: dict | None = None) -> dict:
		components = template_components(template, context or {}, mask_sensitive=False)
		template_payload = {
			"name": template.template_name,
			"language": {"code": template.language or self.account.default_language or "en"},
		}
		if any(component.get("parameters") for component in components):
			template_payload["components"] = components
		return {
			"messaging_product": "whatsapp",
			"to": to_phone,
			"type": "template",
			"template": template_payload,
		}

	def _send_payload(self, payload, queue=None):
		if self.settings and self.settings.get("dry_run"):
			return {
				"provider_message_id": f"dry-run-{queue.name if queue else frappe.generate_hash(length=10)}",
				"status": "sent",
				"payload": payload,
			}
		response = self._post_json("messages", payload)
		message_id = ((response or {}).get("messages") or [{}])[0].get("id")
		return {"provider_message_id": message_id, "status": "sent", "payload": payload, "response": response}

	def _upload_media(self, file_name, content):
		token = self._access_token()
		version = self.account.graph_api_version or "v20.0"
		url = f"https://graph.facebook.com/{version}/{self.account.phone_number_id}/media"
		response = requests.post(
			url,
			headers={"Authorization": f"Bearer {token}"},
			data={"messaging_product": "whatsapp"},
			files={"file": (file_name, content, media_mime_type(file_name))},
			timeout=30,
		)
		data = self._response_json(response)
		media_id = data.get("id")
		if not media_id:
			frappe.throw("Meta did not return a media id.")
		return media_id

	def _access_token(self):
		token = get_decrypted_password("Pet App WhatsApp Account", self.account.name, "access_token", raise_exception=False)
		if not token:
			frappe.throw("WhatsApp access token is not configured.")
		return token

	def _post_json(self, endpoint, payload):
		version = self.account.graph_api_version or "v20.0"
		url = f"https://graph.facebook.com/{version}/{self.account.phone_number_id}/{endpoint}"
		response = requests.post(
			url,
			headers={"Authorization": f"Bearer {self._access_token()}"},
			json=payload,
			timeout=30,
		)
		return self._response_json(response)

	def _response_json(self, response):
		try:
			data = response.json()
		except ValueError:
			data = {}
		if response.ok:
			return data
		error = data.get("error") or {}
		code = error.get("code") or response.status_code
		subcode = error.get("error_subcode")
		message = error.get("message") or f"Meta WhatsApp API returned HTTP {response.status_code}."
		if subcode:
			message = f"{message} (subcode {subcode})"
		raise WhatsAppMetaAPIError(
			message,
			error_code=code,
			details={
				"http_status": response.status_code,
				"error_code": code,
				"error_subcode": subcode,
				"error_type": error.get("type"),
				"trace_id": error.get("fbtrace_id"),
			},
		)


def media_mime_type(file_name):
	return mimetypes.guess_type(file_name or "")[0] or "application/octet-stream"
