import frappe
from frappe import _
from frappe.utils import flt, cint
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
    allowed_fields = {"creation", "modified", "product_name", "sku", "price", "discounted_price", "status"}
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
    row = frappe.db.get_value("Item", item_code, ["name", "disabled"], as_dict=True)
    if not row:
        frappe.throw(
            _("Item {0} was not found. Link this product to an existing item before publishing.").format(item_code)
        )
    if cint(row.disabled):
        frappe.throw(
            _("Item {0} is disabled. Link this product to an active item before publishing.").format(item_code)
        )
    return row.name


@frappe.whitelist(allow_guest=False)
@standardize_response
def publish_product(product_id=None, **kwargs):
    _check_permission("write" if product_id else "create")

    try:
        # ── capture qty before anything ──
        _initial_qty = flt(kwargs.pop("qty", 0))

        # ── Storefront-overlay preconditions, checked BEFORE anything is written ──
        # Items - and their price, stock, warehouse and UOM - are maintained by clinic
        # staff in the Item form. Product is only the storefront face on top of one, so
        # publishing ATTACHES to an existing Item and never mints one. Nothing in this
        # endpoint writes to Item, Item Price, Stock Entry or Bin.
        #
        # Checked up front deliberately: the create branch below inserts AND commits, so
        # a throw after that point could not be rolled back and would strand a half-made
        # Product. Failing here writes nothing at all.
        if product_id:
            _existing = frappe.db.get_value("Product", product_id, ["item", "has_variants"], as_dict=True)
            if not _existing:
                frappe.throw(_("Product {0} was not found.").format(product_id))
            _target_item = kwargs.get("item") or _existing.item
            _has_variants = cint(kwargs["has_variants"] if "has_variants" in kwargs else _existing.has_variants)
        else:
            _target_item = kwargs.get("item")
            _has_variants = cint(kwargs.get("has_variants") or 0)

        if _has_variants:
            frappe.throw(_("Variants are not supported yet. Publish this product without variants."))

        _assert_publishable_item(_target_item)

        if _initial_qty > 0:
            # Refused rather than silently ignored: dropping it would report a successful
            # publish while no stock had actually moved.
            frappe.throw(_("Stock is managed on the Item - update it there. Publishing no longer seeds stock."))

        # ── خلق جديد إذا ما في product_id ──
        if not product_id:
            doc = frappe.new_doc("Product")
            sku = kwargs.get("sku")
            if not sku:
                last = frappe.db.sql("SELECT MAX(CAST(SUBSTRING(sku, 5) AS UNSIGNED)) FROM `tabProduct` WHERE sku LIKE 'SKU-%'")
                last_num = int(last[0][0] or 0)
                sku = f"SKU-{str(last_num + 1).zfill(5)}"
                kwargs["sku"] = sku
            elif frappe.db.get_value("Product", {"sku": sku}, "name"):
                frappe.throw(_(f"SKU '{sku}' already exists"))
            for f in PRODUCT_FIELDS:
                if f in kwargs:
                    doc.set(f, kwargs[f])

            # ── Brand ──
            if kwargs.get("brand"):
                if not frappe.db.exists("Brand", kwargs["brand"]):
                    frappe.throw(_(f"Brand '{kwargs['brand']}' does not exist"))
                doc.set("brand", kwargs["brand"])

            if not doc.status:
                doc.status = "Draft"

            _validate_publish(doc)
            doc.flags.ignore_permissions = True
            doc.insert()
            frappe.db.commit()
            product_id = doc.name

        doc = frappe.get_doc("Product", product_id)


        # The Item-minting branches that used to sit here are gone. Publishing no longer
        # calls _ensure_item / _ensure_item_price and no longer posts a Stock Entry; the
        # Item was validated above and is left exactly as clinic staff maintain it.
        # From here on this endpoint writes to the Product row and nothing else.

        # حدّث حالة الـ Product
        frappe.db.set_value("Product", doc.name, {
            "status":   "Active",
            "in_stock": 1
        }, update_modified=False)

        frappe.db.commit()
        doc.reload()

        # ── Brand details ──
        if doc.brand:
            brand_doc = frappe.db.get_value("Brand", doc.brand, ["name", "brand", "image"], as_dict=True)
            brand_data = {
                "brand_id":    brand_doc.get("name"),
                "brand_name":  brand_doc.get("brand"),
                "brand_image": brand_doc.get("image"),
            }
        else:
            brand_data = {
                "brand_id":    None,
                "brand_name":  None,
                "brand_image": None,
            }

        wh = _resolve_allowed_warehouse()
        frappe.response["data"] = {
            "product":      doc.name,
            "item":         doc.item or doc.sku,
            "has_variants": cint(doc.has_variants),
            # Read-only lookup of the Item's real stock for display; writes nothing.
            "qty":          _get_bin_qty(doc.item or doc.sku, wh),
            # Always empty while variants are unsupported. The old expression referenced
            # `generated`, which was only ever bound inside the deleted variant branch.
            "variants":     [],
            "options":      [],
            **brand_data
        }

    except frappe.ValidationError:
        frappe.db.rollback()
        raise
    except Exception as e:
        frappe.db.rollback()
        _err(str(e), exc=True)

        
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
    """
    جلب معلومات المخزون

    GET /api/method/pet_app.api.product.get_stock_info?product_id=PRODUCT-00001
    GET /api/method/pet_app.api.product.get_stock_info?product_id=PRODUCT-00001&warehouse=Stores - K
    """
    _check_permission("read")

    doc = frappe.get_doc("Product", product_id)
    wh  = _resolve_allowed_warehouse(warehouse)

    # المنتج ما منشور بعد
    if not doc.item:
        frappe.response["data"] = {
            "in_stock":        bool(doc.in_stock),
            "qty":             0,
            "last_restocked":  None,
            "total_lifetime":  0,
            "variants":        []
        }
        return

    if not cint(doc.has_variants):
        # ── Simple ──
        qty = _get_bin_qty(doc.item, wh)

        last_restock = frappe.db.sql("""
            SELECT MAX(se.posting_date)
            FROM `tabStock Entry` se
            JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
            WHERE sed.item_code = %s
              AND se.stock_entry_type = 'Material Receipt'
              AND se.docstatus = 1
        """, (doc.item,))

        total_lifetime = frappe.db.sql("""
            SELECT COALESCE(SUM(sed.qty), 0)
            FROM `tabStock Entry` se
            JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
            WHERE sed.item_code = %s
              AND se.stock_entry_type = 'Material Receipt'
              AND se.docstatus = 1
        """, (doc.item,))[0][0]

        frappe.response["data"] = {
            "in_stock":        qty > 0,
            "qty":             qty,
            "last_restocked":  str(last_restock[0][0]) if last_restock and last_restock[0][0] else None,
            "total_lifetime":  flt(total_lifetime),
            "variants":        []
        }

    else:
        # ── Variants ──
        variant_data = []
        total_qty    = 0

        # variant product uses one item — get total qty from that item
        total_qty = _get_bin_qty(doc.item, wh)

        last_restock = frappe.db.sql("""
            SELECT MAX(se.posting_date)
            FROM `tabStock Entry` se
            JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
            WHERE sed.item_code = %s
              AND se.stock_entry_type = 'Material Receipt'
              AND se.docstatus = 1
        """, (doc.item,))

        total_lifetime = frappe.db.sql("""
            SELECT COALESCE(SUM(sed.qty), 0)
            FROM `tabStock Entry` se
            JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
            WHERE sed.item_code = %s
              AND se.stock_entry_type = 'Material Receipt'
              AND se.docstatus = 1
        """, (doc.item,))[0][0]

        for row in doc.product_variant:
            variant_data.append({
                "options": row.options,
                "value":   row.value,
                "price":   flt(row.price) or _effective_rate(doc)
            })

        frappe.response["data"] = {
            "in_stock":       total_qty > 0,
            "qty":            total_qty,
            "last_restocked": str(last_restock[0][0]) if last_restock and last_restock[0][0] else None,
            "total_lifetime": flt(total_lifetime),
            "variants":       variant_data
        }

