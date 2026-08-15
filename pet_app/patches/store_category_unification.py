"""Unify the storefront taxonomy across both trees.

Product Category and Item Group described the same storefront twice under two different
root names - Product Category "Store" pointed at Item Group "Mobile Shop" - and two of
the store categories carried no link at all while an identically-named Item Group sat
under the root. This patch makes the two trees agree:

  1. junk categories removed (they leaked into the customer-facing catalogue)
  2. missing item_group links backfilled from the groups already under the store root
  3. the store root Item Group renamed "Mobile Shop" -> "Store"
  4. the root category's own link repointed so the bridge is no longer divergent

It also carries the Product Category role permissions that used to live in
product_category_item_group_sync, which was removed from patches.txt so a fresh site
could not replay its Item Group mirror. Without this, a fresh site would grant Product
Category to System Manager only.

Every step is idempotent and re-runnable. Nothing here deletes an Item Group or touches
anything outside the store subtree.
"""

from __future__ import annotations

import frappe
from frappe.permissions import add_permission, update_permission_property

from pet_app.pet_app.doctype.product_category.product_category import (
	LEGACY_STORE_ROOT_ITEM_GROUP,
	PRODUCT_CATEGORY_DOCTYPE,
	STORE_ROOT_CATEGORY,
	STORE_ROOT_ITEM_GROUP,
)


CATEGORY_PERMISSION_ROLES = {
	"System Manager": ("read", "write", "create", "delete", "print", "report", "export", "email", "share"),
	"Item Manager": ("read", "write", "create", "delete", "print", "report", "export", "email", "share"),
	"Stock Manager": ("read", "write", "create", "delete", "print", "report", "export", "email", "share"),
	"E-commerce": ("read", "write", "create", "delete", "print", "report"),
}

ALL_PERMISSION_TYPES = (
	"read", "write", "create", "delete", "submit", "cancel", "amend",
	"print", "email", "report", "export", "import", "share",
)

# Junk rows created through the admin UI. "شسيسش" is the damaging one: an ENABLED root
# outside the store tree, so the unscoped storefront listing served it to customers.
JUNK_CATEGORIES = ("sadas", "شسيسش")


def execute():
	ensure_product_category_permissions()
	clean_junk_categories()
	rename_store_root_item_group()
	backfill_store_category_links()
	frappe.clear_cache()


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
				PRODUCT_CATEGORY_DOCTYPE, role, 0, ptype,
				1 if ptype in allowed_ptypes else 0,
				validate=False,
			)


def clean_junk_categories() -> list[str]:
	"""Remove the two junk categories. Refuses if one has grown real dependents."""
	removed = []
	for name in JUNK_CATEGORIES:
		if not frappe.db.exists(PRODUCT_CATEGORY_DOCTYPE, name):
			continue
		if frappe.db.exists(PRODUCT_CATEGORY_DOCTYPE, {"parent_product_category": name}):
			frappe.log_error(title="STORE_UNIFICATION_JUNK_HAS_CHILDREN", message=name)
			continue
		if frappe.db.exists("Product", {"category": name}):
			frappe.log_error(title="STORE_UNIFICATION_JUNK_HAS_PRODUCTS", message=name)
			continue
		frappe.delete_doc(PRODUCT_CATEGORY_DOCTYPE, name, ignore_permissions=True)
		removed.append(name)
	return removed


def rename_store_root_item_group() -> str | None:
	"""Mobile Shop -> Store, via rename_doc so every Link field follows.

	The model-level rename_doc, not the frappe.rename_doc shim: only the former accepts
	ignore_permissions, and a patch must not depend on the running user holding Item
	Group write. The rename updates all 34 Link fields pointing at Item Group - Item,
	Sales Order Item, Product, Product Category.item_group and the rest - and leaves
	lft/rgt untouched, because a rename is not a move.
	"""
	from frappe.model.rename_doc import rename_doc

	if frappe.db.exists("Item Group", STORE_ROOT_ITEM_GROUP):
		return None
	if not frappe.db.exists("Item Group", LEGACY_STORE_ROOT_ITEM_GROUP):
		return None

	rename_doc(
		doctype="Item Group",
		old=LEGACY_STORE_ROOT_ITEM_GROUP,
		new=STORE_ROOT_ITEM_GROUP,
		ignore_permissions=True,
	)
	return STORE_ROOT_ITEM_GROUP


def current_store_root_item_group() -> str | None:
	"""The store root Item Group under whichever name it currently carries.

	Resolved rather than assumed so the backfill is correct on both sides of the rename
	and stays re-runnable.
	"""
	for name in (STORE_ROOT_ITEM_GROUP, LEGACY_STORE_ROOT_ITEM_GROUP):
		if frappe.db.exists("Item Group", name):
			return name
	return None


def backfill_store_category_links() -> dict[str, str]:
	"""Point every store category at the identically-named group under the store root.

	Adoption, not creation: these eight groups already exist and hold 49 Items. The root
	category is linked to the root group, which is what removes the last divergent
	bridge. `item_group` is read-only on the form, so it is written with db.set_value.
	"""
	linked = {}
	root_group = current_store_root_item_group()
	if not root_group:
		return linked
	if not frappe.db.exists(PRODUCT_CATEGORY_DOCTYPE, STORE_ROOT_CATEGORY):
		return linked

	root = frappe.db.get_value("Item Group", root_group, ["lft", "rgt"], as_dict=True)

	# The root category itself: "Store" -> the root group.
	if frappe.db.get_value(PRODUCT_CATEGORY_DOCTYPE, STORE_ROOT_CATEGORY, "item_group") != root_group:
		frappe.db.set_value(
			PRODUCT_CATEGORY_DOCTYPE, STORE_ROOT_CATEGORY, "item_group",
			root_group, update_modified=False,
		)
		linked[STORE_ROOT_CATEGORY] = root_group

	for row in frappe.get_all(
		PRODUCT_CATEGORY_DOCTYPE,
		filters={"parent_product_category": STORE_ROOT_CATEGORY},
		fields=["name", "item_group"],
	):
		if row.item_group:
			continue
		target = frappe.db.get_value(
			"Item Group",
			{"name": row.name, "lft": [">", root.lft], "rgt": ["<", root.rgt]},
			"name",
		)
		if not target:
			continue
		frappe.db.set_value(
			PRODUCT_CATEGORY_DOCTYPE, row.name, "item_group", target, update_modified=False
		)
		linked[row.name] = target

	return linked
