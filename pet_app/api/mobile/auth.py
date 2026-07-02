import functools
import json
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.integrations.oauth2 import get_oauth_server
from frappe.utils.password import update_password
from oauthlib.oauth2 import FatalClientError, OAuth2Error
from werkzeug.security import generate_password_hash

from pet_app.api.auth_api import login_and_get_oauth_token
from pet_app.api.auth_mobile import (
    forgot_password,
    get_guardian,
    guardian_customer_locks,
    is_debug_mode,
    login,
    register_guardian,
    send_otp,
    validate_full_name,
    validate_iraqi_phone,
    validate_otp,
    validate_password,
    verify_otp,
)
from pet_app.api.mobile.response import error, ok


MOBILE_OAUTH_APP_NAME = "Alkokh Mobile"
MOBILE_OAUTH_SCOPE = "all openid"
RESET_TOKEN_TTL_SECONDS = 15 * 60


class MobileAuthError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def _mobile_endpoint(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        kwargs.pop("cmd", None)
        try:
            return fn(*args, **kwargs)
        except MobileAuthError as exc:
            return error(exc.code, exc.message, exc.http_status)
        except Exception as exc:
            code = _map_error_code(exc)
            status = 401 if code in {"auth.wrong_credentials", "auth.token_invalid"} else 400
            return error(code, frappe.utils.cstr(exc), status)

    return wrapper


def _map_error_code(exc) -> str:
    message = frappe.utils.cstr(exc).lower()
    if "otp expired" in message or "request new one" in message:
        return "auth.otp_expired"
    if "invalid otp" in message or "too many attempts" in message:
        return "auth.invalid_otp"
    if "already verified" in message or "already registered" in message or "login instead" in message:
        return "auth.phone_already_registered"
    if (
        "invalid phone or password" in message
        or "invalid login" in message
        or "invalid credentials" in message
        or "phone not found" in message
        or "verify phone first" in message
    ):
        return "auth.wrong_credentials"
    if "invalid_grant" in message or "invalid token" in message or "refresh_token" in message:
        return "auth.token_invalid"
    return getattr(exc, "code", None) or exc.__class__.__name__


def _legacy_result(result):
    if isinstance(result, dict) and result.get("ok") is False:
        errors = result.get("errors") or []
        message = errors[0].get("message") if errors else _("Request failed.")
        raise MobileAuthError(_map_error_code(Exception(message)), message)
    if isinstance(result, dict) and result.get("ok") is True:
        return result.get("data") or {}
    return result or {}


def _get_mobile_client_id() -> str:
    client_id = frappe.db.get_value("OAuth Client", {"app_name": MOBILE_OAUTH_APP_NAME}, "name")
    if not client_id:
        raise MobileAuthError("auth.token_invalid", _("Mobile OAuth client is not configured."), 500)
    return client_id


def _request_url(default_method: str) -> tuple[str, str]:
    request = getattr(frappe.local, "request", None)
    if request:
        return request.url, request.method
    return "http://localhost/api/method/pet_app.api.mobile.auth." + default_method, "POST"


def _extract_oauth_payload(response) -> dict:
    payload = dict(response or {})
    status = payload.pop("http_status_code", 200)
    if payload.get("error"):
        raise MobileAuthError("auth.token_invalid", payload.get("error_description") or payload["error"], status)
    return {
        "access_token": payload.get("access_token"),
        "refresh_token": payload.get("refresh_token"),
        "expires_in": payload.get("expires_in") or 3600,
        "token_type": payload.get("token_type") or "Bearer",
        "user": payload.get("user"),
        "full_name": payload.get("full_name"),
    }


def _issue_password_token(phone: str, password: str) -> dict:
    client_id = _get_mobile_client_id()
    original_response = frappe.local.response
    frappe.local.response = frappe._dict({})
    try:
        if getattr(frappe.local, "request", None):
            login_and_get_oauth_token(
                username=phone,
                password=password,
                client_id=client_id,
                scope=MOBILE_OAUTH_SCOPE,
                grant_type="password",
            )
            return _extract_oauth_payload(frappe.local.response)

        guardian = get_guardian(phone)
        if not guardian or not guardian.get("user_id"):
            raise MobileAuthError("auth.wrong_credentials", _("Invalid phone or password"), 401)

        return _create_oauth_token(
            {
                "grant_type": "password",
                "username": guardian.get("user_id"),
                "password": password,
                "client_id": client_id,
                "scope": MOBILE_OAUTH_SCOPE,
            },
            "sign_in",
        )
    finally:
        frappe.local.response = original_response


def _create_oauth_token(token_request: dict, method_name: str) -> dict:
    uri, http_method = _request_url(method_name)
    had_request = hasattr(frappe.local, "request")
    original_request = getattr(frappe.local, "request", None) if had_request else None
    had_cookie_manager = hasattr(frappe.local, "cookie_manager")
    original_cookie_manager = getattr(frappe.local, "cookie_manager", None) if had_cookie_manager else None
    had_request_ip = hasattr(frappe.local, "request_ip")
    original_request_ip = getattr(frappe.local, "request_ip", None) if had_request_ip else None
    had_form_dict = hasattr(frappe.local, "form_dict")
    original_form_dict = getattr(frappe.local, "form_dict", None) if had_form_dict else None
    if not had_request:
        frappe.local.request = frappe._dict(
            {
                "path": f"/api/method/pet_app.api.mobile.auth.{method_name}",
                "cookies": {},
                "headers": {},
            }
        )
    if not had_cookie_manager:
        from frappe.auth import CookieManager

        frappe.local.cookie_manager = CookieManager()
    if not frappe.local.request_ip:
        frappe.local.request_ip = "127.0.0.1"
    if not getattr(frappe.local, "form_dict", None):
        frappe.local.form_dict = frappe._dict({})

    try:
        _headers, body, status = get_oauth_server().create_token_response(
            uri=uri,
            http_method=http_method,
            body=urlencode(token_request),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            credentials=getattr(frappe.flags, "oauth_credentials", None),
        )
    except (FatalClientError, OAuth2Error) as exc:
        raise MobileAuthError("auth.token_invalid", frappe.utils.cstr(exc), 401)
    finally:
        if had_request:
            frappe.local.request = original_request
        else:
            del frappe.local.request
        if had_cookie_manager:
            frappe.local.cookie_manager = original_cookie_manager
        else:
            del frappe.local.cookie_manager
        if had_request_ip:
            frappe.local.request_ip = original_request_ip
        else:
            del frappe.local.request_ip
        if had_form_dict:
            frappe.local.form_dict = original_form_dict
        else:
            del frappe.local.form_dict

    payload = frappe._dict(json.loads(body or "{}"))
    payload["http_status_code"] = status or 200
    return _extract_oauth_payload(payload)


def _set_signup_password(phone: str, password: str, full_name: str) -> None:
    frappe.db.set_value(
        "Guardian",
        {"phone": phone},
        {
            "full_name": full_name,
            "pending_password_hash": generate_password_hash(password),
        },
        update_modified=False,
    )


def _ensure_signup_guardian(phone: str) -> None:
    if get_guardian(phone):
        return

    with guardian_customer_locks([phone]):
        if get_guardian(phone):
            return
        guardian = frappe.get_doc(
            {
                "doctype": "Guardian",
                "phone": phone,
                "is_active": 0,
                "otp_verified": 0,
                "wrong_attempts": 0,
                "user_id": None,
                "customer_id": None,
            }
        )
        guardian.flags.skip_guardian_customer_auto_create = True
        guardian.insert(ignore_permissions=True)


def _verify_password_reset_otp(phone: str, otp: str) -> None:
    guardian = get_guardian(phone)
    if not guardian or not guardian.get("otp_verified") or not guardian.get("user_id"):
        raise MobileAuthError("auth.wrong_credentials", _("Invalid phone"), 401)
    if guardian.get("pending_phone_change"):
        raise MobileAuthError("auth.invalid_otp", _("This OTP is for phone change."))
    if not guardian.get("otp_code"):
        raise MobileAuthError("auth.otp_expired", _("OTP expired. Request new one"))
    if guardian.get("otp_expires_at") and frappe.utils.now_datetime() > guardian.get("otp_expires_at"):
        raise MobileAuthError("auth.otp_expired", _("OTP expired. Request new one"))
    if guardian.get("otp_code") != str(otp):
        raise MobileAuthError("auth.invalid_otp", _("Invalid OTP"))


def _revoke_oauth_tokens_for_user(user: str) -> None:
    frappe.db.set_value("OAuth Bearer Token", {"user": user, "status": "Active"}, "status", "Revoked")


def _reset_token_key(token: str) -> str:
    return f"mobile_password_reset:{token}"


@frappe.whitelist(allow_guest=True, methods=["POST"])
@_mobile_endpoint
def sign_up_start(phone, **kwargs):
    phone = validate_iraqi_phone(phone)
    guardian = get_guardian(phone)
    if guardian and guardian.get("otp_verified"):
        raise MobileAuthError("auth.phone_already_registered", _("Phone is already registered."))

    _ensure_signup_guardian(phone)
    _legacy_result(send_otp(phone))
    return ok({"message": "OTP sent"})


@frappe.whitelist(allow_guest=True, methods=["POST"])
@_mobile_endpoint
def sign_up_verify(phone, otp, password, full_name, **kwargs):
    phone = validate_iraqi_phone(phone)
    otp = validate_otp(otp)
    password = validate_password(password)
    full_name = validate_full_name(full_name)

    guardian = get_guardian(phone)
    if guardian and guardian.get("otp_verified"):
        raise MobileAuthError("auth.phone_already_registered", _("Phone is already registered."))
    if not guardian:
        _legacy_result(register_guardian(phone, password, full_name))
        raise MobileAuthError("auth.otp_expired", _("OTP sent. Verify with the latest OTP."))

    _set_signup_password(phone, password, full_name)
    _legacy_result(verify_otp(phone, otp, password))
    return ok(_issue_password_token(phone, password))


@frappe.whitelist(allow_guest=True, methods=["POST"])
@_mobile_endpoint
def sign_in(phone, password, device_id=None, **kwargs):
    phone = validate_iraqi_phone(phone)
    password = str(password or "").strip()
    _legacy_result(login(phone, password))
    return ok(_issue_password_token(phone, password))


@frappe.whitelist(allow_guest=True, methods=["POST"])
@_mobile_endpoint
def refresh(refresh_token, **kwargs):
    refresh_token = (refresh_token or "").strip()
    if not refresh_token:
        raise MobileAuthError("auth.token_invalid", _("Refresh token is required."), 401)

    token = _create_oauth_token(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": _get_mobile_client_id(),
        },
        "refresh",
    )
    return ok(token)


