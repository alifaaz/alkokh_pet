from __future__ import annotations

from urllib.parse import unquote

import frappe
from werkzeug.wrappers import Response


API_PATH_PREFIX = "/api/"
METHOD_PATH_PREFIX = "/api/method/"
RESOURCE_PATH_PREFIX = "/api/resource/"
GUEST_IDENTITY_COOKIE_KEYS = ("sid", "user_id", "full_name", "system_user", "user_lang")
CORS_PREFLIGHT_MAX_AGE_SECONDS = "600"


def enforce_authenticated_api_access():
	request = getattr(frappe.local, "request", None)
	if not _should_guard_request(request):
		return
	if _current_user() != "Guest":
		return
	if _is_guest_allowed_api_request(request):
		return

	_scrub_queued_guest_identity_cookies()
	raise frappe.AuthenticationError("Authentication required")


def after_request(response: Response | None = None, request=None):
	request = request or getattr(frappe.local, "request", None)
	if not _is_api_request(request):
		return

	_scrub_queued_guest_identity_cookies()
	_set_api_preflight_max_age(response, request)


def _should_guard_request(request) -> bool:
	if not _is_api_request(request):
		return False
	if getattr(request, "method", "").upper() == "OPTIONS":
		return False
	return True


def _is_api_request(request) -> bool:
	return bool(request and _request_path(request).startswith(API_PATH_PREFIX))


def _request_path(request) -> str:
	return getattr(request, "path", "") or ""


def _current_user() -> str | None:
	try:
		return frappe.session.user
	except Exception:
		return None


def _is_guest_allowed_api_request(request) -> bool:
	path = _request_path(request)
	if path.startswith(METHOD_PATH_PREFIX):
		return _is_guest_allowed_method(_method_name_from_path(path))
	if path.startswith(RESOURCE_PATH_PREFIX):
		return _is_guest_allowed_resource(_resource_doctype_from_path(path))
	return False


def _method_name_from_path(path: str) -> str:
	method = path.removeprefix(METHOD_PATH_PREFIX)
	return unquote(method.split("/", 1)[0])


def _resource_doctype_from_path(path: str) -> str:
	doctype = path.removeprefix(RESOURCE_PATH_PREFIX)
	return unquote(doctype.split("/", 1)[0])


def _is_guest_allowed_method(method_name: str) -> bool:
	if not method_name:
		return False
	try:
		method = frappe.get_attr(frappe.override_whitelisted_method(method_name))
	except Exception:
		return False
	method = getattr(method, "__func__", method)
	return method in frappe.guest_methods


def _is_guest_allowed_resource(doctype: str) -> bool:
	if not doctype:
		return False
	try:
		return bool(frappe.db.get_value("DocType", doctype, "allow_guest_to_view"))
	except Exception:
		return False


def _scrub_queued_guest_identity_cookies():
	cookie_manager = getattr(frappe.local, "cookie_manager", None)
	if not cookie_manager or not getattr(cookie_manager, "cookies", None):
		return

	cookies = cookie_manager.cookies
	if not _has_queued_guest_identity(cookies):
		return

	for key in GUEST_IDENTITY_COOKIE_KEYS:
		cookies.pop(key, None)


def _has_queued_guest_identity(cookies: dict) -> bool:
	return any(
		(
			cookies.get("sid", {}).get("value") == "Guest",
			cookies.get("user_id", {}).get("value") == "Guest",
			cookies.get("full_name", {}).get("value") == "Guest",
		)
	)


def _set_api_preflight_max_age(response: Response | None, request):
	if not response or getattr(request, "method", "").upper() != "OPTIONS":
		return
	if not getattr(request, "headers", {}).get("Origin"):
		return
	response.headers["Access-Control-Max-Age"] = CORS_PREFLIGHT_MAX_AGE_SECONDS
