from __future__ import annotations

import frappe
from frappe.permissions import add_permission, update_permission_property

from pet_app.patches.operational_healthcare_roles import OPERATIONAL_HEALTHCARE_ROLE_PERMISSIONS


ROLE_PERMISSION_MAP = {
	"Healthcare": {
		"Vet Case Sheet": ("read", "write", "create", "print", "report"),
		"Vet Visit": ("read", "write", "create", "print", "report"),
		"Lab": ("read", "write", "create", "print", "report"),
		"Imaging": ("read", "write", "create", "print", "report"),
		"PetCareService": ("read",),
		"CareService template": ("read", "write", "create", "print", "report"),
		"CategoryCareServices": ("read", "write", "create", "print", "report"),
		"Service Room": ("read", "write", "create", "print", "report"),
		"Driver": ("read",),
		"Supplier": ("read",),
		"Pet Boarding": ("read", "write", "create", "submit", "print", "report"),
		"Healthcare Practitioner": ("read",),
		"Pet": ("read",),
		"Guardian": ("read",),
	},
	"Visit": {
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
		"Healthcare Practitioner": ("read",),
		"PetCareService": ("read",),
		"Pet": ("read",),
		"Guardian": ("read",),
	},
	"Doctor": {
		"Healthcare Practitioner": ("read",),
	},
	"Healthcare Practitioner": {
		"Healthcare Practitioner": ("read",),
		"Vet Case Sheet": ("read", "write", "create", "print", "report"),
		"Vet Visit": ("read", "write", "create", "print", "report"),
		"Lab": ("read", "write", "create", "print", "report"),
		"Imaging": ("read", "write", "create", "print", "report"),
		"PetCareService": ("read",),
		"CareService template": ("read",),
		"CategoryCareServices": ("read",),
		"Pet": ("read",),
		"Guardian": ("read",),
		"Pet Boarding": ("read", "write", "create", "submit", "print", "report"),
	},
	"Laboratory User": {
		"Lab": ("read",),
		"Imaging": ("read",),
		"PetCareService": ("read",),
		"CareService template": ("read",),
		"CategoryCareServices": ("read",),
		"Pet": ("read",),
		"Guardian": ("read",),
		"Healthcare Practitioner": ("read",),
		"Vet Visit": ("read",),
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
	**OPERATIONAL_HEALTHCARE_ROLE_PERMISSIONS,
}


PROFILE_ROLE_MAP = {
	"Healthcare Profile": ["Healthcare", "Healthcare Practitioner"],
	"Visit Read Profile": ["Visit Read"],
	"Visit Admin Profile": ["Visit Admin"],
	"Lab Read Profile": ["Lab Read"],
	"Lab Admin Profile": ["Lab Admin"],
	"Radiology Read Profile": ["Radiology Read"],
	"Radiology Admin Profile": ["Radiology Admin"],
	"Reception Profile": ["Reception"],
	"Coordinator Profile": ["Coordinator", "Reception"],
	"Service Provider Profile": ["Service Provider", "Nursing User", "Groomer"],
	"Service Provider Manager Profile": ["Desk User", "Service Provider Manager"],
	"Pet Profile": ["Pet"],
	"Ecommerce Profile": ["E-commerce"],
	"Ecommerce Admin Profile": ["Ecommerce Admin"],
	"Order Profile": ["Order"],
	"POS Profile": ["POS"],
	"POS Cashier Profile": ["POS Cashier"],
	"POS Admin Profile": ["POS Admin"],
	"Accounting Profile": ["Accounting"],
	"Accounting Read Profile": ["Accounting Read"],
	"Accounting Admin Profile": ["Accounting Admin"],
	"Warehouse Profile": ["Warehouse"],
	"Warehouse Read Profile": ["Warehouse Read"],
	"Warehouse Admin Profile": ["Warehouse Admin"],
	"Audit Profile": ["Audit"],
	"Audit Read Profile": ["Audit Read"],
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
	for profile_name, expected_roles in PROFILE_ROLE_MAP.items():
		if frappe.db.exists("Role Profile", profile_name):
			continue

		role_profile = frappe.get_doc({"doctype": "Role Profile", "role_profile": profile_name, "roles": []})
		for role_name in expected_roles:
			if frappe.db.exists("Role", role_name):
				role_profile.append("roles", {"role": role_name})
		role_profile.insert(ignore_permissions=True)


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
