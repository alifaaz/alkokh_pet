from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt

from pet_app.api.permissions import require_doctype_permission
from pet_app.api.response import standardize_response


# ─────────────────────────────────────────
# Store Item Attributes
#
# Attributes are DATA (Item Attribute records), never a hardcoded Select. The old
# Product Variant.options field pinned them to "size/color/weight" and could not be
# extended without a schema change; that is the mistake this module exists to avoid.
#
# Abbreviations are load-bearing: ERPNext builds variant item codes from them in
# make_variant_item_code() - "{template_code}-{abbr}-{abbr}". They become permanent SKU
# suffixes, so they are declared here explicitly rather than derived from the value.
#
# numeric_values stays 0 on all of these. Setting it to 1 activates
# validate_is_incremental(), which expects a continuous from_range/to_range/increment
# measure - wrong for a fixed pack ladder, and it puts the raw number in the SKU instead
# of the chosen abbreviation.
# ─────────────────────────────────────────

STORE_ITEM_ATTRIBUTES = (
    {
        "attribute_name": "Weight",
        "values": (
            ("400g", "400G"),
            ("900g", "900G"),
            ("2kg", "2KG"),
            ("6kg", "6KG"),
            ("11.4kg", "11K"),
            ("15kg", "15KG"),
        ),
    },
    {
        "attribute_name": "Volume",
        "values": (
            ("100ml", "100M"),
            ("250ml", "250M"),
            ("500ml", "500M"),
            ("1L", "1L"),
            ("5L", "5L"),
        ),
    },
    {
        "attribute_name": "Flavour",
        "values": (
            ("Chicken", "CHK"),
            ("Beef", "BEF"),
            ("Tuna", "TUN"),
            ("Salmon", "SAL"),
            ("Lamb", "LMB"),
            ("Mixed", "MIX"),
        ),
    },
    {
        "attribute_name": "Pack Size",
        "values": (
            ("Single", "SGL"),
            ("6 Pack", "6PK"),
            ("12 Pack", "12PK"),
            ("24 Pack", "24PK"),
        ),
    },
    {
        "attribute_name": "Breed Size",
        "values": (
            ("Small", "SM"),
            ("Medium", "MD"),
            ("Large", "LG"),
            ("Giant", "GT"),
        ),
    },
)

ITEM_ATTRIBUTE_DOCTYPE = "Item Attribute"

# ─────────────────────────────────────────
# Scope guard for the store-as-Item-author amendment
#
# The store may create RETAIL variant templates and nothing else. The allowed set is
# derived from the Item Group nested set - every leaf under this root - rather than a
# hardcoded name list, so adding a retail group makes it available with no code change.
#
# The root is "Mobile Shop", NOT its parent "Pet Supplies". That distinction is the whole
# guard: Mobile Shop (lft 55-68) is a SIBLING of Pharmacy (lft 69-166) under Pet Supplies,
# so rooting at Pet Supplies would hand the store the entire clinical pharmacy tree -
# Antibiotics, Controlled Drugs (Narcotics), Anesthetics, all of it.
# ─────────────────────────────────────────

STORE_ITEM_GROUP_ROOT = "Mobile Shop"

# Store-side ceiling on one generate request. ERPNext only refuses at 600 and silently
# BACKGROUNDS anything from 10 up - so 590 Items from a single click is a supported
# outcome upstream, with no confirmation and no visible failure.
#
# 100 rather than something tighter: the widest pair the seeded ladders can produce is
# Weight x Flavour = 36, so every realistic two-attribute product clears this with room
# to spare, as do sensible three-attribute ones (Breed Size x Pack Size x Weight = 96).
# Past ~100 the constraint stops being technical and becomes curation - every variant is
# a real Item that needs its own price and stock before it can be sold, so a click that
# creates more than an admin can price in one sitting should be a deliberate decision
# taken in batches, not an accident.
MAX_VARIANT_COMBINATIONS = 100

