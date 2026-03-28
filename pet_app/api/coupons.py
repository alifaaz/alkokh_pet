import frappe
from frappe import _
from frappe.utils import nowdate

# ─────────────────────────────────────────────
#  PERMISSION GATE
# ─────────────────────────────────────────────
ALLOWED_ROLES = {"System Manager", "Sales Manager", "Sales User"}

def _check_permission():
    if set(frappe.get_roles(frappe.session.user)).isdisjoint(ALLOWED_ROLES):
        frappe.throw(_("Not permitted"), frappe.PermissionError)


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def _build_pricing_rule_name(coupon_code: str) -> str:
    """Deterministic name so we can always find the linked rule."""
    return f"COUPON-{coupon_code}"


def _make_pricing_rule_doc(data: dict) -> "frappe.Document":
    """
    Build (but do NOT insert) a Pricing Rule document from coupon payload.
    data keys:
        coupon_code, coupon_name,
        discount_type          : "Percentage" | "Fixed Amount" | "Free Shipping" | "Free Item"
        discount_percentage    : float  (only for Percentage)
        discount_amount        : float  (only for Fixed Amount, in IQD)
        free_item              : str    (only for Free Item)
        apply_on               : "All Items" | "Item Group" | "Item" | "Transaction"
        apply_on_value         : str    (item group name or item code — ignored for All/Transaction)
        price_list             : str    (optional, default Standard Selling)
        min_order_amount       : float  (optional)
        valid_upto             : date   (optional)
        customer               : str    (optional)
    """
    discount_type = data.get("discount_type", "Percentage")
    apply_on      = data.get("apply_on", "All Items")

    # Map our apply_on to Frappe's "Apply On" field values
    frappe_apply_on_map = {
        "All Items":   "All Items",
        "Item Group":  "Item Group",
        "Item":        "Item Code",
        "Transaction": "Transaction",
    }
    frappe_apply_on = frappe_apply_on_map.get(apply_on, "All Items")

    pr = frappe.new_doc("Pricing Rule")
    pr.name             = _build_pricing_rule_name(data["coupon_code"])
    pr.title            = data.get("coupon_name") or data["coupon_code"]
    pr.apply_on         = frappe_apply_on
    pr.price_or_product_discount = "Price" if discount_type != "Free Item" else "Product"
    pr.selling          = 1
    pr.buying           = 0
    pr.currency         = "IQD"
    pr.price_list       = data.get("price_list") or "Standard Selling"
    pr.disable          = 0
    pr.coupon_code_based = 1   # ties the rule to a coupon

    # ── apply_on detail ──────────────────────────────
    if frappe_apply_on == "Item Group" and data.get("apply_on_value"):
        pr.append("items", {"item_group": data["apply_on_value"]})
    elif frappe_apply_on == "Item Code" and data.get("apply_on_value"):
        pr.append("items", {"item_code": data["apply_on_value"]})
    # "All Items" and "Transaction" need no items child table rows

    # ── discount type ────────────────────────────────
    if discount_type == "Percentage":
        pr.rate_or_discount = "Discount Percentage"
        pr.discount_percentage = float(data.get("discount_percentage") or 0)

    elif discount_type == "Fixed Amount":
        pr.rate_or_discount = "Discount Amount"
        pr.discount_amount  = float(data.get("discount_amount") or 0)

    elif discount_type == "Free Shipping":
        # Frappe doesn't have a native "free shipping" flag on Pricing Rule.
        # Best practice: 100% discount on the Shipping item code if you have one,
        # OR store it as a note and handle in your cart logic.
        pr.rate_or_discount = "Discount Percentage"
        pr.discount_percentage = 100
        # tag it so cart logic can recognise it
        pr.remarks = "FREE_SHIPPING"

    elif discount_type == "Free Item":
        pr.price_or_product_discount = "Product"
        pr.free_item                  = data.get("free_item") or ""
        pr.free_qty                   = 1
        pr.free_item_rate             = 0

    # ── constraints ──────────────────────────────────
    if data.get("min_order_amount"):
        pr.min_amount = float(data["min_order_amount"])

    if data.get("valid_upto"):
        pr.valid_upto = data["valid_upto"]

    if data.get("customer"):
        pr.applicable_for = "Customer"
        pr.customer        = data["customer"]

    return pr


