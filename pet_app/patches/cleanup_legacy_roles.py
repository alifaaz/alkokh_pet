import frappe


ROLES_TO_DELETE = {
	"Healthcare",
	"Healthcare User",
	"Healthcare Practitioner",
	"Physician",
	"Healthcare Administrator",
	"Visit",
	"Accounting",
	"Accounting Admin",
	"Accounting Read",
	"Accounting User",
	"Warehouse",
	"Warehouse Admin",
	"Warehouse Read",
	"Warehouse User",
	"Audit",
	"Audit Read",
	"Audit User",
	"Ecommerce Admin",
	"Ecommerce User",
	"Order",
	"Order User",
	"POS",
	"POS User",
	"Pet",
	"Pet User",
	"Guardian",
	"Guardians",
	"Guardians User",
	"Setting",
	"Settings User",
	"Users",
	"Users User",
	"_Test Role",
	"_Test Role 2",
	"_Test Role 3",
	"_Test Role 4",
	"Analytics",
	"OPS Visit",
	"OPS Workflow Only",
	"Technician",
}

KEEP_ROLES = {
	"Coordinator",
	"Reception",
	"Doctor",
	"Visit Admin",
	"Visit Read",
	"Lab Admin",
	"Lab Read",
	"Laboratory User",
	"Radiology Admin",
	"Radiology Read",
	"Service Provider",
	"Nursing User",
	"Groomer",
	"POS Cashier",
	"POS Admin",
	"Accounts User",
	"Accounts Manager",
	"Stock User",
	"Stock Manager",
	"Purchase User",
	"Purchase Manager",
	"E-commerce",
	"Auditor",
	"Pet App Admin",
}

CORE_ROLES = {
	"Administrator",
	"All",
	"Desk User",
	"Guest",
	"Newsletter Manager",
	"Prepared Report User",
	"Report Manager",
	"Script Manager",
	"System Manager",
	"Website Manager",
	"Workspace Manager",
}

GAP_ROLES = {
	"Delivery Manager",
	"Delivery User",
	"HR Manager",
	"HR User",
	"Item Manager",
	"Purchase Manager",
	"Purchase User",
	"Sales Manager",
	"Sales User",
	"Stock Manager",
	"Stock User",
}

IGNORED_PROFILE_ROLES = {"Desk User"}


def execute():
	cleanup_legacy_roles()


def cleanup_legacy_roles():
	protected_overlap = ROLES_TO_DELETE & (KEEP_ROLES | CORE_ROLES | GAP_ROLES)
	if protected_overlap:
		frappe.throw(
			"Cleanup role list contains protected roles: {0}".format(", ".join(sorted(protected_overlap)))
		)

	roles = sorted(role for role in ROLES_TO_DELETE if frappe.db.exists("Role", role))
	profiles_to_delete = _role_profiles_used_only_by_deleted_roles(set(roles))

	_delete_rows("User Role Profile", "role_profile", profiles_to_delete)
	_delete_rows("Has Role", "role", roles)
	_delete_rows("Custom DocPerm", "role", roles)
	_delete_rows("DocPerm", "role", roles)

	for profile_name in profiles_to_delete:
		if frappe.db.exists("Role Profile", profile_name):
			frappe.delete_doc("Role Profile", profile_name, ignore_permissions=True, force=True)

	for role_name in roles:
		if frappe.db.exists("Role", role_name):
			frappe.delete_doc("Role", role_name, ignore_permissions=True, force=True)

	frappe.clear_cache(doctype="Role")
	frappe.clear_cache(doctype="Role Profile")


def _role_profiles_used_only_by_deleted_roles(roles_to_delete):
	if not roles_to_delete:
		return []

	rows = frappe.get_all(
		"Has Role",
		filters={"parenttype": "Role Profile"},
		fields=["parent", "role"],
		ignore_permissions=True,
	)
	profile_roles = {}
	for row in rows:
		profile_roles.setdefault(row.parent, set()).add(row.role)

	profiles = []
	for profile_name, roles in profile_roles.items():
		if not roles & roles_to_delete:
			continue
		remaining_roles = roles - roles_to_delete - IGNORED_PROFILE_ROLES
		if not remaining_roles:
			profiles.append(profile_name)
	return sorted(profiles)


def _delete_rows(doctype, fieldname, values):
	if not values or not frappe.db.exists("DocType", doctype):
		return
	frappe.db.delete(doctype, {fieldname: ["in", values]})
