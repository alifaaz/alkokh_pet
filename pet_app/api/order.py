import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import cint, flt, nowdate, rounded
from pet_app.api.driver import (
    _create_driver_debit_entry,
    _collect_driver_cash,
    _reverse_driver_entry,
    COMPANY,
)
from pet_app.utils.guardian_customer import (
    get_guardian_by_user,
    get_guardian_record,
    get_or_create_customer_from_guardian,
)

# ─────────────────────────────────────────
# Roles
# ─────────────────────────────────────────
ORDER_ROLES = ("System Manager", "Item Manager", "Stock Manager", "Administrator", "Order")
GUARDIAN_ORDER_ROLES = {"Guardian", "Guardians"}
ORDER_RATE_LIMIT = 20
ORDER_RATE_WINDOW = 60

def _check_permission():
    if frappe.session.user == "Administrator":
        return
    user_roles = frappe.get_roles(frappe.session.user)
    if not any(r in user_roles for r in ORDER_ROLES):
        frappe.throw(_("Not authorized"), frappe.PermissionError)


def _require_order_access(guardian=None):
    if frappe.session.user == "Administrator":
        return

    user_roles = set(frappe.get_roles(frappe.session.user) or [])
    if user_roles.intersection(ORDER_ROLES):
        return

    if user_roles.intersection(GUARDIAN_ORDER_ROLES):
        current_guardian = get_guardian_by_user(frappe.session.user)
        if not current_guardian:
            frappe.throw(_("Guardian context is required for marketplace sales."), frappe.PermissionError)
        if guardian and guardian != current_guardian.get("name"):
            frappe.throw(_("Not authorized to place orders for another guardian."), frappe.PermissionError)
        return

    frappe.throw(_("Not authorized"), frappe.PermissionError)


def _require_sales_order_create_access():
    if not frappe.has_permission("Sales Order", ptype="create"):
        frappe.throw(_("Not permitted to create Sales Order."), frappe.PermissionError)


def _require_stock_entry_access():
    if frappe.session.user == "Administrator":
        return
    user_roles = set(frappe.get_roles(frappe.session.user) or [])
    allowed_roles = {"System Manager", "Stock Manager", "Item Manager", "Administrator"}
    if user_roles.isdisjoint(allowed_roles):
        frappe.throw(_("Not permitted to create Stock Entry."), frappe.PermissionError)
    if not frappe.has_permission("Stock Entry", ptype="create"):
        frappe.throw(_("Not permitted to create Stock Entry."), frappe.PermissionError)


def _get_stock_basic_rate(item_code, warehouse=None):
    warehouse = warehouse or "Stores - H"
    rate = frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "valuation_rate")
    source = "bin_valuation_rate"

    if flt(rate) <= 0:
        rate = frappe.db.get_value("Item", item_code, "valuation_rate")
        source = "item_valuation_rate"

    if flt(rate) <= 0:
        rate = frappe.db.get_value("Item", item_code, "standard_rate")
        source = "item_standard_rate"

    if flt(rate) <= 0:
        rate = frappe.db.get_value(
            "Item Price",
            {"item_code": item_code, "price_list": "Standard Selling", "selling": 1},
            "price_list_rate",
        )
        source = "standard_selling_item_price"

    if flt(rate) <= 0:
        rate = 1
        source = "safe_minimum_fallback"

    if source in {"standard_selling_item_price", "safe_minimum_fallback"}:
        frappe.log_error(
            title="STOCK_BASIC_RATE_FALLBACK",
            message=frappe.as_json(
                {
                    "event": "STOCK_BASIC_RATE_FALLBACK",
                    "item_code": item_code,
                    "warehouse": warehouse,
                    "basic_rate": flt(rate),
                    "source": source,
                    "action_taken": "set_basic_rate_on_future_stock_entry",
                },
                indent=2,
            ),
        )

    return flt(rate)


def _resolve_sales_identity(guardian=None, customer=None):
    guardian_row = None

    if guardian:
        guardian_row = get_guardian_record(guardian)
    elif frappe.session.user and frappe.session.user != "Guest":
        guardian_row = get_guardian_by_user(frappe.session.user)

    if not guardian_row:
        frappe.throw(_("Guardian context is required for marketplace sales."))

    resolved_customer = get_or_create_customer_from_guardian(guardian_row)
    if customer and customer != resolved_customer:
        frappe.throw(_("Customer does not match the resolved Guardian identity."))

    return guardian_row, resolved_customer


def _round_currency(value, precision):
    return flt(rounded(flt(value), precision))


