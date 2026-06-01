from __future__ import annotations

import frappe


SERVICE_PROVIDER_MANAGER_ROLE = "Service Provider Manager"
SERVICE_PROVIDER_MANAGER_PROFILE = "Service Provider Manager Profile"
SERVICE_PROVIDER_MANAGER_PROFILE_ROLES = ("Desk User", SERVICE_PROVIDER_MANAGER_ROLE)


def execute():
	from pet_app.patches import operational_healthcare_roles

	operational_healthcare_roles.execute()
	ensure_service_provider_manager_profile()
	frappe.clear_cache(doctype="Role")
	frappe.clear_cache(doctype="Role Profile")
	frappe.clear_cache(doctype="User")


def ensure_service_provider_manager_profile():
	role_profile = get_or_create_role_profile(SERVICE_PROVIDER_MANAGER_PROFILE)
	existing_roles = {row.role for row in role_profile.roles if row.role}
	changed = False

	for role_name in SERVICE_PROVIDER_MANAGER_PROFILE_ROLES:
		if role_name in existing_roles or not frappe.db.exists("Role", role_name):
			continue
		role_profile.append("roles", {"role": role_name})
		existing_roles.add(role_name)
		changed = True

	if role_profile.is_new():
		role_profile.insert(ignore_permissions=True)
	elif changed:
		role_profile.save(ignore_permissions=True)


def get_or_create_role_profile(profile_name):
	if frappe.db.exists("Role Profile", profile_name):
		return frappe.get_doc("Role Profile", profile_name)

	return frappe.get_doc({"doctype": "Role Profile", "role_profile": profile_name, "roles": []})
