import frappe


SERVICE_PROVIDER_ROLES = ("Service Provider", "Nursing User", "Groomer")
SERVICE_PROVIDER_PROFILE = "Service Provider Profile"


def execute():
	for role_name in SERVICE_PROVIDER_ROLES:
		ensure_role(role_name)

	role_profile = get_or_create_role_profile(SERVICE_PROVIDER_PROFILE)
	existing_roles = {row.role for row in role_profile.roles if row.role}
	changed = False

	for role_name in SERVICE_PROVIDER_ROLES:
		if role_name in existing_roles:
			continue
		role_profile.append("roles", {"role": role_name})
		existing_roles.add(role_name)
		changed = True

	if role_profile.is_new():
		role_profile.insert(ignore_permissions=True)
	elif changed:
		role_profile.save(ignore_permissions=True)

	frappe.clear_cache(doctype="Role")
	frappe.clear_cache(doctype="Role Profile")


def ensure_role(role_name):
	if frappe.db.exists("Role", role_name):
		return

	frappe.get_doc(
		{
			"doctype": "Role",
			"role_name": role_name,
			"desk_access": 1,
			"is_custom": 1,
		}
	).insert(ignore_permissions=True)


def get_or_create_role_profile(profile_name):
	if frappe.db.exists("Role Profile", profile_name):
		return frappe.get_doc("Role Profile", profile_name)

	return frappe.get_doc({"doctype": "Role Profile", "role_profile": profile_name, "roles": []})
