import frappe
from frappe import _
from frappe.utils import flt, cint, cstr
from itertools import product as iterproduct

from pet_app.api.permissions import (
	get_restriction_values,
	require_doctype_permission,
	require_restriction_value,
	user_has_full_access,
)
from pet_app.pet_app.doctype.product_category.product_category import (
	apply_product_category_to_product,
	get_product_category_summary,
)
from pet_app.api.response import standardize_response

# ─────────────────────────────────────────
# Allowed Roles
# ─────────────────────────────────────────

PRODUCT_ROLES = ("System Manager", "Item Manager", "Stock Manager", "Administrator", "E-commerce")


def _has_product_role(user=None):
    user = user or frappe.session.user
    if user == "Administrator":
        return True
    user_roles = set(frappe.get_roles(user) or [])
    return bool(user_roles & set(PRODUCT_ROLES))


def _has_product_permission(ptype, user=None):
    try:
        return bool(frappe.has_permission("Product", ptype=ptype, user=user or frappe.session.user))
    except Exception:
        return False


def _check_permission(ptype="read"):
    if _has_product_role() or _has_product_permission(ptype):
        return
    frappe.throw(_("Not authorized"), frappe.PermissionError)


def _sanitize_order_by(order_by: str) -> str:
    # `price` and `discounted_price` are deliberately NOT sortable. Those columns now
    # back compare_at_price (a presentation-only strike-through), so sorting by them
    # would order the grid by the "was" price while the column on screen shows the live
    # selling price from Item Price - a sort that lies. Real price lives in Item Price
    # and cannot be sorted in this query, so the option is removed rather than faked.
    allowed_fields = {"creation", "modified", "product_name", "sku", "status"}
    value = (order_by or "creation desc").strip()
    parts = value.split()
    fieldname = parts[0] if parts else "creation"
    direction = parts[1].lower() if len(parts) > 1 else "desc"

    if fieldname not in allowed_fields:
        fieldname = "creation"
    if direction not in {"asc", "desc"}:
        direction = "desc"
    return f"{fieldname} {direction}"


# ─────────────────────────────────────────
# Error Response
# ─────────────────────────────────────────

def _err(msg, exc=None):
    if exc:
        frappe.log_error(frappe.get_traceback(), f"Product API: {msg}")
    frappe.throw(_(msg))


# ─────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────

def _effective_rate(doc):
    """السعر الفعلي للبيع"""
    if flt(doc.discounted_price) > 0:
        return flt(doc.discounted_price)
    return flt(doc.price)


def _get_default_warehouse():
    return frappe.db.get_single_value("Stock Settings", "default_warehouse")


def _get_allowed_warehouses_for_current_user():
    if user_has_full_access():
        return []
    return get_restriction_values("warehouse")


def _resolve_allowed_warehouse(warehouse=None):
    allowed_warehouses = _get_allowed_warehouses_for_current_user()
    if warehouse:
        require_restriction_value("warehouse", warehouse)
        return warehouse

    default_warehouse = _get_default_warehouse()
    if allowed_warehouses and default_warehouse not in allowed_warehouses:
        return allowed_warehouses[0]
    return default_warehouse


def _get_bin_qty(item_code, warehouse=None):
    if warehouse:
        require_restriction_value("warehouse", warehouse)
        return flt(
            frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty") or 0
        )
    allowed_warehouses = _get_allowed_warehouses_for_current_user()
    if allowed_warehouses:
        result = frappe.db.sql("""
            SELECT COALESCE(SUM(actual_qty), 0)
            FROM `tabBin`
            WHERE item_code = %s
              AND warehouse IN %(warehouses)s
        """, {"item_code": item_code, "warehouses": tuple(allowed_warehouses)})
        return flt(result[0][0]) if result else 0.0
    result = frappe.db.sql("""
        SELECT COALESCE(SUM(actual_qty), 0)
        FROM `tabBin`
        WHERE item_code = %s
    """, (item_code,))
    return flt(result[0][0]) if result else 0.0


def _log_product_projection_event(event, product=None, item=None, details=None):
    try:
        payload = {
            "event": event,
            "product": getattr(product, "name", None) if product else None,
            "sku": getattr(product, "sku", None) if product else None,
            "item": item,
            "details": details or {},
            "action_taken": (details or {}).get("action_taken", "none"),
        }
        frappe.log_error(title=event, message=frappe.as_json(payload, indent=2))
    except Exception:
        frappe.logger().warning(f"{event}: unable to write product projection log")


def _products_linked_to_item(item_code, exclude_product=None):
    if not item_code:
        return []

    filters = [["Product", "item", "=", item_code]]
    if exclude_product:
        filters.append(["Product", "name", "!=", exclude_product])

    return frappe.get_all("Product", filters=filters, pluck="name")


def _set_product_item_link(doc, item_name, event):
    if doc.item == item_name:
        return

    frappe.db.set_value("Product", doc.name, "item", item_name, update_modified=False)
    doc.item = item_name
    _log_product_projection_event(
        event,
        product=doc,
        item=item_name,
        details={"action_taken": "linked_product_to_item"},
    )


def _validate_publish(doc):
    """تحقق من البيانات المطلوبة قبل النشر"""
    errors = []
    if not doc.product_name:
        errors.append("product_name مطلوب")
    if not doc.category:
        errors.append("category مطلوبة")
    if not doc.sku:
        errors.append("SKU مطلوب")
    if _effective_rate(doc) <= 0 and not cint(doc.has_variants):
        errors.append("السعر مطلوب (price أو discounted_price)")
    if cint(doc.has_variants) and not doc.product_variant:
        errors.append("أضف variant options إذا has_variants مفعّل")
    if errors:
        frappe.throw("<br>".join(errors))


def _resolve_product_item_group(doc):
    if not doc.category:
        return doc.item_group or "All Item Groups"

    item_group = apply_product_category_to_product(doc, ignore_permissions=True)
    if doc.name and not doc.is_new():
        updates = {}
        db_category = frappe.db.get_value("Product", doc.name, "category")
        db_item_group = frappe.db.get_value("Product", doc.name, "item_group")
        if db_category != doc.category:
            updates["category"] = doc.category
        if db_item_group != item_group:
            updates["item_group"] = item_group
        if updates:
            frappe.db.set_value("Product", doc.name, updates, update_modified=False)
    return item_group or "All Item Groups"


def _enrich_product_category(row):
    category = row.get("category")
    if not category:
        row["category_name"] = None
        row["category_image"] = None
        row["category_item_group"] = None
        return

    summary = get_product_category_summary(category)
    row["category_name"] = summary.get("category_name") or category
    row["category_item_group"] = summary.get("item_group")
    row["category_image"] = summary.get("image")

    if not row["category_image"] and row["category_item_group"]:
        row["category_image"] = frappe.db.get_value("Item Group", row["category_item_group"], "image")

    if not row["category_image"] and frappe.db.exists("Item Group", category):
        row["category_image"] = frappe.db.get_value("Item Group", category, "image")


# ─────────────────────────────────────────
# Item
# ─────────────────────────────────────────

