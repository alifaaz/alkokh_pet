import json

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import cint, cstr, flt, nowdate, rounded
from pet_app.api.link_aliases import with_link_aliases
from pet_app.api.driver import (
    _create_driver_debit_entry,
    _collect_driver_cash,
    _reverse_driver_entry,
)
from pet_app.api.permissions import require_doctype_permission, require_restriction_value
from pet_app.utils.guardian_customer import (
    get_guardian_by_user,
    get_guardian_record,
    get_or_create_customer_from_guardian,
)
from pet_app.api.response import standardize_response

# ─────────────────────────────────────────
# Roles
# ─────────────────────────────────────────
ORDER_ROLES = ("System Manager", "Item Manager", "Stock Manager", "Administrator", "Order")
GUARDIAN_ORDER_ROLES = {"Guardian", "Guardians"}
ORDER_RATE_LIMIT = 20
ORDER_RATE_WINDOW = 60
DELIVERY_LATITUDE_FIELDS = ("custom_delivery_latitude", "custom_delivery_lat")
DELIVERY_LONGITUDE_FIELDS = ("custom_delivery_longitude", "custom_delivery_lng")

def _check_permission():
    if frappe.session.user == "Administrator":
        return
    user_roles = frappe.get_roles(frappe.session.user)
    if not any(r in user_roles for r in ORDER_ROLES):
        frappe.throw(_("Not authorized"), frappe.PermissionError)


ACTOR_ADMIN = "admin"
ACTOR_STAFF = "staff"
ACTOR_GUARDIAN = "guardian"


def _require_order_access(guardian=None):
    """Authorise the caller and report WHICH kind of caller they are.

    The return value matters: a guardian buying for themselves and a staff member
    selling on a customer's behalf are different trust questions, and only the staff
    path is gated on Sales Order DocPerms (see place_order).
    """
    if frappe.session.user == "Administrator":
        return ACTOR_ADMIN

    user_roles = set(frappe.get_roles(frappe.session.user) or [])
    if user_roles.intersection(ORDER_ROLES):
        return ACTOR_STAFF

    if user_roles.intersection(GUARDIAN_ORDER_ROLES):
        current_guardian = get_guardian_by_user(frappe.session.user)
        if not current_guardian:
            frappe.throw(_("Guardian context is required for marketplace sales."), frappe.PermissionError)
        if guardian and guardian != current_guardian.get("name"):
            frappe.throw(_("Not authorized to place orders for another guardian."), frappe.PermissionError)
        return ACTOR_GUARDIAN

    frappe.throw(_("Not authorized"), frappe.PermissionError)


def _require_sales_order_create_access():
    if not frappe.has_permission("Sales Order", ptype="create"):
        frappe.throw(_("Not permitted to create Sales Order."), frappe.PermissionError)


def _require_stock_entry_access():
    require_doctype_permission("Stock Entry", "create")
    require_doctype_permission("Stock Entry", "submit")
    if frappe.session.user == "Administrator":
        return
    user_roles = set(frappe.get_roles(frappe.session.user) or [])
    allowed_roles = {"System Manager", "Stock Manager", "Item Manager", "Administrator"}
    if user_roles.isdisjoint(allowed_roles):
        frappe.throw(_("Not permitted to create Stock Entry."), frappe.PermissionError)


def _order_default_company():
    return (
        frappe.defaults.get_user_default("Company")
        or frappe.defaults.get_global_default("company")
    )


def _validate_resolved_warehouse(warehouse, item_code, source):
    """The checks Medication._validate_default_warehouse applies, reused verbatim in shape.

    A warehouse that is missing, disabled, or a group node cannot hold stock, so resolving
    to one is a configuration error worth naming rather than a value worth passing on.
    """
    require_restriction_value("warehouse", warehouse)

    row = frappe.db.get_value(
        "Warehouse",
        warehouse,
        ["name", "disabled", "is_group"],
        as_dict=True,
    )
    if not row:
        frappe.throw(
            _("Warehouse {0} resolved for item {1} from {2} does not exist.").format(
                frappe.bold(warehouse), frappe.bold(item_code), source
            )
        )
    if cint(row.disabled):
        frappe.throw(
            _("Warehouse {0} resolved for item {1} from {2} is disabled.").format(
                frappe.bold(warehouse), frappe.bold(item_code), source
            )
        )
    if cint(row.is_group):
        frappe.throw(
            _("Warehouse {0} resolved for item {1} from {2} must be a leaf warehouse.").format(
                frappe.bold(warehouse), frappe.bold(item_code), source
            )
        )
    return warehouse


