from __future__ import annotations

import frappe
from frappe.permissions import add_permission, update_permission_property
from frappe.utils import cint

from pet_app.pet_app.doctype.product_category.product_category import (
	PRODUCT_CATEGORY_DOCTYPE,
	ROOT_ITEM_GROUP,
	apply_product_category_to_product,
	ensure_product_category_for_item_group,
	ensure_product_root_item_group,
)


CATEGORY_PERMISSION_ROLES = {
	"System Manager": ("read", "write", "create", "delete", "print", "report", "export", "email", "share"),
	"Item Manager": ("read", "write", "create", "delete", "print", "report", "export", "email", "share"),
	"Stock Manager": ("read", "write", "create", "delete", "print", "report", "export", "email", "share"),
	"E-commerce": ("read", "write", "create", "delete", "print", "report"),
}

ALL_PERMISSION_TYPES = (
	"read",
	"write",
	"create",
	"delete",
	"submit",
	"cancel",
	"amend",
	"print",
	"email",
	"report",
	"export",
	"import",
	"share",
)


def execute():
	original_in_patch = frappe.flags.in_patch
	original_in_migrate = frappe.flags.in_migrate
	frappe.flags.in_patch = True
	frappe.flags.in_migrate = True
	try:
		reload_doctypes()
		ensure_product_category_permissions()
		ensure_product_root_item_group(ignore_permissions=True)
		ensure_product_category_for_item_group(ROOT_ITEM_GROUP, ignore_permissions=True)
		migrate_existing_products()
		frappe.clear_cache()
	finally:
		frappe.flags.in_patch = original_in_patch
		frappe.flags.in_migrate = original_in_migrate


def reload_doctypes():
	frappe.reload_doc("pet_app", "doctype", "product_category")
	frappe.reload_doc("pet_app", "doctype", "product")


def ensure_product_category_permissions():
	if not frappe.db.exists("DocType", PRODUCT_CATEGORY_DOCTYPE):
		return

	for role, allowed_ptypes in CATEGORY_PERMISSION_ROLES.items():
		if not frappe.db.exists("Role", role):
			continue
		if not frappe.db.exists("Custom DocPerm", {"parent": PRODUCT_CATEGORY_DOCTYPE, "role": role, "permlevel": 0}):
			add_permission(PRODUCT_CATEGORY_DOCTYPE, role, 0, "read")
		for ptype in ALL_PERMISSION_TYPES:
			update_permission_property(
				PRODUCT_CATEGORY_DOCTYPE,
				role,
				0,
				ptype,
				1 if ptype in allowed_ptypes else 0,
				validate=False,
			)


def migrate_existing_products():
	if not frappe.db.exists("DocType", "Product"):
		return
	meta = frappe.get_meta("Product")
	if not meta.has_field("category") or not meta.has_field("item_group"):
		return

	rows = frappe.db.sql(
		"""
		SELECT name, category
		FROM `tabProduct`
		WHERE IFNULL(category, '') != ''
		""",
		as_dict=True,
	)

	for row in rows:
		product = frappe.get_doc("Product", row.name)
		try:
			apply_product_category_to_product(product, ignore_permissions=True)
		except Exception:
			category = _create_category_for_unknown_value(row.category)
			if not category:
				frappe.log_error(
					title="PRODUCT_CATEGORY_MIGRATION_SKIPPED",
					message=f"Could not migrate Product {row.name} category {row.category}",
				)
				continue
			product.category = category
			apply_product_category_to_product(product, ignore_permissions=True)

		frappe.db.set_value(
			"Product",
			product.name,
			{
				"category": product.category,
				"item_group": product.item_group,
			},
			update_modified=False,
		)


def _create_category_for_unknown_value(category_name):
	category_name = (category_name or "").strip()
	if not category_name:
		return None
	if frappe.db.exists(PRODUCT_CATEGORY_DOCTYPE, category_name):
		if cint(frappe.db.get_value(PRODUCT_CATEGORY_DOCTYPE, category_name, "is_group")):
			return _ensure_leaf_category_for_group(category_name)
		return category_name
	if frappe.db.exists("Item Group", category_name):
		return ensure_product_category_for_item_group(category_name, ignore_permissions=True)

	doc = frappe.new_doc(PRODUCT_CATEGORY_DOCTYPE)
	doc.category_name = category_name
	doc.enabled = 1
	doc.is_group = 0
	doc.flags.ignore_item_group_permissions = True
	doc.insert(ignore_permissions=True)
	return doc.name


def _ensure_leaf_category_for_group(group_category):
	base_name = f"{group_category} Products"
	category_name = base_name
	counter = 1
	while frappe.db.exists(PRODUCT_CATEGORY_DOCTYPE, category_name):
		if not cint(frappe.db.get_value(PRODUCT_CATEGORY_DOCTYPE, category_name, "is_group")):
			return category_name
		counter += 1
		category_name = f"{base_name} {counter}"

	doc = frappe.new_doc(PRODUCT_CATEGORY_DOCTYPE)
	doc.category_name = category_name
	doc.parent_product_category = group_category
	doc.enabled = 1
	doc.is_group = 0
	doc.flags.ignore_item_group_permissions = True
	doc.insert(ignore_permissions=True)
	return doc.name
