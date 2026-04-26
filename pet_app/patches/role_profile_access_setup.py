from __future__ import annotations

import frappe
from frappe.permissions import add_permission, update_permission_property
from frappe.core.doctype.role_profile.role_profile import RoleProfile


LEGACY_MODULE_ROLE_MAP = {
	"Healthcare User": "Healthcare",
	"Pet User": "Pet",
	"Ecommerce User": "E-commerce",
	"Order User": "Order",
	"POS User": "POS",
	"Accounting User": "Accounting",
	"Warehouse User": "Warehouse",
	"Audit User": "Audit",
	"Users User": "Users",
	"Guardians User": "Guardians",
	"Settings User": "Setting",
}


ROLE_PERMISSION_MAP = {
	"Healthcare": {
		"Vet Case Sheet": ("read", "write", "create", "print", "report"),
		"Vet Visit": ("read", "write", "create", "print", "report"),
		"Lab": ("read", "write", "create", "print", "report"),
		"Imaging": ("read", "write", "create", "print", "report"),
		"CareService template": ("read", "write", "create", "print", "report"),
		"CategoryCareServices": ("read", "write", "create", "print", "report"),
		"Service Room": ("read", "write", "create", "print", "report"),
		"Driver": ("read",),
		"Supplier": ("read",),
		"Pet Boarding": ("read", "write", "create", "submit", "print", "report"),
		"Doctor": ("read",),
	},
	"Doctor": {
		"Doctor": ("read",),
	},
	"Pet": {
		"Pet": ("read", "write", "create", "print", "report"),
		"PetAddRequest": ("read", "write", "create", "print", "report"),
		"PetGuardian": ("read", "write", "create", "print", "report"),
		"Brand": ("read",),
		"FoodType": ("read",),
	},
	"E-commerce": {
		"Product": ("read", "write", "create", "print", "report"),
		"Product Variant": ("read", "write", "create", "print", "report"),
		"Item": ("read",),
		"Item Price": ("read",),
		"Website Item": ("read",),
	},
	"Order": {
		"Sales Order": ("read", "write", "create", "submit", "print", "report"),
		"Customer": ("read",),
		"Delivery Note": ("read", "write", "create", "submit", "print", "report"),
	},
	"POS": {
		"POS Profile": ("read",),
		"Mode of Payment": ("read",),
		"Sales Invoice": ("read", "write", "create", "submit", "print", "report"),
		"Payment Entry": ("read", "write", "create", "submit", "print", "report"),
		"Pet App Cashier Settlement": ("read", "write", "create", "submit", "print", "report"),
	},
	"Accounting": {
		"Pet App Accounting Settings": ("read", "write", "print", "report"),
		"Mode of Payment": ("read",),
		"Sales Invoice": ("read", "write", "create", "submit", "print", "report"),
		"Payment Entry": ("read", "write", "create", "submit", "print", "report"),
		"Journal Entry": ("read", "write", "create", "submit", "print", "report"),
	},
	"Warehouse": {
		"Warehouse": ("read",),
		"Stock Entry": ("read", "write", "create", "submit", "print", "report"),
		"Bin": ("read", "report"),
		"Item": ("read",),
	},
	"Audit": {
		"Audit Trail": ("read", "report"),
		"Version": ("read", "report"),
		"Activity Log": ("read", "report"),
		"Access Log": ("read", "report"),
		"Error Log": ("read", "report"),
	},
	"Users": {
		"User": ("read", "write", "create", "print", "report"),
		"Role Profile": ("read", "write", "create", "print", "report"),
		"Role": ("read",),
	},
	"Guardians": {
		"Guardian": ("read", "write", "create", "print", "report"),
		"PetGuardian": ("read", "write", "create", "print", "report"),
		"Pet": ("read", "write", "create"),
		"PetAddRequest": ("read", "write", "create", "print", "report"),
	},
	"Guardian": {
		"Guardian": ("read", "write"),
		"Pet": ("read", "write", "create"),
		"PetGuardian": ("read",),
	},
	"Setting": {
		"CareService template": ("read", "write", "create", "print", "report"),
		"CategoryCareServices": ("read", "write", "create", "print", "report"),
		"Service Room": ("read", "write", "create", "print", "report"),
		"Driver": ("read", "write", "create", "print", "report"),
		"Supplier": ("read", "write", "create", "print", "report"),
		"Pet Boarding Settings": ("read", "write", "print"),
		"Pet App Accounting Settings": ("read", "write", "print"),
		"Selling Settings": ("read", "write", "print"),
		"Stock Settings": ("read", "write", "print"),
		"System Settings": ("read",),
	},
}


