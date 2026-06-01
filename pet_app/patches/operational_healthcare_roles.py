from __future__ import annotations

import frappe
from frappe.core.doctype.doctype.doctype import validate_permissions_for_doctype
from frappe.permissions import setup_custom_perms


READ = ("read",)
REPORT_READ = ("read", "report")
MANAGE = ("read", "create", "write", "print", "report")
SUBMIT_MANAGE = ("read", "create", "write", "submit", "print", "report")
CRUD_MANAGE = ("read", "create", "write", "delete", "print", "report")
UPDATE_DELETE_MANAGE = ("read", "write", "delete", "print", "report")
SETTINGS_MANAGE = ("read", "write", "print", "report")

MANAGED_PERMISSION_TYPES = (
	"read",
	"create",
	"write",
	"delete",
	"submit",
	"cancel",
	"amend",
	"print",
	"email",
	"export",
	"import",
	"report",
	"share",
)

INTERNAL_VISIT_CHILD_DOCTYPES = (
	"Visit Diagnosis",
	"Visit Order",
	"Visit Consult Request",
	"Vet Visit Vital Sign",
)

CLINICAL_MASTER_READ_DEPENDENCIES = {
	"Healthcare Practitioner": READ,
	"Care Service Billing Option": READ,
	"CareService template": READ,
	"CategoryCareServices": READ,
	"Disease": READ,
	"Medication": READ,
	"Procedure Template": READ,
	"Pet Consent Template": READ,
}

VISIT_READ_DEPENDENCIES = {
	**CLINICAL_MASTER_READ_DEPENDENCIES,
	"Vet Visit": READ,
	"Vet Case Sheet": READ,
	"Vet Visit Addendum": READ,
	"Pet Care Episode": READ,
	"Pet Care Plan Item": READ,
	"Lab": READ,
	"Imaging": READ,
	"Radiology": READ,
	"PetCareService": READ,
	"Pet": READ,
	"Guardian": READ,
	"Appointment": READ,
	"Pet Medical Profile": READ,
	"Notification Log": READ,
	"File": READ,
}

LAB_READ_DEPENDENCIES = {
	**CLINICAL_MASTER_READ_DEPENDENCIES,
	"Lab": READ,
	"PetCareService": READ,
	"Pet": READ,
	"Guardian": READ,
	"Vet Visit": READ,
	"Notification Log": READ,
	"File": READ,
}

RADIOLOGY_READ_DEPENDENCIES = {
	**CLINICAL_MASTER_READ_DEPENDENCIES,
	"Imaging": READ,
	"Radiology": READ,
	"PetCareService": READ,
	"Pet": READ,
	"Guardian": READ,
	"Vet Visit": READ,
	"Notification Log": READ,
	"File": READ,
}

RECEPTION_PERMISSIONS = {
	**CLINICAL_MASTER_READ_DEPENDENCIES,
	"Appointment": MANAGE,
	"Pet Queue Ticket": MANAGE,
	"Vet Case Sheet": MANAGE,
	"Vet Visit": ("read", "create", "print", "report"),
	"Pet Medical Profile": READ,
	"PetCareService": ("read", "create", "write", "print", "report"),
	"Pet Boarding": SUBMIT_MANAGE,
	"Service Room": READ,
	"Pet": MANAGE,
	"Guardian": MANAGE,
	"PetGuardian": MANAGE,
	"Customer": ("read", "create", "write"),
	"Address": ("read", "create", "write"),
	"Sales Invoice": ("read", "create", "submit", "print"),
	"Item": READ,
	"Item Price": READ,
	"Notification Log": READ,
	"File": ("read", "create", "delete"),
}

COORDINATOR_PERMISSIONS = {
	**CLINICAL_MASTER_READ_DEPENDENCIES,
	"Pet Queue Ticket": MANAGE,
	"Appointment": MANAGE,
	"Vet Case Sheet": MANAGE,
	"Vet Visit": MANAGE,
	"Pet Care Plan Item": MANAGE,
	"PetCareService": MANAGE,
	"Lab": ("read", "create", "write", "print", "report"),
	"Imaging": ("read", "create", "write", "print", "report"),
	"Radiology": ("read", "create", "write", "print", "report"),
	"Pet Boarding": SUBMIT_MANAGE,
	"Service Room": READ,
	"Pet": READ,
	"Guardian": READ,
	"PetGuardian": ("read", "create"),
	"Sales Invoice": ("read", "create", "submit", "print"),
	"Item": READ,
	"Item Price": READ,
	"Notification Log": READ,
	"File": ("read", "create", "delete"),
}

SERVICE_PROVIDER_MANAGER_PERMISSIONS = {
	"PetCareService": UPDATE_DELETE_MANAGE,
	"CareService template": UPDATE_DELETE_MANAGE,
	"Care Service Billing Option": READ,
	"CategoryCareServices": READ,
	"Item": READ,
	"Item Price": READ,
	"Price List": READ,
	"Pet": READ,
	"Guardian": READ,
	"PetGuardian": READ,
	"Healthcare Practitioner": READ,
	"Vet Visit": READ,
	"File": READ,
}

