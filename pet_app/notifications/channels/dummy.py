from __future__ import annotations

import hashlib

from pet_app.notifications.channels.base import BaseNotificationChannel


class DummyChannel(BaseNotificationChannel):
	def send_template(self, *, to_phone: str, template, context: dict | None = None, queue=None):
		message_id = _message_id("template", to_phone, template.get("template_name") if template else "", queue.name if queue else "")
		return {"provider_message_id": message_id, "status": "sent", "payload": {"to": to_phone, "template": template.get("template_name") if template else None}}

	def send_text(self, *, to_phone: str, message: str, queue=None):
		message_id = _message_id("text", to_phone, message, queue.name if queue else "")
		return {"provider_message_id": message_id, "status": "sent", "payload": {"to": to_phone, "message": message}}

	def send_interactive(self, *, to_phone: str, message: str, interactive: dict, queue=None):
		message_id = _message_id("interactive", to_phone, message, queue.name if queue else "")
		return {
			"provider_message_id": message_id,
			"status": "sent",
			"payload": {"to": to_phone, "message": message, "interactive": interactive},
		}

	def send_media(self, *, to_phone: str, media_type: str, file_name: str, content: bytes, caption=None, queue=None):
		message_id = _message_id("media", to_phone, media_type, file_name, queue.name if queue else "")
		return {
			"provider_message_id": message_id,
			"provider_media_id": _message_id("upload", file_name, len(content)),
			"status": "sent",
			"payload": {"to": to_phone, "type": media_type, "file_name": file_name, "caption": caption},
		}


def _message_id(*parts) -> str:
	digest = hashlib.sha1("|".join(str(part or "") for part in parts).encode()).hexdigest()[:16]
	return f"dummy-{digest}"