def _apply_item_projection(item, doc):
    changed = _sync_item_fields(item, _get_item_sync_payload(doc))

    if not item.stock_uom:
        item.stock_uom = "Nos"
        changed = True

    if not cint(item.is_stock_item):
        item.is_stock_item = 1
        changed = True

    return changed


def _create_item_from_product(doc):
    require_doctype_permission("Item", "create")

    if not doc.sku:
        _log_product_projection_event(
            "PRODUCT_ITEM_MISSING_SKU",
            product=doc,
            details={"reason": "cannot_create_item_without_sku"},
        )
        return None

    if frappe.db.exists("Item", doc.sku):
        _log_product_projection_event(
            "PRODUCT_ITEM_SKU_CONFLICT",
            product=doc,
            item=doc.sku,
            details={"reason": "item_appeared_before_create", "action_taken": "skipped"},
        )
        return None

    item = frappe.new_doc("Item")
    item.item_code = doc.sku
    item.item_name = doc.product_name
    item.item_group = _resolve_product_item_group(doc)
    item.brand = doc.brand or ""
    item.description = doc.description or ""
    item.stock_uom = "Nos"
    item.is_stock_item = 1
    item.image = doc.image or ""

    item.flags.ignore_permissions = True
    item.flags.from_product_projection = True
    item.insert()

    _set_product_item_link(doc, item.name, "PRODUCT_ITEM_CREATED_FROM_PRODUCT")
    return item


def _resolve_item_for_product(doc):
    current_item = doc.item
    sku = doc.sku

    if current_item and frappe.db.exists("Item", current_item):
        if sku and current_item != sku and frappe.db.exists("Item", sku):
            _log_product_projection_event(
                "PRODUCT_ITEM_SKU_CONFLICT",
                product=doc,
                item=current_item,
                details={
                    "reason": "linked_item_differs_from_existing_sku_item",
                    "linked_item": current_item,
                    "sku_item": sku,
                    "action_taken": "kept_existing_link",
                },
            )
        return frappe.get_doc("Item", current_item)

    if current_item:
        _log_product_projection_event(
            "PRODUCT_ITEM_BROKEN_LINK",
            product=doc,
            item=current_item,
            details={"reason": "linked_item_not_found"},
        )

    if sku and frappe.db.exists("Item", sku):
        linked_products = _products_linked_to_item(sku, exclude_product=doc.name)
        if linked_products:
            _log_product_projection_event(
                "PRODUCT_ITEM_SKU_CONFLICT",
                product=doc,
                item=sku,
                details={
                    "reason": "sku_item_linked_to_other_products",
                    "linked_products": linked_products,
                    "action_taken": "skipped",
                },
            )
            return None

        item = frappe.get_doc("Item", sku)
        if item.item_name and doc.product_name and item.item_name != doc.product_name:
            _log_product_projection_event(
                "PRODUCT_ITEM_SKU_CONFLICT",
                product=doc,
                item=sku,
                details={
                    "reason": "sku_item_name_mismatch",
                    "product_name": doc.product_name,
                    "item_name": item.item_name,
                    "action_taken": "skipped",
                },
            )
            return None

        _set_product_item_link(doc, item.name, "PRODUCT_ITEM_LINKED_BY_SKU")
        return item

    return _create_item_from_product(doc)


def _ensure_item(doc):
    """خلق أو تحديث Item"""
    item = _resolve_item_for_product(doc)
    if not item:
        return None

    changed = _apply_item_projection(item, doc)
    if changed:
        require_doctype_permission("Item", "write")
        item.flags.ignore_permissions = True
        item.flags.from_product_projection = True
        try:
            item.save()
        except Exception:
            payload = _get_item_sync_payload(doc)
            frappe.db.set_value("Item", item.name, payload, update_modified=False)
            _log_product_projection_event(
                "PRODUCT_ITEM_DIRECT_FIELD_SYNC",
                product=doc,
                item=item.name,
                details={
                    "reason": "item_save_failed_for_unrelated_validation",
                    "fields": sorted(payload.keys()),
                    "traceback": frappe.get_traceback(),
                    "action_taken": "synced_projection_fields_with_db_set_value",
                },
            )

    return item


def _get_item_sync_payload(doc):
    return {
        "item_name": doc.product_name,
        "item_group": _resolve_product_item_group(doc),
        "brand": doc.brand or "",
        "description": doc.description or "",
        "image": doc.image or "",
    }


def _sync_item_fields(item, payload):
    changed = False

    for fieldname, value in payload.items():
        if item.get(fieldname) != value:
            item.set(fieldname, value)
            changed = True

    return changed


def sync_product_item(doc, method=None):
    """Keep the linked ERPNext Item in sync with Product changes."""
    item = _ensure_item(doc)
    if not item:
        return

    if _effective_rate(doc) > 0:
        _ensure_item_price(doc, item.name)


def _get_product_for_item(item_code):
    products = _products_linked_to_item(item_code)
    if not products:
        return None

    if len(products) > 1:
        _log_product_projection_event(
            "PRODUCT_ITEM_LINK_CONFLICT",
            item=item_code,
            details={
                "reason": "multiple_products_link_same_item",
                "products": products,
                "action_taken": "skipped",
            },
        )
        return None

    return frappe.get_doc("Product", products[0])


def sync_item_from_product_projection(doc, method=None):
    """Repair manual drift on Product-backed Items without blocking the Item save."""
    if getattr(doc.flags, "from_product_projection", False):
        return

    product = _get_product_for_item(doc.name)
    if not product:
        return

    _log_product_projection_event(
        "PRODUCT_BACKED_ITEM_DRIFT",
        product=product,
        item=doc.name,
        details={"source": method or "Item.on_update", "action_taken": "resync_from_product"},
    )
    sync_product_item(product, method="item_drift_repair")


def sync_item_price_from_product_projection(doc, method=None):
    """Repair Product-backed Standard Selling Item Price drift without blocking saves."""
    if getattr(doc.flags, "from_product_projection", False):
        return

    if doc.price_list != "Standard Selling" or not cint(doc.selling):
        return

    product = _get_product_for_item(doc.item_code)
    if not product:
        return

    _log_product_projection_event(
        "PRODUCT_BACKED_ITEM_PRICE_DRIFT",
        product=product,
        item=doc.item_code,
        details={
            "source": method or "Item Price.on_update",
            "item_price": doc.name,
            "action_taken": "resync_from_product",
        },
    )
    sync_product_item(product, method="item_price_drift_repair")


def repair_product_item_projections(active_only=False, limit=None):
    """Scheduled/bench-safe repair for unambiguous Product -> Item projection drift."""
    filters = {}
    if active_only:
        filters["status"] = "Active"

    query = {
        "filters": filters,
        "fields": ["name"],
        "order_by": "modified desc",
    }
    if limit:
        query["limit_page_length"] = cint(limit)

    products = frappe.get_all("Product", **query)

    repaired = 0
    skipped = 0
    for row in products:
        try:
            product = frappe.get_doc("Product", row.name)
            before_item = product.item
            sync_product_item(product, method="scheduled_repair")
            repaired += 1
            if before_item != product.item:
                frappe.db.commit()
        except Exception:
            skipped += 1
            frappe.log_error(
                title="PRODUCT_PROJECTION_REPAIR_FAILED",
                message=frappe.get_traceback(),
            )

    return {"checked": len(products), "repaired_attempts": repaired, "failed": skipped}


