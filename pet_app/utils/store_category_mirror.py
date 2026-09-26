"""Regenerate the Product Category storefront tree from the Item Group tree.

WHY THIS EXISTS
---------------
The same catalogue used to be described three times - Item Group, Product Category and
Product - and the three descriptions drifted until the mobile shop could see 3 of 2163
items. Item Group is now the ONE tree anyone maintains: it already carries the stock, the
accounting and the clinical taxonomy, and it is where the 1710 migrated Pre Define Items
landed. Product Category survives only so the Flutter SDK and the admin SPA keep the JSON
contract they were built against - `_categories_payload` still reads it, unchanged.

So this module is a projection, not a second source. Nobody hand-edits a generated row.

WHY THE PREVIOUS MIRROR WAS SEVERED, AND WHAT IS DIFFERENT
----------------------------------------------------------
An Item Group -> Product Category mirror existed before and was deleted (see the comment
block in hooks.py). It created 67 auto-rows and surfaced `Antibiotics` and
`Anesthetics - General` as customer-facing storefront categories, because it mirrored
EVERY Item Group and DERIVED each parent, defaulting to "Pet Supplies".

The difference here is a bound, not more care:

  1. THE lft/rgt INTERVAL IS THE GATE. Only groups strictly inside Store's nested-set
     interval are ever considered. Pharmacy sits at 5-102 and Store at 109-200, so no
     clinical group passes - at any depth, under any name, however anyone renames it.
     Scope is a position in the tree, not a string that has to be matched correctly.
  2. GENERATED ROWS ARE MARKED. `auto_generated` separates what this module owns from
     what a person made, so a hand-made row is never clobbered and a row whose group has
     gone can be reaped without guessing.
  3. HIDING IS EXPLICIT. `Item Group.custom_show_in_shop` is the per-node opt-out, so a
     group can hold stock for the clinic and stay out of the shop without being moved.

DELETION IS NEVER FORCED. Reaping goes through ProductCategory._validate_can_delete,
which refuses while Products still point at a row or its Item Group still holds stock. A
refusal is logged and skipped, never overridden - an orphaned category is a cosmetic
problem, and a forced delete is not.
"""

from __future__ import annotations

import frappe
from frappe.utils import cint, cstr
from frappe.utils.nestedset import rebuild_tree

from pet_app.pet_app.doctype.product_category.product_category import (
	PRODUCT_CATEGORY_DOCTYPE,
	STORE_ROOT_CATEGORY,
	STORE_ROOT_ITEM_GROUP,
)


SHOW_IN_SHOP_FIELD = "custom_show_in_shop"
DISPLAY_ORDER_FIELD = "custom_shop_display_order"
DESCRIPTION_FIELD = "custom_shop_description"

# Item Group columns the mirror copies onto its categories. `image` and `arabic_name` are
# pre-existing; the three custom_* fields are added by the store_taxonomy_merge patch.
_GROUP_FIELDS = (
	"name",
	"parent_item_group",
	"is_group",
	"image",
	"lft",
	"rgt",
	SHOW_IN_SHOP_FIELD,
	DISPLAY_ORDER_FIELD,
	DESCRIPTION_FIELD,
)


def _log(message: str):
	frappe.logger("store_category_mirror").info(message)


def _store_bounds() -> dict | None:
	"""lft/rgt of the store root Item Group, or None when it is absent.

	Returning None rather than falling back to an unbounded query is deliberate, and
	mirrors _categories_payload's own guard: an empty storefront is a visible, fixable
	failure; a leaked clinical catalogue is not.
	"""
	if not frappe.db.exists("Item Group", STORE_ROOT_ITEM_GROUP):
		return None
	return frappe.db.get_value("Item Group", STORE_ROOT_ITEM_GROUP, ["lft", "rgt"], as_dict=True)


def _custom_fields_present() -> bool:
	"""True once item_group_shop_fields has created the three shop fields on Item Group."""
	meta = frappe.get_meta("Item Group")
	return bool(meta.get_field(SHOW_IN_SHOP_FIELD))


