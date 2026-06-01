from __future__ import annotations

import frappe


LEGACY_PERMISSION_DOCTYPES = (
	"Pet App Role Profile Workflow Rule",
	"Pet App Workflow Rule Resource",
	"Pet App Workflow Rule DocType",
	"Pet App Workflow Rule",
	"Pet App Permission Rule",
)

LEGACY_DOCTYPE_DELETE_ORDER = (
	"Pet App Role Profile Workflow Rule",
	"Pet App Workflow Rule",
	"Pet App Permission Rule",
	"Pet App Workflow Rule Resource",
	"Pet App Workflow Rule DocType",
)


def execute():
	for doctype in LEGACY_PERMISSION_DOCTYPES:
		_delete_doctype_records(doctype)
		_delete_custom_docperm_rows(doctype)

	for doctype in LEGACY_DOCTYPE_DELETE_ORDER:
		_delete_doctype(doctype)

	frappe.clear_cache()


def _delete_doctype_records(doctype: str):
	if not frappe.db.exists("DocType", doctype):
		return
	try:
		for name in frappe.get_all(doctype, pluck="name", ignore_permissions=True):
			frappe.delete_doc(doctype, name, ignore_permissions=True, force=True)
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"Failed deleting legacy records for {doctype}")


def _delete_custom_docperm_rows(doctype: str):
	if not frappe.db.exists("DocType", "Custom DocPerm"):
		return
	for name in frappe.get_all(
		"Custom DocPerm",
		filters={"parent": doctype},
		pluck="name",
		ignore_permissions=True,
	):
		frappe.delete_doc("Custom DocPerm", name, ignore_permissions=True, force=True)


def _delete_doctype(doctype: str):
	if not frappe.db.exists("DocType", doctype):
		return
	try:
		frappe.delete_doc("DocType", doctype, ignore_permissions=True, force=True)
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"Failed deleting legacy DocType {doctype}")
