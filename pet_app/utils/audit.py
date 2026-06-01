from __future__ import annotations

import json

import frappe
from frappe.utils import now_datetime


def log_event(event_type: str, *, reference_doctype=None, reference_name=None, before=None, after=None, details=None, user=None):
	if not frappe.db.exists("DocType", "Pet App Audit Log"):
		return None
	try:
		doc = frappe.get_doc(
			{
				"doctype": "Pet App Audit Log",
				"event_type": event_type,
				"reference_doctype": reference_doctype,
				"reference_name": reference_name,
				"user": user or frappe.session.user,
				"before_json": _json(before),
				"after_json": _json(after),
				"details_json": _json(details),
				"created_at": now_datetime(),
			}
		)
		doc.insert(ignore_permissions=True)
		return doc.name
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Pet App Audit Log failed")
		return None


def _json(value):
	if value in (None, ""):
		return None
	if isinstance(value, str):
		return value
	return json.dumps(value, default=str, ensure_ascii=False)