def _schema_ready() -> str | None:
	"""None when the mirror can run, otherwise why it cannot.

	Both halves of the projection are new schema: the three Item Group fields carry what
	is projected, and Product Category.auto_generated records what the projection owns.
	Without the latter the reaper cannot tell a generated row from a hand-made one, and
	the only safe thing to do with that ambiguity is refuse - so this reports rather than
	falling back to a mode that would either clobber or leak.
	"""
	if not frappe.db.has_column(PRODUCT_CATEGORY_DOCTYPE, "auto_generated"):
		return "Product Category.auto_generated is missing - run bench migrate first"
	if not _custom_fields_present():
		return f"Item Group.{SHOW_IN_SHOP_FIELD} is missing - run bench migrate first"
	return None


def in_scope_groups() -> list[dict]:
	"""Item Groups inside the store subtree that are not hidden, parents before children.

	Ordering by lft is what makes this a single pass: a nested set guarantees an
	ancestor's lft is lower than every descendant's, so each row's parent category has
	already been written by the time the row is reached. No second pass, no deferred
	re-parenting.
	"""
	bounds = _store_bounds()
	if not bounds:
		return []

	filters = {"lft": [">", bounds["lft"]], "rgt": ["<", bounds["rgt"]]}
	if _custom_fields_present():
		filters[SHOW_IN_SHOP_FIELD] = 1

	fields = [f for f in _GROUP_FIELDS if f != "name"]
	if not _custom_fields_present():
		fields = [f for f in fields if not f.startswith("custom_")]

	return frappe.get_all(
		"Item Group",
		filters=filters,
		fields=["name"] + fields,
		order_by="lft asc",
		ignore_permissions=True,
	)


def _desired_row(group: dict) -> dict:
	"""The category fields a group implies. The single place the projection is defined."""
	parent = group.get("parent_item_group")
	# A group whose parent is the store root hangs off the root CATEGORY, which carries
	# the same name. Deeper groups hang off their parent group's category, which - by the
	# lft ordering - already exists.
	return {
		"category_name": group["name"],
		"parent_product_category": parent if parent else STORE_ROOT_CATEGORY,
		"is_group": cint(group.get("is_group")),
		"enabled": 1,
		"item_group": group["name"],
		"image": group.get("image") or None,
		"description": group.get(DESCRIPTION_FIELD) or None,
		"display_order": cint(group.get(DISPLAY_ORDER_FIELD)),
		"auto_generated": 1,
	}


def _ensure_root_category() -> bool:
	"""The store root category, created if missing. Never marked auto_generated.

	The root is the fixed constant both trees are named after and the thing
	`_store_tree_bounds` resolves; before_rename and _validate_can_delete already refuse
	to rename or delete it. Marking it generated would invite the reaper to consider it.
	"""
	if frappe.db.exists(PRODUCT_CATEGORY_DOCTYPE, STORE_ROOT_CATEGORY):
		frappe.db.set_value(
			PRODUCT_CATEGORY_DOCTYPE,
			STORE_ROOT_CATEGORY,
			{"is_group": 1, "enabled": 1, "item_group": STORE_ROOT_ITEM_GROUP},
			update_modified=False,
		)
		return False

	doc = frappe.new_doc(PRODUCT_CATEGORY_DOCTYPE)
	doc.category_name = STORE_ROOT_CATEGORY
	doc.is_group = 1
	doc.enabled = 1
	doc.item_group = STORE_ROOT_ITEM_GROUP
	doc.flags.from_store_mirror = True
	doc.insert(ignore_permissions=True)
	return True