PROFILE_ROLE_MAP = {
	"Healthcare Profile": ["Healthcare", "Doctor"],
	"Pet Profile": ["Pet"],
	"Ecommerce Profile": ["E-commerce"],
	"Order Profile": ["Order"],
	"POS Profile": ["POS"],
	"Accounting Profile": ["Accounting"],
	"Warehouse Profile": ["Warehouse"],
	"Audit Profile": ["Audit"],
	"Users Profile": ["Users"],
	"Guardians Profile": ["Guardians", "Guardian"],
	"Settings Profile": ["Setting"],
}


def execute():
	ensure_roles()
	ensure_role_profiles()
	ensure_permissions()
	clear_access_caches()


def ensure_roles():
	for role_name in ROLE_PERMISSION_MAP:
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


def ensure_role_profiles():
	original_on_update = RoleProfile.on_update

	def _patch_safe_on_update(self):
		self.clear_cache()

	RoleProfile.on_update = _patch_safe_on_update
	try:
		for profile_name, expected_roles in PROFILE_ROLE_MAP.items():
			role_profile = get_or_create_role_profile(profile_name)
			changed = normalize_profile_roles(role_profile)
			existing_roles = {row.role for row in role_profile.roles if row.role}

			for role_name in expected_roles:
				if role_name in existing_roles:
					continue
				role_profile.append("roles", {"role": role_name})
				existing_roles.add(role_name)
				changed = True

			if changed:
				role_profile.save(ignore_permissions=True)
	finally:
		RoleProfile.on_update = original_on_update


def get_or_create_role_profile(profile_name: str):
	if frappe.db.exists("Role Profile", profile_name):
		return frappe.get_doc("Role Profile", profile_name)

	return frappe.get_doc(
		{
			"doctype": "Role Profile",
			"role_profile": profile_name,
			"roles": [],
		}
	).insert(ignore_permissions=True)


def normalize_profile_roles(role_profile):
	seen_roles = set()
	normalized_roles = []
	changed = False

	for row in role_profile.roles:
		role_name = row.role
		normalized_name = LEGACY_MODULE_ROLE_MAP.get(role_name, role_name)
		if normalized_name != role_name or normalized_name in seen_roles:
			changed = True
		if normalized_name in seen_roles:
			continue
		seen_roles.add(normalized_name)
		normalized_roles.append({"role": normalized_name})

	if changed:
		role_profile.set("roles", normalized_roles)
	return changed


def ensure_permissions():
	for role_name, doctype_permissions in ROLE_PERMISSION_MAP.items():
		for doctype, rights in doctype_permissions.items():
			if not frappe.db.exists("DocType", doctype):
				continue
			ensure_doctype_permissions(doctype, role_name, rights)


def ensure_doctype_permissions(doctype: str, role_name: str, rights: tuple[str, ...]):
	if not has_role_perm(doctype, role_name):
		add_permission(doctype, role_name, 0)

	for right in rights:
		update_permission_property(doctype, role_name, 0, right, 1)


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


def clear_access_caches():
	for doctype_permissions in ROLE_PERMISSION_MAP.values():
		for doctype in doctype_permissions:
			if frappe.db.exists("DocType", doctype):
				frappe.clear_cache(doctype=doctype)

	frappe.clear_cache(doctype="Role")
	frappe.clear_cache(doctype="Role Profile")
	frappe.clear_cache(doctype="User")
