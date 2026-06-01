from __future__ import annotations

from collections import defaultdict

import frappe
from frappe.utils import cint


APPROVED_ROLE_MIGRATIONS = {
	"Healthcare": "Doctor",
	"Healthcare User": "Doctor",
	"Healthcare Practitioner": "Doctor",
	"Physician": "Doctor",
	"Healthcare Administrator": "Pet App Admin",
	"Visit": "Visit Admin",
	"Accounting": "Accounts Manager",
	"Accounting Admin": "Accounts Manager",
	"Accounting Read": "Accounts User",
	"Accounting User": "Accounts User",
	"Warehouse": "Stock Manager",
	"Warehouse Admin": "Stock Manager",
	"Warehouse Read": "Stock User",
	"Warehouse User": "Stock User",
	"Audit": "Auditor",
	"Audit Read": "Auditor",
	"Audit User": "Auditor",
	"Ecommerce Admin": "E-commerce",
	"Ecommerce User": "E-commerce",
	"Order": "E-commerce",
	"Order User": "E-commerce",
	"POS": "POS Cashier",
	"POS User": "POS Cashier",
	"Pet": "Coordinator",
	"Pet User": "Coordinator",
	"Guardian": "Coordinator",
	"Guardians": "Coordinator",
	"Guardians User": "Coordinator",
	"Setting": "Pet App Admin",
	"Settings User": "Pet App Admin",
	"Users": "Pet App Admin",
	"Users User": "Pet App Admin",
}

PERMISSION_FIELDS = (
	"select",
	"read",
	"write",
	"create",
	"delete",
	"submit",
	"cancel",
	"amend",
	"report",
	"export",
	"import",
	"share",
	"print",
	"email",
	"mask",
	"impersonate",
)


def execute():
	migrate_docperms()


def build_migration_report():
	return migrate_docperms(dry_run=True)


def migrate_docperms(dry_run: bool = False) -> dict:
	desired_permissions = _source_permissions_by_target()
	report = {
		"dryRun": dry_run,
		"merged": [],
		"skipped": [],
		"sourceRoles": _source_role_summary(),
	}

	for key in sorted(desired_permissions):
		target_role, doctype, permlevel, if_owner = key
		source_payload = desired_permissions[key]
		sources = sorted(source_payload["sources"])
		desired_flags = set(source_payload["permissions"])

		if not frappe.db.exists("Role", target_role):
			report["skipped"].append(
				_skip_entry(target_role, doctype, permlevel, if_owner, sources, "target role missing")
			)
			continue

		if not frappe.db.exists("DocType", doctype):
			report["skipped"].append(
				_skip_entry(target_role, doctype, permlevel, if_owner, sources, "doctype missing")
			)
			continue

		existing_flags = _effective_permissions(target_role, doctype, permlevel, if_owner)
		missing_flags = desired_flags - existing_flags
		if not missing_flags:
			report["skipped"].append(
				_skip_entry(target_role, doctype, permlevel, if_owner, sources, "already covered")
			)
			continue

		action = _merge_target_permission(
			target_role,
			doctype,
			permlevel,
			if_owner,
			existing_flags | desired_flags,
			dry_run=dry_run,
		)
		report["merged"].append(
			{
				"targetRole": target_role,
				"doctype": doctype,
				"permlevel": permlevel,
				"ifOwner": if_owner,
				"sources": sources,
				"action": action,
				"added": sorted(missing_flags),
				"final": sorted(existing_flags | desired_flags),
			}
		)

	if not dry_run:
		_clear_permission_caches(desired_permissions)

	return report


def _source_permissions_by_target() -> dict:
	result = defaultdict(lambda: {"permissions": set(), "sources": set()})
	source_roles = sorted(APPROVED_ROLE_MIGRATIONS)
	for doctype in ("DocPerm", "Custom DocPerm"):
		if not frappe.db.exists("DocType", doctype):
			continue

		fields = ["parent", "role", "permlevel", "if_owner", *PERMISSION_FIELDS]
		for row in frappe.get_all(
			doctype,
			filters={"role": ["in", source_roles]},
			fields=fields,
			ignore_permissions=True,
		):
			source_role = row.role
			target_role = APPROVED_ROLE_MIGRATIONS.get(source_role)
			target_doctype = row.parent
			if not target_role or not target_doctype:
				continue

			flags = _permission_flags(row)
			if not flags:
				continue

			key = (target_role, target_doctype, cint(row.permlevel), cint(row.if_owner))
			result[key]["permissions"].update(flags)
			result[key]["sources"].add(source_role)
	return result


def _source_role_summary() -> dict:
	summary = {}
	for source_role, target_role in sorted(APPROVED_ROLE_MIGRATIONS.items()):
		doctypes = set()
		rows = 0
		for doctype in ("DocPerm", "Custom DocPerm"):
			if not frappe.db.exists("DocType", doctype):
				continue
			for row in frappe.get_all(
				doctype,
				filters={"role": source_role},
				fields=["parent"],
				ignore_permissions=True,
			):
				rows += 1
				if row.parent:
					doctypes.add(row.parent)
		summary[source_role] = {
			"targetRole": target_role,
			"docpermRows": rows,
			"doctypes": sorted(doctypes),
			"doctypeCount": len(doctypes),
		}
	return summary


def _effective_permissions(role: str, doctype: str, permlevel: int, if_owner: int) -> set[str]:
	flags = set()
	for permission_doctype in ("DocPerm", "Custom DocPerm"):
		if not frappe.db.exists("DocType", permission_doctype):
			continue

		for row in frappe.get_all(
			permission_doctype,
			filters={
				"parent": doctype,
				"role": role,
				"permlevel": permlevel,
				"if_owner": if_owner,
			},
			fields=list(PERMISSION_FIELDS),
			ignore_permissions=True,
		):
			flags.update(_permission_flags(row))
	return flags


def _permission_flags(row) -> set[str]:
	return {field for field in PERMISSION_FIELDS if cint(row.get(field))}


def _merge_target_permission(
	role: str,
	doctype: str,
	permlevel: int,
	if_owner: int,
	final_flags: set[str],
	dry_run: bool = False,
) -> str:
	existing_custom = frappe.db.exists(
		"Custom DocPerm",
		{
			"parent": doctype,
			"parenttype": "DocType",
			"role": role,
			"permlevel": permlevel,
			"if_owner": if_owner,
		},
	)
	action = "update" if existing_custom else "insert"
	if dry_run:
		return f"dry-run-{action}"

	if existing_custom:
		docperm = frappe.get_doc("Custom DocPerm", existing_custom)
	else:
		docperm = frappe.get_doc(
			{
				"doctype": "Custom DocPerm",
				"parent": doctype,
				"parenttype": "DocType",
				"parentfield": "permissions",
				"role": role,
				"permlevel": permlevel,
				"if_owner": if_owner,
			}
		)

	for field in PERMISSION_FIELDS:
		docperm.set(field, 1 if field in final_flags else 0)

	if existing_custom:
		docperm.save(ignore_permissions=True)
	else:
		docperm.insert(ignore_permissions=True)
	return action


def _skip_entry(role: str, doctype: str, permlevel: int, if_owner: int, sources: list[str], reason: str) -> dict:
	return {
		"targetRole": role,
		"doctype": doctype,
		"permlevel": permlevel,
		"ifOwner": if_owner,
		"sources": sources,
		"reason": reason,
	}


def _clear_permission_caches(desired_permissions: dict):
	for _, doctype, _, _ in desired_permissions:
		frappe.clear_cache(doctype=doctype)
	frappe.clear_cache(doctype="Role")