POS_CASHIER_PERMISSIONS = {
	"Sales Invoice": ("read", "create", "write", "submit", "print", "report"),
	"Customer": ("read", "create"),
	"Address": READ,
	"Item": READ,
	"Item Price": READ,
	"Item Barcode": READ,
	"Item Group": READ,
	"Price List": READ,
	"Bin": READ,
	"Warehouse": READ,
	"POS Profile": READ,
	"Mode of Payment": READ,
	"Account": READ,
	"Payment Entry": READ,
	"Pet App Cashier Settlement": ("read", "create", "submit", "print", "report"),
	"Notification Log": READ,
}

POS_ADMIN_PERMISSIONS = {
	**POS_CASHIER_PERMISSIONS,
	"POS Profile": MANAGE,
	"Sales Invoice": SUBMIT_MANAGE,
	"Payment Entry": SUBMIT_MANAGE,
	"Pet App Cashier Settlement": SUBMIT_MANAGE,
	"Customer": MANAGE,
	"Address": MANAGE,
}

WAREHOUSE_READ_PERMISSIONS = {
	"Warehouse": READ,
	"Item": READ,
	"Item Price": READ,
	"Item Group": READ,
	"UOM": READ,
	"Bin": REPORT_READ,
	"Stock Entry": READ,
	"Stock Ledger Entry": REPORT_READ,
	"Purchase Invoice": READ,
	"Supplier": READ,
	"File": READ,
	"Notification Log": READ,
}

WAREHOUSE_ADMIN_PERMISSIONS = {
	**WAREHOUSE_READ_PERMISSIONS,
	"Warehouse": MANAGE,
	"Item": CRUD_MANAGE,
	"Item Price": MANAGE,
	"Stock Entry": SUBMIT_MANAGE,
	"Purchase Invoice": SUBMIT_MANAGE,
	"Supplier": ("read", "create", "write", "print", "report"),
	"Payment Entry": SUBMIT_MANAGE,
	"Account": READ,
	"File": ("read", "create", "delete"),
}

ACCOUNTING_READ_PERMISSIONS = {
	"Sales Invoice": READ,
	"Payment Entry": READ,
	"Journal Entry": READ,
	"GL Entry": REPORT_READ,
	"Account": READ,
	"Customer": READ,
	"Supplier": READ,
	"Address": READ,
	"Item": READ,
	"Item Price": READ,
	"POS Profile": READ,
	"Mode of Payment": READ,
	"Pet App Accounting Settings": READ,
	"Pet App Cashier Settlement": READ,
	"Notification Log": READ,
}

ACCOUNTING_ADMIN_PERMISSIONS = {
	**ACCOUNTING_READ_PERMISSIONS,
	"Sales Invoice": SUBMIT_MANAGE,
	"Payment Entry": SUBMIT_MANAGE,
	"Journal Entry": SUBMIT_MANAGE,
	"Account": CRUD_MANAGE,
	"Customer": MANAGE,
	"Supplier": ("read", "create", "write", "print", "report"),
	"Address": MANAGE,
	"Pet App Accounting Settings": SETTINGS_MANAGE,
	"Pet App Cashier Settlement": SUBMIT_MANAGE,
}

ECOMMERCE_ADMIN_PERMISSIONS = {
	"Item": CRUD_MANAGE,
	"Item Price": MANAGE,
	"Item Group": READ,
	"UOM": READ,
	"Warehouse": READ,
	"Supplier": READ,
	"Product": CRUD_MANAGE,
	"Product Variant": CRUD_MANAGE,
	"Product Category": CRUD_MANAGE,
	"Brand": READ,
	"FoodType": READ,
	"Coupon Code": CRUD_MANAGE,
	"Sales Order": SUBMIT_MANAGE,
	"Delivery Note": SUBMIT_MANAGE,
	"Customer": READ,
	"File": ("read", "create", "delete"),
	"Notification Log": READ,
}

AUDIT_READ_PERMISSIONS = {
	"Audit Trail": REPORT_READ,
	"Version": REPORT_READ,
	"Activity Log": REPORT_READ,
	"Access Log": REPORT_READ,
	"Error Log": REPORT_READ,
	"Pet App Cashier Settlement": READ,
	"Notification Log": READ,
}

