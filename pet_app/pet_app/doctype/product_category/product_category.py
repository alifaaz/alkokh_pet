from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, cstr
from frappe.utils.nestedset import NestedSet


PRODUCT_CATEGORY_DOCTYPE = "Product Category"

# Root of the customer-facing storefront taxonomy. Declared once here and read through
# get_store_root_category() so the name is never scattered as a literal across the mobile
# modules - the same failure mode as the hardcoded "Stores - K" warehouse and the
# hardcoded "Standard Selling" price list. Moving this to a setting later means changing
# one constant, not hunting call sites.
STORE_ROOT_CATEGORY = "Store"

# The ONE Item Group every generated storefront group hangs from. Deliberately the same
# literal as STORE_ROOT_CATEGORY: the two trees are unified, so the storefront root is
# spelled "Store" on both sides. Declared as its own constant rather than reusing the
# category one because they name rows in different tables and could diverge again.
#
# This is a CONSTANT, never derived. The mirror that was severed computed its parent and
# defaulted to "Pet Supplies", which is how storefront groups ended up inside the
# clinical catalogue as siblings of Pharmacy.
STORE_ROOT_ITEM_GROUP = "Store"

# Pre-unification name of the same Item Group. Referenced only by the migration patch.
LEGACY_STORE_ROOT_ITEM_GROUP = "Mobile Shop"


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

	# ── Item Group is the source of truth; this doctype is its projection ────────
	# The authority reversed. Item Group is the ONE tree anyone maintains - it carries
	# the stock, the accounting and the clinical taxonomy - and Product Category exists
	# so the mobile SDK's JSON contract does not have to change. Rows are regenerated
	# from Item Group by pet_app.utils.store_category_mirror.
	#
	# A category can therefore no longer CREATE an Item Group. _ensure_store_item_group
	# resolves and refuses; it does not mint. What remains here is the narrow legacy
	# path for hand-made rows: keeping a category's item_group link honest, and carrying
	# a rename across. Rows the mirror owns (auto_generated) opt out of all of it.
	#
	# The bound that made the severed mirror safe to bring back is unchanged in kind,
	# only in shape:
	#
	#   1. IN SCOPE ONLY, BY INTERVAL. Membership is Store's lft/rgt interval, not a
	#      parent name and not a derived parent. The old mirror DERIVED the parent and
	#      defaulted to "Pet Supplies", which is how Antibiotics and Anesthetics became
	#      storefront categories. Pharmacy sits outside the Store interval, so no
	#      clinical group passes at any depth, whatever anyone names it.
	#   2. DEPTH IS PERMITTED, THE BOUNDARY IS NOT. Store > Cat > Cat Dry Food is a
	#      legitimate shape; Store > Antibiotics is not, and never becomes one.
	#   3. GENERATED ROWS ARE MARKED. auto_generated separates what the mirror owns from
	#      what a person made, so neither silently overwrites the other.
	#
	# Deleting a category still never deletes, moves or empties an Item Group.
	# ────────────────────────────────────────────────────────────────────────────

	def validate(self):
		self._normalize()
		self._validate_parent()
		self.validate_ledger()
		self._validate_group_assignment()

	def on_update(self):
		super().on_update()
		self._sync_store_item_group()

	def on_trash(self):
		self._validate_can_delete()
		super().on_trash()

	def before_rename(self, olddn, newdn, merge=False):
		super().before_rename(olddn, newdn, merge)
		if olddn == STORE_ROOT_CATEGORY:
			frappe.throw(
				_("The {0} root category cannot be renamed - its name is a fixed constant that the storefront and the variants guard both resolve against.").format(
					frappe.bold(STORE_ROOT_CATEGORY)
				)
			)
		if frappe.db.exists("Item Group", newdn):
			_assert_group_is_adoptable(newdn)

	def after_rename(self, olddn, newdn, merge=False):
		super().after_rename(olddn, newdn, merge)
		frappe.db.set_value(self.doctype, newdn, "category_name", newdn, update_modified=False)
		if merge:
			return
		self.name = newdn
		self._rename_store_item_group(newdn)

	# ── sync internals ──────────────────────────────────────────────────────────

	def _in_store_tree(self) -> bool:
		"""True when STORE_ROOT_CATEGORY is this category's root.

		Walks the parent chain rather than reading lft/rgt: on insert the nested set has
		not been rebuilt yet, so lft/rgt would be 0 and every new category would read as
		out of scope.
		"""
		if self.name == STORE_ROOT_CATEGORY:
			return True
		seen = set()
		parent = self.parent_product_category
		while parent and parent not in seen:
			if parent == STORE_ROOT_CATEGORY:
				return True
			seen.add(parent)
			parent = frappe.db.get_value(self.doctype, parent, "parent_product_category")
		return False

	def _syncable(self) -> bool:
		if cint(self.get("auto_generated")):
			return False  # the mirror wrote this row FROM an Item Group; do not write back
		if self.name == STORE_ROOT_CATEGORY:
			return False  # the root group IS the confining parent; never regenerated
		if cint(self.is_group):
			return False  # structural nodes hold no Items, so they get no Item Group
		if not cint(self.enabled):
			return False  # disabled categories are not published; existing links are left alone
		return self._in_store_tree()

	def _sync_store_item_group(self):
		if not self._syncable():
			return
		target = _ensure_store_item_group(self.name)
		if self.item_group != target:
			frappe.db.set_value(self.doctype, self.name, "item_group", target, update_modified=False)
			self.item_group = target

	def _rename_store_item_group(self, newdn):
		"""Carry a category rename across to its Item Group so both trees stay in step."""
		if not self._syncable():
			return
		linked = frappe.db.get_value(self.doctype, newdn, "item_group")
		if not linked or linked == newdn or not frappe.db.exists("Item Group", linked):
			self._sync_store_item_group()
			return

		_assert_group_is_adoptable(linked, must_exist=True)

		from frappe.model.rename_doc import rename_doc

		rename_doc(doctype="Item Group", old=linked, new=newdn, ignore_permissions=True)
		# The rename cascades into every Link field, item_group included, but re-assert
		# rather than assume - this is the invariant the whole design exists to hold.
		_assert_confined(newdn)
		frappe.db.set_value(self.doctype, newdn, "item_group", newdn, update_modified=False)
		self.item_group = newdn

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

	def _validate_can_delete(self):
		"""Refuse a delete that would strand stock or children. Never cascade, never orphan.

		Deleting a category does NOT delete, move or empty its Item Group. The old
		"Products use its linked Item Group" check guarded a cascade that no longer
		exists, and blocked storefront edits for purely clinical reasons - deleting the
		category "Dog Food" was refused because the Item Group "Dog Food" held Items,
		even though the group would have been left untouched.

		Two Item Group reasons survive, and only because the group would be left with no
		category in front of it while still holding stock or structure.
		"""
		if self.name == STORE_ROOT_CATEGORY:
			frappe.throw(_("The {0} root category cannot be deleted.").format(frappe.bold(STORE_ROOT_CATEGORY)))
		if frappe.db.exists(self.doctype, {"parent_product_category": self.name}):
			frappe.throw(_("Cannot delete category with child categories."))
		if frappe.db.exists("Product", {"category": self.name}):
			frappe.throw(_("Cannot delete category because Products use it."))

		linked_item_group = self.item_group
		if not linked_item_group or not frappe.db.exists("Item Group", linked_item_group):
			return

		item_count = frappe.db.count("Item", {"item_group": linked_item_group})
		if item_count:
			frappe.throw(
				_("Cannot delete {0}: its Item Group {1} still holds {2} Item(s). Move or remove those Items first.").format(
					frappe.bold(self.name), frappe.bold(linked_item_group), item_count
				)
			)

		child_count = frappe.db.count("Item Group", {"parent_item_group": linked_item_group})
		if child_count:
			frappe.throw(
				_("Cannot delete {0}: its Item Group {1} still has {2} child group(s). Remove them first.").format(
					frappe.bold(self.name), frappe.bold(linked_item_group), child_count
				)
			)


