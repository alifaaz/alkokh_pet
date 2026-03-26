import frappe
from frappe import _
from frappe.utils import flt, cint
from itertools import product as iterproduct


# ─────────────────────────────────────────
# Allowed Roles
# ─────────────────────────────────────────

PRODUCT_ROLES = ("System Manager", "Item Manager", "Stock Manager", "Administrator")


def _check_permission():
    if frappe.session.user == "Administrator":
        return
    user_roles = frappe.get_roles(frappe.session.user)
    if not any(r in user_roles for r in PRODUCT_ROLES):
        frappe.throw(_("غير مصرح — تحتاج صلاحية Item Manager أو Stock Manager"), frappe.PermissionError)


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


def _get_bin_qty(item_code, warehouse=None):
    if warehouse:
        return flt(
            frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty") or 0
        )
    result = frappe.db.sql("""
        SELECT COALESCE(SUM(actual_qty), 0)
        FROM `tabBin`
        WHERE item_code = %s
    """, (item_code,))
    return flt(result[0][0]) if result else 0.0


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


# ─────────────────────────────────────────
# Item
# ─────────────────────────────────────────

def _ensure_item(doc):
    """خلق أو تحديث Item"""
    item_code = doc.item or doc.sku
    exists    = frappe.db.exists("Item", item_code)
    item      = frappe.get_doc("Item", item_code) if exists else frappe.new_doc("Item")

    if not exists:
        item.item_code = item_code

    item.item_name     = doc.product_name
    item.item_group    = doc.category or "All Item Groups"
    item.description   = doc.description or ""
    item.stock_uom     = item.stock_uom or "Nos"
    item.is_stock_item = 1
    item.image         = doc.image or ""

    item.flags.ignore_permissions = True
    item.save() if exists else item.insert()

    frappe.db.set_value("Product", doc.name, "item", item.name, update_modified=False)
    return item


# ─────────────────────────────────────────
# Item Price
# ─────────────────────────────────────────

def _ensure_item_price(doc, item_code, rate=None):
    """خلق أو تحديث Item Price"""
    rate       = rate if rate is not None else _effective_rate(doc)
    price_list = "Standard Selling"
    currency   = frappe.defaults.get_global_default("currency") or "IQD"

    if rate <= 0:
        return None

    existing = frappe.db.get_value("Item Price", {
        "item_code":  item_code,
        "price_list": price_list,
        "selling":    1
    }, "name")

    ip = frappe.get_doc("Item Price", existing) if existing else frappe.new_doc("Item Price")

    if not existing:
        ip.item_code  = item_code
        ip.price_list = price_list
        ip.selling    = 1

    ip.price_list_rate = rate
    ip.currency        = currency

    ip.flags.ignore_permissions = True
    ip.save() if existing else ip.insert()

    frappe.db.set_value("Product", doc.name, "item_price", ip.name, update_modified=False)
    return ip


# ─────────────────────────────────────────
# Website Item
# ─────────────────────────────────────────

def _ensure_website_item(doc, item_code, published=1):
    """خلق أو تحديث Website Item"""
    existing = frappe.db.get_value("Website Item", {"item_code": item_code}, "name")
    wi       = frappe.get_doc("Website Item", existing) if existing else frappe.new_doc("Website Item")

    if not existing:
        wi.item_code = item_code

    wi.published            = published
    wi.website_item_name    = doc.product_name
    wi.web_long_description = doc.description or ""

    wi.flags.ignore_permissions = True
    wi.save() if existing else wi.insert()

    frappe.db.set_value("Product", doc.name, "website_item", wi.name, update_modified=False)
    return wi


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
    for attr_name in attributes_map:
        if attr_name not in existing_attrs:
            template_item.append("attributes", {"attribute": attr_name})

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
            variant              = frappe.new_doc("Item")
            variant.item_code    = item_code
            variant.item_name    = f"{doc.product_name} - {suffix}"
            variant.variant_of   = template_item.name
            variant.item_group   = doc.category or "All Item Groups"
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
    "product_variant"
]