def _resolve_item_warehouse(item_code, company=None):
    """The warehouse an Item's stock moves through, resolved from configuration only.

    Item Default for the company, then Stock Settings, then a throw. Deliberately no Bin
    lookup and no literal fallback: picking whichever warehouse happens to hold stock makes
    the answer drift as stock moves, and a hardcoded default is what this replaces.
    """
    company = company or _order_default_company()

    warehouse = None
    source = None

    if company:
        warehouse = frappe.db.get_value(
            "Item Default",
            {"parent": item_code, "parenttype": "Item", "company": company},
            "default_warehouse",
        )
        if warehouse:
            source = _("the Item Default for {0}").format(company)

    if not warehouse:
        warehouse = frappe.db.get_single_value("Stock Settings", "default_warehouse")
        if warehouse:
            source = _("Stock Settings Default Warehouse")

    if not warehouse:
        frappe.throw(
            _("No warehouse is configured for item {0}. Set a Default Warehouse on the Item "
              "for company {1}, or set Stock Settings Default Warehouse.").format(
                frappe.bold(item_code), frappe.bold(company or _("(none)"))
            )
        )

    return _validate_resolved_warehouse(warehouse, item_code, source)


def _get_stock_basic_rate(item_code, warehouse=None):
    warehouse = warehouse or _resolve_item_warehouse(item_code)
    require_restriction_value("warehouse", warehouse)
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


def _set_first_existing_field(doc, fieldnames, value):
    if value in (None, ""):
        return None

    for fieldname in fieldnames:
        if doc.meta.has_field(fieldname):
            doc.set(fieldname, flt(value))
            return fieldname

    frappe.logger().warning(
        f"[ORDER] delivery coordinate skipped for {doc.doctype}: missing fields {', '.join(fieldnames)}"
    )
    return None


def _set_sales_order_delivery_coordinates(doc, delivery_lat=None, delivery_lng=None):
    return {
        "latitude_field": _set_first_existing_field(doc, DELIVERY_LATITUDE_FIELDS, delivery_lat),
        "longitude_field": _set_first_existing_field(doc, DELIVERY_LONGITUDE_FIELDS, delivery_lng),
    }


def _first_existing_value(doc, fieldnames):
    for fieldname in fieldnames:
        if doc.meta.has_field(fieldname):
            return flt(doc.get(fieldname))
    return 0.0