# Stated verbatim in create_variant_template's and set_template_attributes' responses so
# the client shows one wording, not two paraphrases of the same rule.
ATTRIBUTES_LOCKED_NOTICE = (
    "Attributes are fixed once the first variant exists. Add every attribute you need "
    "before generating variants."
)

def _store_attribute_names():
    """Attributes the store may build templates from: every ENABLED Item Attribute.

    Derived from a property, not a name list. The previous version whitelisted the five
    seeded constants, which meant an attribute an admin legitimately created through
    create_item_attribute could never be used - the same hardcoding the item-group guard
    already removed.

    `disabled` is Item Attribute's own native field, so this needs no schema change and
    gives the retirement path deletion cannot: disabling an attribute stops NEW templates
    using it while leaving existing variant item codes intact (their SKUs are built from
    its abbreviations and would be orphaned by a delete).

    Scope note: the primary boundary is the item-group guard, which confines the store to
    retail groups no matter which attribute is chosen. This is a validity check on top of
    it. Variants are a store-only feature today (0 templates and 0 Item Variant Attribute
    rows anywhere), so "enabled" and "store" are currently the same set. If clinical
    variants ever appear, this is the place to add an explicit store flag.
    """
    return frappe.get_all(
        ITEM_ATTRIBUTE_DOCTYPE,
        filters={"disabled": 0},
        pluck="name",
        order_by="name asc",
        ignore_permissions=True,
    )


def _store_item_groups():
    """Leaf Item Groups the store may create templates in.

    `lft > root.lft` / `rgt < root.rgt` excludes the root itself, and `is_group = 0`
    requires a leaf - a template belongs in "Dog Food", not in the "Mobile Shop" node.
    """
    root = frappe.db.get_value("Item Group", STORE_ITEM_GROUP_ROOT, ["lft", "rgt"], as_dict=True)
    if not root:
        return []
    return frappe.get_all(
        "Item Group",
        filters={"lft": [">", root.lft], "rgt": ["<", root.rgt], "is_group": 0},
        pluck="name",
        order_by="lft asc",
        ignore_permissions=True,
    )


def _assert_store_item_group(item_group):
    """Refuse any group outside the retail subtree."""
    item_group = cstr(item_group).strip()
    if not item_group:
        frappe.throw(_("Item group is required."))
    allowed = _store_item_groups()
    if not allowed:
        frappe.throw(
            _("No retail item groups are configured under {0}. Create one before adding store products.").format(
                STORE_ITEM_GROUP_ROOT
            )
        )
    if item_group not in allowed:
        frappe.throw(
            _("{0} is not a retail item group. The store can only create products in: {1}.").format(
                item_group, ", ".join(allowed)
            )
        )
    return item_group


def _assert_store_attributes(attributes):
    """Attribute names must be storefront attributes that actually exist."""
    if isinstance(attributes, str) and attributes.strip():
        attributes = json.loads(attributes)
    if not isinstance(attributes, (list, tuple)) or not attributes:
        frappe.throw(_("At least one attribute is required to build a variant template."))

    allowed = _store_attribute_names()
    names, seen = [], set()
    for entry in attributes:
        name = cstr(entry.get("attribute") if isinstance(entry, dict) else entry).strip()
        if not name:
            frappe.throw(_("Attribute name cannot be blank."))
        if not frappe.db.exists(ITEM_ATTRIBUTE_DOCTYPE, name):
            frappe.throw(_("Item Attribute {0} does not exist. Create it first.").format(name))
        if name not in allowed:
            # Exists but disabled - said plainly, because "does not exist" would send the
            # admin looking for the wrong problem.
            frappe.throw(
                _("Item Attribute {0} is disabled and cannot be used for new templates. Available: {1}.").format(
                    name, ", ".join(allowed) or "none"
                )
            )
        if name.lower() in seen:
            frappe.throw(_("Duplicate attribute {0}.").format(name))
        seen.add(name.lower())
        names.append(name)
    return names


