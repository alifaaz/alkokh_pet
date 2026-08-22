from __future__ import annotations

import functools

import frappe
from frappe import _
from frappe.utils import cstr
from frappe.utils.password import update_password
from werkzeug.security import generate_password_hash

from frappe.core.doctype.user.user import check_password as frappe_check_password
from pet_app.api.auth_mobile import (
	change_guardian_phone,
	request_guardian_phone_change_otp,
	validate_full_name,
	validate_iraqi_phone,
	validate_otp,
	validate_password,
)
from pet_app.api.mobile.auth import _revoke_oauth_tokens_for_user
from pet_app.api.mobile.files import MobileFileError, attach_public_image, get_first_uploaded_file
from pet_app.api.mobile.response import error, ok
from pet_app.utils.guardian_customer import (
	get_guardian_by_user,
	get_or_create_customer_from_guardian,
	sync_customer_from_guardian,
)


PROFILE_AUTH_ERROR = "auth.wrong_credentials"
PROFILE_REQUEST_INVALID = "profile.request_invalid"
PROFILE_NOT_FOUND = "profile.not_found"


class MobileProfileError(Exception):
	def __init__(self, code: str, message: str, http_status: int = 400):
		super().__init__(message)
		self.code = code
		self.message = message
		self.http_status = http_status


def _mobile_profile_endpoint(fn):
	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		kwargs.pop("cmd", None)
		try:
			return ok(fn(*args, **kwargs))
		except MobileProfileError as exc:
			return error(exc.code, exc.message, exc.http_status)
		except MobileFileError as exc:
			return error(exc.code, exc.message, exc.http_status)
		except frappe.PermissionError as exc:
			return error(PROFILE_AUTH_ERROR, cstr(exc) or _("Not permitted"), 401)
		except Exception as exc:
			return error(getattr(exc, "code", None) or exc.__class__.__name__, cstr(exc), 400)

	return wrapper


def _current_guardian() -> dict:
	if frappe.session.user == "Guest":
		raise MobileProfileError(PROFILE_AUTH_ERROR, _("Authentication required."), 401)
	guardian = get_guardian_by_user(frappe.session.user)
	if not guardian:
		raise MobileProfileError(PROFILE_AUTH_ERROR, _("No Guardian is linked to the current user."), 401)
	return guardian


def _doctype_has_field(doctype: str, fieldname: str) -> bool:
	return bool(frappe.get_meta(doctype).has_field(fieldname))


def _set_existing_values(doctype: str, name: str, values: dict, *, update_modified=True):
	existing = {field: value for field, value in values.items() if _doctype_has_field(doctype, field)}
	if existing:
		frappe.db.set_value(doctype, name, existing, update_modified=update_modified)


def _map_legacy_profile_error(message: str, code: str | None = None) -> tuple[str, int]:
	text = cstr(message).lower()
	if "already in use" in text:
		return "auth.phone_already_registered", 400
	if "otp expired" in text or "request new otp" in text or "request new one" in text:
		return "auth.otp_expired", 400
	if "invalid otp" in text or "too many attempts" in text:
		return "auth.invalid_otp", 400
	if "authentication required" in text or "not permitted" in text:
		return PROFILE_AUTH_ERROR, 401
	return code or PROFILE_REQUEST_INVALID, 400


def _legacy_result(result):
	if isinstance(result, dict) and result.get("ok") is True:
		return result.get("data") or {}
	if isinstance(result, dict) and result.get("ok") is False:
		errors = result.get("errors") or []
		message = errors[0].get("message") if errors else _("Request failed.")
		code, status = _map_legacy_profile_error(message, (result.get("meta") or {}).get("code"))
		raise MobileProfileError(code, message, status)
	return result or {}


def _primary_address(customer_id: str | None) -> dict:
	if not customer_id:
		return {}
	address_name = frappe.db.get_value(
		"Dynamic Link",
		{"link_doctype": "Customer", "link_name": customer_id, "parenttype": "Address"},
		"parent",
	)
	if not address_name:
		return {}
	fields = ["name", "address_line1", "address_line2", "city", "country", "is_primary_address"]
	# Guarded on meta, not assumed: between a code deploy and bench migrate the Custom
	# Field does not exist yet, and naming a missing column here would 500 the profile.
	if frappe.get_meta("Address").has_field("custom_notes"):
		fields.append("custom_notes")

	row = dict(
		frappe.db.get_value(
			"Address",
			address_name,
			fields,
			as_dict=True,
		)
		or {}
	)
	if "custom_notes" in row:
		row["notes"] = row.pop("custom_notes")
	return row


def _profile_payload(guardian: dict) -> dict:
	customer_id = guardian.get("customer_id")
	customer = (
		frappe.db.get_value("Customer", customer_id, ["name", "customer_name", "mobile_no", "email_id"], as_dict=True)
		if customer_id
		else None
	)
	return {
		"user": guardian.get("user_id"),
		"guardian_id": guardian.get("name"),
		"customer_id": customer_id,
		"full_name": guardian.get("full_name"),
		"phone": guardian.get("phone"),
		"email_id": guardian.get("email_id"),
		"city": guardian.get("city"),
		"country": guardian.get("country"),
		"address_line1": guardian.get("address_line1"),
		"guardian_image": guardian.get("guardian_image"),
		"customer": dict(customer or {}),
		"address": _primary_address(customer_id),
	}


def _avatar_payload(file_payload: dict, guardian: dict) -> dict:
	return {
		"file": file_payload,
		"guardian_image": file_payload.get("file_url"),
		"profile": _profile_payload(guardian),
	}


@frappe.whitelist(methods=["GET"])
@_mobile_profile_endpoint
def me(**kwargs):
	guardian = _current_guardian()
	return _profile_payload(guardian)