# ─────────────────────────────────────────────
#  CREATE
# ─────────────────────────────────────────────
@frappe.whitelist()
def create_coupon(
    coupon_code,
    coupon_name=None,
    discount_type="Percentage",
    discount_percentage=10,
    discount_amount=0,
    free_item=None,
    apply_on="All Items",
    apply_on_value=None,
    price_list="Standard Selling",
    min_order_amount=None,
    valid_upto=None,
    customer=None,
    max_redemption=None,
):
    _check_permission()

    coupon_code = coupon_code.strip().upper()

    if frappe.db.exists("Coupon Code", coupon_code):
        frappe.throw(_(f"Coupon '{coupon_code}' already exists."))

    data = {
        "coupon_code":        coupon_code,
        "coupon_name":        coupon_name or coupon_code,
        "discount_type":      discount_type,
        "discount_percentage": discount_percentage,
        "discount_amount":    discount_amount,
        "free_item":          free_item,
        "apply_on":           apply_on,
        "apply_on_value":     apply_on_value,
        "price_list":         price_list,
        "min_order_amount":   min_order_amount,
        "valid_upto":         valid_upto,
        "customer":           customer,
    }

    # 1) Create Pricing Rule first so we can link its name to the coupon
    pr = _make_pricing_rule_doc(data)
    pr.insert(ignore_permissions=True)
    frappe.db.commit()

    # 2) Create Coupon Code and point it at the Pricing Rule
    coupon = frappe.new_doc("Coupon Code")
    coupon.coupon_code      = coupon_code
    coupon.coupon_name      = data["coupon_name"]
    coupon.coupon_type      = "Promotional"
    coupon.pricing_rule     = pr.name
    coupon.maximum_use      = int(max_redemption) if max_redemption else 0
    coupon.valid_from       = nowdate()
    coupon.valid_upto       = valid_upto or None
    coupon.insert(ignore_permissions=True)
    frappe.db.commit()

    frappe.response["data"] = {
        "coupon_code":  coupon.coupon_code,
        "pricing_rule": pr.name,
        "status":       "created",
    }


# ─────────────────────────────────────────────
#  READ (single)
# ─────────────────────────────────────────────
@frappe.whitelist()
def get_coupon(coupon_code):
    _check_permission()

    coupon_code = coupon_code.strip().upper()

    if not frappe.db.exists("Coupon Code", coupon_code):
        frappe.throw(_(f"Coupon '{coupon_code}' not found."), frappe.DoesNotExistError)

    coupon = frappe.get_doc("Coupon Code", coupon_code)
    pr     = frappe.get_doc("Pricing Rule", coupon.pricing_rule) if coupon.pricing_rule else None

    frappe.response["data"] = {
        "coupon_code":         coupon.coupon_code,
        "coupon_name":         coupon.coupon_name,
        "coupon_type":         coupon.coupon_type,
        "pricing_rule":        coupon.pricing_rule,
        "valid_from":          str(coupon.valid_from or ""),
        "valid_upto":          str(coupon.valid_upto or ""),
        "maximum_use":         coupon.maximum_use,
        "used":                coupon.used,
        "pricing_rule_detail": {
            "apply_on":            pr.apply_on            if pr else None,
            "rate_or_discount":    pr.rate_or_discount    if pr else None,
            "discount_percentage": pr.discount_percentage if pr else None,
            "discount_amount":     pr.discount_amount     if pr else None,
            "min_amount":          pr.min_amount          if pr else None,
            "valid_upto":          str(pr.valid_upto or "") if pr else None,
            "disable":             pr.disable             if pr else None,
            "remarks":             pr.remarks             if pr else None,
        } if pr else None,
    }


# ─────────────────────────────────────────────
#  READ (list)
# ─────────────────────────────────────────────
@frappe.whitelist()
def list_coupons(limit_start=0, limit_page_length=20, search=None, active_only=0):
    _check_permission()

    limit_start       = int(limit_start)
    limit_page_length = int(limit_page_length)
    active_only       = int(active_only)

    filters = {}
    if active_only:
        filters["valid_upto"] = [">=", nowdate()]

    if search:
        filters["coupon_code"] = ["like", f"%{search.upper()}%"]

    coupons = frappe.get_all(
        "Coupon Code",
        filters=filters,
        fields=[
            "coupon_code", "coupon_name", "pricing_rule",
            "valid_from", "valid_upto", "maximum_use", "used",
        ],
        limit_start=limit_start,
        limit_page_length=limit_page_length,
        order_by="creation desc",
    )

    frappe.response["data"] = coupons