def repair_active_product_item_projections():
    return repair_product_item_projections(active_only=True)


def repair_all_product_item_projections():
    return repair_product_item_projections(active_only=False)


# ─────────────────────────────────────────
# Item Price
# ─────────────────────────────────────────

def _ensure_item_price(doc, item_code, rate=None):
    """خلق أو تحديث Item Price"""
    rate       = rate if rate is not None else _effective_rate(doc)
    price_list = "Standard Selling"
    currency   = frappe.defaults.get_global_default("currency") or "IQD"

    if rate <= 0:
        _log_product_projection_event(
            "PRODUCT_ITEM_PRICE_SKIPPED",
            product=doc,
            item=item_code,
            details={"reason": "effective_rate_not_positive", "rate": rate},
        )
        return None

    filters = {
        "item_code":  item_code,
        "price_list": price_list,
        "selling":    1
    }
    existing_prices = frappe.get_all("Item Price", filters=filters, pluck="name")

    if len(existing_prices) > 1:
        _log_product_projection_event(
            "PRODUCT_ITEM_PRICE_DUPLICATE",
            product=doc,
            item=item_code,
            details={
                "price_list": price_list,
                "item_prices": existing_prices,
                "action_taken": "skipped",
            },
        )
        return None

    existing = existing_prices[0] if existing_prices else None
    ip = frappe.get_doc("Item Price", existing) if existing else frappe.new_doc("Item Price")

    if not existing:
        ip.item_code  = item_code
        ip.price_list = price_list
        ip.selling    = 1

    old_rate = flt(ip.price_list_rate or 0)
    ip.price_list_rate = rate
    ip.currency        = currency

    ip.flags.ignore_permissions = True
    ip.flags.from_product_projection = True
    ip.save() if existing else ip.insert()

    if not existing:
        event = "PRODUCT_ITEM_PRICE_CREATED"
    elif old_rate != rate:
        event = "PRODUCT_ITEM_PRICE_UPDATED"
    else:
        event = None

    if event:
        _log_product_projection_event(
            event,
            product=doc,
            item=item_code,
            details={
                "price_list": price_list,
                "old_rate": old_rate if existing else None,
                "new_rate": rate,
                "item_price": ip.name,
                "action_taken": "synced_item_price",
            },
        )

    frappe.db.set_value("Product", doc.name, "item_price", ip.name, update_modified=False)
    return ip


# ─────────────────────────────────────────
# Variants
# ─────────────────────────────────────────

def _ensure_item_attribute(attribute_name, values):
    """خلق أو تحديث Item Attribute"""
    if not frappe.db.exists("Item Attribute", attribute_name):
        attr                = frappe.new_doc("Item Attribute")
        attr.attribute_name = attribute_name
        for v in values:
            attr.append("item_attribute_values", {
                "value": v,
                "abbr":            v[:3].upper()
            })
        attr.flags.ignore_permissions = True
        attr.insert()
    else:
        attr            = frappe.get_doc("Item Attribute", attribute_name)
        existing_values = [r.attribute_value for r in attr.item_attribute_values if r.attribute_value]
        changed         = False
        for v in values:
            if v and v not in existing_values:
                attr.append("item_attribute_values", {
                    "value": v,
                    "abbr":            v[:3].upper()
                })
                changed = True
        if changed:
            attr.flags.ignore_permissions = True
            attr.save()


def _ensure_variant_items(doc, template_item):
    """
    توليد Variant Items من product_variant child table
    كل row = attribute_name + attribute_value
    مثال:
      Weight | 1kg
      Weight | 3kg
      Color  | Red
    النتيجة: RC-001-1kg-Red, RC-001-3kg-Red
    """
    # جمّع القيم لكل attribute
    attributes_map = {}
    for row in doc.product_variant:
        attr = row.options
        val  = row.value
        if not attr or not val:
            continue
        attributes_map.setdefault(attr, [])
        if val not in attributes_map[attr]:
            attributes_map[attr].append(val)

    if not attributes_map:
        frappe.throw(_("أضف variant options (attribute_name + attribute_value)"))

    # خلق Item Attributes بـ ERP
    for attr_name, values in attributes_map.items():
        _ensure_item_attribute(attr_name, values)

    # ضيف الـ attributes للـ Template
    existing_attrs = [r.attribute for r in template_item.attributes]
    template_changed = False
    for attr_name in attributes_map:
        if attr_name not in existing_attrs:
            template_item.append("attributes", {"attribute": attr_name})
            template_changed = True

    if template_changed:
        require_doctype_permission("Item", "write")
        template_item.flags.ignore_permissions = True
        template_item.save()

    # ولّد كل التوليفات
    attr_names  = list(attributes_map.keys())
    attr_values = [attributes_map[a] for a in attr_names]
    rate        = _effective_rate(doc)
    currency    = frappe.defaults.get_global_default("currency") or "IQD"

    generated = []

    for combination in iterproduct(*attr_values):
        suffix    = "-".join(combination)
        item_code = f"{template_item.name}-{suffix}"

        if not frappe.db.exists("Item", item_code):
            require_doctype_permission("Item", "create")
            variant              = frappe.new_doc("Item")
            variant.item_code    = item_code
            variant.item_name    = f"{doc.product_name} - {suffix}"
            variant.variant_of   = template_item.name
            variant.item_group   = _resolve_product_item_group(doc)
            variant.stock_uom    = "Nos"
            variant.is_stock_item = 1

            for i, attr_name in enumerate(attr_names):
                variant.append("attributes", {
                    "attribute":       attr_name,
                    "value": combination[i]
                })

            variant.flags.ignore_permissions = True
            variant.insert()

            # Item Price لكل variant
            if rate > 0:
                ip                 = frappe.new_doc("Item Price")
                ip.item_code       = item_code
                ip.price_list      = "Standard Selling"
                ip.selling         = 1
                ip.price_list_rate = rate
                ip.currency        = currency
                ip.flags.ignore_permissions = True
                ip.insert()

        generated.append({
            "item_code":  item_code,
            "attributes": dict(zip(attr_names, combination))
        })

    # حدّث item_variant بالـ product_variant rows
    for row in doc.product_variant:
        for gen in generated:
            if row.value in gen["attributes"].values():
                if not row.item_variant:
                    row.db_set("item_variant", gen["item_code"])
                break

    return generated


# ─────────────────────────────────────────
# PUBLIC ENDPOINTS
# ─────────────────────────────────────────

PRODUCT_FIELDS = [
    "product_name", "sku", "barcode", "description", "image",
    "price", "discounted_price", "charge_tax", "in_stock",
    "vendor", "category", "status", "tags", "has_variants",
    "product_variant",
    # The overlay attaches to an Item that already exists, so the caller has to be able
    # to supply the link. Previously publish minted the Item itself and back-filled this
    # field, which is exactly the Product -> Item write being removed.
    "item",
]


