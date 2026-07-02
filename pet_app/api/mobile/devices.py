from __future__ import annotations

import functools

import frappe
from frappe import _
from frappe.utils import cstr, now_datetime

from pet_app.api.mobile.response import error, ok
from pet_app.utils.guardian_customer import get_guardian_by_user


DEVICE_AUTH_ERROR = "auth.wrong_credentials"
DEVICE_REQUEST_INVALID = "device.request_invalid"


class MobileDeviceError(Exception):
	def __init__(self, code: str, message: str, http_status: int = 400):
		super().__init__(message)
		self.code = code
		self.message = message
		self.http_status = http_status


def _mobile_device_endpoint(fn):
	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		kwargs.pop("cmd", None)
		try:
			return ok(fn(*args, **kwargs))
		except MobileDeviceError as exc:
			return error(exc.code, exc.message, exc.http_status)
		except Exception as exc:
			return error(getattr(exc, "code", None) or exc.__class__.__name__, cstr(exc), 400)

	return wrapper


def _current_guardian() -> dict:
	if frappe.session.user == "Guest":
		raise MobileDeviceError(DEVICE_AUTH_ERROR, _("Authentication required."), 401)
	guardian = get_guardian_by_user(frappe.session.user)
	if not guardian:
		raise MobileDeviceError(DEVICE_AUTH_ERROR, _("No Guardian is linked to the current user."), 401)
	return guardian


def _device_payload(doc) -> dict:
	return {
		"id": doc.name,
		"fcm_token": doc.fcm_token,
		"platform": doc.platform,
		"device_id": doc.device_id,
		"last_seen_at": cstr(doc.last_seen_at or ""),
		"disabled": bool(doc.disabled),
	}


@frappe.whitelist(methods=["POST"])
@_mobile_device_endpoint
def register_device(fcm_token=None, platform=None, device_id=None, **kwargs):
	guardian = _current_guardian()
	fcm_token = cstr(fcm_token).strip()
	if not fcm_token:
		raise MobileDeviceError(DEVICE_REQUEST_INVALID, _("FCM token is required."))

	name = frappe.db.get_value("Mobile Device", {"fcm_token": fcm_token}, "name")
	values = {
		"guardian": guardian.get("name"),
		"user": frappe.session.user,
		"fcm_token": fcm_token,
		"platform": cstr(platform or "unknown").strip().lower() or "unknown",
		"device_id": cstr(device_id).strip() or None,
		"last_seen_at": now_datetime(),
		"disabled": 0,
	}
	if name:
		frappe.db.set_value("Mobile Device", name, values, update_modified=True)
		doc = frappe.get_doc("Mobile Device", name)
	else:
		doc = frappe.get_doc({"doctype": "Mobile Device", **values})
		doc.insert(ignore_permissions=True)
	return _device_payload(doc)


@frappe.whitelist(methods=["POST", "DELETE"])
@_mobile_device_endpoint
def delete_device(fcm_token=None, **kwargs):
	guardian = _current_guardian()
	fcm_token = cstr(fcm_token).strip()
	if not fcm_token:
		raise MobileDeviceError(DEVICE_REQUEST_INVALID, _("FCM token is required."))

	name = frappe.db.get_value(
		"Mobile Device",
		{"fcm_token": fcm_token, "guardian": guardian.get("name")},
		"name",
	)
	if name:
		frappe.db.set_value("Mobile Device", name, "disabled", 1, update_modified=True)
	return {"fcm_token": fcm_token, "disabled": True}
