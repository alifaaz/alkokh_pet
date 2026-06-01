import frappe


TESTING_ROLE_PROFILE_ROLES = {
	"Coordinator Profile": ["Coordinator", "Reception"],
	"Veterinarian": ["Doctor", "Visit Admin", "Visit Read"],
	"Lab And Radiology Operator": ["Lab Admin", "Lab Read", "Radiology Admin", "Radiology Read"],
	"POS Cashier Profile": ["POS Cashier"],
}


def execute():
	for profile_name, required_roles in TESTING_ROLE_PROFILE_ROLES.items():
		role_profile = _get_or_create_role_profile(profile_name)
		existing_roles = {row.role for row in role_profile.roles}
		changed = False

		for role_name in required_roles:
			if role_name in existing_roles or not frappe.db.exists("Role", role_name):
				continue

			role_profile.append("roles", {"role": role_name})
			existing_roles.add(role_name)
			changed = True

		if role_profile.is_new():
			role_profile.insert(ignore_permissions=True)
		elif changed:
			role_profile.save(ignore_permissions=True)

	frappe.clear_cache()


def _get_or_create_role_profile(profile_name):
	if frappe.db.exists("Role Profile", profile_name):
		return frappe.get_doc("Role Profile", profile_name)

	return frappe.get_doc({"doctype": "Role Profile", "role_profile": profile_name, "roles": []})
