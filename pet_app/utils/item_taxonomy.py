"""Retail Item Group taxonomy for the Pre Define Item sheet migration.

The sheet's ``category`` column is free text typed by shop staff: 66 distinct values for
what are really 30 kinds of product, with `cat snack`/`cat snacks`, `catwet`, `caat dry`,
`medicel`, `car cans`, `cat mik` and five spellings of shampoo all present. This module is
the one place that turns those strings into a real tree.

Why a constant module and not a DocType: the mapping normalises one fixed sheet. A constant
is diff-reviewable, importable by tests, and re-runnable; a DocType would put the mapping
outside code review and buy nothing.

Two structural facts drive the shape of everything here.

1. ``erpnext.setup.doctype.item_group.item_group.get_item_group_defaults`` reads ONLY the
   item's own Item Group and never walks ancestors, so accounting has to live on every
   **leaf**. Defaults written on a parent look right in the tree and do nothing at invoicing.

2. ``pet_app.pet_app.doctype.product_category.product_category._assert_group_is_adoptable``
   refuses any Item Group that is not a direct child of "Store". Eight enabled Product
   Category rows own the eight existing Store leaves 1:1, so re-parenting `Cat Food` under a
   new `Cat` would break that storefront category's next save. The new parents therefore go
   in BESIDE those leaves, and every new name is chosen not to collide with them
   ("Toys & Play" != "Toys", "Cat Dry Food" != "Cat Food").
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint

RETAIL_ROOT = "Store"
DEFAULT_COMPANY = "Kokh-vet"

# profile -> (income number, expense/COGS number, inventory number, warehouse label)
# Numbers are French PCG; they are resolved to real Account docnames at runtime because the
# names carry the Arabic label plus the " - K" company suffix.
ACCOUNTING_PROFILES = {
	"food": ("707300", "603700", "373000", "المخزن الرئيسي"),
	"accessory": ("707400", "603700", "374000", "المخزن الرئيسي"),
	"vetmed": ("707100", "603700", "371000", "رفوف الصيدلية"),
	# Added for the existing Pharmacy / Services / original Store leaves, which carried no
	# accounting at all and fell through to Company.default_income_account - 707000, a GOODS
	# account. The chart already had a matching income/purchase/inventory set for each of
	# these; none of it was wired to an Item Group.
	"vaccine": ("707200", "603700", "372000", "ثلاجة اللقاحات"),
	"consumable": ("707500", "603700", "375000", "رفوف الصيدلية"),
	"controlled": ("707600", "603700", "377000", "رفوف الصيدلية"),
	# Services hold no stock, so inventory and warehouse are deliberately None. The expense
	# side is 611000 (outsourced services), not COGS: nothing is taken out of inventory when
	# a surgery or a bath is sold.
	"service": ("706200", "611000", None, None),
}

# The purchase account per profile. french_pcg's ensure_item_group writes the COGS number
# into purchase_expense_account too, which posts purchases to COGS; use the matching 607xxx.
PURCHASE_ACCOUNTS = {
	"food": "607300",
	"accessory": "607400",
	"vetmed": "607100",
	"vaccine": "607200",
	"consumable": "607500",
	"controlled": "607600",
	# A service is bought as an outsourced service, not as merchandise, so its purchase side
	# is the same 611000 as its expense side rather than a 607xxx goods-purchase account.
	"service": "611000",
}

# Profiles whose revenue is a service, and which therefore take the services discount account
# rather than the goods one.
SERVICE_PROFILES = frozenset({"service"})

# Discounts granted on goods. Every leaf in this taxonomy is merchandise, so they all take
# 709700; 709600 is the services counterpart and is not used here.
#
# This cannot be left to fall back: get_default_discount_account (get_item_details.py:928)
# walks Item -> Item Group -> Brand -> ctx and stops. Unlike income_account and
# expense_account there is NO Company fallback, so Company.default_discount_account is never
# consulted and a blank row means the invoice line simply has no discount account.
DISCOUNT_ACCOUNT_GOODS = "709700"
DISCOUNT_ACCOUNT_SERVICES = "709600"

# (english, arabic) - created as is_group=1 directly under RETAIL_ROOT
PARENTS = (
	("Cat", "قطط"),
	("Dog", "كلاب"),
	("Pet Health", "صحة الحيوان"),
	("Litter & Hygiene", "رمل ونظافة"),
	("Grooming & Care", "عناية وتنظيف"),
	("Home & Accessories", "مستلزمات واكسسوارات"),
	("Birds & Small Pets", "طيور وحيوانات صغيرة"),
)

# (english, arabic, parent, profile) - created as is_group=0, each carrying accounting
LEAVES = (
	("Cat Dry Food", "طعام قطط جاف", "Cat", "food"),
	("Cat Wet Food", "طعام قطط رطب", "Cat", "food"),
	("Cat Snacks", "مكافآت قطط", "Cat", "food"),
	("Cat Milk", "حليب قطط", "Cat", "food"),
	("Cat Essentials", "مستلزمات قطط", "Cat", "accessory"),
	("Dog Dry Food", "طعام كلاب جاف", "Dog", "food"),
	("Dog Wet Food", "طعام كلاب رطب", "Dog", "food"),
	("Dog Snacks", "مكافآت كلاب", "Dog", "food"),
	("Dog Essentials", "مستلزمات كلاب", "Dog", "accessory"),
	("OTC Pet Health", "منتجات صحية", "Pet Health", "vetmed"),
	("Supplements & Oils", "مكملات وزيوت", "Pet Health", "food"),
	("Liquid Medicines", "أشربة وقطرات", "Pet Health", "vetmed"),
	("Tablets", "حبوب", "Pet Health", "vetmed"),
	("Cat Litter", "رمل قطط", "Litter & Hygiene", "accessory"),
	("Litter Boxes & Scoops", "صناديق وفرش رمل", "Litter & Hygiene", "accessory"),
	("Litter Deodorisers", "معطرات رمل", "Litter & Hygiene", "accessory"),
	("Pads & Diapers", "حفاضات ولبادات", "Litter & Hygiene", "accessory"),
	("Cleaning Wipes", "مناديل تنظيف", "Litter & Hygiene", "accessory"),
	("Cleaning Supplies", "مواد تنظيف", "Litter & Hygiene", "accessory"),
	("Shampoo & Conditioner", "شامبو وبلسم", "Grooming & Care", "accessory"),
	("Grooming Tools", "أدوات عناية", "Grooming & Care", "accessory"),
	("Multi-Pet Care", "عناية قطط وكلاب", "Grooming & Care", "accessory"),
	("Bowls & Feeders", "مواعين ومشارب", "Home & Accessories", "accessory"),
	("Toys & Play", "ألعاب", "Home & Accessories", "accessory"),
	("Bags & Carriers", "حقائب ونقل", "Home & Accessories", "accessory"),
	("Pet Clothing", "ملابس حيوانات", "Home & Accessories", "accessory"),
	("Bulk / Open Kg", "بيع بالوزن", "Home & Accessories", "food"),
	("Uncategorised", "غير مصنف", "Home & Accessories", "accessory"),
	("Bird Food", "حبوب وأعلاف طيور", "Birds & Small Pets", "food"),
	("Small Pet Housing", "أقفاص ومساكن", "Birds & Small Pets", "accessory"),
)

# Leaves whose stock UOM is not the default "Unit". "open kg" is loose food sold by
# weight (فل كلاب نص كيلو = half a kilo), so a per-piece UOM would be meaningless.
LEAF_STOCK_UOM = {"Bulk / Open Kg": "Kg"}

# Normalised raw sheet category -> leaf. Every one of the 66 values the sheet actually
# contains appears here; test_item_taxonomy asserts that, so a 67th value in a future sheet
# fails loudly on its own row instead of landing somewhere plausible-looking and wrong.
CATEGORY_MAP = {
	"cat dry": "Cat Dry Food",
	"caat dry": "Cat Dry Food",
	"cat wet": "Cat Wet Food",
	"catwet": "Cat Wet Food",
	"cat cans": "Cat Wet Food",
	"car cans": "Cat Wet Food",  # typo for "cat cans": معلبات قطط سناكي دجاج كتن
	"cat square": "Cat Wet Food",
	"cat snacks": "Cat Snacks",
	"cat snack": "Cat Snacks",
	"cat milk": "Cat Milk",
	"cat mik": "Cat Milk",
	"milk": "Cat Milk",
	"cat": "Cat Essentials",  # CAT TREE LIANA/CASBAH/... - cat trees, not cat food
	"cats": "Cat Essentials",
	"for cat": "Cat Essentials",
	"dog dry": "Dog Dry Food",
	"dog wet": "Dog Wet Food",
	"dog cans": "Dog Wet Food",
	"dog square": "Dog Wet Food",
	"dog snacks": "Dog Snacks",
	"dog snack": "Dog Snacks",
	"dogs snack": "Dog Snacks",
	"dog": "Dog Essentials",
	"dog poll": "Dog Essentials",  # مسبح كلاب - a dog pool, not a leash
	"medical": "OTC Pet Health",
	"medicel": "OTC Pet Health",
	"tubes": "Supplements & Oils",  # عصارة - hairball/vitamin pastes
	"oil": "Supplements & Oils",
	"syrup": "Liquid Medicines",
	"drops": "Liquid Medicines",
	"tablets": "Tablets",
	"litter": "Cat Litter",
	"met": "Litter Boxes & Scoops",  # فرشة لتر بوكس - litter box liner
	"perfume": "Litter Deodorisers",
	"box perfiume": "Litter Deodorisers",
	"pads": "Pads & Diapers",
	"dog diapers": "Pads & Diapers",
	"dog daipers": "Pads & Diapers",
	"wipes": "Cleaning Wipes",
	"wipes glove": "Cleaning Wipes",
	"powder": "Cleaning Supplies",
	"sapry": "Cleaning Supplies",
	"gloves": "Cleaning Supplies",
	"shampo": "Shampoo & Conditioner",
	"cat shampo": "Shampoo & Conditioner",
	"dog shampo": "Shampoo & Conditioner",
	"cat&dog shampo": "Shampoo & Conditioner",
	"cat& dog shampo": "Shampoo & Conditioner",
	"hair remover": "Grooming Tools",
	"cat&dogs": "Multi-Pet Care",
	"cat &dog": "Multi-Pet Care",
	"cats&dog": "Multi-Pet Care",
	"dog&cat": "Multi-Pet Care",
	"plate": "Bowls & Feeders",
	"toy": "Toys & Play",
	"bag": "Bags & Carriers",
	"cloths": "Pet Clothing",  # ملابس قطط وكلاب - pet clothes, NOT cleaning cloths
	"open kg": "Bulk / Open Kg",
	"others": "Uncategorised",
	"other": "Uncategorised",
	"parrot dry": "Bird Food",
	"parrot": "Bird Food",
	"feed": "Bird Food",  # علف بلجيكي primus - parrot feed
	"home homster": "Small Pet Housing",
	# "box" and "dry" are resolved per row against the Arabic name - see resolve_leaf().
}

# One English word, two products. Only the Arabic name separates them.
_ARABIC_SPLITS = {
	"box": (("لتر بوكس", "Litter Boxes & Scoops"), (None, "Bags & Carriers")),
	"dry": (("طيور", "Bird Food"), (None, "Dog Dry Food")),
}

# Damage/waste bookkeeping rows (تلف), not sellable products. Migrated disabled so the till
# can still resolve a scanned code but nobody can put one on an invoice.
SCRAP_ARABIC_PREFIXES = ("تلف", "تالف")


def normalise_category(text: str | None) -> str:
	"""Casefold and collapse whitespace so '  CAT   Dry ' matches 'cat dry'."""
	return " ".join((text or "").split()).casefold()


def resolve_leaf(category: str | None, arabic_name: str | None = None) -> str | None:
	"""The leaf Item Group for a sheet category, or None when the sheet value is unknown."""
	key = normalise_category(category)
	if key in _ARABIC_SPLITS:
		for needle, leaf in _ARABIC_SPLITS[key]:
			if needle is None or needle in (arabic_name or ""):
				return leaf
	return CATEGORY_MAP.get(key)


def is_scrap_row(arabic_name: str | None) -> bool:
	return (arabic_name or "").strip().startswith(SCRAP_ARABIC_PREFIXES)


def leaf_stock_uom(leaf: str) -> str | None:
	return LEAF_STOCK_UOM.get(leaf)


def _leaf_index() -> dict[str, tuple[str, str, str]]:
	return {en: (ar, parent, profile) for en, ar, parent, profile in LEAVES}


def _purchase_account(company: str, profile: str) -> str | None:
	"""The 607xxx purchase account for a profile, resolved to its real docname."""
	from pet_app.setup.french_pcg_vet_store import get_account_name

	return get_account_name(company, PURCHASE_ACCOUNTS[profile])


def _discount_account(company: str, profile: str | None = None) -> str | None:
	"""709700 for merchandise, 709600 for services.

	Defaults to goods so the retail taxonomy - every leaf of which is merchandise - keeps
	calling this with one argument.
	"""
	from pet_app.setup.french_pcg_vet_store import get_account_name

	number = DISCOUNT_ACCOUNT_SERVICES if profile in SERVICE_PROFILES else DISCOUNT_ACCOUNT_GOODS
	return get_account_name(company, number)


def _ensure_parent(name_en: str, name_ar: str) -> str:
	"""Create the parent as a group node under RETAIL_ROOT. Never save an existing group.

	Saving an existing Item Group would reach pet_app.api.product.before_save, which renames
	the doc whenever item_group_name differs from name. This writer has no reason to save.
	"""
	row = frappe.db.get_value("Item Group", name_en, ["name", "is_group"], as_dict=True)
	if row:
		if not cint(row.is_group):
			frappe.throw(
				_("Item Group {0} already exists as a leaf and holds items; refusing to promote it.").format(
					frappe.bold(name_en)
				)
			)
		return row.name

	doc = frappe.get_doc(
		{
			"doctype": "Item Group",
			"item_group_name": name_en,
			"parent_item_group": RETAIL_ROOT,
			"is_group": 1,
			"arabic_name": name_ar,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def ensure_leaf_chain(leaf: str, company: str = DEFAULT_COMPANY) -> str:
	"""Ensure RETAIL_ROOT > parent > leaf exists, with accounting written on the leaf."""
	from pet_app.setup.french_pcg_vet_store import ensure_item_group

	index = _leaf_index()
	if leaf not in index:
		frappe.throw(_("{0} is not a declared taxonomy leaf.").format(frappe.bold(leaf)))
	leaf_ar, parent_en, profile = index[leaf]

	root = frappe.db.get_value("Item Group", RETAIL_ROOT, ["name", "is_group"], as_dict=True)
	if not root:
		frappe.throw(_("Item Group {0} does not exist.").format(frappe.bold(RETAIL_ROOT)))
	if not cint(root.is_group):
		frappe.throw(_("Item Group {0} is not a group node.").format(frappe.bold(RETAIL_ROOT)))

	parent = _ensure_parent(parent_en, dict(PARENTS)[parent_en])

	# ensure_item_group sets is_group=0 unconditionally, so a group node handed to it would
	# be demoted and its children orphaned. Refuse before calling rather than after.
	existing = frappe.db.get_value("Item Group", leaf, ["name", "is_group"], as_dict=True)
	if existing and cint(existing.is_group):
		frappe.throw(
			_("Item Group {0} is a group node and cannot hold items.").format(frappe.bold(leaf))
		)

	name = ensure_item_group(
		company=company,
		item_group=leaf,
		parent_item_group=parent,
		income_number=ACCOUNTING_PROFILES[profile][0],
		expense_number=ACCOUNTING_PROFILES[profile][1],
		inventory_number=ACCOUNTING_PROFILES[profile][2],
		warehouse_name=ACCOUNTING_PROFILES[profile][3],
	)

	if not frappe.db.get_value("Item Group", name, "arabic_name"):
		frappe.db.set_value("Item Group", name, "arabic_name", leaf_ar, update_modified=False)

	# ensure_item_group writes the COGS number into purchase_expense_account; point it at the
	# matching 607xxx purchase account instead so purchases do not post to COGS.
	# ensure_item_group handles income/expense/inventory/warehouse but knows nothing about the
	# purchase or discount accounts, so those two are written here.
	row = frappe.db.get_value(
		"Item Default", {"parent": name, "parenttype": "Item Group", "company": company}, "name"
	)
	if row:
		extra = {
			"purchase_expense_account": _purchase_account(company, profile),
			"default_discount_account": _discount_account(company),
		}
		for field, value in extra.items():
			if value and frappe.db.has_column("Item Default", field):
				frappe.db.set_value("Item Default", row, field, value, update_modified=False)
	return name


def ensure_taxonomy(company: str = DEFAULT_COMPANY, dry_run: bool = False) -> dict:
	"""Build the whole tree. Idempotent; safe to re-run."""
	report = {"parents_created": [], "leaves_created": [], "leaves_updated": [], "company": company}

	for name_en, _name_ar in PARENTS:
		exists = frappe.db.exists("Item Group", name_en)
		if not exists:
			report["parents_created"].append(name_en)
		if not dry_run:
			_ensure_parent(name_en, dict(PARENTS)[name_en])

	for leaf_en, _ar, _parent, _profile in LEAVES:
		exists = frappe.db.exists("Item Group", leaf_en)
		(report["leaves_updated"] if exists else report["leaves_created"]).append(leaf_en)
		if not dry_run:
			ensure_leaf_chain(leaf_en, company=company)

	if not dry_run:
		frappe.db.commit()
		# get_item_group_defaults reads a cached doc; without this the new rows are invisible.
		frappe.clear_cache()
	return report


# ---------------------------------------------------------------------------
# Accounting for the leaves that already existed before this taxonomy was built
# ---------------------------------------------------------------------------
#
# The retail tree above was created with accounting from the start. Three older branches
# were not: the whole Pharmacy tree, the Services leaf, and the eight original Store leaves.
# 61 leaves holding 355 items resolved to Company.default_income_account instead - 707000,
# "مبيعات بضائع - افتراضي". For the 94 Services items that is not merely untidy: a surgery,
# a bath and a CBC were all booking their revenue to merchandise sales.

PHARMACY_ROOT = "Pharmacy"

# Everything under Pharmacy is a veterinary medicine unless it is named here. Expressed as a
# rule rather than 42 rows so a new drug category added to the tree inherits the right
# account instead of silently falling back to 707000.
PHARMACY_DEFAULT_PROFILE = "vetmed"

EXISTING_TREE_PROFILES = {
	# --- Pharmacy exceptions ------------------------------------------------
	"Vaccines & Immunologicals": "vaccine",
	"Controlled Drugs (Narcotics)": "controlled",
	# Hill's Prescription Diet i/d, Royal Canin Recovery, Fortiflora. Prescription food is
	# still food; booking it as drug revenue would overstate pharmacy sales.
	"Feed Additives & Nutritional Support": "food",
	"Probiotics & Digestive Enzymes": "food",
	# Consumed during treatment rather than dispensed: saline, dextrose, fluorescein strips,
	# ear cleaner, antiseptic creams.
	"Disinfectants & Antiseptics": "consumable",
	"IV Fluids & Plasma Expanders": "consumable",
	# --- Services -----------------------------------------------------------
	"Services": "service",
	# --- The eight original Store leaves ------------------------------------
	# Accounting only. These must NEVER be re-parented or promoted: eight enabled Product
	# Category rows own them 1:1 and _assert_group_is_adoptable refuses any group that is not
	# a direct child of Store. Writing an Item Default row does not touch the tree.
	"Cat Food": "food",
	"Dog Food": "food",
	"Bird Care": "food",
	"Aquatics": "food",
	"Accessories": "accessory",
	"Toys": "accessory",
	"Grooming": "accessory",
	"Health": "vetmed",
}

# ERPNext's own scaffolding groups. All empty, none is a sales category, and giving them
# revenue accounts would imply they are sold from.
UNMANAGED_LEAVES = frozenset({"Consumable", "Products", "Raw Material", "Sub Assemblies"})

# The Item Default columns this writes. Kept as one tuple so the report, the writer and the
# verification all agree on what "complete" means.
ITEM_DEFAULT_FIELDS = (
	"company",
	"income_account",
	"expense_account",
	"purchase_expense_account",
	"default_discount_account",
	"default_inventory_account",
	"default_warehouse",
)


def _is_under(node: str, ancestor: str) -> bool:
	"""Nested-set descendant test, so the Pharmacy rule needs no hardcoded child list."""
	parent = frappe.db.get_value("Item Group", ancestor, ["lft", "rgt"], as_dict=True)
	child = frappe.db.get_value("Item Group", node, ["lft", "rgt"], as_dict=True)
	if not parent or not child:
		return False
	return cint(parent.lft) < cint(child.lft) and cint(child.rgt) < cint(parent.rgt)


def resolve_existing_profile(leaf: str) -> str | None:
	"""Which accounting profile an already-existing leaf should carry, or None to skip."""
	if leaf in UNMANAGED_LEAVES:
		return None
	if leaf in EXISTING_TREE_PROFILES:
		return EXISTING_TREE_PROFILES[leaf]
	if _is_under(leaf, PHARMACY_ROOT):
		return PHARMACY_DEFAULT_PROFILE
	return None


def _warehouse_name(company: str, label: str | None) -> str | None:
	if not label:
		return None
	return frappe.db.get_value("Warehouse", {"company": company, "warehouse_name": label}, "name")


def _profile_values(company: str, profile: str) -> dict:
	from pet_app.setup.french_pcg_vet_store import get_account_name

	income, expense, inventory, warehouse = ACCOUNTING_PROFILES[profile]
	return {
		"company": company,
		"income_account": get_account_name(company, income),
		"expense_account": get_account_name(company, expense),
		"purchase_expense_account": _purchase_account(company, profile),
		"default_discount_account": _discount_account(company, profile),
		"default_inventory_account": get_account_name(company, inventory) if inventory else None,
		"default_warehouse": _warehouse_name(company, warehouse),
	}


def ensure_existing_tree_accounting(company: str = DEFAULT_COMPANY, dry_run: bool = False) -> dict:
	"""Write accounting onto the pre-existing Pharmacy / Services / Store leaves.

	Writes the Item Default row directly instead of going through french_pcg's
	ensure_item_group, which sets is_group=0 unconditionally and rewrites parent_item_group -
	handing it "Pharmacy" would demote the branch and orphan its 48 children.

	Idempotent. Safe to re-run; a leaf already holding the intended values reports as
	unchanged.
	"""
	report = {"company": company, "created": [], "updated": [], "unchanged": [], "skipped": []}

	leaves = frappe.get_all(
		"Item Group", filters={"is_group": 0}, fields=["name"], order_by="lft", ignore_permissions=True
	)
	for row in leaves:
		leaf = row.name
		profile = resolve_existing_profile(leaf)
		if not profile:
			continue

		# A group node cannot hold items, and get_item_group_defaults would never read it.
		if cint(frappe.db.get_value("Item Group", leaf, "is_group")):
			report["skipped"].append((leaf, "is a group node"))
			continue

		values = _profile_values(company, profile)
		missing = [f for f in ("income_account", "expense_account") if not values.get(f)]
		if missing:
			report["skipped"].append((leaf, f"unresolved account(s): {', '.join(missing)}"))
			continue

		existing = frappe.db.get_value(
			"Item Default",
			{"parent": leaf, "parenttype": "Item Group", "company": company},
			"name",
		)

		if existing:
			current = frappe.db.get_value("Item Default", existing, ITEM_DEFAULT_FIELDS, as_dict=True)
			changes = {f: v for f, v in values.items() if (current.get(f) or None) != (v or None)}
			if not changes:
				report["unchanged"].append((leaf, profile))
				continue
			report["updated"].append((leaf, profile, sorted(changes)))
			if not dry_run:
				for field, value in changes.items():
					frappe.db.set_value("Item Default", existing, field, value, update_modified=False)
			continue

		report["created"].append((leaf, profile))
		if dry_run:
			continue
		# append + save on the parent rather than a raw insert, so Frappe assigns the child
		# name, idx and parent fields the way every other Item Default row on this site got them.
		doc = frappe.get_doc("Item Group", leaf)
		doc.append("item_group_defaults", values)
		doc.flags.ignore_permissions = True
		doc.save(ignore_permissions=True)

	if not dry_run:
		frappe.db.commit()
		# get_item_group_defaults reads a cached doc; without this the new rows are invisible.
		frappe.clear_cache()
	return report