def _require_attribute_permission(ptype: str):
    """Gate on the REAL doctype permission, not on a store role.

    Item Attribute is a global ERPNext master shared with the clinical catalogue, so the
    store-admin-creates-Items amendment has to be granted deliberately by an
    administrator rather than implied by this API. Today only "Item Manager" carries
    create rights on Item Attribute; a store admin needs that role (or an explicit Custom
    DocPerm) before this endpoint will let them through.

    Deliberately NOT reusing product.py's _check_permission, which passes on a store role
    OR a Product permission - that would let the E-commerce role author a global master
    without anyone granting it.
    """
    if frappe.has_permission(ITEM_ATTRIBUTE_DOCTYPE, ptype=ptype):
        return
    frappe.throw(
        _("Not permitted to {0} Item Attribute. This is a shared catalogue master.").format(ptype),
        frappe.PermissionError,
    )


def _attribute_payload(name: str) -> dict:
    """One attribute with its values, in declaration order."""
    row = frappe.db.get_value(
        ITEM_ATTRIBUTE_DOCTYPE, name, ["name", "attribute_name", "numeric_values", "disabled"], as_dict=True
    )
    if not row:
        return {}
    values = frappe.get_all(
        "Item Attribute Value",
        filters={"parent": name, "parenttype": ITEM_ATTRIBUTE_DOCTYPE},
        fields=["attribute_value", "abbr", "idx"],
        order_by="idx asc",
        ignore_permissions=True,
    )
    return {
        "name": row.name,
        "attribute_name": row.attribute_name or row.name,
        "numeric_values": cint(row.numeric_values),
        # Gates whether NEW templates may use this attribute - see _store_attribute_names.
        "disabled": bool(cint(row.disabled)),
        "usable_for_templates": not bool(cint(row.disabled)),
        # `value`/`abbr` deliberately mirrors the shape the variant pickers will consume.
        "values": [{"value": v.attribute_value, "abbr": v.abbr} for v in values],
        "value_count": len(values),
    }


def _coerce_values(values):
    """Accept [{value, abbr}] or [[value, abbr]] or a JSON string of either."""
    if isinstance(values, str) and values.strip():
        values = json.loads(values)
    if not isinstance(values, (list, tuple)) or not values:
        frappe.throw(_("At least one attribute value is required."))

    cleaned, seen_values, seen_abbrs = [], set(), set()
    for entry in values:
        if isinstance(entry, dict):
            value = cstr(entry.get("value") or entry.get("attribute_value")).strip()
            abbr = cstr(entry.get("abbr")).strip()
        elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
            value, abbr = cstr(entry[0]).strip(), cstr(entry[1]).strip()
        else:
            value, abbr = cstr(entry).strip(), ""

        if not value:
            frappe.throw(_("Attribute value cannot be blank."))
        if not abbr:
            # Not auto-derived: the abbreviation ends up in every variant's item code, so
            # a silent guess would mint permanent SKUs nobody chose.
            frappe.throw(_("Abbreviation is required for value {0} - it becomes part of the variant SKU.").format(value))
        if value.lower() in seen_values:
            frappe.throw(_("Duplicate attribute value {0}.").format(value))
        if abbr.lower() in seen_abbrs:
            frappe.throw(_("Duplicate abbreviation {0} - abbreviations must be unique within an attribute.").format(abbr))
        seen_values.add(value.lower())
        seen_abbrs.add(abbr.lower())
        cleaned.append({"attribute_value": value, "abbr": abbr})

    return cleaned


