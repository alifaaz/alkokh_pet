from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, cstr
from frappe.utils.nestedset import NestedSet


PRODUCT_CATEGORY_DOCTYPE = "Product Category"
ROOT_ITEM_GROUP = "Pet Supplies"
ALL_ITEM_GROUPS = "All Item Groups"

# Root of the customer-facing storefront taxonomy. Declared once here and read through
# get_store_root_category() so the name is never scattered as a literal across the mobile
# modules - the same failure mode as the hardcoded "Stores - K" warehouse and the
# hardcoded "Standard Selling" price list. Moving this to a setting later means changing
# one constant, not hunting call sites.
STORE_ROOT_CATEGORY = "Store"


def get_store_root_category() -> str | None:
	"""The storefront root category name, or None when it is not present.

	Read-only. Callers that page the store tree should treat None as "no store root
	configured" and fall back to an unparented listing rather than inventing one.
	"""
	if not frappe.db.exists("DocType", PRODUCT_CATEGORY_DOCTYPE):
		return None
	if frappe.db.exists(PRODUCT_CATEGORY_DOCTYPE, STORE_ROOT_CATEGORY):
		return STORE_ROOT_CATEGORY
	return None


class ProductCategory(NestedSet):
	nsm_parent_field = "parent_product_category"

	def autoname(self):
		self.category_name = _clean_name(self.category_name)
		if not self.category_name:
			frappe.throw(_("Category Name is required."))
		self.name = self.category_name

	# ── Item Group coupling severed ─────────────────────────────────────────────
	# Product Category is the STOREFRONT taxonomy. Item Groups are the clinical
	# catalogue's own tree and belong to clinic staff: this doctype no longer creates,
	# updates, renames or deletes them under any circumstance. `item_group` is left as
	# a vestigial read-only field for a later cleanup pass.
	#
	# The delete path was the dangerous one - on_trash used to delete the linked Item
	# Group, and the store categories (Cat Food, Toys, Grooming ...) are linked to the
	# very Item Groups that hold the retail Items.
	# ────────────────────────────────────────────────────────────────────────────

	def validate(self):
		self._normalize()
		self._validate_parent()
		self.validate_ledger()
		self._validate_group_assignment()

	def on_update(self):
		super().on_update()

	def on_trash(self):
		self._validate_can_delete(self.item_group)
		super().on_trash()

	def before_rename(self, olddn, newdn, merge=False):
		# The Item Group name checks that used to live here guarded a rename that no
		# longer happens, so they only blocked legitimate storefront renames.
		super().before_rename(olddn, newdn, merge)

	def after_rename(self, olddn, newdn, merge=False):
		super().after_rename(olddn, newdn, merge)
		frappe.db.set_value(self.doctype, newdn, "category_name", newdn, update_modified=False)

	def _normalize(self):
		self.category_name = _clean_name(self.category_name or self.name)
		if not self.category_name:
			frappe.throw(_("Category Name is required."))
		if self.is_new():
			self.name = self.category_name
		elif self.category_name != self.name:
			self.category_name = self.name

		self.enabled = cint(self.enabled)
		self.is_group = cint(self.is_group)
		self.display_order = cint(self.display_order)

	def _validate_parent(self):
		if not self.parent_product_category:
			return
		if self.parent_product_category == self.name:
			frappe.throw(_("A category cannot be its own parent."))
		if not frappe.db.exists(self.doctype, self.parent_product_category):
			frappe.throw(_("Parent Product Category {0} does not exist.").format(frappe.bold(self.parent_product_category)))
		if not cint(frappe.db.get_value(self.doctype, self.parent_product_category, "is_group")):
			frappe.throw(_("Parent Product Category must be a group."))

	def _validate_group_assignment(self):
		if not cint(self.is_group):
			return
		if frappe.db.exists("Product", {"category": self.name}):
			frappe.throw(_("Cannot mark this category as a group while Products use it."))

	def _ensure_item_group(self):
		if not frappe.db.exists("DocType", "Item Group"):
			return

		if self.item_group and not frappe.db.exists("Item Group", self.item_group):
			self.item_group = None

		if self.item_group:
			_validate_item_group_not_linked_elsewhere(self.item_group, self.name)
			return

		existing_item_group = self.name if frappe.db.exists("Item Group", self.name) else None
		if existing_item_group:
			_validate_item_group_not_linked_elsewhere(existing_item_group, self.name)
			self.item_group = existing_item_group
			return

		parent_item_group = _target_parent_item_group(self)
		_ensure_parent_item_group_can_hold_children(parent_item_group, self)

		item_group = frappe.new_doc("Item Group")
		item_group.item_group_name = self.name
		item_group.parent_item_group = parent_item_group
		item_group.is_group = cint(self.is_group)
		_apply_optional_item_group_fields(item_group, self)
		item_group.flags.from_product_category = True
		item_group.insert(ignore_permissions=_should_ignore_permissions(self))
		self.item_group = item_group.name

	def _sync_item_group(self):
		if not self.item_group or not frappe.db.exists("Item Group", self.item_group):
			return

		item_group = frappe.get_doc("Item Group", self.item_group)
		target_parent = _target_parent_item_group(self)
		_ensure_parent_item_group_can_hold_children(target_parent, self)

		changed = False
		if item_group.item_group_name != self.name:
			item_group.item_group_name = self.name
			changed = True
		if (item_group.parent_item_group or "") != (target_parent or ""):
			item_group.parent_item_group = target_parent
			changed = True
		if item_group.name != ROOT_ITEM_GROUP and cint(item_group.is_group) != cint(self.is_group):
			item_group.is_group = cint(self.is_group)
			changed = True
		if _apply_optional_item_group_fields(item_group, self):
			changed = True

		if changed:
			item_group.save(ignore_permissions=_should_ignore_permissions(self))

	def _validate_can_delete(self, linked_item_group=None):
		linked_item_group = linked_item_group or self.item_group
		if linked_item_group == ROOT_ITEM_GROUP:
			frappe.throw(_("The default Pet Supplies category cannot be deleted."))
		if frappe.db.exists(self.doctype, {"parent_product_category": self.name}):
			frappe.throw(_("Cannot delete category with child categories."))
		if frappe.db.exists("Product", {"category": self.name}):
			frappe.throw(_("Cannot delete category because Products use it."))
		if linked_item_group and frappe.db.exists("Product", {"item_group": linked_item_group}):
			frappe.throw(_("Cannot delete category because Products use its linked Item Group."))
		if linked_item_group and frappe.db.exists("Item", {"item_group": linked_item_group}):
			frappe.throw(_("Cannot delete category because Items use its linked Item Group."))
		if linked_item_group and frappe.db.exists("Item Group", {"parent_item_group": linked_item_group}):
			frappe.throw(_("Cannot delete category because child Item Groups use its linked Item Group."))

	def _delete_linked_item_group(self, linked_item_group=None):
		if not linked_item_group or not frappe.db.exists("Item Group", linked_item_group):
			return
		if frappe.db.exists(self.doctype, self.name):
			frappe.db.set_value(self.doctype, self.name, "item_group", None, update_modified=False)
		frappe.delete_doc(
			"Item Group",
			linked_item_group,
			ignore_permissions=_should_ignore_permissions(self),
		)


