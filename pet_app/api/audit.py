from __future__ import annotations

import frappe
from frappe.utils import cint, cstr, getdate

from pet_app.api.permissions import user_has_full_access
from pet_app.api.response import fail, ok


AUDIT_ROLES = {"System Manager", "Pet App Admin", "Auditor", "Accounts Manager", "Healthcare Administrator"}


@frappe.whitelist()
def list_audit_logs(event_type=None, reference_doctype=None, reference_name=None, user=None, date_from=None, date_to=None, limit=50):
	try:
		if not (user_has_full_access() or set(frappe.get_roles()) & AUDIT_ROLES):
			return fail("Not permitted", code="PERMISSION_ERROR")
		if not frappe.db.exists("DocType", "Pet App Audit Log"):
			return ok({"logs": []}, meta={"total": 0})
		filters = {}
		if event_type:
			filters["event_type"] = event_type
		if reference_doctype:
			filters["reference_doctype"] = reference_doctype
		if reference_name:
			filters["reference_name"] = reference_name
		if user:
			filters["user"] = user
		if date_from and date_to:
			filters["event_datetime"] = ["between", [f"{getdate(date_from)} 00:00:00", f"{getdate(date_to)} 23:59:59"]]
		elif date_from:
			filters["event_datetime"] = [">=", f"{getdate(date_from)} 00:00:00"]
		elif date_to:
			filters["event_datetime"] = ["<=", f"{getdate(date_to)} 23:59:59"]
		rows = frappe.get_all(
			"Pet App Audit Log",
			filters=filters,
			fields=[
				"name",
				"event_type",
				"event_datetime",
				"user",
				"reference_doctype",
				"reference_name",
				"details_json",
			],
			order_by="event_datetime desc, creation desc",
			limit_page_length=max(min(cint(limit) or 50, 500), 1),
			ignore_permissions=True,
		)
		return ok({"logs": [dict(row) for row in rows]}, meta={"total": len(rows)})
	except Exception as exc:
		return fail(cstr(exc), code=exc.__class__.__name__, details=frappe.get_traceback())
