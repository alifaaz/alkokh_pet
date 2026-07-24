from __future__ import annotations


class BaseNotificationChannel:
	def __init__(self, account=None, settings=None):
		self.account = account
		self.settings = settings

	def send_template(self, *, to_phone: str, template, context: dict | None = None, queue=None):
		raise NotImplementedError

	def send_text(self, *, to_phone: str, message: str, queue=None):
		raise NotImplementedError

	def send_interactive(self, *, to_phone: str, message: str, interactive: dict, queue=None):
		raise NotImplementedError

	def send_media(self, *, to_phone: str, media_type: str, file_name: str, content: bytes, caption=None, queue=None):
		raise NotImplementedError

	def parse_response(self, response):
		return response