def _assert_publishable_item(item_code):
    """The linked Item must already exist and be active. Publish never creates one.

    Read-only: resolves and validates the link, writes nothing.
    """
    item_code = (item_code or "").strip()
    if not item_code:
        frappe.throw(_("Link this product to an item before publishing."))
    row = frappe.db.get_value("Item", item_code, ["name", "disabled", "variant_of"], as_dict=True)
    if not row:
        frappe.throw(
            _("Item {0} was not found. Link this product to an existing item before publishing.").format(item_code)
        )
    if cint(row.disabled):
        frappe.throw(
            _("Item {0} is disabled. Link this product to an active item before publishing.").format(item_code)
        )
    if cstr(row.variant_of).strip():
        # A variant is reachable through its TEMPLATE's product; giving it one of its own
        # makes the same Item sellable through two products, and nothing in the read path
        # looks upward from a variant to notice. Enforced here rather than only in the
        # picker because a hidden option is not a rule - create_product, update_product's
        # re-attach and publish_product all resolve through this one function.
        frappe.throw(
            _("Item {0} is a variant of {1}. Variants are sold through their template's "
              "product - attach the product to {1} instead.").format(item_code, row.variant_of)
        )
    return row.name


# ─────────────────────────────────────────
# THIN OVERLAY: live resolution from the Item
#
# Item owns price, stock, warehouse, UOM and brand. Everything below READS them at
# request time and never copies them onto Product, so the two cannot drift.
# ─────────────────────────────────────────

# Declared once, read through _store_price_list(), never inlined as a literal in the
# read path - the same discipline as STORE_ROOT_CATEGORY. A dedicated "Store" price list
# later means changing this constant, not hunting call sites.
STORE_PRICE_LIST = "Standard Selling"

# Storefront-owned fields an admin may write through this API.
STOREFRONT_WRITABLE_FIELDS = (
    "product_name",
    "description",
    "image",
    "category",
    "sku",
    "barcode",
    "tags",
    "vendor",
    "mobile_home_filter",
)

# The strike-through "was" price, stored on the vestigial Product.price column but
# exposed under an unambiguous name. It is PRESENTATION ONLY and is never charged: the
# selling price is always item.price.rate, read live from the Item.
#
# The old `price` / `discounted_price` pair is exactly what caused the confusion this
# name removes - `price` looked like the selling price while `discounted_price` was the
# one that actually matched Item Price.
COMPARE_AT_PRICE_API_FIELD = "compare_at_price"
COMPARE_AT_PRICE_DB_FIELD = "price"

# Item-owned or variant-era fields. Writing them here would recreate the mirror this
# refactor removed, so they are refused loudly rather than silently dropped - a caller
# that sent a price has to learn the store does not set prices.
#
# `price` stays rejected on purpose even though it now backs compare_at_price: a caller
# sending "price" almost certainly means the selling price, and silently treating that as
# a strike-through would reintroduce the ambiguity. They must say compare_at_price.
ITEM_OWNED_FIELDS = {
    "price": "selling price",
    "discounted_price": "selling price",
    "rate": "selling price",
    "in_stock": "stock",
    "qty": "stock",
    "stock": "stock",
    "warehouse": "stock",
    "item_group": "item group",
    "uom": "unit of measure",
    "stock_uom": "unit of measure",
    "brand": "brand",
    "item_price": "selling price",
}


def _truthy_flag(value, default=False) -> bool:
    """Boolean coercion for HTTP flags.

    `_truthy`-style, not raw truthiness: the string "0" arrives over HTTP and is truthy
    in Python, so `include_attached=0` would otherwise switch the filter ON.
    """
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(cint(value))


def _store_price_list() -> str:
    """The selling price list the storefront quotes from."""
    return STORE_PRICE_LIST


def _item_price(item_code):
    """Live selling price for an Item. `found` is explicit so callers can show
    "no price set" instead of rendering a free product."""
    price_list = _store_price_list()
    row = frappe.db.get_value(
        "Item Price",
        {"item_code": item_code, "price_list": price_list, "selling": 1},
        ["price_list_rate", "currency"],
        as_dict=True,
    )
    if not row:
        return {"rate": None, "currency": None, "price_list": price_list, "found": False}
    return {
        "rate": flt(row.price_list_rate),
        "currency": row.currency or frappe.defaults.get_global_default("currency"),
        "price_list": price_list,
        "found": True,
    }


def _item_stock(item_code):
    """Live stock summed across EVERY warehouse holding the Item, plus the breakdown.

    Deliberately not scoped to Stock Settings.default_warehouse: that currently points at
    another company's empty warehouse, so a scoped read reports 0 for everything. Summing
    the Item's own bins is both correct and immune to that misconfiguration.
    """
    rows = frappe.get_all(
        "Bin",
        filters={"item_code": item_code},
        fields=["warehouse", "actual_qty"],
        ignore_permissions=True,
    )
    breakdown = [
        {"warehouse": r.warehouse, "qty": flt(r.actual_qty)}
        for r in rows
        if flt(r.actual_qty)
    ]
    total = sum(b["qty"] for b in breakdown)
    return {"qty": total, "in_stock": total > 0, "warehouses": breakdown}


# Per-variant storefront visibility, added by
# pet_app.patches.add_item_store_published_field. Named once here so the read path, the
# write endpoint and the publish rule cannot drift onto different spellings.
VARIANT_PUBLISHED_FIELD = "custom_store_published"


def _variant_rows(template_code):
    """Variants of a template, each with its own price and stock.

    Batched deliberately: three queries for the whole set (attributes, prices, bins)
    rather than the two-per-variant that calling _item_price/_item_stock in a loop would
    cost. A six-variant product would otherwise be 12 round trips inside a grid that
    already renders 20 products.

    Returns [] for a non-template or a template with no children.
    """
    children = frappe.get_all(
        "Item",
        filters={"variant_of": template_code},
        fields=["name", "item_name", "stock_uom", "image", "disabled",
                VARIANT_PUBLISHED_FIELD],
        order_by="name asc",
        ignore_permissions=True,
    )
    if not children:
        return []

    codes = [c.name for c in children]

    # 1) attributes: {item_code: {attribute: value}}
    attr_map = {}
    for row in frappe.get_all(
        "Item Variant Attribute",
        filters={"parent": ["in", codes], "parenttype": "Item"},
        fields=["parent", "attribute", "attribute_value", "idx"],
        order_by="parent asc, idx asc",
        ignore_permissions=True,
    ):
        attr_map.setdefault(row.parent, {})[row.attribute] = row.attribute_value

    # 2) prices on the store price list only
    price_map = {}
    for row in frappe.get_all(
        "Item Price",
        filters={"item_code": ["in", codes], "price_list": _store_price_list(), "selling": 1},
        fields=["item_code", "price_list_rate", "currency"],
        ignore_permissions=True,
    ):
        price_map.setdefault(row.item_code, row)

    # 3) bins across every warehouse holding each variant
    stock_map = {}
    for row in frappe.get_all(
        "Bin",
        filters={"item_code": ["in", codes]},
        fields=["item_code", "warehouse", "actual_qty"],
        ignore_permissions=True,
    ):
        if flt(row.actual_qty):
            stock_map.setdefault(row.item_code, []).append(
                {"warehouse": row.warehouse, "qty": flt(row.actual_qty)}
            )

    variants = []
    for child in children:
        priced = price_map.get(child.name)
        warehouses = stock_map.get(child.name, [])
        qty = sum(w["qty"] for w in warehouses)
        variants.append(
            {
                "code": child.name,
                "item_name": child.item_name,
                "uom": child.stock_uom,
                "image": child.image,
                "disabled": bool(cint(child.disabled)),
                "attributes": attr_map.get(child.name, {}),
                "price": {
                    "rate": flt(priced.price_list_rate) if priced else None,
                    "currency": (priced.currency if priced else None)
                    or frappe.defaults.get_global_default("currency"),
                    "price_list": _store_price_list(),
                    "found": bool(priced),
                },
                "stock": {"qty": qty, "in_stock": qty > 0, "warehouses": warehouses},
                "published": bool(cint(child.get(VARIANT_PUBLISHED_FIELD))),
            }
        )
    return variants