def _get_item_group_bounds(item_group, group_cache):
    if not item_group:
        return None

    if item_group not in group_cache:
        group_cache[item_group] = frappe.get_cached_value("Item Group", item_group, ["lft", "rgt"])

    return group_cache[item_group]


def _item_group_matches_targets(item_group, target_groups, group_cache=None):
    if not item_group or not target_groups:
        return False

    group_cache = group_cache or {}
    item_group_bounds = _get_item_group_bounds(item_group, group_cache)
    if not item_group_bounds:
        return False

    item_lft, item_rgt = item_group_bounds
    for target_group in target_groups:
        target_group_bounds = _get_item_group_bounds(target_group, group_cache)
        if not target_group_bounds:
            continue

        target_lft, target_rgt = target_group_bounds
        if target_lft <= item_lft <= item_rgt <= target_rgt:
            return True

    return False


def _get_rate_or_discount_and_value(rule):
    rate_or_discount = rule.get("rate_or_discount")
    discount_percentage = flt(rule.get("discount_percentage"))
    discount_amount = flt(rule.get("discount_amount"))

    if rate_or_discount == "Discount Percentage" and discount_percentage > 0:
        return rate_or_discount, discount_percentage
    if rate_or_discount == "Discount Amount" and discount_amount > 0:
        return rate_or_discount, discount_amount

    if discount_percentage > 0:
        return "Discount Percentage", discount_percentage
    if discount_amount > 0:
        return "Discount Amount", discount_amount

    return None, 0.0


def _get_scope_matches(item, rule, group_cache=None):
    apply_on = rule.get("apply_on")

    if apply_on == "Transaction":
        return True
    if apply_on == "Item Code":
        return item.get("item_code") in rule.get("_items", set())
    if apply_on == "Item Group":
        return _item_group_matches_targets(
            item.get("item_group"),
            rule.get("_item_groups", ()),
            group_cache=group_cache,
        )
    if apply_on == "Brand":
        return item.get("brand") in rule.get("_brands", set())

    return False


def _prepare_rule(rule, index):
    if not isinstance(rule, dict):
        return None

    items = rule.get("items")
    if items is None:
        items = rule.get("item_codes") or []

    normalized_rule = dict(rule)
    normalized_rule["_items"] = set(items or [])
    normalized_rule["_item_groups"] = tuple(rule.get("item_groups") or [])
    normalized_rule["_brands"] = set(rule.get("brands") or [])
    normalized_rule["rate_or_discount"] = rule.get("rate_or_discount") or (
        "Discount Percentage" if flt(rule.get("discount_percentage")) > 0 else "Discount Amount"
    )
    normalized_rule["_sort_key"] = (
        cint(rule.get("priority") or 1),
        str(rule.get("creation") or ""),
        str(rule.get("name") or rule.get("coupon_code") or ""),
        index,
    )
    return normalized_rule


def _normalize_rules(rule_or_rules):
    if not rule_or_rules:
        return []

    if isinstance(rule_or_rules, (list, tuple)):
        rules = [r for r in rule_or_rules if r]
    else:
        rules = [rule_or_rules]

    normalized_rules = []
    for index, rule in enumerate(rules):
        normalized_rule = _prepare_rule(rule, index)
        if normalized_rule:
            normalized_rules.append(normalized_rule)

    return sorted(normalized_rules, key=lambda r: r["_sort_key"])


def _calculate_discount_breakdown(so_items, rule_or_rules, precision=2):
    rules = _normalize_rules(rule_or_rules)
    total_discount = 0.0
    breakdown = []
    consumed_rows = set()
    group_cache = {}

    for rule in rules:
        rate_or_discount, discount_value = _get_rate_or_discount_and_value(rule)
        if not rate_or_discount or discount_value <= 0:
            continue

        stackable = cint(rule.get("stackable", 1))
        eligible_rows = []

        for idx, item in enumerate(so_items):
            if not stackable and idx in consumed_rows:
                continue

            if _get_scope_matches(item, rule, group_cache=group_cache):
                eligible_rows.append((idx, item))

        if not eligible_rows:
            continue

        eligible_total = _round_currency(
            sum(flt(item.get("net_amount") or item.get("amount")) for _, item in eligible_rows),
            precision,
        )
        if eligible_total <= 0:
            continue

        if rate_or_discount == "Discount Percentage":
            rule_discount = eligible_total * (discount_value / 100.0)
        elif rule.get("apply_on") == "Transaction":
            rule_discount = discount_value
        else:
            rule_discount = min(discount_value, eligible_total)

        max_discount = flt(rule.get("max_discount"))
        if max_discount > 0:
            rule_discount = min(rule_discount, max_discount)

        rule_discount = _round_currency(rule_discount, precision)
        if rule_discount <= 0:
            continue

        total_discount = _round_currency(total_discount + rule_discount, precision)
        breakdown.append(
            {
                "rule": rule.get("name") or rule.get("coupon_code") or rule.get("apply_on") or "discount_rule",
                "scope": rule.get("apply_on"),
                "rate_or_discount": rate_or_discount,
                "eligible_total": eligible_total,
                "rate": _round_currency(discount_value, precision),
                "amount": rule_discount,
            }
        )

        if not stackable:
            consumed_rows.update(idx for idx, _ in eligible_rows)

        if cint(rule.get("exclusive")):
            break

    return _round_currency(total_discount, precision), breakdown