def _copy_address_coordinates_to_order(doc):
    """Fall back to the shipping address's saved pin when the order carries none.

    The two coordinate pairs answer different questions and must not be collapsed into
    one. `Address.custom_latitude` is "where this address is": stable, reused across
    orders, and the guardian can move it. `Sales Order.custom_delivery_latitude` is
    "where this delivery went": a historical fact that must stay true after the guardian
    later drags the pin on their saved address. So this copies forward at creation and
    never afterwards, and never in the other direction.

    Three refusals, in order of how easily each would be got wrong:

    - An order that already has coordinates is left alone. The app sends a per-delivery
      pin when the user adjusts the map at checkout, and that is the more specific
      signal - "deliver to the side gate today" must beat the address's stored default.
    - Nothing is ever written back to the Address. A one-off adjustment is not a
      correction to the saved address, and treating it as one would let a single odd
      delivery quietly relocate every future order to that address.
    - (0, 0) on the Address means unset, not the Gulf of Guinea. Frappe's Float column
      is NOT NULL DEFAULT 0, so this is the only "empty" available; the mobile API makes
      the same read in `_coordinate_value`.

    A no-op until `address_delivery_coordinates` has run, because the guard below finds
    no such field. Safe to deploy ahead of the patch.
    """
    address = doc.get("shipping_address_name")
    if not address:
        return None

    if _first_existing_value(doc, DELIVERY_LATITUDE_FIELDS) or _first_existing_value(
        doc, DELIVERY_LONGITUDE_FIELDS
    ):
        return None

    address_meta = frappe.get_meta("Address")
    if not (address_meta.has_field("custom_latitude") and address_meta.has_field("custom_longitude")):
        return None

    row = frappe.db.get_value(
        "Address", address, ["custom_latitude", "custom_longitude"], as_dict=True
    )
    if not row:
        return None

    latitude = flt(row.get("custom_latitude"))
    longitude = flt(row.get("custom_longitude"))
    if not latitude and not longitude:
        return None

    return _set_sales_order_delivery_coordinates(
        doc, delivery_lat=latitude, delivery_lng=longitude
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


# ─────────────────────────────────────────
# ERPNext-priced preview
#
# A discount only exists once a document exists: ERPNext's engine reads doc.total,
# doc.total_qty, doc.customer, doc.selling_price_list and writes into the document. So
# the cart is previewed by building the Sales Order place_order would build, pricing it,
# and reading the numbers back off it - without ever inserting it.
#
# The whitelisted apply_pricing_rule() helper is deliberately NOT used: it only handles
# item-level rules. Transaction-scoped rules - "5,000 off your order", the common coupon
# shape - go through apply_pricing_rule_on_transaction(), which ERPNext calls only from
# AccountsController.validate(). Running the full validate() on an unsaved doc would drag
# in party, address and stock validation that can throw for reasons unrelated to pricing,
# so the two pricing steps are invoked directly instead.
# ─────────────────────────────────────────
PREVIEW_PRICE_LIST = "Standard Selling"
PREVIEW_CURRENCY = "IQD"


def _coupon_doc_for_code(coupon_code):
    """Resolve a customer-supplied code to its Coupon Code document, or None."""
    normalized = cstr(coupon_code).strip().upper()
    if not normalized:
        return None
    name = frappe.db.get_value("Coupon Code", {"coupon_code": normalized}, "name")
    if not name:
        return None
    return frappe.get_doc("Coupon Code", name)


def _coupon_preview_issue(coupon_code, customer=None):
    """Coupon problems as a cart issue, never as an exception.

    ERPNext's validate_coupon_code carries the specific messages (validity not started /
    expired / no longer valid) and, unlike the retired pet_app validator, it enforces
    valid_from as well as valid_upto.
    """
    normalized = cstr(coupon_code).strip().upper()
    coupon = _coupon_doc_for_code(normalized)
    if not coupon:
        return None, {
            "code": "promo.invalid",
            "message": _("Invalid promo code."),
            "coupon_code": normalized,
        }

    from erpnext.accounts.doctype.pricing_rule.utils import validate_coupon_code

    try:
        validate_coupon_code(coupon.name)
    except Exception as exc:
        return None, {
            "code": "promo.invalid",
            "message": cstr(exc) or _("Invalid promo code."),
            "coupon_code": normalized,
        }

    per_customer_issue = _per_customer_limit_issue(coupon, customer, normalized)
    if per_customer_issue:
        return None, per_customer_issue

    return coupon, None


def _per_customer_limit_issue(coupon, customer, normalized_code):
    """Read-only mirror of the per-customer cap, for the quote and for error shaping.

    Enforcement lives in pet_app.api.coupons.enforce_coupon_limits, bound to Sales Order
    before_validate, which is what actually holds the row lock and covers the desk path.
    This copy answers the same question without a lock so the cart can show the issue
    before an order exists, and so place_order can return the promo.invalid shape the
    mobile client expects instead of a raw ValidationError. It never decides alone -
    the hook refuses regardless of what this returns. 0 means unlimited.
    """
    if not customer:
        return None

    from pet_app.api.coupons import (
        MAX_USES_PER_CUSTOMER_FIELD,
        per_customer_limit_message,
    )

    limit = cint(coupon.get(MAX_USES_PER_CUSTOMER_FIELD))
    if limit <= 0:
        return None

    used_by_customer = frappe.db.count(
        "Sales Order",
        {"customer": customer, "coupon_code": coupon.name, "docstatus": ["!=", 2]},
    )
    if used_by_customer >= limit:
        return {
            "code": "promo.invalid",
            "message": per_customer_limit_message(),
            "coupon_code": normalized_code,
        }
    return None


def _build_preview_sales_order(items, customer, coupon_doc=None, apply_rules=True):
    so = frappe.new_doc("Sales Order")
    # Nothing here is ever persisted - this is arithmetic on a throwaway document, and
    # "may this guardian quote this cart" was already settled upstream by
    # _current_guardian()/_guardian_customer(). Without the flag, set_missing_values()
    # forwards it to _get_party_details(), which calls
    # frappe.has_permission("Customer", "read", throw=True) - and a Guardian holds no
    # read permission on Customer, so the preview would raise for every real customer.
    so.flags.ignore_permissions = True
    so.customer = customer
    so.company = _order_default_company()
    so.transaction_date = nowdate()
    so.delivery_date = nowdate()
    so.selling_price_list = PREVIEW_PRICE_LIST
    so.price_list_currency = PREVIEW_CURRENCY
    so.currency = PREVIEW_CURRENCY
    # 0 is the whole point of the preview: it is what lets ERPNext price the cart.
    so.ignore_pricing_rule = 0 if apply_rules else 1
    if coupon_doc:
        # The docname, not the code string - ERPNext looks the rule up by it.
        so.coupon_code = coupon_doc.name

    for item in items:
        item_code = cstr(item.get("item_code")).strip()
        so.append(
            "items",
            {
                "item_code": item_code,
                "qty": flt(item.get("qty")),
                # Same resolver place_order uses, so preview and order can never price
                # against different warehouses.
                "warehouse": _resolve_item_warehouse(item_code),
                "delivery_date": nowdate(),
            },
        )
    return so


def _price_preview_document(so, coupon_doc=None):
    from erpnext.accounts.doctype.pricing_rule.utils import apply_pricing_rule_on_transaction

    # set_missing_values fetches rates and applies ITEM-level rules (it forwards
    # ignore_pricing_rule into get_item_details).
    so.set_missing_values()
    so.calculate_taxes_and_totals()

    if coupon_doc:
        apply_pricing_rule_on_transaction(so)
        so.calculate_taxes_and_totals()

    return so


def _preview_discount_breakdown(so):
    """Per-rule attribution, which ERPNext itself does not store.

    Sales Order Item.pricing_rules records WHICH rules applied but not how much each
    contributed, and at transaction scope the only artifact is a single scalar. This
    reconstructs the amounts so the cart, and later custom_discount_breakdown, can say
    why a total moved.
    """
    breakdown = []
    item_level_total = 0.0

    for row in so.items:
        line_discount = flt(flt(row.price_list_rate) - flt(row.rate)) * flt(row.qty)
        if line_discount <= 0:
            continue
        item_level_total += line_discount

        rules = []
        try:
            rules = json.loads(row.pricing_rules or "[]")
        except Exception:
            rules = []

        breakdown.append(
            {
                "rule": rules[0] if rules else "item_discount",
                "scope": "Item",
                "rate_or_discount": "Discount Percentage"
                if flt(row.discount_percentage)
                else "Discount Amount",
                "eligible_total": flt(flt(row.price_list_rate) * flt(row.qty)),
                "rate": flt(row.discount_percentage) or flt(row.discount_amount),
                "amount": flt(line_discount),
                "item_code": row.item_code,
            }
        )

    # Transaction-scoped discount. A percentage rule writes
    # additional_discount_percentage, an amount rule writes discount_amount - so both are
    # read, and calculate_taxes_and_totals has already resolved the percentage into
    # discount_amount by this point.
    transaction_discount = flt(so.discount_amount)
    if transaction_discount:
        rule_name = None
        if so.coupon_code:
            rule_name = frappe.db.get_value("Coupon Code", so.coupon_code, "pricing_rule")
        breakdown.append(
            {
                "rule": rule_name or "transaction_discount",
                "scope": "Transaction",
                "rate_or_discount": "Discount Percentage"
                if flt(so.additional_discount_percentage)
                else "Discount Amount",
                "eligible_total": flt(so.total),
                "rate": flt(so.additional_discount_percentage) or transaction_discount,
                "amount": transaction_discount,
            }
        )

    return _round_currency(item_level_total + transaction_discount, 2), breakdown


def preview_order_pricing(items, customer, coupon_code=None):
    """Price a cart the way place_order will, without creating anything.

    Returns (payload, issues). Coupon problems and pricing-rule conflicts are returned as
    issues so the cart still renders; nothing here escapes as an exception.
    """
    issues = []
    coupon_doc = None

    if coupon_code:
        coupon_doc, issue = _coupon_preview_issue(coupon_code, customer=customer)
        if issue:
            issues.append(issue)

    def _price(with_coupon):
        so = _build_preview_sales_order(
            items, customer, coupon_doc=coupon_doc if with_coupon else None
        )
        return _price_preview_document(so, coupon_doc=coupon_doc if with_coupon else None)

    try:
        so = _price(bool(coupon_doc))
    except Exception as exc:
        # Degrade rather than 500. A MultiplePricingRuleConflict here means two rules
        # cover one item; the customer must still see their cart.
        issues.append(
            {
                "code": "promo.conflict"
                if "Multiple Price Rules" in cstr(exc)
                else "promo.invalid",
                "message": cstr(exc) or _("Could not apply the promo code."),
                "coupon_code": cstr(coupon_code or "").strip().upper() or None,
            }
        )
        coupon_doc = None
        try:
            so = _price(False)
        except Exception:
            # Undiscounted pricing failed too - that is a cart problem, not a coupon one.
            return None, issues + [
                {"code": "cart.pricing_failed", "message": _("Could not price this cart.")}
            ]

    discount_total, breakdown = _preview_discount_breakdown(so)

    payload = {
        "subtotal": flt(so.total),
        "net_total": flt(so.net_total),
        "grand_total": flt(so.grand_total),
        "rounded_total": flt(so.rounded_total),
        "discount_amount": discount_total,
        "discount_total": discount_total,
        "discount_breakdown": breakdown,
        "additional_discount_percentage": flt(so.additional_discount_percentage),
        "transaction_discount_amount": flt(so.discount_amount),
        "coupon_code": coupon_doc.coupon_code if coupon_doc else None,
        "coupon_name": coupon_doc.name if coupon_doc else None,
        "items": [
            {
                "item_code": row.item_code,
                "item_name": row.item_name,
                "qty": flt(row.qty),
                "price_list_rate": flt(row.price_list_rate),
                "rate": flt(row.rate),
                "amount": flt(row.amount),
                "net_amount": flt(row.net_amount),
                "warehouse": row.warehouse,
            }
            for row in so.items
        ],
    }
    return payload, issues


@frappe.whitelist()
@standardize_response
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
    actor = _require_order_access(guardian=guardian)

    # Staff selling on a customer's behalf still needs the Sales Order DocPerm - that is a
    # different trust question from a customer buying for themselves. A guardian holds no
    # DocPerm on Sales Order and never will: granting the role one would expose every
    # customer's orders over /api/resource, because nothing scopes Customer today.
    if actor != ACTOR_GUARDIAN:
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

        # Resolved once per item here and stashed on the row below, so the availability
        # check, the Sales Order line, and the eventual Material Issue all name the same
        # warehouse without three separate lookups that could disagree.
        warehouse = _resolve_item_warehouse(item_code)
        require_restriction_value("warehouse", warehouse)
        stock_qty = frappe.db.get_value(
            "Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty"
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
        item["warehouse"] = warehouse
        item["item_group"] = frappe.db.get_value("Item", item_code, "item_group")
        item["brand"] = frappe.db.get_value("Item", item_code, "brand")

    # ── Validate coupon and decide how to apply it ──
    coupon_doc = None
    normalized_coupon_code = None
    standard_coupon_code = None
    pricing_rule = {}

    # Normalize, resolve, and hand the coupon to ERPNext. Validation is no longer done
    # here: setting so.coupon_code before insert makes SalesOrder.validate() call
    # validate_coupon_code() (which checks valid_from as well as valid_upto), pricing
    # apply the rule, and on_submit maintain `used`.
    if coupon_code:
        if isinstance(coupon_code, (list, tuple, set)):
            return _coupon_validation_response(_("Only one coupon can be applied per order"))

        normalized_coupon_code = cstr(coupon_code).strip().upper()
        if not normalized_coupon_code:
            return _coupon_validation_response(_("Invalid coupon"))
        if normalized_coupon_code.startswith("[") or "," in normalized_coupon_code:
            return _coupon_validation_response(_("Only one coupon can be applied per order"))

        coupon_doc = _coupon_doc_for_code(normalized_coupon_code)
        if not coupon_doc:
            return _coupon_validation_response(_("Invalid coupon"), coupon_code=normalized_coupon_code)

        standard_coupon_code = coupon_doc.coupon_code.strip().upper()

        # Validity is checked up front purely to preserve the client contract. ERPNext
        # would catch it again inside insert(), but as a raw ValidationError - and the
        # mobile client branches on the promo.invalid code that _coupon_validation_response
        # produces. Running it here keeps ERPNext's specific message ("validity has not
        # started" / "has expired" / "no longer valid") under the code the client expects.
        from erpnext.accounts.doctype.pricing_rule.utils import validate_coupon_code

        try:
            validate_coupon_code(coupon_doc.name)
        except Exception as exc:
            return _coupon_validation_response(
                cstr(exc) or _("Invalid coupon"), coupon_code=normalized_coupon_code
            )

        # The per-customer cap is NOT checked here. It lives in the Sales Order
        # before_validate hook (pet_app.api.coupons.enforce_coupon_limits) so the desk
        # and every other creation path are covered too, and so the row lock is taken
        # inside the transaction that will do the increment. This pre-check exists only
        # to translate the refusal into the promo.invalid shape the mobile client reads.
        per_customer = _per_customer_limit_issue(coupon_doc, customer, normalized_coupon_code)
        if per_customer:
            return _coupon_validation_response(
                per_customer["message"], coupon_code=normalized_coupon_code
            )

    # ── Build Sales Order ──
    so = frappe.new_doc("Sales Order")

    # Elevation for the customer path, immediately after the ownership proof.
    #
    # What is already established at this line: the session is an authenticated user
    # (not Guest); _require_order_access resolved them to a Guardian and refused any
    # `guardian` argument naming someone else; and _resolve_sales_identity derived
    # `customer` FROM that Guardian, throwing if the caller supplied a different one.
    # The order can therefore only be written against the caller's own Customer.
    #
    # The flag covers insert(), the internal save() and submit(), and the
    # has_permission("Customer", "read") that set_missing_values() performs via
    # _get_party_details - none of which a Guardian can satisfy, and none of which is
    # deciding anything the checks above have not already decided.
    if actor == ACTOR_GUARDIAN:
        so.flags.ignore_permissions = True

    so.customer = customer
    so.transaction_date = nowdate()
    so.custom_order_status = "Draft"
    so.custom_payment_method = payment_method
    so.custom_payment_status = "Pending"

    _set_sales_order_delivery_coordinates(so, delivery_lat=delivery_lat, delivery_lng=delivery_lng)
    if shipping_address_name:
        so.shipping_address_name = shipping_address_name
    # After the address is set and after any per-delivery pin from the caller, so it can
    # see both. Fills the coordinates only when the caller supplied none.
    _copy_address_coordinates_to_order(so)
    # ERPNext's pricing engine now owns discounts on this path. The flag stays at 1 on
    # the clinical invoice paths (boarding.py, vet_visit.py), which compute their own
    # rates and must not have them overwritten.
    so.ignore_pricing_rule = 0
    if shipping_rule:
        so.shipping_rule = shipping_rule

    # Set BEFORE insert so validate() sees it: that is what triggers
    # validate_coupon_code() and apply_pricing_rule_on_transaction().
    if coupon_doc:
        so.coupon_code = coupon_doc.name

    for item in items:
        so.append("items", {
            "item_code": item.get("item_code"),
            "qty": flt(item.get("qty", 1)),
            "warehouse": item.get("warehouse"),
        })

    so.selling_price_list = "Standard Selling"
    so.price_list_currency = "IQD"

    total_discount = 0.0
    discount_breakdown = []
    try:
        so.insert()
        # No manual discount arithmetic and no CouponUsage.apply(): insert() prices the
        # order, and submit() runs ERPNext's update_coupon_code_count(). The old code did
        # both, so every redemption consumed two uses.
        so.submit()

        if coupon_doc:
            # ERPNext records WHICH rules applied but not how much each contributed, so
            # the per-rule amounts are reconstructed and stored for audit. Written with
            # db_set because the document is already submitted.
            total_discount, discount_breakdown = _preview_discount_breakdown(so)
            if so.meta.has_field("custom_discount_breakdown"):
                so.db_set(
                    "custom_discount_breakdown",
                    frappe.as_json(discount_breakdown),
                    update_modified=False,
                )
    except Exception:
        frappe.db.rollback()
        raise

    response = {
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
    frappe.response["data"] = with_link_aliases(response, guardian_field="guardian", include_pet=False, include_doctor=False, include_provider=False)
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


# Targets transition_order will actually perform today. The rest of ALLOWED_TRANSITIONS is
# reachable in principle but every remaining target depends on a driver being assigned, and
# custom_driver is still allow_on_submit = 0 - so they are refused explicitly rather than
# attempted and failed deep inside the hooks.
SUPPORTED_TRANSITION_TARGETS = (
    "Preparing",
    "Cancelled",
    "Out for Delivery",
    "Cash Collected",
    "Returned",
    "Completed",
)

# Targets a driver may set on their OWN order. Drivers self-report progress; they do not
# run the warehouse and they do not close orders, so Preparing, Cancelled, Returned and
# Completed stay with staff.
#
# Cash Collected is deliberately NOT here. The cash entry debits the till of whoever is
# acting, resolved from their cashier profile, and a driver has none - so a driver
# performing it could only ever fail. The cashier records the handover with the driver
# standing there, which is what happens in practice anyway.
DRIVER_SELF_SERVICE_TARGETS = ("Out for Delivery",)

# Refused for drivers with an instruction a driver can actually act on, rather than the
# generic staff-transition message.
DRIVER_REFUSAL_MESSAGES = {
    "Cash Collected": (
        "Cash handover is recorded by the cashier who receives the money. "
        "Hand the cash over and ask the cashier to record it."
    ),
}


def _session_driver():
    """The Driver record behind the current session, or None.

    Driver.user is the link - Driver.custom_user was queried in delivery.py and never
    existed. Drivers hold no Sales Order DocPerm, so this is the only thing that ties a
    driver session to an order.
    """
    if frappe.session.user in ("Guest", "Administrator"):
        return None
    return frappe.db.get_value(
        "Driver", {"user": frappe.session.user}, ["name", "status"], as_dict=True
    )


@frappe.whitelist()
@standardize_response
@rate_limit(limit=ORDER_RATE_LIMIT, seconds=ORDER_RATE_WINDOW)
def assign_driver_to_order(order, driver, note=None):
    """Attach a driver to a submitted Sales Order.

    One driver per order, never reassigned: this is the linkage model, and Delivery
    Assignment is not used. Gated exactly like transition_order - assignment is a
    dispatch decision, not something a driver does for themselves.
    """
    order = cstr(order).strip()
    driver = cstr(driver).strip()

    if not order or not frappe.db.exists("Sales Order", order):
        frappe.throw(_("Sales Order {0} was not found.").format(order or _("(missing)")))
    if not driver or not frappe.db.exists("Driver", driver):
        frappe.throw(_("Driver {0} was not found.").format(driver or _("(missing)")))

    _require_transition_access()

    doc = frappe.get_doc("Sales Order", order)
    if doc.docstatus != 1:
        frappe.throw(
            _("Sales Order {0} is not submitted; only submitted orders can be assigned a driver.").format(
                doc.name
            )
        )

    if doc.custom_driver:
        frappe.throw(
            _(
                "Sales Order {0} is already assigned to driver {1}. "
                "An order is never reassigned."
            ).format(doc.name, frappe.bold(doc.custom_driver))
        )

    driver_row = frappe.db.get_value("Driver", driver, ["name", "status", "user"], as_dict=True)
    if cstr(driver_row.status) != "Active":
        frappe.throw(
            _("Driver {0} is not Active (status: {1}).").format(
                frappe.bold(driver), driver_row.status
            )
        )
    if driver_row.user and not cint(frappe.db.get_value("User", driver_row.user, "enabled")):
        frappe.throw(_("Driver {0} has a disabled login.").format(frappe.bold(driver)))

    # allow_on_submit was opened by pet_app.patches.driver_assignment_allow_on_submit.
    # Rolled back as a unit, the same shape transition_order uses.
    doc.custom_driver = driver
    doc.flags.ignore_permissions = True
    try:
        doc.save(ignore_permissions=True)
    except Exception:
        frappe.db.rollback()
        raise

    doc.add_comment(
        "Comment",
        _("Driver {0} assigned to order by {1}.{2}").format(
            driver, frappe.session.user,
            _(" Note: {0}").format(cstr(note).strip()) if cstr(note).strip() else "",
        ),
    )

    return {"order": doc.name, "driver": driver, "driver_status": driver_row.status}


@frappe.whitelist()
@standardize_response
@rate_limit(limit=ORDER_RATE_LIMIT, seconds=ORDER_RATE_WINDOW)
def transition_order(order, target_status, note=None):
    """
    POST /api/method/pet_app.api.order.transition_order
    {"order": "SAL-ORD-2026-00003", "target_status": "Preparing", "note": "picked"}

    Moves a submitted Sales Order along ALLOWED_TRANSITIONS by writing custom_order_status,
    which is what fires before_update_after_submit / on_update_after_submit and therefore
    the stock and payment side effects in this module.
    """
    order = cstr(order).strip()
    target_status = cstr(target_status).strip()

    if not order or not frappe.db.exists("Sales Order", order):
        frappe.throw(_("Sales Order {0} was not found.").format(order or _("(missing)")))

    doc = frappe.get_doc("Sales Order", order)
    if doc.docstatus != 1:
        frappe.throw(
            _("Sales Order {0} is not submitted (docstatus {1}); only submitted orders can be transitioned.").format(
                doc.name, doc.docstatus
            )
        )

    current_status = doc.custom_order_status
    if not target_status:
        frappe.throw(_("target_status is required."))

    # Validated before any write, and before the permission check, so a caller always learns
    # that a transition is impossible rather than being told they are merely unauthorised.
    allowed = ALLOWED_TRANSITIONS.get(current_status, [])
    if target_status not in allowed:
        frappe.throw(
            _("Cannot move Sales Order {0} from '{1}' to '{2}'. Legal targets from '{1}': {3}.").format(
                doc.name,
                current_status,
                target_status,
                ", ".join(allowed) if allowed else _("none - '{0}' is a terminal status").format(current_status),
            )
        )

    # Ownership-then-elevate, the pattern the mobile modules use: a driver holds no Sales
    # Order DocPerm and must not gain one, so authorisation is settled here by matching
    # the session's Driver record against this order's custom_driver, and the save below
    # runs with ignore_permissions.
    driver_row = _session_driver()
    acting_as_driver = False
    if driver_row and not set(frappe.get_roles(frappe.session.user) or []).intersection(ORDER_ROLES):
        if cstr(driver_row.status) != "Active":
            frappe.throw(
                _("Driver {0} is not Active.").format(frappe.bold(driver_row.name)),
                frappe.PermissionError,
            )
        if cstr(doc.custom_driver) != cstr(driver_row.name):
            # Not "you are not allowed on someone else's order" - that would confirm the
            # order exists and who it belongs to.
            frappe.throw(
                _("Sales Order {0} was not found.").format(doc.name), frappe.PermissionError
            )
        if target_status not in DRIVER_SELF_SERVICE_TARGETS:
            frappe.throw(
                _(DRIVER_REFUSAL_MESSAGES.get(target_status))
                if target_status in DRIVER_REFUSAL_MESSAGES
                else _("A driver may only set {0}. {1} is a staff transition.").format(
                    ", ".join(DRIVER_SELF_SERVICE_TARGETS), frappe.bold(target_status)
                ),
                frappe.PermissionError,
            )
        acting_as_driver = True
    else:
        _require_transition_access()

    if target_status not in SUPPORTED_TRANSITION_TARGETS:
        frappe.throw(
            _("Transitioning to '{0}' is not supported; available targets are {1}.").format(
                target_status, ", ".join(SUPPORTED_TRANSITION_TARGETS)
            )
        )

    # Plain save on the submitted doc: update_after_submit is what the doc_events in hooks.py
    # are bound to. Permission was settled by _require_transition_access above - deferring to
    # the Sales Order DocPerms here would lock out Order/Item Manager/Stock Manager, which
    # ORDER_ROLES grants but the doctype does not.
    # Rolled back as a unit, the same way place_order guards its insert/submit. save() writes
    # custom_order_status and only then runs on_update_after_submit, so a side effect that
    # throws would otherwise leave the status committed with its stock movement missing -
    # @standardize_response swallows the exception, and the request commits regardless.
    doc.custom_order_status = target_status
    doc.flags.ignore_permissions = True
    try:
        doc.save(ignore_permissions=True)
    except Exception:
        frappe.db.rollback()
        raise

    doc.add_comment(
        "Comment",
        _("Order status moved {0} → {1} via transition_order by {2}{3}.{4}").format(
            current_status, target_status, frappe.session.user,
            _(" (driver self-report)") if acting_as_driver else "",
            _(" Note: {0}").format(cstr(note).strip()) if cstr(note).strip() else "",
        ),
    )

    return {
        "order": doc.name,
        "previous_status": current_status,
        "new_status": target_status,
    }


def _require_transition_access():
    if frappe.session.user == "Administrator":
        return
    user_roles = set(frappe.get_roles(frappe.session.user) or [])
    if user_roles.intersection(ORDER_ROLES):
        return
    frappe.throw(
        _("Not permitted to change order status. One of these roles is required: {0}.").format(
            ", ".join(ORDER_ROLES)
        ),
        frappe.PermissionError,
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

    if not doc.company:
        frappe.throw(
            _("Sales Order {0} has no company; cannot issue stock against it.").format(doc.name)
        )

    se = frappe.new_doc("Stock Entry")
    se.stock_entry_type = "Material Issue"
    se.custom_sales_order = doc.name
    se.company = doc.company
    se.remarks = f"Order Preparing - {doc.name}"

    # The warehouse is read off the Sales Order Item row rather than resolved again here:
    # stock has to leave the warehouse the order reserved against, and re-deriving it would
    # let the issue and the reservation drift apart. The restriction check stays inside the
    # loop because the value is now per-row - hoisting it would mean asserting that every
    # row shares one warehouse, which this function has no business assuming.
    for item in doc.items:
        if not item.warehouse:
            frappe.throw(
                _("Sales Order {0} item {1} has no warehouse; cannot issue stock for it.").format(
                    doc.name, item.item_code
                )
            )
        require_restriction_value("warehouse", item.warehouse)
        se.append("items", {
            "item_code": item.item_code,
            "qty": item.qty,
            "s_warehouse": item.warehouse,
            "basic_rate": _get_stock_basic_rate(item.item_code, item.warehouse),
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

    # Elevated, and only on the reversal path.
    #
    # This runs underneath a cancellation the caller has already been authorised for -
    # by ownership for a guardian (_assert_order_access matched the order's customer to
    # the session guardian's customer) or by ORDER_ROLES for staff. Re-checking
    # _require_stock_entry_access here would ask the customer for Stock Manager, which no
    # customer will ever hold, and the effect was worse than a refusal: the status write
    # had already been committed, so the order went Cancelled with its stock never
    # returned.
    #
    # Safe because the elevation cannot widen what is touched. The Stock Entry is located
    # by custom_sales_order = doc.name, so this function is structurally incapable of
    # reaching stock belonging to any other order - which is exactly the containment a
    # Stock Entry DocPerm would NOT have provided.
    #
    # _create_stock_issue keeps _require_stock_entry_access: issuing stock is
    # staff-triggered on the Preparing transition and is not a customer action.
    se = frappe.get_doc("Stock Entry", stock_entry)
    se.flags.ignore_permissions = True
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
    # company is carried through because the driver Journal Entries derive it from the
    # document rather than from a module constant, and these callers hand the driver
    # helpers this dict instead of the Sales Order itself.
    return frappe._dict(
        name=doc.name,
        custom_driver=driver,
        custom_payment_method=doc.custom_payment_method,
        customer=doc.customer,
        grand_total=doc.grand_total,
        company=doc.company,
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