@frappe.whitelist(allow_guest=True, methods=["POST"])
@_mobile_endpoint
def sign_out(refresh_token, **kwargs):
    refresh_token = (refresh_token or "").strip()
    if not refresh_token:
        raise MobileAuthError("auth.token_invalid", _("Refresh token is required."), 401)

    token_name = frappe.db.get_value("OAuth Bearer Token", {"refresh_token": refresh_token}, "name")
    if not token_name:
        raise MobileAuthError("auth.token_invalid", _("Invalid refresh token."), 401)

    frappe.db.set_value("OAuth Bearer Token", token_name, "status", "Revoked")
    return ok()


@frappe.whitelist(allow_guest=True, methods=["POST"])
@_mobile_endpoint
def password_reset_request(phone, **kwargs):
    phone = validate_iraqi_phone(phone)
    _legacy_result(forgot_password(phone))
    return ok({"message": "OTP sent"})


@frappe.whitelist(allow_guest=True, methods=["POST"])
@_mobile_endpoint
def password_reset_verify(phone, otp, **kwargs):
    phone = validate_iraqi_phone(phone)
    otp = validate_otp(otp)
    _verify_password_reset_otp(phone, otp)

    reset_token = frappe.generate_hash(length=32)
    frappe.cache.set_value(
        _reset_token_key(reset_token),
        {"phone": phone, "verified": True},
        expires_in_sec=RESET_TOKEN_TTL_SECONDS,
    )
    return ok({"reset_token": reset_token})