@frappe.whitelist(methods=["POST", "PATCH"])
@_mobile_profile_endpoint
def update_me(full_name=None, email_id=None, city=None, address_line1=None, country=None, **kwargs):
	guardian = _current_guardian()
	updates = {}
	if full_name is not None:
		updates["full_name"] = validate_full_name(full_name)
	if email_id is not None:
		updates["email_id"] = cstr(email_id).strip() or None
	if city is not None:
		updates["city"] = cstr(city).strip() or None
	if address_line1 is not None:
		updates["address_line1"] = cstr(address_line1).strip() or None
	if country is not None:
		updates["country"] = cstr(country).strip() or None

	if not updates:
		raise MobileProfileError(PROFILE_REQUEST_INVALID, _("No profile fields were supplied."))

	frappe.db.set_value("Guardian", guardian.get("name"), updates, update_modified=True)
	guardian = frappe.db.get_value("Guardian", guardian.get("name"), "*", as_dict=True)
	customer_id = get_or_create_customer_from_guardian(guardian)
	sync_customer_from_guardian(guardian, customer_id)

	if updates.get("full_name") and guardian.get("user_id"):
		frappe.db.set_value("User", guardian.get("user_id"), "first_name", updates["full_name"], update_modified=False)

	guardian = frappe.db.get_value("Guardian", guardian.get("name"), "*", as_dict=True)
	return _profile_payload(guardian)


@frappe.whitelist(methods=["POST"])
@_mobile_profile_endpoint
def change_password(current_password=None, new_password=None, **kwargs):
	guardian = _current_guardian()
	user = guardian.get("user_id")
	if not user:
		raise MobileProfileError(PROFILE_AUTH_ERROR, _("No User is linked to the current Guardian."), 401)

	new_password = validate_password(new_password)
	try:
		frappe_check_password(user, cstr(current_password))
	except Exception:
		raise MobileProfileError(PROFILE_AUTH_ERROR, _("Current password is incorrect."), 401)

	update_password(user, new_password, logout_all_sessions=True)
	_set_existing_values(
		"Guardian",
		guardian.get("name"),
		{
			"pending_password_hash": generate_password_hash(new_password),
			"wrong_attempts": 0,
			"otp_code": None,
			"otp_expires_at": None,
		},
		update_modified=False,
	)
	_revoke_oauth_tokens_for_user(user)
	return {}


@frappe.whitelist(methods=["POST"])
@_mobile_profile_endpoint
def phone_change_start(new_phone=None, phone=None, **kwargs):
	_current_guardian()
	new_phone = validate_iraqi_phone(new_phone or phone)
	data = _legacy_result(request_guardian_phone_change_otp(new_phone))
	return {
		"message": data.get("message") or "OTP sent",
		"guardian_id": data.get("guardian_id"),
		"new_phone": data.get("new_phone") or new_phone,
	}


@frappe.whitelist(methods=["POST"])
@_mobile_profile_endpoint
def phone_change_verify(otp=None, otp_code=None, new_phone=None, phone=None, **kwargs):
	guardian = _current_guardian()
	target_phone = validate_iraqi_phone(new_phone or phone or guardian.get("pending_phone_change"))
	otp_code = validate_otp(otp_code or otp)
	data = _legacy_result(change_guardian_phone(target_phone, otp_code))
	if guardian.get("user_id"):
		_revoke_oauth_tokens_for_user(guardian.get("user_id"))
	guardian = frappe.db.get_value("Guardian", guardian.get("name"), "*", as_dict=True)
	return {
		"old_phone": data.get("old_phone"),
		"new_phone": data.get("new_phone") or target_phone,
		"profile": _profile_payload(guardian),
	}


@frappe.whitelist(methods=["POST"])
@_mobile_profile_endpoint
def upload_avatar(**kwargs):
	guardian = _current_guardian()
	file_payload = attach_public_image(
		"Guardian",
		guardian.get("name"),
		"guardian_image",
		get_first_uploaded_file(),
		folder="Home/Guardian",
	)
	if guardian.get("user_id") and _doctype_has_field("User", "user_image"):
		frappe.db.set_value("User", guardian.get("user_id"), "user_image", file_payload.get("file_url"), update_modified=False)
	guardian = frappe.db.get_value("Guardian", guardian.get("name"), "*", as_dict=True)
	return _avatar_payload(file_payload, guardian)


@frappe.whitelist(methods=["POST", "DELETE"])
@_mobile_profile_endpoint
def delete_account(**kwargs):
	guardian = _current_guardian()
	guardian_name = guardian.get("name")
	user = guardian.get("user_id")
	customer_id = guardian.get("customer_id")
	deleted_phone = f"deleted-{guardian_name}"

	_set_existing_values(
		"Guardian",
		guardian_name,
		{
			"phone": deleted_phone,
			"full_name": "Deleted User",
			"email_id": None,
			"address_line1": None,
			"city": None,
			"country": None,
			"guardian_image": None,
			"is_active": 0,
			"otp_verified": 0,
			"otp_code": None,
			"otp_expires_at": None,
			"pending_password_hash": None,
			"pending_phone_change": None,
			"wrong_attempts": 0,
		},
		update_modified=True,
	)
	if customer_id and frappe.db.exists("Customer", customer_id):
		_set_existing_values(
			"Customer",
			customer_id,
			{
				"customer_name": "Deleted User",
				"mobile_no": None,
				"email_id": None,
			},
			update_modified=True,
		)
	if user and frappe.db.exists("User", user):
		_set_existing_values(
			"User",
			user,
			{
				"enabled": 0,
				"first_name": "Deleted User",
				"full_name": "Deleted User",
				"user_image": None,
			},
			update_modified=True,
		)
		_revoke_oauth_tokens_for_user(user)

	return {"deleted": True}