def ensure_product_root_item_group(ignore_permissions=False) -> str:
	"""Read-only since the coupling was severed: reports the root, never creates it.

	This used to create the "Pet Supplies" Item Group and re-parent it under "All Item
	Groups" - a storefront code path reshaping the clinical tree. It is reachable from a
	plain Product save (Product.validate -> apply_product_category_to_product), so it had
	to become inert rather than merely uncalled. Name kept so callers still resolve.
	"""
	return ROOT_ITEM_GROUP


def ensure_product_category_for_item_group(item_group, ignore_permissions=False) -> str | None:
	item_group = _clean_name(item_group)
	if not item_group or not frappe.db.exists("DocType", PRODUCT_CATEGORY_DOCTYPE):
		return None
	if item_group == ALL_ITEM_GROUPS:
		ensure_product_root_item_group(ignore_permissions=ignore_permissions)
		return ensure_product_category_for_item_group(ROOT_ITEM_GROUP, ignore_permissions=ignore_permissions)
	if not frappe.db.exists("Item Group", item_group):
		return None

	existing = frappe.db.get_value(PRODUCT_CATEGORY_DOCTYPE, {"item_group": item_group}, "name")
	if existing:
		return existing

	if frappe.db.exists(PRODUCT_CATEGORY_DOCTYPE, item_group):
		category = frappe.get_doc(PRODUCT_CATEGORY_DOCTYPE, item_group)
		if category.item_group and category.item_group != item_group:
			frappe.throw(_("Product Category {0} already links to another Item Group.").format(frappe.bold(item_group)))
		category.item_group = item_group
		category.flags.ignore_item_group_permissions = True
		category.save(ignore_permissions=ignore_permissions or _in_system_context())
		return category.name

	item_group_doc = frappe.get_doc("Item Group", item_group)
	parent_category = None
	if item_group_doc.parent_item_group and item_group_doc.parent_item_group not in {ALL_ITEM_GROUPS, ROOT_ITEM_GROUP}:
		parent_category = ensure_product_category_for_item_group(
			item_group_doc.parent_item_group,
			ignore_permissions=ignore_permissions,
		)

	category = frappe.new_doc(PRODUCT_CATEGORY_DOCTYPE)
	category.category_name = item_group
	category.parent_product_category = parent_category
	category.enabled = 1
	category.is_group = _is_item_group_structural_category(item_group_doc)
	category.item_group = item_group
	if item_group_doc.get("image"):
		category.image = item_group_doc.image
	if item_group_doc.meta.has_field("description") and item_group_doc.get("description"):
		category.description = item_group_doc.description
	if item_group_doc.meta.has_field("weightage"):
		category.display_order = cint(item_group_doc.get("weightage"))
	category.flags.ignore_item_group_permissions = True
	category.insert(ignore_permissions=ignore_permissions or _in_system_context())
	return category.name