def calculate_discounts(so_items, rule_or_rules, precision=2):
    total_discount, _ = _calculate_discount_breakdown(so_items, rule_or_rules, precision=precision)
    return total_discount


def _set_discount_observability(so, breakdown):
    if so.meta.has_field("custom_discount_breakdown"):
        so.custom_discount_breakdown = frappe.as_json(breakdown)

    if so.meta.has_field("applied_discount_rules"):
        so.applied_discount_rules = "\n".join(
            f"{row['rule']}: {flt(row['amount'])}" for row in breakdown
        )


def _coupon_validation_response(message, coupon_code=None, http_status_code=422):
    frappe.local.response["http_status_code"] = http_status_code
    response = {
        "success": False,
        "message": message,
    }
    if coupon_code:
        response["coupon_code"] = coupon_code
    return response


@frappe.whitelist()
@rate_limit(limit=ORDER_RATE_LIMIT, seconds=ORDER_RATE_WINDOW)
def place_order(customer=None, items=None, payment_method="Cash on Delivery",
                delivery_lat=None, delivery_lng=None,
                shipping_address_name=None, coupon_code=None,
                shipping_rule=None, guardian=None):
    """
    POST /api/method/pet_app.api.order.place_order
    {
      "customer": "CUST-2026-00001",
      "items": [{"item_code": "RC-PUPPY-001", "qty": 2}],
      "payment_method": "Cash on Delivery",
      "delivery_lat": 33.3152,
      "delivery_lng": 44.3661,
      "coupon_code": "SAVE10",
      "shipping_rule": "Standard Delivery"
    }
    """
    import json
    _require_order_access(guardian=guardian)
    _require_sales_order_create_access()

    if isinstance(items, str):
        items = json.loads(items)
    if not isinstance(items, list):
        frappe.throw(_("Items payload must be a list"))

    if not items:
        frappe.throw(_("No items provided"))

    guardian_row, customer = _resolve_sales_identity(guardian=guardian, customer=customer)

    # ── Validate items + stock + price ──
    for item in items:
        item_code = item.get("item_code")

        if not frappe.db.exists("Item", item_code):
            frappe.throw(_(f"Item '{item_code}' does not exist"))

        qty = flt(item.get("qty", 0))
        if qty <= 0:
            frappe.throw(_(f"Invalid qty for item '{item_code}'"))

        stock_qty = frappe.db.get_value(
            "Bin", {"item_code": item_code, "warehouse": "Stores - H"}, "actual_qty"
        ) or 0
        if stock_qty < qty:
            frappe.throw(_(f"Insufficient stock for '{item_code}'. Available: {stock_qty}"))

        rate = frappe.db.get_value(
            "Item Price",
            {"item_code": item_code, "price_list": "Standard Selling"},
            "price_list_rate"
        )
        if not rate:
            frappe.throw(_(f"Price not set for '{item_code}'"))

        item["rate"] = rate
        item["item_group"] = frappe.db.get_value("Item", item_code, "item_group")
        item["brand"] = frappe.db.get_value("Item", item_code, "brand")

    # ── Validate coupon and decide how to apply it ──
    coupon_doc = None
    normalized_coupon_code = None
    standard_coupon_code = None
    pricing_rule = {}

    if coupon_code:
        from pet_app.api.coupons import CouponValidator

        if isinstance(coupon_code, (list, tuple, set)):
            return _coupon_validation_response(_("Only one coupon can be applied per order"))

        normalized_coupon_code = coupon_code.strip().upper()
        if not normalized_coupon_code:
            return _coupon_validation_response(_("Invalid coupon"))
        if normalized_coupon_code.startswith("[") or "," in normalized_coupon_code:
            return _coupon_validation_response(_("Only one coupon can be applied per order"))

        try:
            coupon_doc = frappe.get_doc("Coupon Code", {"coupon_code": normalized_coupon_code})
        except frappe.DoesNotExistError:
            return _coupon_validation_response(_("Invalid coupon"), coupon_code=normalized_coupon_code)

        if not coupon_doc:
            return _coupon_validation_response(_("Invalid coupon"), coupon_code=normalized_coupon_code)

        standard_coupon_code = coupon_doc.coupon_code.strip().upper()

        cart = {
            "total": sum(flt(i["qty"]) * flt(i["rate"]) for i in items),
            "items": items,
            "shipping": 0,
        }

        res = CouponValidator.validate(normalized_coupon_code, cart, customer=customer)
        if not res.get("valid"):
            return _coupon_validation_response(_(res.get("message")), coupon_code=normalized_coupon_code)
        pricing_rule = res.get("pricing_rule") or {}

    # ── Build Sales Order ──
    so = frappe.new_doc("Sales Order")
    so.customer = customer
    so.transaction_date = nowdate()
    so.custom_order_status = "Draft"
    so.custom_payment_method = payment_method
    so.custom_payment_status = "Pending"

    if delivery_lat:
        so.custom_delivery_lat = flt(delivery_lat)
    if delivery_lng:
        so.custom_delivery_lng = flt(delivery_lng)
    if shipping_address_name:
        so.shipping_address_name = shipping_address_name
    so.ignore_pricing_rule = 1
    if shipping_rule:
        so.shipping_rule = shipping_rule

    for item in items:
        so.append("items", {
            "item_code": item.get("item_code"),
            "qty": flt(item.get("qty", 1)),
        })

    so.selling_price_list = "Standard Selling"
    so.price_list_currency = "IQD"

    total_discount = 0.0
    discount_breakdown = []
    try:
        so.insert()
        so.run_method("set_missing_values")
        so.run_method("calculate_taxes_and_totals")

        if coupon_doc:
            precision = so.precision("grand_total")
            so.apply_discount_on = "Grand Total"
            so.discount_amount = 0
            _set_discount_observability(so, [])

            total_discount, discount_breakdown = _calculate_discount_breakdown(
                so.items,
                pricing_rule,
                precision=precision,
            )
            total_discount = _round_currency(
                min(flt(total_discount), flt(so.grand_total)),
                precision,
            )
            so.apply_discount_on = "Grand Total"
            so.discount_amount = total_discount
            so.coupon_code = coupon_doc.name
            _set_discount_observability(so, discount_breakdown)
            so.run_method("calculate_taxes_and_totals")
            so.save()

        so.submit()

        if coupon_doc:
            from pet_app.api.coupons import CouponUsage
            CouponUsage.apply(standard_coupon_code)
    except Exception:
        frappe.db.rollback()
        raise

    frappe.response["data"] = {
        "order": so.name,
        "guardian": guardian_row.get("name"),
        "customer": so.customer,
        "customer_name": so.customer_name,
        "order_status": so.custom_order_status,
        "payment_method": so.custom_payment_method,
        "payment_status": so.custom_payment_status,
        "grand_total": so.grand_total,
        "discount_amount": so.discount_amount,
        "coupon_code": standard_coupon_code if coupon_doc else None,
        "discount_breakdown": discount_breakdown,
        "discount_total": total_discount,
        "items": [
            {
                "item_code": i.item_code,
                "item_name": i.item_name,
                "qty": i.qty,
                "rate": i.rate,
                "amount": i.amount,
            } for i in so.items
        ],
    }
    so.add_comment("Comment", _("Sales Order created via guarded order API by {0}.").format(frappe.session.user))