def _upsert(group: dict, *, dry_run: bool) -> str:
	"""Create or refresh one category. Returns 'created', 'updated', 'adopted' or 'ok'."""
	name = group["name"]
	desired = _desired_row(group)

	existing = frappe.db.get_value(
		PRODUCT_CATEGORY_DOCTYPE,
		name,
		["name", "parent_product_category", "is_group", "enabled", "item_group",
		 "image", "description", "display_order", "auto_generated"],
		as_dict=True,
	)

	if not existing:
		if dry_run:
			return "created"
		doc = frappe.new_doc(PRODUCT_CATEGORY_DOCTYPE)
		doc.update(desired)
		# insert(), not save(): a full save on an existing row re-enters on_update ->
		# _sync_store_item_group -> update_nsm, which does far more work than a metadata
		# refresh needs and can fight the rebuild_tree at the end.
		doc.flags.from_store_mirror = True
		doc.insert(ignore_permissions=True)
		return "created"

	adopted = not cint(existing.get("auto_generated"))
	changed = {k: v for k, v in desired.items() if k != "category_name" and existing.get(k) != v}
	if not changed:
		return "ok"
	if dry_run:
		return "adopted" if adopted else "updated"

	frappe.db.set_value(PRODUCT_CATEGORY_DOCTYPE, name, changed, update_modified=False)
	frappe.clear_document_cache(PRODUCT_CATEGORY_DOCTYPE, name)
	return "adopted" if adopted else "updated"


def _reap(live_names: set[str], bounds: dict, *, dry_run: bool) -> dict:
	"""Delete generated categories whose Item Group is gone, hidden or out of scope.

	Only ever touches auto_generated rows - a hand-made category is somebody's decision,
	and the mirror is not entitled to overrule it by deletion.
	"""
	result = {"deleted": [], "refused": []}

	candidates = frappe.get_all(
		PRODUCT_CATEGORY_DOCTYPE,
		filters={"auto_generated": 1},
		fields=["name", "lft", "rgt"],
		order_by="rgt asc",  # children before parents, so no delete strands a subtree
		ignore_permissions=True,
	)

	for row in candidates:
		if row["name"] in live_names:
			continue
		if dry_run:
			result["deleted"].append(row["name"])
			continue
		try:
			frappe.delete_doc(
				PRODUCT_CATEGORY_DOCTYPE, row["name"], ignore_permissions=True, delete_permanently=True
			)
			result["deleted"].append(row["name"])
		except Exception as exc:  # noqa: BLE001 - a refusal is data, not a crash
			result["refused"].append({"name": row["name"], "reason": cstr(exc)})
			_log(f"reap refused for {row['name']}: {exc}")

	return result


def rebuild_store_categories(dry_run: bool = False) -> dict:
	"""Regenerate the whole storefront category tree from Item Group. Idempotent.

	The only writer of auto_generated rows. Safe to run repeatedly; running it twice in a
	row leaves the second run reporting all-'ok'. Callable from bench console:

	    from pet_app.utils.store_category_mirror import rebuild_store_categories
	    rebuild_store_categories(dry_run=True)
	"""
	dry_run = bool(cint(dry_run))
	not_ready = _schema_ready()
	if not_ready:
		return {"skipped": not_ready, "dry_run": dry_run}

	bounds = _store_bounds()
	if not bounds:
		return {"skipped": f"Item Group {STORE_ROOT_ITEM_GROUP} does not exist", "dry_run": dry_run}

	summary = {
		"dry_run": dry_run,
		"created": [],
		"updated": [],
		"adopted": [],
		"unchanged": 0,
		"root_created": False,
	}

	if not dry_run:
		summary["root_created"] = _ensure_root_category()
	elif not frappe.db.exists(PRODUCT_CATEGORY_DOCTYPE, STORE_ROOT_CATEGORY):
		summary["root_created"] = True

	groups = in_scope_groups()
	live_names = {g["name"] for g in groups}

	for group in groups:
		outcome = _upsert(group, dry_run=dry_run)
		if outcome == "ok":
			summary["unchanged"] += 1
		else:
			summary[outcome].append(group["name"])

	summary["reaped"] = _reap(live_names, bounds, dry_run=dry_run)
	summary["in_scope"] = len(groups)

	if not dry_run:
		# Once, at the end. The table carries gaps from historical deletions and every
		# insert widens them; rebuilding per row would be quadratic and pointless.
		rebuild_tree(PRODUCT_CATEGORY_DOCTYPE)
		frappe.clear_cache()
		try:
			from pet_app.api.mobile.home_builder import clear_home_cache

			clear_home_cache()
		except Exception as exc:  # noqa: BLE001 - cache invalidation must not fail a rebuild
			_log(f"home cache clear failed: {exc}")

	return summary