def _template_attributes(template_code):
    """The attributes a TEMPLATE declares, each with the values it may take.

    Read INDEPENDENTLY of _variant_rows, which returns early for a childless template.
    A template with zero variants is exactly when a client needs this - it is what the
    "generate variants" form is built from - so hanging it off the variant query would
    make it arrive only once it is no longer needed.

    Note the deliberate asymmetry with `variants[].attributes`: that is a DICT of one
    variant's resolved values ({"Weight": "2kg"}), an assignment. This is a LIST of what
    the template declares plus every value each attribute allows. Different shapes because
    they answer different questions, hence the different key name.
    """
    rows = frappe.get_all(
        "Item Variant Attribute",
        filters={"parent": template_code, "parenttype": "Item"},
        fields=["attribute", "idx"],
        order_by="idx asc",
        ignore_permissions=True,
    )
    names = []
    for row in rows:
        if row.attribute and row.attribute not in names:
            names.append(row.attribute)
    if not names:
        return []

    # One batched query for every attribute's values, not one per attribute.
    value_map = {}
    for row in frappe.get_all(
        "Item Attribute Value",
        filters={"parent": ["in", names], "parenttype": "Item Attribute"},
        fields=["parent", "attribute_value", "abbr", "idx"],
        order_by="parent asc, idx asc",
        ignore_permissions=True,
    ):
        value_map.setdefault(row.parent, []).append(
            # abbr is included because it is what ERPNext builds the variant item code
            # from - a client showing "2kg" can show the SKU suffix it will produce.
            {"value": row.attribute_value, "abbr": row.abbr}
        )
    return [{"attribute": name, "values": value_map.get(name, [])} for name in names]


def _is_purchasable_variant(variant):
    """Published AND really priced - the single definition of "a customer can buy this".

    Both the "from" price and publish_product's template rule read this, so the price a
    shopper is quoted and the rule that let the product go live can never disagree.
    Rates of 0 are excluded: MIN(price_list_rate) on Standard Selling is currently 0.0
    because some items carry zero-rated prices, and a naive MIN would advertise
    "from 0 IQD".
    """
    return (
        variant["published"]
        and variant["price"]["found"]
        and flt(variant["price"]["rate"]) > 0
    )


def _variant_price_summary(variants):
    """The "from X" price across purchasable variants.

    An UNPUBLISHED variant is excluded even when priced: a product that only sells the
    6kg must not advertise the hidden 2kg's cheaper price. When nothing is purchasable,
    `found` is False so the frontend can render "price on request" instead of a free
    product.
    """
    rates = [flt(v["price"]["rate"]) for v in variants if _is_purchasable_variant(v)]
    if not rates:
        return {
            "from_rate": None,
            "to_rate": None,
            "currency": None,
            "price_list": _store_price_list(),
            "found": False,
            "priced_variants": 0,
        }
    currency = next(
        (v["price"]["currency"] for v in variants if _is_purchasable_variant(v)), None
    )
    return {
        "from_rate": min(rates),
        "to_rate": max(rates),
        "currency": currency,
        "price_list": _store_price_list(),
        "found": True,
        "priced_variants": len(rates),
    }


def _item_snapshot(item_code):
    """The Item block of the read shape. Read-only to the store.

    For a TEMPLATE item (has_variants=1) this additionally carries `is_template`,
    `variants` and `variant_price`. A simple item's payload is unchanged - the extra keys
    are only added inside the template branch, so existing consumers see byte-identical
    output for every non-template product.
    """
    if not item_code:
        return None
    row = frappe.db.get_value(
        "Item",
        item_code,
        ["name", "item_name", "item_group", "stock_uom", "brand", "disabled", "image", "description",
         "has_variants"],
        as_dict=True,
    )
    if not row:
        # Dangling link: reported rather than hidden, so the admin can see and fix it.
        return {"code": item_code, "exists": False}
    snapshot = {
        "code": row.name,
        "exists": True,
        "item_name": row.item_name,
        "item_group": row.item_group,
        "uom": row.stock_uom,
        "brand": row.brand,
        "disabled": bool(cint(row.disabled)),
        "image": row.image,
        "description": row.description,
        "price": _item_price(row.name),
        "stock": _item_stock(row.name),
    }
    if cint(row.has_variants):
        # A template holds no stock and carries no sellable price of its own; both live on
        # the children. `price`/`stock` above stay for shape stability and will read
        # not-found / zero, which is accurate for a template.
        variants = _variant_rows(row.name)
        snapshot["is_template"] = True
        snapshot["variants"] = variants
        snapshot["variant_count"] = len(variants)
        snapshot["variant_price"] = _variant_price_summary(variants)
        # Populated even when `variants` is empty - a client builds its generate form from
        # this, and a template with no children is when that form matters most.
        snapshot["template_attributes"] = _template_attributes(row.name)
    return snapshot


def _product_payload(row):
    """The overlay + its live Item resolution.

    Vestigial Product columns (price, discounted_price, in_stock, item_price,
    has_variants, product_variant) are deliberately NOT read: they still exist in the
    schema but no longer mean anything. `in_stock` is derived from the Item's bins.
    """
    item = _item_snapshot(row.get("item"))
    return {
        "name": row.get("name"),
        "status": row.get("status"),
        "published": row.get("status") == "Active",
        "display_name": row.get("product_name"),
        "description": row.get("description"),
        "image": row.get("image"),
        "gallery": _product_gallery(row.get("name")),
        "category": row.get("category"),
        "sku": row.get("sku"),
        "barcode": row.get("barcode"),
        "tags": row.get("tags"),
        "vendor": row.get("vendor"),
        "item": item,
        # Presentation-only strike-through. NOT the selling price - item.price.rate is.
        "compare_at_price": flt(row.get(COMPARE_AT_PRICE_DB_FIELD)) or None,
        "discount": _discount_block(row.get(COMPARE_AT_PRICE_DB_FIELD), item),
        # Convenience mirrors of the LIVE item values, so grids do not have to dig.
        "in_stock": bool(item and item.get("stock", {}).get("in_stock")) if item else False,
        "price": (item or {}).get("price", {}).get("rate") if item else None,
        "currency": (item or {}).get("price", {}).get("currency") if item else None,
        "modified": row.get("modified"),
    }


