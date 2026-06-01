from __future__ import annotations

from collections.abc import Callable

import frappe
from frappe.utils import cstr

from pet_app.api.response import fail, ok


def normalize_response(value) -> dict:
	if isinstance(value, dict) and {"ok", "data", "meta", "errors"}.issubset(value.keys()):
		return value
	if value is None:
		data = frappe.response.get("data") or frappe.response.get("message") or {}
		return ok(data if isinstance(data, dict) else {"result": data})
	return ok(value if isinstance(value, dict) else {"result": value})


def call_api(fn: Callable, *args, **kwargs) -> dict:
	try:
		return normalize_response(fn(*args, **kwargs))
	except Exception as exc:
		code = "PERMISSION_ERROR" if isinstance(exc, frappe.PermissionError) else exc.__class__.__name__
		return fail(cstr(exc), code=code, details=frappe.get_traceback())