OPERATIONAL_HEALTHCARE_ROLE_PERMISSIONS = {
	"Visit Read": VISIT_READ_DEPENDENCIES,
	"Visit Admin": {
		**VISIT_READ_DEPENDENCIES,
		"Vet Visit": MANAGE,
		"Vet Case Sheet": MANAGE,
		"Vet Visit Addendum": MANAGE,
		"Pet Care Plan Item": MANAGE,
		"Lab": MANAGE,
		"Imaging": MANAGE,
		"Radiology": MANAGE,
		"PetCareService": SUBMIT_MANAGE,
		"Care Service Billing Option": READ,
		"File": ("read", "create", "delete"),
	},
	"Lab Read": LAB_READ_DEPENDENCIES,
	"Lab Admin": {
		**LAB_READ_DEPENDENCIES,
		"Lab": SUBMIT_MANAGE,
		"PetCareService": SUBMIT_MANAGE,
		"Care Service Billing Option": READ,
		"File": ("read", "create", "delete"),
	},
	"Radiology Read": RADIOLOGY_READ_DEPENDENCIES,
	"Radiology Admin": {
		**RADIOLOGY_READ_DEPENDENCIES,
		"Imaging": SUBMIT_MANAGE,
		"Radiology": SUBMIT_MANAGE,
		"PetCareService": SUBMIT_MANAGE,
		"Care Service Billing Option": READ,
		"File": ("read", "create", "delete"),
	},
	"Reception": RECEPTION_PERMISSIONS,
	"Coordinator": COORDINATOR_PERMISSIONS,
	"Service Provider Manager": SERVICE_PROVIDER_MANAGER_PERMISSIONS,
	"POS Cashier": POS_CASHIER_PERMISSIONS,
	"POS Admin": POS_ADMIN_PERMISSIONS,
	"Warehouse Read": WAREHOUSE_READ_PERMISSIONS,
	"Warehouse Admin": WAREHOUSE_ADMIN_PERMISSIONS,
	"Accounting Read": ACCOUNTING_READ_PERMISSIONS,
	"Accounting Admin": ACCOUNTING_ADMIN_PERMISSIONS,
	"Ecommerce Admin": ECOMMERCE_ADMIN_PERMISSIONS,
	"Audit Read": AUDIT_READ_PERMISSIONS,
}


def execute():
	ensure_roles()
	remove_internal_child_table_permissions()
	ensure_permissions()
	ensure_seeded_role_profiles()
	clear_access_caches()


def ensure_roles():
	for role_name in OPERATIONAL_HEALTHCARE_ROLE_PERMISSIONS:
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


def ensure_permissions():
	touched_doctypes = set()
	for role_name, doctype_permissions in OPERATIONAL_HEALTHCARE_ROLE_PERMISSIONS.items():
		for doctype, rights in doctype_permissions.items():
			if not frappe.db.exists("DocType", doctype):
				continue
			ensure_doctype_permissions(doctype, role_name, rights)
			touched_doctypes.add(doctype)
	for doctype in touched_doctypes:
		validate_permissions_for_doctype(doctype)


def remove_internal_child_table_permissions():
	for role_name in OPERATIONAL_HEALTHCARE_ROLE_PERMISSIONS:
		for doctype in INTERNAL_VISIT_CHILD_DOCTYPES:
			for name in frappe.get_all(
				"Custom DocPerm",
				filters={"parent": doctype, "role": role_name, "permlevel": 0, "if_owner": 0},
				pluck="name",
				ignore_permissions=True,
			):
				frappe.delete_doc("Custom DocPerm", name, ignore_permissions=True, force=True)


def ensure_doctype_permissions(doctype: str, role_name: str, rights: tuple[str, ...]):
	setup_custom_perms(doctype)

	rights = set(rights)
	if not is_submittable(doctype):
		rights -= {"submit", "cancel", "amend"}

	values = {}
	for ptype in MANAGED_PERMISSION_TYPES:
		if not frappe.get_meta("Custom DocPerm").has_field(ptype):
			continue
		values[ptype] = 1 if ptype in rights else 0

	name = frappe.db.get_value(
		"Custom DocPerm",
		{"parent": doctype, "role": role_name, "permlevel": 0, "if_owner": 0},
	)
	if name:
		frappe.db.set_value("Custom DocPerm", name, values, update_modified=False)
		return

	frappe.get_doc(
		{
			"doctype": "Custom DocPerm",
			"parent": doctype,
			"parenttype": "DocType",
			"parentfield": "permissions",
			"role": role_name,
			"permlevel": 0,
			"if_owner": 0,
			**values,
		}
	).insert(ignore_permissions=True)


def ensure_seeded_role_profiles():
	from pet_app.api import permissions

	permissions.ensure_roles()
	permissions.ensure_role_profiles()


def is_submittable(doctype: str) -> bool:
	try:
		return bool(frappe.get_meta(doctype).is_submittable)
	except Exception:
		return False


def clear_access_caches():
	frappe.clear_cache(doctype="Role")
	frappe.clear_cache(doctype="Role Profile")
	frappe.clear_cache(doctype="User")
	for doctype_permissions in OPERATIONAL_HEALTHCARE_ROLE_PERMISSIONS.values():
		for doctype in doctype_permissions:
			if frappe.db.exists("DocType", doctype):
				frappe.clear_cache(doctype=doctype)