def _product_gallery(product_name):
    if not product_name:
        return []
    return frappe.get_all(
        "File",
        filters={"attached_to_doctype": "Product", "attached_to_name": product_name, "is_private": 0},
        fields=["name", "file_url", "file_name", "custom_is_default"],
        order_by="custom_is_default desc, creation asc",
        ignore_permissions=True,
    )


def _reject_blank_product_name(payload):
    """Sending product_name empty is not the same as omitting it.

    These endpoints are PATCH-shaped: a key that is absent means "leave it alone". A key
    that is PRESENT but blank is a client asserting a value, and for the one field whose
    entire job is presentation that assertion is always a mistake - an edit dialog that
    posts every field unconditionally would erase the display name of any product whose
    input the user never touched.

    Frappe does stop the write (product_name is reqd = 1, so the save raises
    MandatoryError), but it reports "[Product, PRODUCT-00051]: product_name", which names
    no cause and suggests no action. This refuses earlier and says why.
    """
    if "product_name" not in payload:
        return
    if not cstr(payload.get("product_name")).strip():
        frappe.throw(
            _("Product name cannot be empty. Omit the field to leave the current name "
              "unchanged, or send a name to replace it.")
        )


def _reject_item_owned_fields(payload):
    """Refuse Item-owned writes explicitly instead of ignoring them."""
    offenders = sorted({ITEM_OWNED_FIELDS[k] for k in payload if k in ITEM_OWNED_FIELDS})
    if not offenders:
        return
    hint = ""
    if "selling price" in offenders:
        hint = _(" To set a strike-through 'was' price, send {0} instead.").format(COMPARE_AT_PRICE_API_FIELD)
    frappe.throw(
        _("{0} is managed on the Item, not on the product. Update it on the linked Item instead.").format(
            ", ".join(offenders).capitalize()
        )
        + hint
    )


def _apply_compare_at_price(doc, payload):
    """Write the presentation-only strike-through price, if supplied."""
    if COMPARE_AT_PRICE_API_FIELD not in payload:
        return
    value = payload.get(COMPARE_AT_PRICE_API_FIELD)
    if value in (None, ""):
        doc.set(COMPARE_AT_PRICE_DB_FIELD, 0)
        return
    amount = flt(value)
    if amount < 0:
        frappe.throw(_("{0} cannot be negative.").format(COMPARE_AT_PRICE_API_FIELD))
    doc.set(COMPARE_AT_PRICE_DB_FIELD, amount)


def _discount_block(compare_at, item):
    """Whether the strike-through is renderable, and why not when it is not.

    A compare-at at or below the live selling price is not a discount - rendering it
    would show a nonsense strike-through. The state is reported rather than hidden so the
    admin UI can flag it instead of silently dropping the value.
    """
    compare_at = flt(compare_at) or None
    price = (item or {}).get("price") or {}
    rate = flt(price.get("rate")) if price.get("found") else None

    if not compare_at:
        reason = "no_compare_at_price"
    elif rate is None:
        reason = "no_item_price"
    elif compare_at <= rate:
        reason = "not_above_selling_price"
    else:
        reason = None

    return {
        "has_discount": reason is None,
        "reason": reason,
        "compare_at_price": compare_at,
        "price": rate,
        "amount": (compare_at - rate) if reason is None else None,
        "percent": round((compare_at - rate) / compare_at * 100, 2) if reason is None and compare_at else None,
    }


@frappe.whitelist(allow_guest=False)
@standardize_response
def create_product(item=None, **kwargs):
    """Create a storefront overlay on top of an EXISTING Item, as a Draft.

    Creation is separate from publishing: publish_product used to do both, which meant a
    half-valid product could be inserted and committed before validation failed.
    """
    _check_permission("create")
    _reject_item_owned_fields(kwargs)
    _reject_blank_product_name(kwargs)

    item_code = _assert_publishable_item(item or kwargs.get("item"))
    if cint(kwargs.get("has_variants") or 0):
        frappe.throw(_("Variants are not supported yet."))

    # One Product per Item, and for a variant product that Item is the TEMPLATE - so a
    # template already carrying an overlay cannot be attached again. Keying on the linked
    # Item covers both shapes with one rule, because the overlay never points at a child.
    existing = frappe.db.get_value("Product", {"item": item_code}, "name")
    if existing:
        is_template = cint(frappe.db.get_value("Item", item_code, "has_variants"))
        frappe.throw(
            _("Template {0} is already linked to product {1}.").format(item_code, existing)
            if is_template
            else _("Item {0} is already linked to product {1}.").format(item_code, existing)
        )

    sku = cstr(kwargs.get("sku")).strip()
    if not sku:
        last = frappe.db.sql(
            "SELECT MAX(CAST(SUBSTRING(sku, 5) AS UNSIGNED)) FROM `tabProduct` WHERE sku LIKE 'SKU-%'"
        )
        sku = f"SKU-{str(int(last[0][0] or 0) + 1).zfill(5)}"
    elif frappe.db.get_value("Product", {"sku": sku}, "name"):
        frappe.throw(_("SKU {0} already exists.").format(sku))

    doc = frappe.new_doc("Product")
    doc.item = item_code
    doc.sku = sku
    for field in STOREFRONT_WRITABLE_FIELDS:
        if field in kwargs and field != "sku":
            doc.set(field, kwargs[field])
    _apply_compare_at_price(doc, kwargs)
    # Reached only when product_name was OMITTED - an explicitly blank one was refused by
    # _reject_blank_product_name above. Defaulting a name nobody supplied is helpful;
    # defaulting one a client actively sent as empty would hide the client's bug.
    if not doc.product_name:
        doc.product_name = frappe.db.get_value("Item", item_code, "item_name") or item_code
    doc.status = "Draft"
    doc.flags.ignore_permissions = True
    doc.insert()

    frappe.response["data"] = _product_payload(doc.as_dict())


@frappe.whitelist(allow_guest=False)
@standardize_response
def update_product(product_id=None, **kwargs):
    """Update storefront fields only. Item-owned fields are refused."""
    _check_permission("write")
    if not product_id:
        frappe.throw(_("Product is required."))
    if not frappe.db.exists("Product", product_id):
        frappe.throw(_("Product {0} was not found.").format(product_id))

    _reject_item_owned_fields(kwargs)
    _reject_blank_product_name(kwargs)
    if "has_variants" in kwargs and cint(kwargs.get("has_variants")):
        frappe.throw(_("Variants are not supported yet."))

    doc = frappe.get_doc("Product", product_id)

    # Re-attaching to a different Item is allowed, but only to a real active one.
    if kwargs.get("item") and kwargs["item"] != doc.item:
        new_item = _assert_publishable_item(kwargs["item"])
        clash = frappe.db.get_value("Product", {"item": new_item, "name": ["!=", doc.name]}, "name")
        if clash:
            frappe.throw(_("Item {0} is already linked to product {1}.").format(new_item, clash))
        doc.item = new_item

    for field in STOREFRONT_WRITABLE_FIELDS:
        if field in kwargs:
            doc.set(field, kwargs[field])
    _apply_compare_at_price(doc, kwargs)

    doc.flags.ignore_permissions = True
    doc.save()
    frappe.response["data"] = _product_payload(doc.as_dict())


