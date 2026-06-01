from __future__ import annotations

import json

import frappe
from frappe.utils import cint, cstr, get_datetime

from pet_app.api.response import fail, ok


@frappe.whitelist()
def get_offline_request(idempotency_key=None, client_request_id=None):
	try:
		if not frappe.db.exists("DocType", "Pet App Offline Request"):
			return fail("Offline request tracking is not installed.", code="NOT_INSTALLED")
		filters = {}
		if idempotency_key:
			filters["idempotency_key"] = idempotency_key
		if client_request_id:
			filters["client_request_id"] = client_request_id
		if not filters:
			return fail("idempotency_key or client_request_id is required.", code="VALIDATION_ERROR")
		name = frappe.db.get_value("Pet App Offline Request", filters, "name")
		if not name:
			return fail("Offline request was not found.", code="NOT_FOUND")
		doc = frappe.get_doc("Pet App Offline Request", name)
		_assert_owner(doc.user)
		return ok({"request": _payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def list_offline_requests(last_synced_at=None, limit=50):
	try:
		if not frappe.db.exists("DocType", "Pet App Offline Request"):
			return ok({"requests": []}, meta={"total": 0})
		filters = {"user": frappe.session.user}
		if last_synced_at:
			filters["modified"] = [">", get_datetime(last_synced_at)]
		rows = frappe.get_all(
			"Pet App Offline Request",
			filters=filters,
			fields=[
				"name",
				"idempotency_key",
				"client_request_id",
				"endpoint",
				"status",
				"last_synced_at",
				"modified",
			],
			order_by="modified desc",
			limit_page_length=max(min(cint(limit) or 50, 200), 1),
			ignore_permissions=True,
		)
		return ok({"requests": [dict(row) for row in rows]}, meta={"total": len(rows)})
	except Exception as exc:
		return _error_response(exc)


def _payload(doc) -> dict:
	response = {}
	if doc.response_json:
		try:
			response = json.loads(doc.response_json)
		except Exception:
			response = {"raw": doc.response_json}
	return {
		"name": doc.name,
		"idempotency_key": doc.idempotency_key,
		"client_request_id": doc.client_request_id,
		"endpoint": doc.endpoint,
		"status": doc.status,
		"request_hash": doc.request_hash,
		"last_synced_at": doc.last_synced_at,
		"response": response,
	}


def _assert_owner(user):
	if frappe.session.user == "Administrator" or "System Manager" in frappe.get_roles():
		return
	if user != frappe.session.user:
		frappe.throw("Not permitted", frappe.PermissionError)


def _error_response(exc):
	code = "PERMISSION_ERROR" if isinstance(exc, frappe.PermissionError) else exc.__class__.__name__
	return fail(cstr(exc), code=code, details=frappe.get_traceback())
