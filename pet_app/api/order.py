import frappe
from frappe import _
from frappe.utils import flt, nowdate

# ─────────────────────────────────────────
# Roles
# ─────────────────────────────────────────

ORDER_ROLES = ("System Manager", "Item Manager", "Stock Manager", "Administrator")

def _check_permission():
    if frappe.session.user == "Administrator":
        return
    user_roles = frappe.get_roles(frappe.session.user)
    if not any(r in user_roles for r in ORDER_ROLES):
        frappe.throw(_("Not authorized"), frappe.PermissionError)


# ─────────────────────────────────────────
# 1. place_order
# ─────────────────────────────────────────

@frappe.whitelist()
def place_order(customer, items, payment_method="Cash on Delivery",
                delivery_lat=None, delivery_lng=None,
                shipping_address_name=None, coupon_code=None,
                shipping_rule=None):
    """
    POST /api/method/pet_app.api.order.place_order
    {
      "customer": "CUST-2026-00001",
      "items": [{"item_code": "RC-PUPPY-001", "qty": 2, "rate": 45000}],
      "payment_method": "Cash on Delivery",
      "delivery_lat": 33.3152,
      "delivery_lng": 44.3661,
      "coupon_code": "SAVE10",
      "shipping_rule": "Standard Delivery"
    }
    """
    import json

    if isinstance(items, str):
        items = json.loads(items)

    if not items:
        frappe.throw(_("No items provided"))

    # Validate customer
    if not frappe.db.exists("Customer", customer):
        frappe.throw(_(f"Customer '{customer}' does not exist"))

    # Validate items + stock + price
    for item in items:
        item_code = item.get("item_code")
        if not frappe.db.exists("Item", item_code):
            frappe.throw(_(f"Item '{item_code}' does not exist"))

        qty = flt(item.get("qty", 0))
        if qty <= 0:
            frappe.throw(_(f"Invalid qty for item '{item_code}'"))

        # Check stock
        stock_qty = frappe.db.get_value("Bin",
            {"item_code": item_code, "warehouse": "Stores - H"}, "actual_qty") or 0
        if stock_qty < qty:
            frappe.throw(_(f"Insufficient stock for '{item_code}'. Available: {stock_qty}"))

        # Always fetch price from ERPNext — never trust mobile
        rate = frappe.db.get_value("Item Price",
            {"item_code": item_code, "price_list": "Standard Selling"}, "price_list_rate")
        if not rate:
            frappe.throw(_(f"Price not set for '{item_code}'"))
        item["rate"] = rate

    # Validate coupon
    if coupon_code:
        coupon = frappe.db.get_value("Coupon Code",
            {"coupon_code": coupon_code}, ["name", "valid_upto", "maximum_use", "used"], as_dict=True)
        if not coupon:
            frappe.throw(_(f"Coupon '{coupon_code}' is not valid"))
        if coupon.valid_upto and str(coupon.valid_upto) < nowdate():
            frappe.throw(_(f"Coupon '{coupon_code}' has expired"))
        if coupon.maximum_use and coupon.used >= coupon.maximum_use:
            frappe.throw(_(f"Coupon '{coupon_code}' usage limit reached"))

    # Build Sales Order
    so = frappe.new_doc("Sales Order")
    so.customer = customer
    so.transaction_date = nowdate()
    so.delivery_date = nowdate()
    so.custom_order_status = "Draft"
    so.custom_payment_method = payment_method
    so.custom_payment_status = "Pending"

    if delivery_lat:
        so.custom_delivery_lat = flt(delivery_lat)
    if delivery_lng:
        so.custom_delivery_lng = flt(delivery_lng)
    if shipping_address_name:
        so.shipping_address_name = shipping_address_name
    if coupon_code:
        so.coupon_code = coupon_code
    if shipping_rule:
        so.shipping_rule = shipping_rule

    for item in items:
        so.append("items", {
            "item_code": item.get("item_code"),
            "qty": flt(item.get("qty", 1)),
            "rate": flt(item.get("rate", 0)),
            "delivery_date": nowdate()
        })

    so.flags.ignore_permissions = True
    so.insert()
    so.submit()
    frappe.db.commit()

    frappe.response["data"] = {
        "order": so.name,
        "customer": so.customer,
        "customer_name": so.customer_name,
        "order_status": so.custom_order_status,
        "payment_method": so.custom_payment_method,
        "payment_status": so.custom_payment_status,
        "grand_total": so.grand_total,
        "items": [
            {
                "item_code": i.item_code,
                "item_name": i.item_name,
                "qty": i.qty,
                "rate": i.rate,
                "amount": i.amount
            } for i in so.items
        ]
    }


# ─────────────────────────────────────────
# 2. complete_order
# ─────────────────────────────────────────

@frappe.whitelist()
def complete_order(order_id):
    """
    POST /api/method/pet_app.api.order.complete_order
    {"order_id": "SAL-ORD-2026-00001"}

    - Creates Sales Invoice
    - Submits it (stock reduces automatically)
    - Sets order_status → Completed
    - Sets payment_status → Paid
    """
    _check_permission()

    doc = frappe.get_doc("Sales Order", order_id)

    if doc.custom_order_status == "Completed":
        frappe.throw(_("Order is already completed"))

    if doc.custom_order_status == "Cancelled":
        frappe.throw(_("Cannot complete a cancelled order"))

    try:
        from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

        si = make_sales_invoice(order_id)
        si.flags.ignore_permissions = True
        si.insert()
        si.submit()

        frappe.db.set_value("Sales Order", order_id, {
            "custom_order_status": "Completed",
            "custom_payment_status": "Paid"
        })

        frappe.db.commit()

        frappe.response["data"] = {
            "order": order_id,
            "order_status": "Completed",
            "payment_status": "Paid",
            "invoice": si.name,
            "grand_total": si.grand_total
        }

    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), f"complete_order: {order_id}")
        frappe.throw(_(str(e)))


# ─────────────────────────────────────────
# 3. cancel_order
# ─────────────────────────────────────────

@frappe.whitelist()
def cancel_order(order_id, reason=None):
    """
    POST /api/method/pet_app.api.order.cancel_order
    {
      "order_id": "SAL-ORD-2026-00001",
      "reason": "Customer request"
    }
    """
    _check_permission()

    doc = frappe.get_doc("Sales Order", order_id)

    if doc.custom_order_status == "Completed":
        frappe.throw(_("Cannot cancel a completed order"))

    if doc.custom_order_status == "Cancelled":
        frappe.throw(_("Order is already cancelled"))

    try:
        doc.flags.ignore_permissions = True
        doc.cancel()

        frappe.db.set_value("Sales Order", order_id, {
            "custom_order_status": "Cancelled",
            "custom_payment_status": "Refunded" if doc.custom_payment_status == "Paid" else "Pending"
        })

        frappe.db.commit()

        frappe.response["data"] = {
            "order": order_id,
            "order_status": "Cancelled",
            "reason": reason or ""
        }

    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), f"cancel_order: {order_id}")
        frappe.throw(_(str(e)))


# ─────────────────────────────────────────
# Document Event — on_update
# ─────────────────────────────────────────

def on_sales_order_update(doc, method):
    """Auto-set payment_status when order_status → Completed"""
    if doc.custom_order_status != "Completed":
        return

    if doc.custom_payment_status == "Paid":
        return

    frappe.db.set_value("Sales Order", doc.name, "custom_payment_status", "Paid")
    frappe.db.commit()
    frappe.logger().info(f"✅ payment_status → Paid for {doc.name}")