@frappe.whitelist(allow_guest=False)
@standardize_response
def publish_product(product_id=None, **kwargs):
    """Mark an existing overlay published. Writes to Product and nothing else.

    The create path moved to create_product. The old "price is required" rule checked the
    PRODUCT's price; under the overlay the Item owns pricing, so the requirement is now
    that the linked Item has a price on the store price list.
    """
    _check_permission("write")
    if not product_id:
        frappe.throw(_("Product is required. Create it first, then publish."))

    row = frappe.db.get_value("Product", product_id, ["name", "item", "has_variants"], as_dict=True)
    if not row:
        frappe.throw(_("Product {0} was not found.").format(product_id))
    if cint(row.has_variants):
        # Product.has_variants is a vestigial column from the deleted variant model. The
        # overlay never sets it - variance now lives entirely on the linked Item - so a
        # product carrying it is stale data, not a supported shape.
        frappe.throw(_("Variants are not supported yet. Publish this product without variants."))
    if flt(kwargs.get("qty") or 0) > 0:
        frappe.throw(_("Stock is managed on the Item - update it there. Publishing no longer seeds stock."))

    item_code = _assert_publishable_item(row.item)

    if cint(frappe.db.get_value("Item", item_code, "has_variants")):
        # Template: the template itself carries no sellable price, so the rule is that at
        # least one CHILD is purchasable - published AND priced. Reusing
        # _variant_price_summary means the rule is literally "a from-price exists".
        summary = _variant_price_summary(_variant_rows(item_code))
        if not summary["found"]:
            frappe.throw(
                _("This product has no purchasable variants - add at least one variant "
                  "that is both published and priced.")
            )
    else:
        # Simple item: unchanged rule.
        price = _item_price(item_code)
        if not price["found"] or flt(price["rate"]) <= 0:
            frappe.throw(
                _("Item {0} has no price on the {1} price list. Set the price on the Item before publishing.").format(
                    item_code, price["price_list"]
                )
            )

    # Product row only. in_stock is NOT written - it is derived from the Item's bins on
    # read, so a stored flag can never contradict the warehouse.
    frappe.db.set_value("Product", product_id, {"status": "Active"})
    frappe.response["data"] = _product_payload(
        frappe.db.get_value("Product", product_id, "*", as_dict=True)
    )


@frappe.whitelist(allow_guest=False)
@standardize_response
def unpublish_product(product_id=None):
    """Take a product off the storefront. Writes to Product only."""
    _check_permission("write")
    if not product_id or not frappe.db.exists("Product", product_id):
        frappe.throw(_("Product {0} was not found.").format(product_id))
    frappe.db.set_value("Product", product_id, {"status": "Draft"})
    frappe.response["data"] = _product_payload(
        frappe.db.get_value("Product", product_id, "*", as_dict=True)
    )


@frappe.whitelist(allow_guest=False)
@standardize_response
def delete_product(product_id=None):
    """Delete the overlay. The linked Item is never touched."""
    _check_permission("delete")
    if not product_id or not frappe.db.exists("Product", product_id):
        frappe.throw(_("Product {0} was not found.").format(product_id))
    item_code = frappe.db.get_value("Product", product_id, "item")
    frappe.delete_doc("Product", product_id, ignore_permissions=True)
    frappe.response["data"] = {"deleted": product_id, "item_untouched": item_code}


@frappe.whitelist()
@standardize_response
def list_attachable_items(search=None, limit_start=0, limit_page_length=20,
                          include_attached=0, include_services=0, variant_mode=0):
    """Item picker: Items an admin can attach a storefront overlay to.

    Defaults exclude Services (60 clinical rows, not shippable), disabled Items, variant
    templates, and Items already carrying a product - attaching two products to one Item
    creates the PRODUCT_ITEM_LINK_CONFLICT the resolver refuses to act on.

    `variant_mode=1` inverts the template filter and returns ONLY templates, for the
    variant-product flow where the overlay attaches to a template rather than a simple
    item. Everything else about the query is identical, so the two modes cannot drift.

    VARIANTS are excluded from BOTH modes. Templates in variant mode, simple non-variant
    items in the default mode, and nothing else - a variant is sold through its template's
    product, never through one of its own.

    Items without a price or stock ARE returned: surfacing "no price set" is useful, and
    hiding them would conceal exactly the ones needing attention.
    """
    _check_permission("read")

    # 1 in variant mode, 0 otherwise - the ONLY difference between the two modes.
    filters = {"disabled": 0, "has_variants": 1 if _truthy_flag(variant_mode) else 0}
    # Never a variant, in EITHER mode. `has_variants = 0` alone does not say this: a
    # variant is an ordinary Item with has_variants = 0, so it sailed through the default
    # filter and every variant of every template was offered as a standalone product.
    # Stated once, unconditionally, rather than per-mode - "a variant is not attachable"
    # is a property of variants, not of a mode.
    filters["variant_of"] = ["in", ["", None]]
    if not _truthy_flag(include_services):
        filters["item_group"] = ["!=", "Services"]
    if not _truthy_flag(include_attached):
        attached = frappe.get_all("Product", filters={"item": ["!=", ""]}, pluck="item", ignore_permissions=True)
        attached = [a for a in attached if a]
        if attached:
            filters["name"] = ["not in", attached]

    or_filters = None
    term = cstr(search).strip()
    if term:
        or_filters = [
            ["name", "like", f"%{term}%"],
            ["item_name", "like", f"%{term}%"],
            ["item_group", "like", f"%{term}%"],
            ["brand", "like", f"%{term}%"],
        ]

    query = {"filters": filters, "or_filters": or_filters}

    rows = frappe.get_all(
        "Item",
        fields=["name", "item_name", "item_group", "stock_uom", "brand", "image"],
        order_by="item_name asc",
        limit_start=cint(limit_start),
        limit_page_length=cint(limit_page_length),
        ignore_permissions=True,
        **query,
    )
    items = []
    for row in rows:
        items.append({
            "code": row.name,
            "item_name": row.item_name,
            "item_group": row.item_group,
            "uom": row.stock_uom,
            "brand": row.brand,
            "image": row.image,
            "price": _item_price(row.name),
            "stock": _item_stock(row.name),
        })

    # Counted from the SAME filtered population the rows come from, exactly as in
    # get_products. The previous `frappe.db.count("Item", filters)` dropped or_filters,
    # so searching "ACANA" returned 0 rows while the pager still advertised 258 results.
    # Sharing one `query` dict is what makes the two structurally unable to disagree.
    frappe.response["data"] = {
        "items": items,
        "total": len(frappe.get_all("Item", pluck="name", limit_page_length=0,
                                    ignore_permissions=True, **query)),
    }


@frappe.whitelist()
@standardize_response
def get_product(product_id=None):
    """One overlay with its live Item resolution."""
    _check_permission("read")
    row = frappe.db.get_value("Product", product_id, "*", as_dict=True) if product_id else None
    if not row:
        frappe.throw(_("Product {0} was not found.").format(product_id))
    frappe.response["data"] = _product_payload(row)