@frappe.whitelist(allow_guest=True, methods=["POST"])
@_mobile_endpoint
def password_reset_confirm(reset_token, new_password, **kwargs):
    reset_token = (reset_token or "").strip()
    new_password = validate_password(new_password)
    cached = frappe.cache.get_value(_reset_token_key(reset_token), expires=True)
    if not cached or not cached.get("verified") or not cached.get("phone"):
        raise MobileAuthError("auth.token_invalid", _("Invalid or expired reset token."), 401)

    phone = cached["phone"]
    guardian = get_guardian(phone)
    if not guardian or not guardian.get("user_id"):
        raise MobileAuthError("auth.token_invalid", _("Invalid or expired reset token."), 401)

    user = guardian.get("user_id")
    update_password(user, new_password, logout_all_sessions=True)
    guardian_updates = {
        "pending_password_hash": generate_password_hash(new_password),
        "wrong_attempts": 0,
        "otp_code": None,
        "otp_expires_at": None,
    }
    if frappe.db.has_column("Guardian", "pending_phone_change"):
        guardian_updates["pending_phone_change"] = None
    frappe.db.set_value("Guardian", guardian.get("name"), guardian_updates, update_modified=False)
    _revoke_oauth_tokens_for_user(user)
    frappe.cache.delete_value(_reset_token_key(reset_token))
    return ok()