def _require_store_root_item_group() -> str:
	"""The confining parent. Refuses loudly rather than inventing or deriving one."""
	if not frappe.db.exists("Item Group", STORE_ROOT_ITEM_GROUP):
		frappe.throw(
			_("The store root Item Group {0} does not exist. Storefront categories cannot be synced until it is created.").format(
				frappe.bold(STORE_ROOT_ITEM_GROUP)
			)
		)
	return STORE_ROOT_ITEM_GROUP


def is_in_store_subtree(item_group) -> bool:
	"""True when the group sits strictly inside the store root's nested-set interval.

	An lft/rgt interval test rather than the parent-chain walk this module used to do,
	because the storefront taxonomy is no longer flat: it is Store > Cat > Cat Dry Food,
	two levels deep, so "direct child of Store" stopped describing a legitimate group.
	The interval is one query at any depth, and it is the SAME bound the mirror and the
	mobile catalogue use, so all three agree on what "in the shop" means.

	Reads committed lft/rgt, so it must not be called against a doc mid-insert whose
	nested set has not been rebuilt (they would both be 0 and every new group would read
	as out of scope). The mirror's hook path walks parents instead, for that reason.
	"""
	root = _require_store_root_item_group()
	if item_group == root:
		return False  # the root confines; it is not itself confined
	bounds = frappe.db.get_value("Item Group", root, ["lft", "rgt"], as_dict=True)
	row = frappe.db.get_value("Item Group", item_group, ["lft", "rgt"], as_dict=True)
	if not bounds or not row or not row.lft or not row.rgt:
		return False
	return bounds.lft < row.lft and row.rgt < bounds.rgt