@frappe.whitelist(methods=["POST"])
@standardize_response
def create_variant_template(item_name=None, item_group=None, stock_uom=None,
                            brand=None, attributes=None, **kwargs):
    """Create a retail variant TEMPLATE Item (has_variants=1).

    This is the store acting as an Item author, so the scope guard is the point: the
    item_group must be a leaf under STORE_ITEM_GROUP_ROOT and the attributes must be
    storefront attributes. Both are checked before anything is written.

    ATTRIBUTES ARE FIXED ONCE THE FIRST VARIANT EXISTS. Declare every attribute the
    product will ever need here. Until the first generate they can still be changed
    through set_template_attributes; after that neither this app nor ERPNext can change
    them safely, so the only remedy is a new template. The response says so explicitly
    rather than leaving a client to infer later editability - this is the one moment the
    fact is actionable.

    Gated on the real `Item` create permission, not a store role - same principle as
    create_item_attribute. Item is a shared clinical master; the amendment has to be
    granted by an administrator, not implied by this endpoint.
    """
    from pet_app.api.product import _item_snapshot

    require_doctype_permission("Item", "create")

    item_name = cstr(item_name or kwargs.get("item_code")).strip()
    if not item_name:
        frappe.throw(_("Item name is required."))
    item_group = _assert_store_item_group(item_group)
    stock_uom = cstr(stock_uom).strip()
    if not stock_uom:
        frappe.throw(_("Stock UOM is required."))
    if not frappe.db.exists("UOM", stock_uom):
        frappe.throw(_("UOM {0} does not exist.").format(stock_uom))
    brand = cstr(brand).strip() or None
    if brand and not frappe.db.exists("Brand", brand):
        frappe.throw(_("Brand {0} does not exist.").format(brand))

    attribute_names = _assert_store_attributes(attributes)

    # Idempotency: Item autoname is the item_code, so a duplicate would collide anyway -
    # this reports it as a clear conflict instead of a DuplicateEntryError.
    if frappe.db.exists("Item", item_name):
        frappe.throw(_("Item {0} already exists.").format(item_name))

    doc = frappe.new_doc("Item")
    doc.item_code = item_name
    doc.item_name = item_name
    doc.item_group = item_group
    doc.stock_uom = stock_uom
    doc.is_stock_item = 1
    doc.has_variants = 1
    doc.variant_based_on = "Item Attribute"
    if brand:
        doc.brand = brand
    for name in attribute_names:
        doc.append("attributes", {"attribute": name})
    doc.insert()

    frappe.response["data"] = {
        "template": _item_snapshot(doc.name),
        # True by construction - a template created this instant has no variants. Stated
        # anyway so a client renders the window and its expiry at the moment it can act
        # on them, instead of discovering the lock when the edit is refused.
        "attributes_editable": True,
        "attributes_notice": _(ATTRIBUTES_LOCKED_NOTICE),
    }


