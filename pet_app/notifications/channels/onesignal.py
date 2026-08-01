from __future__ import annotations

import json

import frappe
import requests
from frappe import _
from frappe.utils import cstr
from frappe.utils.password import get_decrypted_password

from pet_app.notifications.channels.base import BaseNotificationChannel


ONESIGNAL_NOTIFICATIONS_URL = "https://api.onesignal.com/notifications"
ONESIGNAL_APPS_URL = "https://api.onesignal.com/apps"


class OneSignalAPIError(frappe.ValidationError):
	def __init__(self, message, error_code=None, details=None):
		super().__init__(message)
		self.exc_type = f"ONESIGNAL_{error_code}" if error_code else "ONESIGNAL_API_ERROR"
		self.details = details or {}


class OneSignalPushChannel(BaseNotificationChannel):
	def get_user_by_alias(self, alias_label: str, alias_id: str, missing_ok: bool = False):
		app_id = self._app_id()
		alias_label = cstr(alias_label).strip()
		alias_id = cstr(alias_id).strip()
		if not alias_label or not alias_id:
			frappe.throw(_("OneSignal user alias is required."))
		response = requests.get(
			f"{ONESIGNAL_APPS_URL}/{app_id}/users/by/{alias_label}/{alias_id}",
			headers={"Authorization": f"Key {self._api_key()}"},
			timeout=30,
		)
		if missing_ok and response.status_code == 404:
			return None
		return self._response_json(response)

	def send_push(self, *, user: str, title: str, body: str, url: str | None = None, data: dict | None = None, queue=None):
		payload = self.build_push_payload(user=user, title=title, body=body, url=url, data=data or {})
		if self.settings and self.settings.get("dry_run"):
			return {
				"provider_message_id": f"dry-run-{queue.name if queue else frappe.generate_hash(length=10)}",
				"status": "sent",
				"payload": payload,
			}

		response = self._post_json(payload)
		return self._sent_response(response, payload)

	def send_test_push(
		self,
		*,
		title: str,
		body: str,
		user: str | None = None,
		subscription_id: str | None = None,
		onesignal_id: str | None = None,
		url: str | None = None,
		data: dict | None = None,
	):
		payload = self.build_targeted_payload(
			title=title,
			body=body,
			user=user,
			subscription_id=subscription_id,
			onesignal_id=onesignal_id,
			url=url,
			data=data or {},
		)
		if self.settings and self.settings.get("dry_run"):
			return {
				"provider_message_id": f"dry-run-{frappe.generate_hash(length=10)}",
				"status": "sent",
				"payload": payload,
			}

		response = self._post_json(payload)
		return self._sent_response(response, payload)

	def _sent_response(self, response, payload):
		message_id = response.get("id")
		if not message_id:
			raise OneSignalAPIError(_onesignal_error_message(response), details={"response": response})
		return {"provider_message_id": message_id, "status": "sent", "payload": payload, "response": response}

	def build_push_payload(self, *, user: str, title: str, body: str, url: str | None = None, data: dict | None = None) -> dict:
		user = cstr(user).strip()
		if not user:
			frappe.throw(_("Push notification user is required."))
		body = frappe.utils.strip_html(cstr(body or title or _("New notification")))[:1000]
		title = frappe.utils.strip_html(cstr(title or _("New notification")))[:192]
		payload = {
			"app_id": self._app_id(),
			"include_aliases": {"external_id": [user]},
			"target_channel": "push",
			"headings": {"en": title},
			"contents": {"en": body},
		}
		if url:
			payload["url"] = url
		if data:
			payload["data"] = data
		return payload

	def build_targeted_payload(
		self,
		*,
		title: str,
		body: str,
		user: str | None = None,
		subscription_id: str | None = None,
		onesignal_id: str | None = None,
		url: str | None = None,
		data: dict | None = None,
	) -> dict:
		payload = self.build_push_payload(user=user or onesignal_id or subscription_id, title=title, body=body, url=url, data=data or {})
		if cstr(subscription_id).strip():
			payload.pop("include_aliases", None)
			payload["include_subscription_ids"] = [cstr(subscription_id).strip()]
		elif cstr(onesignal_id).strip():
			payload["include_aliases"] = {"onesignal_id": [cstr(onesignal_id).strip()]}
		elif cstr(user).strip():
			payload["include_aliases"] = {"external_id": [cstr(user).strip()]}
		else:
			frappe.throw(_("Push notification target is required."))
		return payload

	def _post_json(self, payload):
		response = requests.post(
			ONESIGNAL_NOTIFICATIONS_URL,
			headers={
				"Authorization": f"Key {self._api_key()}",
				"Content-Type": "application/json; charset=utf-8",
			},
			data=json.dumps(payload),
			timeout=30,
		)
		return self._response_json(response)

	def _app_id(self):
		app_id = cstr((self.settings or {}).get("onesignal_app_id")).strip()
		if not app_id:
			frappe.throw(_("OneSignal App ID is not configured."))
		return app_id

	def _api_key(self):
		token = get_decrypted_password(
			"Pet App Notification Settings",
			"Pet App Notification Settings",
			"onesignal_rest_api_key",
			raise_exception=False,
		)
		if not token:
			frappe.throw(_("OneSignal REST API key is not configured."))
		return token

	def _response_json(self, response):
		try:
			data = response.json()
		except ValueError:
			data = {}
		if response.ok:
			return data
		errors = data.get("errors") or data.get("error") or data or {}
		if isinstance(errors, list):
			message = "; ".join(cstr(error) for error in errors)
		elif isinstance(errors, dict):
			message = errors.get("message") or errors.get("detail") or frappe.as_json(errors)
		else:
			message = cstr(errors)
		raise OneSignalAPIError(
			message or _("OneSignal API request failed."),
			error_code=response.status_code,
			details={"http_status": response.status_code, "response": data},
		)


def _onesignal_error_message(data):
	errors = data.get("errors") if isinstance(data, dict) else None
	if isinstance(errors, dict):
		return frappe.as_json(errors)
	if isinstance(errors, list):
		return "; ".join(cstr(error) for error in errors)
	if errors:
		return cstr(errors)
	return _("OneSignal did not return a message id.")
