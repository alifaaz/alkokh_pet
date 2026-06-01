from __future__ import annotations

import hashlib
import json
from collections.abc import Callable

import frappe
from frappe import _
from frappe.utils import cstr, now_datetime

from pet_app.api.response import fail


def request_hash(payload) -> str:
	return hashlib.sha256(json.dumps(payload or {}, sort_keys=True, default=str).encode()).hexdigest()


def run_idempotent(endpoint: str, payload: dict, handler: Callable[[], dict], *, idempotency_key=None, client_request_id=None):
	key = cstr(idempotency_key or payload.get("idempotency_key")).strip()
	if not key:
		return handler()
	if not frappe.db.exists("DocType", "Pet App Offline Request"):
		return handler()

	current_hash = request_hash(payload)
	existing = frappe.db.get_value(
		"Pet App Offline Request",
		{"idempotency_key": key},
		["name", "request_hash", "status", "response_json"],
		as_dict=True,
	)
	if existing:
		if existing.request_hash and existing.request_hash != current_hash:
			return fail(_("Idempotency key was already used with a different payload."), code="CONFLICT")
		if existing.status == "Completed" and existing.response_json:
			return json.loads(existing.response_json)

	doc = frappe.get_doc(
		{
			"doctype": "Pet App Offline Request",
			"idempotency_key": key,
			"client_request_id": client_request_id or payload.get("client_request_id"),
			"user": frappe.session.user,
			"endpoint": endpoint,
			"request_hash": current_hash,
			"status": "Processing",
			"last_synced_at": now_datetime(),
		}
	)
	if existing:
		doc = frappe.get_doc("Pet App Offline Request", existing.name)
		doc.status = "Processing"
	else:
		doc.insert(ignore_permissions=True)
	response = handler()
	doc.status = "Completed" if response.get("ok") else "Failed"
	doc.response_json = json.dumps(response, default=str, ensure_ascii=False)
	doc.last_synced_at = now_datetime()
	doc.save(ignore_permissions=True)
	return response