# ─────────────────────────────────────────────
#  UPDATE
# ─────────────────────────────────────────────
@frappe.whitelist()
def update_coupon(
    coupon_code,
    coupon_name=None,
    discount_type=None,
    discount_percentage=None,
    discount_amount=None,
    free_item=None,
    apply_on=None,
    apply_on_value=None,
    price_list=None,
    min_order_amount=None,
    valid_upto=None,
    customer=None,
    max_redemption=None,
):
    _check_permission()

    coupon_code = coupon_code.strip().upper()

    if not frappe.db.exists("Coupon Code", coupon_code):
        frappe.throw(_(f"Coupon '{coupon_code}' not found."), frappe.DoesNotExistError)

    coupon = frappe.get_doc("Coupon Code", coupon_code)

    # ── update Coupon Code doc ────────────────
    if coupon_name:
        coupon.coupon_name = coupon_name
    if max_redemption is not None:
        coupon.maximum_use = int(max_redemption)
    if valid_upto:
        coupon.valid_upto = valid_upto
    coupon.save(ignore_permissions=True)

    # ── update linked Pricing Rule ────────────
    if coupon.pricing_rule and frappe.db.exists("Pricing Rule", coupon.pricing_rule):
        pr = frappe.get_doc("Pricing Rule", coupon.pricing_rule)

        if coupon_name:
            pr.title = coupon_name
        if price_list:
            pr.price_list = price_list
        if min_order_amount is not None:
            pr.min_amount = float(min_order_amount)
        if valid_upto:
            pr.valid_upto = valid_upto
        if customer:
            pr.applicable_for = "Customer"
            pr.customer        = customer

        # discount changes
        if discount_type == "Percentage" and discount_percentage is not None:
            pr.rate_or_discount    = "Discount Percentage"
            pr.discount_percentage = float(discount_percentage)
        elif discount_type == "Fixed Amount" and discount_amount is not None:
            pr.rate_or_discount = "Discount Amount"
            pr.discount_amount  = float(discount_amount)
        elif discount_type == "Free Shipping":
            pr.rate_or_discount    = "Discount Percentage"
            pr.discount_percentage = 100
            pr.remarks             = "FREE_SHIPPING"
        elif discount_type == "Free Item" and free_item:
            pr.price_or_product_discount = "Product"
            pr.free_item                 = free_item

        # apply_on changes — clear items child and re-add
        if apply_on:
            frappe_apply_on_map = {
                "All Items":   "All Items",
                "Item Group":  "Item Group",
                "Item":        "Item Code",
                "Transaction": "Transaction",
            }
            pr.apply_on = frappe_apply_on_map.get(apply_on, pr.apply_on)
            pr.set("items", [])  # clear existing
            if apply_on == "Item Group" and apply_on_value:
                pr.append("items", {"item_group": apply_on_value})
            elif apply_on == "Item" and apply_on_value:
                pr.append("items", {"item_code": apply_on_value})

        pr.save(ignore_permissions=True)

    frappe.db.commit()
    frappe.response["data"] = {"coupon_code": coupon_code, "status": "updated"}


# ─────────────────────────────────────────────
#  DELETE  (disables Pricing Rule, removes Coupon)
# ─────────────────────────────────────────────
@frappe.whitelist()
def delete_coupon(coupon_code):
    _check_permission()

    coupon_code = coupon_code.strip().upper()

    if not frappe.db.exists("Coupon Code", coupon_code):
        frappe.throw(_(f"Coupon '{coupon_code}' not found."), frappe.DoesNotExistError)

    coupon = frappe.get_doc("Coupon Code", coupon_code)

    # ── disable linked Pricing Rule (don't delete) ──
    if coupon.pricing_rule and frappe.db.exists("Pricing Rule", coupon.pricing_rule):
        frappe.db.set_value("Pricing Rule", coupon.pricing_rule, "disable", 1)

    # ── delete the Coupon Code doc ───────────────────
    frappe.delete_doc("Coupon Code", coupon_code, ignore_permissions=True)
    frappe.db.commit()

    frappe.response["data"] = {
        "coupon_code":  coupon_code,
        "pricing_rule": coupon.pricing_rule,
        "status":       "deleted",
        "pricing_rule_status": "disabled",
    }