@frappe.whitelist(methods=["POST"])
@standardize_response
def generate_variants(template=None, attribute_values=None, use_template_image=0, **kwargs):
    """Generate variant Items for a template from {attribute: [values]}.

    Delegates the cartesian product, the existing-variant skip and the background
    dispatch to ERPNext's enqueue_multiple_variant_creation. Reimplementing any of that
    is how the deleted variant code ended up with its blank-attribute_value bug.

    Boundary, straight from the ERPNext helper:
      * < 10 combinations  -> created synchronously, returns the count
      * >= 10              -> enqueued as a background job, returns queued
      * >= 600             -> ERPNext throws ("more than 500 items at a time")
    """
    from erpnext.controllers.item_variant import enqueue_multiple_variant_creation

    from pet_app.api.product import _item_snapshot, _variant_rows

    require_doctype_permission("Item", "create")

    template = cstr(template).strip()
    if not template:
        frappe.throw(_("Template item is required."))
    row = frappe.db.get_value("Item", template, ["name", "has_variants", "item_group"], as_dict=True)
    if not row:
        frappe.throw(_("Item {0} was not found.").format(template))
    if not cint(row.has_variants):
        frappe.throw(_("Item {0} is not a variant template.").format(template))
    # Re-checked on generate, not just on create: a template could have been moved into a
    # clinical group after creation, and this endpoint mints Items under it.
    _assert_store_item_group(row.item_group)

    if isinstance(attribute_values, str) and attribute_values.strip():
        attribute_values = json.loads(attribute_values)
    if attribute_values is None:
        attribute_values = kwargs.get("values")
    if not isinstance(attribute_values, dict) or not attribute_values:
        frappe.throw(_("attribute_values must be a mapping of attribute to a list of values."))

    template_attributes = [d.attribute for d in frappe.get_doc("Item", template).attributes]
    cleaned = {}
    for attribute, values in attribute_values.items():
        attribute = cstr(attribute).strip()
        if attribute not in template_attributes:
            frappe.throw(
                _("{0} is not an attribute of template {1}. Template attributes: {2}.").format(
                    attribute, template, ", ".join(template_attributes) or "none"
                )
            )
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, (list, tuple)) or not values:
            frappe.throw(_("Provide at least one value for attribute {0}.").format(attribute))
        allowed = frappe.get_all(
            "Item Attribute Value",
            filters={"parent": attribute, "parenttype": ITEM_ATTRIBUTE_DOCTYPE},
            pluck="attribute_value",
            ignore_permissions=True,
        )
        picked = []
        for value in values:
            value = cstr(value).strip()
            if value not in allowed:
                frappe.throw(
                    _("{0} is not a value of attribute {1}. Allowed: {2}.").format(
                        value, attribute, ", ".join(allowed)
                    )
                )
            if value not in picked:
                picked.append(value)
        cleaned[attribute] = picked

    count = _combination_count(cleaned)
    if count > MAX_VARIANT_COMBINATIONS:
        frappe.throw(
            _("This would create {0} variants ({1}), more than the limit of {2}. "
              "Select fewer values, or generate in smaller batches.").format(
                count,
                " x ".join("{0} {1}".format(a, len(v)) for a, v in cleaned.items()),
                MAX_VARIANT_COMBINATIONS,
            )
        )

    before = {v["code"] for v in _variant_rows(template)}

    # MUST be a JSON string. enqueue_multiple_variant_creation only assigns its local
    # `variants` inside `if isinstance(args, str)`, then iterates it unconditionally - so
    # passing a dict raises NameError inside ERPNext. Verified in the installed source.
    result = enqueue_multiple_variant_creation(
        template, json.dumps(cleaned), use_template_image=cint(use_template_image)
    )
    queued = result == "queued"

    variants = _variant_rows(template)
    created = [v for v in variants if v["code"] not in before]
    frappe.response["data"] = {
        "template": _item_snapshot(template),
        # 0 with queued=True means the job has not run yet, not that nothing will be made.
        "created": len(created),
        "queued": queued,
        "requested_combinations": _combination_count(cleaned),
        "skipped_existing": _combination_count(cleaned) - len(created) if not queued else None,
        "variants": variants,
        "total": len(variants),
    }


def _combination_count(cleaned):
    total = 1
    for values in cleaned.values():
        total *= len(values)
    return total


@frappe.whitelist()
@standardize_response
def list_template_variants(template=None):
    """The variants under a template Item, each with its own price and stock.

    Read-only. Price and stock come from the variant's own Item Price and Bins - the
    template carries neither, which is the whole reason native ERPNext variants were
    chosen over a Product-level options model.
    """
    # Imported here rather than at module scope: product.py owns the Item resolution
    # helpers, so a top-level import in both directions would be circular.
    from pet_app.api.product import _item_snapshot, _variant_price_summary, _variant_rows

    require_doctype_permission("Item", "read")

    template = cstr(template).strip()
    if not template:
        frappe.throw(_("Template item is required."))
    row = frappe.db.get_value("Item", template, ["name", "has_variants"], as_dict=True)
    if not row:
        frappe.throw(_("Item {0} was not found.").format(template))
    if not cint(row.has_variants):
        frappe.throw(
            _("Item {0} is not a variant template. Only items with variants have variant children.").format(template)
        )

    variants = _variant_rows(row.name)
    frappe.response["data"] = {
        "template": _item_snapshot(row.name),
        "variants": variants,
        "variant_price": _variant_price_summary(variants),
        "total": len(variants),
    }


