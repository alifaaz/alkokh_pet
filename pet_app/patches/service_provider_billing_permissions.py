"""Restore invoicing capability to the service-provider roles.

Service providers billed 292 Sales Invoices between 19 May and 11 June 2026. The June
permission migration moved them onto a page-based role model and never carried the
invoicing permission across, so the capability disappeared silently: the work carried on
(1,935 services completed since) and the billing simply stopped.

The permission had come from `POS Service Provider`, which granted sixteen doctypes
including Payment Entry, POS Opening/Closing Entry, POS Profile, Sales Order, and
write+create on Item, Item Price and Customer. Only one of those is billing. This grants
that one and nothing else.

No `submit`. The observed path always inserted a DRAFT - 288 of the 292 are still drafts,
and the four submitted ones were submitted between 2 and 23 days later, by someone else.
Submission stays a separate action requiring a separate role.

Registered AFTER operational_healthcare_roles, remove_legacy_permission_doctypes and
cleanup_legacy_roles in patches.txt: all three delete Custom DocPerm rows, and a grant
that runs before them does not survive.

Idempotent. The same rows are also exported to fixtures/role.json and
fixtures/custom_docperm.json so a fresh site gets them declaratively - a hand-made Custom
DocPerm with no source of truth behind it is what evaporated the first time.
"""

from __future__ import annotations

import frappe
from frappe.permissions import setup_custom_perms
from frappe.utils import cint


# Full intended state per row, not a delta: every managed flag is stated so a row that
# has drifted (a stray submit, say) is corrected rather than left alone.
ROLE_PERMISSIONS = {
	"Service Provider": {
		# The billing grant. read + write + create so a provider can raise a draft and
		# append to one they already opened. Explicitly NOT submit/cancel/amend/delete.
		"Sales Invoice": {
			"read": 1, "write": 1, "create": 1,
			"delete": 0, "submit": 0, "cancel": 0, "amend": 0,
		},
		# Required by the invoice itself, not a convenience. ERPNext's set_address_details
		# calls render_address(check_permissions=True) during validate, which runs
		# Address.check_permission() on the customer's billing address. The only Address
		# grant everyone holds is the `All` role's, and that row is if_owner = 1 - these
		# addresses belong to reception staff, so a provider reading one fails the
		# DOCUMENT-level check and the whole insert dies with a bare PermissionError.
		# Empirically confirmed: without this, all three test inserts failed here.
		# READ ONLY - a provider never writes an address.
		"Address": {
			"read": 1, "write": 0, "create": 0,
			"delete": 0, "submit": 0, "cancel": 0, "amend": 0,
		},
	},
	"Service Provider Update": {
		# Unrelated to billing: this row carried write + submit with read = 0, which is
		# incoherent on its own - nothing can edit what it cannot read.
		"PetCareService": {
			"read": 1, "write": 1, "submit": 1,
			"create": 0, "delete": 0, "cancel": 0, "amend": 0,
		},
	},
}

MANAGED_FLAGS = ("read", "write", "create", "delete", "submit", "cancel", "amend")


def execute():
	for role, doctype_permissions in ROLE_PERMISSIONS.items():
		if not frappe.db.exists("Role", role):
			continue
		for doctype, rights in doctype_permissions.items():
			if not frappe.db.exists("DocType", doctype):
				continue
			apply_permission(doctype, role, rights)

	frappe.clear_cache()


def apply_permission(doctype: str, role: str, rights: dict) -> str:
	"""Set one Custom DocPerm row to exactly `rights`, creating it if absent.

	setup_custom_perms() FIRST, always. Frappe ignores the standard DocPerm table
	entirely once a single Custom DocPerm exists for a doctype
	(frappe/permissions.py: get_valid_perms), so inserting one row into a doctype that
	has none would silently strip every other role's access to it. setup_custom_perms
	materialises the standard rows first and is a no-op when they already exist.
	"""
	setup_custom_perms(doctype)

	rights = {k: cint(v) for k, v in rights.items() if k in MANAGED_FLAGS}
	if not is_submittable(doctype):
		for flag in ("submit", "cancel", "amend"):
			rights.pop(flag, None)

	name = frappe.db.get_value(
		"Custom DocPerm",
		{"parent": doctype, "role": role, "permlevel": 0, "if_owner": 0},
	)

	if name:
		current = frappe.db.get_value("Custom DocPerm", name, list(rights), as_dict=True) or {}
		changed = {k: v for k, v in rights.items() if cint(current.get(k)) != v}
		if changed:
			frappe.db.set_value("Custom DocPerm", name, changed, update_modified=False)
		return name

	doc = frappe.get_doc(
		{
			"doctype": "Custom DocPerm",
			"parent": doctype,
			"parenttype": "DocType",
			"parentfield": "permissions",
			"role": role,
			"permlevel": 0,
			"if_owner": 0,
			**rights,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def is_submittable(doctype: str) -> bool:
	return bool(cint(frappe.db.get_value("DocType", doctype, "is_submittable")))
