from __future__ import annotations

import frappe
from frappe import _


class PetAppAPIError(frappe.ValidationError):
	def __init__(self, message: str, code: str | None = None, details=None):
		super().__init__(message)
		self.code = code or "ERROR"
		self.details = details or {}


def api_success(data=None, meta=None) -> dict:
	return {
		"ok": True,
		"data": data or {},
		"meta": meta or {},
		"errors": [],
	}


def api_error(message, code: str | None = None, details=None) -> dict:
	return {
		"ok": False,
		"data": {},
		"meta": {"code": code or "ERROR"},
		"errors": [
			{
				"message": message or _("Request failed."),
				"details": details or {},
			}
		],
	}


def ok_response(data=None, meta=None) -> dict:
	return api_success(data=data, meta=meta)


def error_response(message=None, code: str | None = None, details=None) -> dict:
	return api_error(message=message, code=code, details=details)


def raise_api_error(message, code: str | None = None, details=None):
	raise PetAppAPIError(message or _("Request failed."), code=code, details=details)