@frappe.whitelist(methods=["POST"])
@standardize_response
def set_template_attributes(template=None, attributes=None):
    """Replace a template's declared attributes - ONLY while it has no variants.

    This exists to make a window visible that already existed and was undiscoverable:
    the attribute set is freely editable right up to the first generate, which is
    precisely when someone realises they forgot Flavour.

    It REFUSES once any variant exists, and that refusal is the point. ERPNext itself
    does not: Item.validate_attributes_in_variants returns early whenever the old
    attributes are a subset of the new ones, which adding always is - so an add against a
    populated template passes silently and leaves every existing variant without a row
    for the new attribute. Nothing throws afterwards either, because
    Item.validate_variant_attributes only runs for new docs. The corruption surfaces at
    generate time, in item_variant.find_variant, which matches candidates on
    `len(args) == len(variant.attributes)`: the old short-attribute variants can never
    match, so the duplicate check goes blind and the old ones survive as orphans that are
    still priced, still publishable and still sellable. See AGENTS.md.

    Removal is guarded by ERPNext (the same function's non-subset branch names the
    offending variants), but the variant-count check here makes both directions refuse
    for the same stated reason instead of one failing with a stock ERPNext table.
    """
    from pet_app.api.product import _item_snapshot

    require_doctype_permission("Item", "write")

    template = cstr(template).strip()
    if not template:
        frappe.throw(_("Template item is required."))
    row = frappe.db.get_value("Item", template, ["name", "has_variants", "item_group"], as_dict=True)
    if not row:
        frappe.throw(_("Item {0} was not found.").format(template))
    if not cint(row.has_variants):
        frappe.throw(_("Item {0} is not a variant template.").format(template))
    # Re-checked here as well as at creation: a template could have been moved into a
    # clinical group afterwards, and this endpoint decides what Items get minted from it.
    _assert_store_item_group(row.item_group)

    variant_count = frappe.db.count("Item", {"variant_of": row.name})
    if variant_count:
        frappe.throw(
            _("Attributes are fixed once the first variant exists. {0} already has {1} "
              "variant(s), so its attributes can no longer be changed - create a new "
              "template declaring the attributes you need.").format(row.name, variant_count)
        )

    names = _assert_store_attributes(attributes)

    doc = frappe.get_doc("Item", row.name)
    doc.set("attributes", [])
    for name in names:
        doc.append("attributes", {"attribute": name})
    # Saving a template normally cascades through Item.on_update -> update_variants, which
    # re-saves every child. Harmless here by construction: we only get this far when the
    # variant count is 0.
    doc.flags.ignore_permissions = True
    doc.save()

    frappe.response["data"] = {
        "template": _item_snapshot(doc.name),
        "attributes_editable": True,
        "attributes_notice": _(ATTRIBUTES_LOCKED_NOTICE),
    }