# ─────────────────────────────────────────
# Allowed Status Transitions
# ─────────────────────────────────────────
ALLOWED_TRANSITIONS = {
    "Draft":            ["Preparing", "Cancelled"],
    "Preparing":        ["Out for Delivery", "Cancelled"],
    "Out for Delivery": ["Cash Collected", "Returned"],
    "Returned":         ["Out for Delivery", "Cancelled"],
    "Cash Collected":   ["Completed"],
    "Completed":        [],
    "Cancelled":        [],
}

def _validate_status_transition(old_status, new_status):
    allowed = ALLOWED_TRANSITIONS.get(old_status, [])
    if new_status not in allowed:
        frappe.throw(
            _(f"Cannot change status from '{old_status}' to '{new_status}'")
        )


def _create_stock_issue(doc):
    """
    لما يصير Preparing — ينقص المخزن
    """
    existing = frappe.db.exists("Stock Entry", {
        "custom_sales_order": doc.name,
        "stock_entry_type": "Material Issue",
        "docstatus": 1
    })
    if existing:
        return

    se = frappe.new_doc("Stock Entry")
    se.stock_entry_type = "Material Issue"
    se.custom_sales_order = doc.name
    se.company = COMPANY
    se.remarks = f"Order Preparing - {doc.name}"

    for item in doc.items:
        se.append("items", {
            "item_code": item.item_code,
            "qty": item.qty,
            "s_warehouse": "Stores - H",
            "basic_rate": _get_stock_basic_rate(item.item_code, "Stores - H"),
        })

    _require_stock_entry_access()
    se.insert()
    se.submit()
    frappe.logger().info(f"[Stock] Issue created for {doc.name}")