@frappe.whitelist()
@standardize_response
def restock_product(product_id, qty, warehouse=None, item_variant=None):
    """
    إضافة مخزون للمنتج

    POST /api/method/pet_app.api.product.restock_product
    Body:
    {
      "product_id": "PRODUCT-00001",
      "qty": 20,
      "warehouse": "Stores - K",
      "item_variant": "RC-001-1kg"   ← مطلوب فقط إذا has_variants
    }
    """
    # Neutered under the thin-overlay model. This endpoint posted a Material Receipt
    # Stock Entry against the linked Item, priced off the PRODUCT's rate - a storefront
    # record moving clinic stock. Stock belongs to the Item and is maintained by clinic
    # staff, so the write is refused here rather than performed.
    #
    # Refused, not silently no-op'd: a caller told it to add 20 units has to learn that
    # nothing moved. The body below is left in place for the later dead-code pass and is
    # now unreachable.
    frappe.throw(_("Stock is managed on the Item — update it there."))

    _check_permission("write")
    require_doctype_permission("Stock Entry", "create")
    require_doctype_permission("Stock Entry", "submit")

    qty = flt(qty)
    if qty <= 0:
        frappe.throw(_("الكمية يجب أن تكون أكبر من صفر"))

    doc = frappe.get_doc("Product", product_id)

    if not doc.item:
        frappe.throw(_("المنتج غير منشور — انشره أولاً"))

    # variant products use one template item
    item_code = doc.item

    wh = _resolve_allowed_warehouse(warehouse)
    if not wh:
        frappe.throw(_("حدد المستودع أو اضبط Default Warehouse بـ Stock Settings"))

    # تحقق إن الـ warehouse موجود
    if not frappe.db.exists("Warehouse", wh):
        frappe.throw(_(f"المستودع '{wh}' غير موجود"))

    try:
        se                  = frappe.new_doc("Stock Entry")
        se.stock_entry_type = "Material Receipt"
        se.remarks          = f"Restock from Product {doc.name}"

        se.append("items", {
            "item_code":   item_code,
            "t_warehouse": wh,
            "qty":         qty,
            "basic_rate":  _effective_rate(doc) or 1
        })

        se.flags.ignore_permissions = True
        se.insert()
        se.submit()

        current_qty = _get_bin_qty(item_code, wh)
        frappe.db.commit()

        frappe.response["data"] = {
            "stock_entry": se.name,
            "item_code":   item_code,
            "warehouse":   wh,
            "qty_added":   qty,
            "current_qty": current_qty
        }

    except Exception as e:
        frappe.db.rollback()
        _err(str(e), exc=True)


@frappe.whitelist()
@standardize_response
def get_stock_info(product_id, warehouse=None):
    """Live stock for a product's linked Item.

    Summed across every warehouse holding the Item, with the per-warehouse breakdown.
    The previous version scoped to Stock Settings.default_warehouse, which points at
    another company's empty warehouse and therefore reported 0 for everything.

    `warehouse` narrows the response to one warehouse when supplied.
    """
    _check_permission("read")

    item_code = frappe.db.get_value("Product", product_id, "item")
    if not item_code:
        frappe.response["data"] = {"in_stock": False, "qty": 0, "warehouses": [], "item": None}
        return

    stock = _item_stock(item_code)
    if warehouse:
        require_restriction_value("warehouse", warehouse)
        rows = [w for w in stock["warehouses"] if w["warehouse"] == warehouse]
        qty = sum(w["qty"] for w in rows)
        stock = {"qty": qty, "in_stock": qty > 0, "warehouses": rows}

    frappe.response["data"] = {
        "item": item_code,
        "qty": stock["qty"],
        "in_stock": stock["in_stock"],
        "warehouses": stock["warehouses"],
    }


@frappe.whitelist()
@standardize_response
def get_products(search=None, category=None, status=None, item=None,
                 order_by="product_name asc", limit_start=0, limit_page_length=20,
                 search_term=None):
    """Admin grid: overlays with their price and stock resolved LIVE from the Item.

    The old signature accepted raw `filters`/`fields` and returned the Product columns
    verbatim - including the vestigial price/in_stock, which is how a stale price could
    reach the storefront. The shape is fixed now and always resolves through the Item.
    """
    _check_permission("read")

    filters = {}
    if category:
        filters["category"] = category
    if status:
        filters["status"] = status
    if item:
        filters["item"] = item

    or_filters = None
    term = cstr(search or search_term).strip()
    if term:
        or_filters = [
            ["product_name", "like", f"%{term}%"],
            ["sku", "like", f"%{term}%"],
            ["barcode", "like", f"%{term}%"],
            ["item", "like", f"%{term}%"],
        ]

    # Hard ceiling, same shape as list_due_plan_items. `limit_page_length=0` means
    # "no limit" to frappe.get_all, so an accidental 0 would scan the whole table -
    # it falls back to the default page size instead. Floor of 1, ceiling of 200.
    limit_start = max(cint(limit_start), 0)
    limit_page_length = max(min(cint(limit_page_length) or 20, 200), 1)

    query = {"filters": filters, "or_filters": or_filters}

    rows = frappe.get_all(
        "Product",
        fields=["name", "product_name", "sku", "barcode", "description", "image",
                "category", "status", "vendor", "tags", "item", "modified",
                # backs compare_at_price; the only vestigial column still read, and only
                # for the strike-through, never as the selling price
                COMPARE_AT_PRICE_DB_FIELD],
        order_by=_sanitize_order_by(order_by),
        limit_start=limit_start,
        limit_page_length=limit_page_length,
        **query,
    )

    # Counted from the SAME filtered population the rows come from - `or_filters` and
    # all. The previous `frappe.db.count("Product", filters)` ignored or_filters, so any
    # search reported the unfiltered total: searching "zzz" returned 0 rows while the
    # pager still advertised 35 results across 2 pages. Rows and count are one
    # computation now and cannot drift.
    total = len(frappe.get_all("Product", pluck="name", limit_page_length=0, **query))

    frappe.response["data"] = {
        "products": [_product_payload(r) for r in rows],
        "total": total,
    }


RENAME_FIELD_MAP = {
    "Brand": "brand",
    "Item Group": "item_group_name",
    "Supplier": "supplier_name",
}


from pet_app.utils.auto_update_links import (
    LinkedDocSyncer,
    SYNC_CONFIG,
    _refresh_primary_address,
)


class DocTypeRenameHandler:
    """Handles automatic rename when title field changes."""

    def __init__(self, doc, method=None):
        self.doc = doc
        self.method = method

    def before_save(self):
        title_field = RENAME_FIELD_MAP.get(self.doc.doctype)
        if not title_field:
            return

        old_name = self.doc.name
        new_name = self.doc.get(title_field)

        if new_name and new_name != old_name:
            frappe.rename_doc(self.doc.doctype, old_name, new_name, force=True)
            self.doc.name = new_name

            payload = getattr(frappe.local, "_pending_sync", None)
            if payload and self.doc.doctype in SYNC_CONFIG:
                new_doc = frappe.get_doc(self.doc.doctype, self.doc.name)
                LinkedDocSyncer(new_doc, payload=payload).run()
                _refresh_primary_address(new_doc, payload)


def before_save(doc, method):
    DocTypeRenameHandler(doc, method).before_save()