@frappe.whitelist()
@standardize_response
def get_products(filters=None, fields=None, order_by="creation desc",
                 limit_start=0, limit_page_length=20,
                 search_term=None):
    import json
    _check_permission("read")

    _filters = json.loads(filters) if isinstance(filters, str) else (filters or [])
    _fields  = json.loads(fields)  if isinstance(fields,  str) else (fields or [
        "name", "product_name", "sku", "image", "description",
        "price", "discounted_price", "status",
        "in_stock", "category", "vendor", "has_variants", "item", "brand"
    ])

    if search_term:
        _filters.append(["product_name", "like", f"%{search_term}%"])

    products = frappe.get_all(
        "Product",
        filters=_filters,
        fields=_fields,
        order_by=_sanitize_order_by(order_by),
        limit_start=cint(limit_start),
        limit_page_length=cint(limit_page_length)
    )

    wh = _resolve_allowed_warehouse()
    for p in products:
        p["qty"] = _get_bin_qty(p["item"], wh) if p.get("item") else 0

        p["images"] = frappe.db.get_all("File", 
            filters={"attached_to_doctype": "Product", "attached_to_name": p["name"], "is_private": 0},
            fields=["name", "file_url", "file_name", "custom_is_default"],
            order_by="custom_is_default desc, creation asc"
        )

        _enrich_product_category(p)

        if p.get("brand"):
            brand_doc = frappe.db.get_value("Brand", p.get("brand"), ["name", "brand", "image"], as_dict=True)
            p["brand_id"]    = brand_doc.get("name")
            p["brand_name"]  = brand_doc.get("brand")
            p["brand_image"] = brand_doc.get("image")
        else:
            p["brand_id"]    = None
            p["brand_name"]  = None
            p["brand_image"] = None

    frappe.response["data"] = products


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