# ── hook handlers ───────────────────────────────────────────────────────────────
# Narrow, single-node work. A full rebuild on every Item Group save would be a nested-set
# rebuild per keystroke in the tree view.


def _suppressed() -> bool:
	"""True when a bulk process owns the tree and the hook must stand down.

	During migrate/patch/install/import the taxonomy is mid-flight: lft/rgt are stale
	between writes, and the merge patch rebuilds the tree and calls the mirror ONCE at
	the end. Firing per row would mirror 40 intermediate states, several of which are
	momentarily wrong by construction.
	"""
	flags = frappe.flags
	return bool(
		getattr(flags, "in_migrate", False)
		or getattr(flags, "in_patch", False)
		or getattr(flags, "in_install", False)
		or getattr(flags, "in_import", False)
		or getattr(flags, "in_test", False)
	)


def _walk_to_store_root(doc) -> bool:
	"""True when the store root Item Group is this group's ancestor.

	Walks parent_item_group rather than reading lft/rgt, because after_insert fires
	BEFORE the nested set is rebuilt: lft and rgt are still 0 there, so an interval test
	would report every newly created group as out of scope. This is the same reasoning as
	ProductCategory._in_store_tree, and the reason the batch path and the hook path use
	different scope tests on purpose.
	"""
	if doc.name == STORE_ROOT_ITEM_GROUP:
		return False
	seen = set()
	parent = doc.get("parent_item_group")
	while parent and parent not in seen:
		if parent == STORE_ROOT_ITEM_GROUP:
			return True
		seen.add(parent)
		parent = frappe.db.get_value("Item Group", parent, "parent_item_group")
	return False


def on_item_group_change(doc, method=None):
	"""after_insert / on_update: project one Item Group onto its category."""
	if _suppressed() or doc.flags.get("from_store_mirror"):
		return
	if _schema_ready():
		return
	if not frappe.db.exists("Item Group", STORE_ROOT_ITEM_GROUP):
		return
	if not _walk_to_store_root(doc):
		return

	hidden = not cint(doc.get(SHOW_IN_SHOP_FIELD))
	if hidden:
		on_item_group_trash(doc, method)
		return

	group = {
		"name": doc.name,
		"parent_item_group": doc.get("parent_item_group"),
		"is_group": doc.get("is_group"),
		"image": doc.get("image"),
		SHOW_IN_SHOP_FIELD: doc.get(SHOW_IN_SHOP_FIELD),
		DISPLAY_ORDER_FIELD: doc.get(DISPLAY_ORDER_FIELD),
		DESCRIPTION_FIELD: doc.get(DESCRIPTION_FIELD),
	}

	try:
		_upsert(group, dry_run=False)
	except Exception as exc:  # noqa: BLE001
		# A storefront projection must never be the reason an Item Group cannot be saved.
		frappe.log_error(
			title="store_category_mirror: upsert failed",
			message=f"Item Group {doc.name}: {exc}",
		)


def on_item_group_trash(doc, method=None):
	"""on_trash: retire the generated category, if the projection still owns it."""
	if _suppressed() or doc.flags.get("from_store_mirror"):
		return
	if _schema_ready():
		return
	if not frappe.db.exists(PRODUCT_CATEGORY_DOCTYPE, doc.name):
		return
	if not cint(frappe.db.get_value(PRODUCT_CATEGORY_DOCTYPE, doc.name, "auto_generated")):
		return  # hand-made: not ours to delete

	try:
		frappe.delete_doc(
			PRODUCT_CATEGORY_DOCTYPE, doc.name, ignore_permissions=True, delete_permanently=True
		)
	except Exception as exc:  # noqa: BLE001
		_log(f"trash refused for {doc.name}: {exc}")