@frappe.whitelist(allow_guest=False)
def publish_product(product_id=None, **kwargs):
    _check_permission()

    try:
        # ── capture qty before anything ──
        _initial_qty = flt(kwargs.pop("qty", 0))

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
            doc.flags.ignore_permissions = True
            doc.insert()
            frappe.db.commit()
            product_id = doc.name

        doc = frappe.get_doc("Product", product_id)

        # Validate
        _validate_publish(doc)

        if not cint(doc.has_variants):
            # ── Simple Product ──
            item = _ensure_item(doc)
            _ensure_item_price(doc, item.name)
            _ensure_website_item(doc, item.name)

            # ── Initial Stock ──
            if _initial_qty > 0:
                wh = _get_default_warehouse()
                if not wh:
                    frappe.throw(_("Default Warehouse not set in Stock Settings"))
                se = frappe.new_doc("Stock Entry")
                se.stock_entry_type = "Material Receipt"
                se.remarks = f"Initial stock for {doc.name}"
                se.append("items", {
                    "item_code":   item.name,
                    "t_warehouse": wh,
                    "qty":         _initial_qty,
                    "basic_rate":  _effective_rate(doc) or 1
                })
                se.flags.ignore_permissions = True
                se.insert()
                se.submit()

        else:
            # ── Variant Product ──
            exists = frappe.db.exists("Item", doc.sku)
            item   = frappe.get_doc("Item", doc.sku) if exists else frappe.new_doc("Item")

            if not exists:
                item.item_code = doc.sku

            item.item_name      = doc.product_name
            item.item_group     = doc.category or "All Item Groups"
            item.brand          = doc.brand or ""
            item.stock_uom      = item.stock_uom or "Nos"
            item.is_stock_item  = 1
            item.description    = doc.description or ""
            item.image          = doc.image or ""

            item.flags.ignore_permissions = True
            item.save() if exists else item.insert()

            frappe.db.set_value("Product", doc.name, "item", item.name, update_modified=False)

            _ensure_item_price(doc, item.name)
            _ensure_website_item(doc, item.name)

            # ── Initial Stock for variant ──
            if _initial_qty > 0:
                wh = _get_default_warehouse()
                if not wh:
                    frappe.throw(_("Default Warehouse not set in Stock Settings"))
                se = frappe.new_doc("Stock Entry")
                se.stock_entry_type = "Material Receipt"
                se.remarks = f"Initial stock for {doc.name}"
                se.append("items", {
                    "item_code":   item.name,
                    "t_warehouse": wh,
                    "qty":         _initial_qty,
                    "basic_rate":  _effective_rate(doc) or 1
                })
                se.flags.ignore_permissions = True
                se.insert()
                se.submit()

            generated = []

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

        wh = _get_default_warehouse()
        frappe.response["data"] = {
            "product":      doc.name,
            "item":         doc.item or doc.sku,
            "website_item": doc.website_item,
            "has_variants": cint(doc.has_variants),
            "qty":          _get_bin_qty(doc.item or doc.sku, wh),
            "variants":     generated if cint(doc.has_variants) else [],
            "options":      [{"options": r.options, "value": r.value, "price": flt(r.price) if flt(r.price) > 0 else _effective_rate(doc)} for r in doc.product_variant] if cint(doc.has_variants) else [],
            **brand_data
        }

    except frappe.ValidationError:
        frappe.db.rollback()
        raise
    except Exception as e:
        frappe.db.rollback()
        _err(str(e), exc=True)

        
@frappe.whitelist()
def restock_product(product_id, qty, warehouse=None, item_variant=None):
    """
    إضافة مخزون للمنتج

    POST /api/method/pet_app.api.product.restock_product
    Body:
    {
      "product_id": "PRODUCT-00001",
      "qty": 20,
      "warehouse": "Stores - H",
      "item_variant": "RC-001-1kg"   ← مطلوب فقط إذا has_variants
    }
    """
    _check_permission()

    qty = flt(qty)
    if qty <= 0:
        frappe.throw(_("الكمية يجب أن تكون أكبر من صفر"))

    doc = frappe.get_doc("Product", product_id)

    if not doc.item:
        frappe.throw(_("المنتج غير منشور — انشره أولاً"))

    # variant products use one template item
    item_code = doc.item

    wh = warehouse or _get_default_warehouse()
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
def get_stock_info(product_id, warehouse=None):
    """
    جلب معلومات المخزون

    GET /api/method/pet_app.api.product.get_stock_info?product_id=PRODUCT-00001
    GET /api/method/pet_app.api.product.get_stock_info?product_id=PRODUCT-00001&warehouse=Stores - H
    """
    _check_permission()

    doc = frappe.get_doc("Product", product_id)
    wh  = warehouse or _get_default_warehouse()

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
def get_products(filters=None, fields=None, order_by="creation desc",
                 limit_start=0, limit_page_length=20,
                 search_term=None):
    import json

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
        order_by=order_by,
        limit_start=cint(limit_start),
        limit_page_length=cint(limit_page_length)
    )

    wh = _get_default_warehouse()
    for p in products:
        p["qty"] = _get_bin_qty(p["item"], wh) if p.get("item") else 0

        p["images"] = frappe.db.get_all("File", 
            filters={"attached_to_doctype": "Product", "attached_to_name": p["name"], "is_private": 0},
            fields=["name", "file_url", "file_name", "custom_is_default"],
            order_by="custom_is_default desc, creation asc"
        )

        p["category_image"] = frappe.db.get_value("Item Group", p.get("category"), "image") if p.get("category") else None

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