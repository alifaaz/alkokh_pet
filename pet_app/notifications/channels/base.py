from __future__ import annotations


class BaseNotificationChannel:
	def __init__(self, account=None, settings=None):
		self.account = account
		self.settings = settings

	def send_template(self, *, to_phone: str, template, context: dict | None = None, queue=None):
		raise NotImplementedError

	def send_text(self, *, to_phone: str, message: str, queue=None):
		raise NotImplementedError

	def parse_response(self, response):
		return response