def _assert_group_is_adoptable(item_group, must_exist=False):
	"""An existing group may be adopted ONLY if it already sits inside the store subtree.

	This is the refusal that keeps the two trees from colliding, and it survives the move
	to a nested storefront taxonomy unchanged in intent. A category named "Antibiotics"
	must not quietly adopt - or move - the clinical Item Group of the same name, and a
	category rename must not drag a clinical group into the store.

	Widening this from "direct child of Store" to "anywhere under Store" loosens the
	SHAPE it permits, not the BOUNDARY it enforces: Pharmacy sits outside the Store
	interval, so no clinical group passes at any depth. That boundary is the property
	being bought; depth never was.
	"""
	root = _require_store_root_item_group()
	if not frappe.db.exists("Item Group", item_group):
		if must_exist:
			frappe.throw(_("Item Group {0} no longer exists.").format(frappe.bold(item_group)))
		return
	if item_group == root:
		frappe.throw(
			_("Item Group {0} is the store root itself and cannot be used as a category's group.").format(
				frappe.bold(root)
			)
		)
	if not is_in_store_subtree(item_group):
		parent = frappe.db.get_value("Item Group", item_group, "parent_item_group")
		frappe.throw(
			_("Item Group {0} sits under {1}, outside the store subtree {2}. Storefront categories may only own Item Groups beneath {2} - move that group into the store tree first.").format(
				frappe.bold(item_group), frappe.bold(parent or _("no parent")), frappe.bold(root)
			)
		)


def _assert_confined(item_group):
	"""Post-write invariant: the group is somewhere inside the store subtree, nowhere else."""
	root = _require_store_root_item_group()
	if not is_in_store_subtree(item_group):
		parent = frappe.db.get_value("Item Group", item_group, "parent_item_group")
		frappe.throw(
			_("Refusing to leave Item Group {0} under {1}. Storefront groups must sit inside {2}.").format(
				frappe.bold(item_group), frappe.bold(parent or _("no parent")), frappe.bold(root)
			)
		)


def _ensure_store_item_group(category_name) -> str:
	"""Resolve the same-named Item Group inside the store subtree. Never creates one.

	The direction of authority reversed. Item Group is now the single tree anyone
	maintains, and Product Category is regenerated from it by
	pet_app.utils.store_category_mirror - so a category minting an Item Group would be
	the tail writing the dog. Every group this function can return was authored on the
	Item Group side; a category that names one which does not exist is a mistake to
	report, not a gap to fill silently.

	Kept under its old name because it is still "give me the group this category owns",
	and the callers' intent has not changed - only who is allowed to create.
	"""
	_require_store_root_item_group()

	if not frappe.db.exists("Item Group", category_name):
		frappe.throw(
			_("Item Group {0} does not exist. Item Group is the source of truth for the storefront taxonomy - create the group under {1} first, and its category is generated from it.").format(
				frappe.bold(category_name), frappe.bold(STORE_ROOT_ITEM_GROUP)
			)
		)

	_assert_group_is_adoptable(category_name)
	return category_name


def resolve_product_category(category) -> str | None:
	"""Name of an EXISTING Product Category, or None.

	Resolution only - it never creates a category. The removed `create_from_item_group`
	branch fell back to the Item Group table and minted a category from any group it
	found, which is how clinical groups became storefront categories.
	"""
	category = _clean_name(category)
	if not category:
		return None
	if frappe.db.exists(PRODUCT_CATEGORY_DOCTYPE, category):
		return category
	return None


def get_product_category_item_group(category, require_leaf=True) -> str | None:
	"""The Item Group a category is linked to. Reports the stored link and nothing more."""
	category_name = resolve_product_category(category)
	if not category_name:
		return None

	category_doc = frappe.get_doc(PRODUCT_CATEGORY_DOCTYPE, category_name)
	if require_leaf and cint(category_doc.is_group):
		frappe.throw(_("Select a leaf Product Category for Products."))

	return category_doc.item_group


def apply_product_category_to_product(doc) -> str | None:
	if not doc.category:
		doc.item_group = None
		return None

	category_name = resolve_product_category(doc.category)
	if not category_name:
		frappe.throw(_("Product Category {0} does not exist.").format(frappe.bold(doc.category)))

	item_group = get_product_category_item_group(category_name, require_leaf=True)
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


def _clean_name(value) -> str:
	return cstr(value or "").strip()