def _reverse_stock_issue(doc):
    """
    لما يصير Cancelled أو Returned — يرجع المخزن
    """
    stock_entry = frappe.db.get_value(
        "Stock Entry",
        {
            "custom_sales_order": doc.name,
            "stock_entry_type": "Material Issue",
            "docstatus": 1
        },
        "name"
    )

    if not stock_entry:
        return

    _require_stock_entry_access()
    se = frappe.get_doc("Stock Entry", stock_entry)
    se.cancel()
    frappe.logger().info(f"[Stock] Issue reversed for {doc.name}")


def _get_status_transition_context(doc, fallback_to_db=False):
    old_doc = doc.get_doc_before_save()
    if not old_doc and fallback_to_db and doc.name and frappe.db.exists(doc.doctype, doc.name):
        old_doc = frappe.get_doc(doc.doctype, doc.name)

    old_status = old_doc.custom_order_status if old_doc else None
    new_status = doc.custom_order_status
    if old_status == new_status:
        return None

    return frappe._dict(
        old_status=old_status,
        new_status=new_status,
        old_driver=old_doc.custom_driver if old_doc else None,
        new_driver=doc.custom_driver,
    )


def _driver_context(doc, driver):
    return frappe._dict(
        name=doc.name,
        custom_driver=driver,
        custom_payment_method=doc.custom_payment_method,
        customer=doc.customer,
        grand_total=doc.grand_total,
    )


def before_sales_order_update(doc, method):
    if not doc.has_value_changed("custom_order_status"):
        doc.flags.order_status_transition = None
        return

    transition = _get_status_transition_context(doc, fallback_to_db=True)
    if not transition:
        doc.flags.order_status_transition = None
        return

    if not transition.old_status:
        frappe.throw(
            _("Unable to determine the previous order status for Sales Order {0}.").format(doc.name)
        )

    _validate_status_transition(transition.old_status, transition.new_status)

    if (
        transition.new_status == "Out for Delivery"
        and doc.custom_payment_method == "Cash on Delivery"
        and not transition.new_driver
    ):
        frappe.throw(_("Driver is required before moving the order to Out for Delivery"))

    doc.flags.order_status_transition = transition
    frappe.logger().info(
        f"[ORDER] validated transition old={transition.old_status} new={transition.new_status} doc={doc.name}"
    )


def on_sales_order_update(doc, method):
    transition = getattr(doc.flags, "order_status_transition", None)
    if not transition:
        transition = _get_status_transition_context(doc)

    if not transition:
        return

    old_status = transition.old_status
    new_status = transition.new_status
    old_driver = transition.old_driver
    current_driver = old_driver or doc.custom_driver

    frappe.logger().info(f"[ORDER] applying transition old={old_status} new={new_status} doc={doc.name}")

    if new_status == "Preparing":
        _create_stock_issue(doc)
        return

    if new_status == "Out for Delivery":
        if doc.custom_payment_method == "Cash on Delivery":
            _create_driver_debit_entry(doc)
        return

    if new_status == "Returned":
        if doc.custom_payment_method == "Cash on Delivery":
            _reverse_driver_entry(_driver_context(doc, current_driver))
        return

    if new_status == "Cash Collected":
        if doc.custom_payment_method == "Cash on Delivery":
            _collect_driver_cash(_driver_context(doc, current_driver))
        return

    if new_status == "Cancelled":
        _reverse_stock_issue(doc)
        return

    if new_status == "Completed" and doc.custom_payment_status != "Paid":
        doc.db_set("custom_payment_status", "Paid", update_modified=False)
