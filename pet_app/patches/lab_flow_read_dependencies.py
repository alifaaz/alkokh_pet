from __future__ import annotations

import frappe
from frappe.permissions import add_permission, update_permission_property


LAB_FLOW_READ_DEPENDENCIES = (
	"Lab",
	"Imaging",
	"PetCareService",
	"CareService template",
	"CategoryCareServices",
	"Pet",
	"Guardian",
	"Healthcare Practitioner",
	"Vet Visit",
)

LAB_FLOW_READ_ROLES = (
	"Laboratory User",
	"Visit",
	"Healthcare",
	"Healthcare Practitioner",
)

READ_ONLY_MUTATING_PERMISSIONS = (
	"create",
	"write",
	"delete",
	"submit",
	"cancel",
	"amend",
)


def execute():
	ensure_roles()
	ensure_read_dependencies()
	frappe.clear_cache(doctype="Role")
	for doctype in LAB_FLOW_READ_DEPENDENCIES:
		if frappe.db.exists("DocType", doctype):
			frappe.clear_cache(doctype=doctype)


def ensure_roles():
	for role_name in LAB_FLOW_READ_ROLES:
		if frappe.db.exists("Role", role_name):
			continue
		frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": role_name,
				"desk_access": 1,
				"is_custom": 1,
			}
		).insert(ignore_permissions=True)


def ensure_read_dependencies():
	for role_name in LAB_FLOW_READ_ROLES:
		ensure_role_read_dependencies(role_name)

	for doctype in LAB_FLOW_READ_DEPENDENCIES:
		if frappe.db.exists("DocType", doctype):
			ensure_laboratory_user_is_read_only(doctype)


def ensure_role_read_dependencies(role_name: str, read_only: bool = False):
	if not frappe.db.exists("Role", role_name):
		return
	for doctype in LAB_FLOW_READ_DEPENDENCIES:
		if not frappe.db.exists("DocType", doctype):
			continue
		ensure_read_permission(doctype, role_name)
		if read_only:
			for ptype in READ_ONLY_MUTATING_PERMISSIONS:
				update_permission_property(doctype, role_name, 0, ptype, 0)
		frappe.clear_cache(doctype=doctype)


def ensure_read_permission(doctype: str, role_name: str):
	if not has_role_perm(doctype, role_name):
		add_permission(doctype, role_name, 0)
	update_permission_property(doctype, role_name, 0, "read", 1)


def ensure_laboratory_user_is_read_only(doctype: str):
	if not has_role_perm(doctype, "Laboratory User"):
		return
	for ptype in READ_ONLY_MUTATING_PERMISSIONS:
		update_permission_property(doctype, "Laboratory User", 0, ptype, 0)


def has_role_perm(doctype: str, role_name: str) -> bool:
	return bool(
		frappe.db.exists(
			"Custom DocPerm",
			{
				"parent": doctype,
				"parenttype": "DocType",
				"role": role_name,
				"permlevel": 0,
				"if_owner": 0,
			},
		)
		or frappe.db.exists(
			"DocPerm",
			{
				"parent": doctype,
				"role": role_name,
				"permlevel": 0,
				"if_owner": 0,
			},
		)
	)