def sync_product_category_for_item_group(doc, method=None):
	if getattr(doc.flags, "from_product_category", False):
		return
	if not doc.name or doc.name in {ALL_ITEM_GROUPS, ROOT_ITEM_GROUP}:
		return
	ensure_product_category_for_item_group(doc.name, ignore_permissions=True)


def resolve_product_category(category, create_from_item_group=True, ignore_permissions=False) -> str | None:
	category = _clean_name(category)
	if not category:
		return None
	if category == ALL_ITEM_GROUPS and create_from_item_group:
		return ensure_product_category_for_item_group(ROOT_ITEM_GROUP, ignore_permissions=ignore_permissions)
	if frappe.db.exists(PRODUCT_CATEGORY_DOCTYPE, category):
		return category
	if create_from_item_group and frappe.db.exists("Item Group", category):
		return ensure_product_category_for_item_group(category, ignore_permissions=ignore_permissions)
	return None


def get_product_category_item_group(
	category,
	create_from_item_group=True,
	require_leaf=True,
	ignore_permissions=False,
) -> str | None:
	category_name = resolve_product_category(
		category,
		create_from_item_group=create_from_item_group,
		ignore_permissions=ignore_permissions,
	)
	if not category_name:
		return None

	category_doc = frappe.get_doc(PRODUCT_CATEGORY_DOCTYPE, category_name)
	if require_leaf and cint(category_doc.is_group):
		frappe.throw(_("Select a leaf Product Category for Products."))

	# Reports the stored link and nothing more. It used to SAVE the category here to
	# force an Item Group into existence when the link was missing or dangling - a read
	# accessor with a hidden write. A missing link now simply reads as None.
	return category_doc.item_group


def apply_product_category_to_product(doc, create_from_item_group=True, ignore_permissions=False) -> str | None:
	if not doc.category:
		doc.item_group = None
		return None

	category_name = resolve_product_category(
		doc.category,
		create_from_item_group=create_from_item_group,
		ignore_permissions=ignore_permissions,
	)
	if not category_name:
		frappe.throw(_("Product Category {0} does not exist.").format(frappe.bold(doc.category)))

	item_group = get_product_category_item_group(
		category_name,
		create_from_item_group=False,
		require_leaf=True,
		ignore_permissions=ignore_permissions,
	)
	doc.category = category_name
	doc.item_group = item_group
	return item_group


def get_product_category_summary(category) -> dict:
	category = _clean_name(category)
	if not category:
		return {}
	if not frappe.db.exists(PRODUCT_CATEGORY_DOCTYPE, category):
		return {}

	row = frappe.db.get_value(
		PRODUCT_CATEGORY_DOCTYPE,
		category,
		["name", "category_name", "image", "description", "item_group"],
		as_dict=True,
	)
	return dict(row or {})


def _target_parent_item_group(category_doc) -> str | None:
	"""Vestigial. Only the now-uncalled _ensure_item_group / _sync_item_group used this.

	The "Parent Product Category must have a linked Item Group" throw is gone with it -
	it forced every storefront category to own an Item Group, which is the coupling being
	removed. Returns None so nothing downstream can act on a target parent.
	"""
	return None


def _ensure_parent_item_group_can_hold_children(parent_item_group, category_doc=None):
	"""Vestigial and inert: it used to flip a clinical Item Group to is_group=1."""
	return


def _apply_optional_item_group_fields(item_group, category_doc) -> bool:
	changed = False
	field_map = {
		"image": "image",
		"description": "description",
		"weightage": "display_order",
	}
	for item_group_field, category_field in field_map.items():
		if not item_group.meta.has_field(item_group_field):
			continue
		value = category_doc.get(category_field)
		if item_group.get(item_group_field) != value:
			item_group.set(item_group_field, value)
			changed = True
	return changed


def _validate_item_group_not_linked_elsewhere(item_group, category_name):
	existing = frappe.db.get_value(PRODUCT_CATEGORY_DOCTYPE, {"item_group": item_group}, "name")
	if existing and existing != category_name:
		frappe.throw(_("Item Group {0} is already linked to Product Category {1}.").format(
			frappe.bold(item_group),
			frappe.bold(existing),
		))


def _is_item_group_structural_category(item_group_doc) -> int:
	if item_group_doc.name == ROOT_ITEM_GROUP:
		return 0
	if frappe.db.exists("Product", {"category": item_group_doc.name}):
		return 0
	if frappe.db.exists("Item", {"item_group": item_group_doc.name}):
		return 0
	return cint(item_group_doc.is_group)


def _should_ignore_permissions(doc=None) -> bool:
	if doc and getattr(doc.flags, "ignore_item_group_permissions", False):
		return True
	return _in_system_context()


def _in_system_context() -> bool:
	return bool(
		getattr(frappe.flags, "in_patch", False)
		or getattr(frappe.flags, "in_migrate", False)
		or getattr(frappe.flags, "in_install", False)
		or getattr(frappe.flags, "in_install_db", False)
		or getattr(frappe.flags, "in_import", False)
	)


def _clean_name(value) -> str:
	return cstr(value or "").strip()