@frappe.whitelist(methods=["POST"])
@standardize_response
def set_variant_published(variant=None, published=None, **kwargs):
    """Show or hide ONE variant on the storefront.

    Per-variant visibility, so an admin can retire the 11.4kg while the 2kg stays live.
    Writes a single field on the variant Item and nothing else - no Product row is
    touched, because product-level visibility is Product.status and the two are
    deliberately separate levers.

    Rejects anything that is not a variant. A template is not purchasable so its own flag
    would be read by nothing, and a simple item's visibility is its Product's status;
    accepting either would let an admin set a value that silently does nothing.
    """
    from pet_app.api.product import VARIANT_PUBLISHED_FIELD, _truthy_flag, _variant_rows

    require_doctype_permission("Item", "write")

    variant = cstr(variant).strip()
    if not variant:
        frappe.throw(_("Variant item is required."))
    row = frappe.db.get_value("Item", variant, ["name", "variant_of", "has_variants"], as_dict=True)
    if not row:
        frappe.throw(_("Item {0} was not found.").format(variant))
    if not cstr(row.variant_of).strip():
        frappe.throw(
            _("Item {0} is not a variant. Only variants can be published individually - "
              "use publish_product / unpublish_product for the product itself.").format(variant)
        )

    if published is None:
        published = kwargs.get("is_published")
    if published is None:
        frappe.throw(_("published is required."))
    # Same _truthy discipline as the rest of the HTTP surface: the string "0" arrives
    # from a query string and is truthy in Python.
    value = 1 if _truthy_flag(published) else 0

    doc = frappe.get_doc("Item", row.name)
    doc.set(VARIANT_PUBLISHED_FIELD, value)
    doc.flags.ignore_permissions = True
    doc.save()

    # Returned from the same read path the storefront uses, so the caller sees exactly
    # what the next GET will report rather than an optimistic echo of the input.
    match = [v for v in _variant_rows(row.variant_of) if v["code"] == row.name]
    frappe.response["data"] = {"variant": match[0] if match else None}


@frappe.whitelist(methods=["POST"])
@standardize_response
def set_variant_price(variant=None, rate=None, **kwargs):
    """Set or clear ONE variant's selling price on the store price list.

    Owned server-side so the storefront never writes Item Price directly. A raw REST
    upsert would force the client to hardcode the price list, the `selling` flag and the
    UOM, and would bypass ItemPrice.validate - the same doctype-bypass the overlay
    rebuild removed. Here the store price list is the STORE_PRICE_LIST constant and
    everything else is derived.

    `rate = null` DELETES the row rather than storing 0. A 0-rate row would read back as
    `found: true` with an unbuyable price, which is the ambiguous state the "from" price
    logic already has to defend against; deleting makes "no price set" unambiguous, and
    the variant then reports `found: false` and cannot be published.
    """
    from pet_app.api.product import STORE_PRICE_LIST, _item_price, _variant_rows

    # The doctype actually mutated is Item Price, so that is what is gated - the same
    # "real doctype permission, never a store role" principle as set_variant_published,
    # applied to the record being written. Item read is required too, to resolve the
    # variant at all.
    require_doctype_permission("Item", "read")

    variant = cstr(variant).strip()
    if not variant:
        frappe.throw(_("Variant item is required."))
    row = frappe.db.get_value("Item", variant, ["name", "variant_of", "stock_uom"], as_dict=True)
    if not row:
        frappe.throw(_("Item {0} was not found.").format(variant))
    if not cstr(row.variant_of).strip():
        frappe.throw(
            _("Item {0} is not a variant. Prices for a simple item are set on the Item "
              "itself, not through the variant API.").format(variant)
        )

    if rate is None:
        rate = kwargs.get("price_list_rate")
    clearing = rate is None or cstr(rate).strip() == ""

    # Found with the SAME filter the read path uses, so an upsert can never create a
    # second row that _item_price would then ignore.
    existing = frappe.db.get_value(
        "Item Price",
        {"item_code": row.name, "price_list": STORE_PRICE_LIST, "selling": 1},
        "name",
    )

    if clearing:
        require_doctype_permission("Item Price", "delete")
        if existing:
            frappe.delete_doc("Item Price", existing, ignore_permissions=True)
        cleared = bool(existing)
    else:
        value = flt(rate)
        if value <= 0:
            frappe.throw(
                _("Rate must be greater than 0. To remove the price, send rate as null.")
            )
        if existing:
            require_doctype_permission("Item Price", "write")
            doc = frappe.get_doc("Item Price", existing)
            doc.price_list_rate = value
        else:
            require_doctype_permission("Item Price", "create")
            doc = frappe.new_doc("Item Price")
            doc.item_code = row.name
            doc.price_list = STORE_PRICE_LIST
            doc.price_list_rate = value
            # Matches the 355 existing rows on this price list, which all carry the UOM.
            # Guarded because ItemPrice.validate_item rejects a UOM missing from the
            # Item's conversion table.
            if frappe.db.exists(
                "UOM Conversion Detail",
                {"parenttype": "Item", "parent": row.name, "uom": row.stock_uom},
            ):
                doc.uom = row.stock_uom
        # `selling`, `buying` and `currency` are read-only and derived from the Price List
        # by ItemPrice.update_price_list_details - setting them here would be a guess.
        doc.flags.ignore_permissions = True
        doc.save()
        cleared = False

    # Read back through the storefront's own path, so the caller sees what the next GET
    # will report rather than an echo of the input.
    match = [v for v in _variant_rows(row.variant_of) if v["code"] == row.name]
    frappe.response["data"] = {
        "variant": match[0] if match else None,
        "price": _item_price(row.name),
        "cleared": cleared,
    }


