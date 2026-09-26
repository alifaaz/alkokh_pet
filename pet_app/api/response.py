from __future__ import annotations

from functools import wraps

import frappe
from frappe.utils import cstr

from pet_app.utils.api_response import api_error, api_success


ENVELOPE_KEYS = {"ok", "data", "meta", "errors"}


def ok(data=None, meta=None) -> dict:
	return api_success(data=data, meta=meta)


def fail(message=None, *, code: str = "ERROR", details=None, data=None) -> dict:
	response = api_error(message=message, code=code, details=details)
	if data:
		response["data"] = data
	return response


ACTION_SAVEPOINT = "pet_app_action"
ACTION_FLAG = "pet_app_action_savepoint"


def begin_action():
	"""Open a savepoint for one write action, so a failure can undo all of it.

	These endpoints catch their own exceptions and return a `fail(...)` envelope. That is a
	normal return, so the request still commits whatever ran before the error - which is how
	a cancelled plan item ended up saved while the follow-up sync that failed after it did
	not (PCPI-2026-01265). Pair with `rollback_action()` in the endpoint's error handler.
	"""
	frappe.db.savepoint(ACTION_SAVEPOINT)
	frappe.flags[ACTION_FLAG] = True


def rollback_action():
	"""Undo everything written since `begin_action()`. Safe to call when none was opened."""
	if not frappe.flags.get(ACTION_FLAG):
		return
	frappe.flags[ACTION_FLAG] = False
	try:
		frappe.db.rollback(save_point=ACTION_SAVEPOINT)
	except Exception:
		# Never mask the original error with a rollback failure; the request-level
		# rollback on an uncaught error is the backstop.
		frappe.log_error(frappe.get_traceback(), "pet_app action rollback failed")


def is_standard_envelope(value) -> bool:
	return isinstance(value, dict) and ENVELOPE_KEYS.issubset(value.keys())


def ok_response(data=None, meta=None, *, legacy: bool = False) -> dict:
	if is_standard_envelope(data):
		return data
	if data is None:
		payload = {}
	elif isinstance(data, dict):
		payload = data
	elif isinstance(data, list):
		payload = {"items": data, "result": data}
	else:
		payload = {"result": data}
	response = ok(payload, meta=meta)
	if legacy and isinstance(payload, dict):
		for key, value in payload.items():
			if key not in ENVELOPE_KEYS:
				response.setdefault(key, value)
	return response


def error_response(message=None, *, code: str = "ERROR", details=None) -> dict:
	return fail(message=message, code=code, details=details)


def standardize_response(fn):
	@wraps(fn)
	def wrapper(*args, **kwargs):
		try:
			result = fn(*args, **kwargs)
			response = getattr(frappe.local, "response", None)
			if result is None and isinstance(response, dict) and "data" in response:
				result = response.pop("data")
			return ok_response(result, legacy=True)
		except Exception as exc:
			return error_response(
				cstr(exc),
				code=getattr(exc, "code", None) or exc.__class__.__name__,
				details=frappe.get_traceback(),
			)

	return wrapper