@frappe.whitelist()
@standardize_response
def list_store_item_groups():
    """The retail Item Groups a store template may be created in.

    Derived from the nested set under STORE_ITEM_GROUP_ROOT - the SAME helper the
    create-template guard uses - so the picker a client renders and the rule the server
    enforces can never list different groups.
    """
    require_doctype_permission("Item Group", "read")

    groups = _store_item_groups()
    frappe.response["data"] = {
        "item_groups": groups,
        "root": STORE_ITEM_GROUP_ROOT,
        "total": len(groups),
    }


@frappe.whitelist()
@standardize_response
def list_item_attributes(search=None):
    """All Item Attributes with their values.

    Returns a DICT, never a bare list: ok_response wraps a list into BOTH `items` and
    `result` and then hoists them, which makes the contract ambiguous for callers.
    """
    _require_attribute_permission("read")

    filters = {}
    term = cstr(search).strip()
    if term:
        filters["name"] = ["like", f"%{term}%"]

    names = frappe.get_all(
        ITEM_ATTRIBUTE_DOCTYPE,
        filters=filters,
        pluck="name",
        order_by="name asc",
        ignore_permissions=True,
    )
    attributes = [_attribute_payload(n) for n in names]
    frappe.response["data"] = {
        "attributes": [a for a in attributes if a],
        "total": len(attributes),
    }


@frappe.whitelist(methods=["POST"])
@standardize_response
def create_item_attribute(attribute_name=None, values=None, **kwargs):
    """Create a new Item Attribute with its values.

    Create-only. Editing an existing attribute's values is a separate concern: renaming
    or removing a value that variants already use would orphan their item codes, and
    Item Variant Settings.allow_rename_attribute_value is 0 on this site.
    """
    _require_attribute_permission("create")

    attribute_name = cstr(attribute_name or kwargs.get("name")).strip()
    if not attribute_name:
        frappe.throw(_("Attribute name is required."))
    if frappe.db.exists(ITEM_ATTRIBUTE_DOCTYPE, attribute_name):
        frappe.throw(_("Item Attribute {0} already exists.").format(attribute_name))

    rows = _coerce_values(values if values is not None else kwargs.get("values"))

    doc = frappe.new_doc(ITEM_ATTRIBUTE_DOCTYPE)
    doc.attribute_name = attribute_name
    # Fixed ladder, not a continuous measure - see the module note on numeric_values.
    doc.numeric_values = 0
    for row in rows:
        # Correct child fieldnames. The deleted variant code wrote `value` here, where the
        # field is `attribute_value`, so every row it created came out blank.
        doc.append("item_attribute_values", {"attribute_value": row["attribute_value"], "abbr": row["abbr"]})
    doc.insert()

    frappe.response["data"] = {"attribute": _attribute_payload(doc.name)}
