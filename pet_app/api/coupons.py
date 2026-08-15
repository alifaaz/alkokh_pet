# =========================================
# IMPORTS
# =========================================
import json

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, getdate, nowdate

from pet_app.api.response import standardize_response


# =========================================
# CONSTANTS
#
# Pricing Rule is the single source of truth for discount configuration. Coupon Code
# carries identity (the code), validity, and the usage counters ERPNext maintains; it
# holds no mirror of the discount. Everything below that is not admin-facing is a fixed
# default, written explicitly on every rule so a simplified form cannot emit a
# half-configured rule that silently discounts nothing.
# =========================================
COUPON_PRICE_LIST = "Standard Selling"
COUPON_CURRENCY = "IQD"
COUPON_NAMING_SERIES = "COUPON-.####"
COUPON_DEFAULT_PRIORITY = 1

MAX_USES_PER_CUSTOMER_FIELD = "custom_max_uses_per_customer"

COUPON_ADMIN_ROLES = (
    "System Manager",
    "Administrator",
    "E-commerce",
    "Item Manager",
)

# Admin-facing discount vocabulary -> ERPNext's rate_or_discount. Free Item and Free
# Shipping are deliberately absent: product discounts are out of scope, and "free
# shipping" has no meaning while delivery pricing is zero everywhere.
DISCOUNT_TYPES = {
    "Percentage": "Discount Percentage",
    "Fixed": "Discount Amount",
}
DISCOUNT_TYPE_BY_RATE_OR_DISCOUNT = {v: k for k, v in DISCOUNT_TYPES.items()}

APPLY_ON_CHOICES = ("Transaction", "Item Group", "Item Code")

# Scope child table per apply_on, and the column inside it.
SCOPE_TABLE = {
    "Item Code": ("items", "item_code"),
    "Item Group": ("item_groups", "item_group"),
}

APPLICABLE_FOR_CUSTOMER = "Customer"


class CouponConfigError(frappe.ValidationError):
    pass


# =========================================
# PERMISSIONS
#
# The gate is the endpoint's, not the doctype's. Coupon Code has provisioned DocPerms
# (E-commerce *, Sales Manager, Accounts User, ...) but Pricing Rule has none in
# pet_app, so the rule is written with ignore_permissions after this check passes.
# Authorisation is settled here, once, for both documents.
# =========================================
def _has_coupon_role(user=None) -> bool:
    user = user or frappe.session.user
    if user == "Administrator":
        return True
    return bool(set(frappe.get_roles(user) or []) & set(COUPON_ADMIN_ROLES))


def _has_coupon_permission(ptype, user=None) -> bool:
    try:
        return bool(frappe.has_permission("Coupon Code", ptype=ptype, user=user or frappe.session.user))
    except Exception:
        return False


def _check_permission(ptype="read"):
    if _has_coupon_role() or _has_coupon_permission(ptype):
        return
    frappe.throw(_("Not permitted to manage coupons."), frappe.PermissionError)


# =========================================
# PAYLOAD COERCION
# =========================================
def _coerce_list(value) -> list:
    """Accept a JSON string, a comma-joined string, a list, or None."""
    if value in (None, ""):
        return []

    if isinstance(value, str):
        raw = value.strip()
        if raw.startswith("["):
            try:
                value = json.loads(raw)
            except Exception:
                raise frappe.ValidationError(_("Could not read the scope list."))
        else:
            value = raw.split(",")

    if not isinstance(value, (list, tuple, set)):
        value = [value]

    cleaned = []
    seen = set()
    for entry in value:
        entry = cstr(entry).strip()
        if not entry or entry in seen:
            continue
        seen.add(entry)
        cleaned.append(entry)
    return cleaned


def _optional_date(value, label):
    value = cstr(value).strip()
    if not value:
        return None
    try:
        return getdate(value)
    except Exception:
        raise frappe.ValidationError(_("{0} is not a valid date.").format(label))


def _normalize_code(code) -> str:
    return cstr(code).strip().upper()


# =========================================
# SCOPE OVERLAP
#
# ERPNext throws MultiplePricingRuleConflict when more than one ITEM-LEVEL rule survives
# filtering for the same item, and that throw happens in get_pricing_rules() BEFORE
# get_pricing_rule_for_item() applies the coupon gate - so two coupon rules covering one
# item collide at checkout even though only one coupon is ever presented. Overlapping
# item-level rules are therefore refused at creation.
#
# Transaction-scoped rules are exempt: apply_pricing_rule_on_transaction() loops over
# every match and gates each on the coupon inline, with no conflict throw. Several
# "amount off order" coupons can coexist safely, so refusing them would be wrong.
# =========================================
def _pricing_rule_scope(pr) -> tuple[str, list[str]]:
    apply_on = pr.get("apply_on") if isinstance(pr, dict) else pr.apply_on

    if apply_on == "Item Code":
        rows = pr.get("items") or []
        return apply_on, [r.get("item_code") if isinstance(r, dict) else r.item_code for r in rows]
    if apply_on == "Item Group":
        rows = pr.get("item_groups") or []
        return apply_on, [r.get("item_group") if isinstance(r, dict) else r.item_group for r in rows]
    if apply_on == "Brand":
        rows = pr.get("brands") or []
        return apply_on, [r.get("brand") if isinstance(r, dict) else r.brand for r in rows]

    return "Transaction", []


def _pricing_rule_scope_signature(pr):
    """Retained for the conflict scanner's log payload."""
    apply_on, targets = _pricing_rule_scope(pr)
    if apply_on == "Transaction":
        return ("Transaction",)
    return tuple(sorted(t for t in targets if t))


def _item_group_bounds(item_group, cache):
    if item_group not in cache:
        cache[item_group] = frappe.get_cached_value("Item Group", item_group, ["lft", "rgt"])
    return cache[item_group]


def _item_groups_overlap(left, right) -> list[str]:
    """Nested-set overlap: a parent group and its descendant DO overlap.

    Plain set intersection would miss "All Item Groups" against "Dog Food", which is the
    overlap most likely to be created by accident.
    """
    cache = {}
    hits = []
    for a in left:
        a_bounds = _item_group_bounds(a, cache)
        if not a_bounds:
            continue
        a_lft, a_rgt = a_bounds
        for b in right:
            b_bounds = _item_group_bounds(b, cache)
            if not b_bounds:
                continue
            b_lft, b_rgt = b_bounds
            if (a_lft <= b_lft and b_rgt <= a_rgt) or (b_lft <= a_lft and a_rgt <= b_rgt):
                hits.append(b)
    return hits


def _scopes_overlap(apply_on, targets, other_apply_on, other_targets) -> list[str]:
    if apply_on == "Transaction" or other_apply_on == "Transaction":
        return []
    if apply_on != other_apply_on:
        # Item Code vs Item Group can overlap in principle, but ERPNext queries the two
        # scopes independently and only collides within a scope, so cross-scope pairs
        # cannot produce MultiplePricingRuleConflict.
        return []
    if apply_on == "Item Group":
        return _item_groups_overlap(targets, other_targets)
    return sorted(set(targets) & set(other_targets))


def _find_conflicting_rules(apply_on, targets, exclude_rule=None) -> list[dict]:
    """Enabled selling coupon rules on the same price list whose scope overlaps."""
    if apply_on == "Transaction":
        return []

    filters = {
        "selling": 1,
        "disable": 0,
        "coupon_code_based": 1,
        "apply_on": apply_on,
        "for_price_list": COUPON_PRICE_LIST,
    }
    if exclude_rule:
        filters["name"] = ["!=", exclude_rule]

    conflicts = []
    for row in frappe.get_all("Pricing Rule", filters=filters, fields=["name"], limit_page_length=200):
        try:
            candidate = frappe.get_doc("Pricing Rule", row.name)
        except Exception:
            continue
        _, candidate_targets = _pricing_rule_scope(candidate)
        overlap = _scopes_overlap(apply_on, targets, candidate.apply_on, candidate_targets)
        if overlap:
            conflicts.append({"pricing_rule": candidate.name, "overlapping_on": overlap})
    return conflicts


def _assert_no_conflict(apply_on, targets, exclude_rule=None):
    conflicts = _find_conflicting_rules(apply_on, targets, exclude_rule=exclude_rule)
    if not conflicts:
        return

    detail = "; ".join(
        "{0} ({1})".format(c["pricing_rule"], ", ".join(c["overlapping_on"])) for c in conflicts
    )
    frappe.throw(
        _(
            "This scope overlaps an active coupon rule, which would make checkout fail "
            "for every customer once both are live. Conflicting rules: {0}."
        ).format(detail),
        CouponConfigError,
    )


def _log_pricing_rule_conflict_review(pr):
    """Observability for rules that already exist. Creation is refused up front by
    _assert_no_conflict; this reports what is already in the database."""
    if not pr or pr.disable or not pr.selling:
        return

    apply_on, targets = _pricing_rule_scope(pr)
    defects = []

    # Risk A: a blank for_price_list matches EVERY price list, because ERPNext filters
    # with `ifnull(for_price_list,'') in (%(price_list)s, '')`. Clinical and retail share
    # Standard Selling today, so a blank rule reaches both.
    if not cstr(pr.for_price_list).strip():
        defects.append("blank_for_price_list")

    overlaps = _find_conflicting_rules(apply_on, targets, exclude_rule=pr.name)
    if overlaps:
        defects.append("scope_overlap")

    if not defects:
        return

    frappe.log_error(
        title="PRICING_RULE_CONFLICT_REVIEW",
        message=frappe.as_json(
            {
                "event": "PRICING_RULE_CONFLICT_REVIEW",
                "pricing_rule": pr.name,
                "apply_on": pr.apply_on,
                "for_price_list": pr.for_price_list,
                "scope": _pricing_rule_scope_signature(pr),
                "defects": defects,
                "overlapping_rules": [o["pricing_rule"] for o in overlaps],
                "action_taken": "logged_only",
            },
            indent=2,
        ),
    )


def review_active_pricing_rule_conflicts(limit=None):
    query = {
        "filters": {"selling": 1, "disable": 0, "coupon_code_based": 1},
        "fields": ["name"],
        "order_by": "modified desc",
    }
    if limit:
        query["limit_page_length"] = int(limit)

    rules = frappe.get_all("Pricing Rule", **query)

    checked = 0
    for row in rules:
        try:
            _log_pricing_rule_conflict_review(frappe.get_doc("Pricing Rule", row.name))
            checked += 1
        except Exception:
            frappe.log_error(
                title="PRICING_RULE_CONFLICT_SCAN_FAILED",
                message=frappe.get_traceback(),
            )

    return {"checked": checked}


# =========================================
# REPOSITORY
# =========================================
class CouponRepository:

    @staticmethod
    def get_coupon(code):
        """Lookup by coupon_code field, not name."""
        name = frappe.db.get_value("Coupon Code", {"coupon_code": code}, "name")
        if not name:
            frappe.throw(f"Coupon '{code}' not found")
        return frappe.get_doc("Coupon Code", name)

    @staticmethod
    def exists(code):
        return frappe.db.exists("Coupon Code", {"coupon_code": code})

    @staticmethod
    def get_pricing_rule(name):
        return frappe.get_doc("Pricing Rule", name)

    @staticmethod
    def pricing_rule_exists(name):
        return frappe.db.exists("Pricing Rule", name)

    @staticmethod
    def disable_pricing_rule(name):
        frappe.db.set_value("Pricing Rule", name, "disable", 1)

    @staticmethod
    def lock_for_redemption(coupon_name):
        """Serialise concurrent redemptions of one coupon.

        ERPNext owns the counter, but update_coupon_code_count() is a plain
        read-modify-write (`coupon.used = coupon.used + 1; coupon.save()`), and
        validate_coupon_code() reads `used` in a separate earlier statement. Two
        simultaneous submits of the same coupon can therefore both pass the
        maximum_use check and both increment - overselling a limited coupon.

        Taking the row lock in before_validate holds it for the rest of the
        transaction, so the read, the per-customer count below, and ERPNext's
        increment on submit all happen under one writer at a time. This replaces the
        guarded atomic UPDATE that pet_app used before ERPNext owned the counter.
        """
        frappe.db.sql(
            "SELECT name FROM `tabCoupon Code` WHERE name = %s FOR UPDATE",
            (coupon_name,),
        )


# =========================================
# PRICING RULE WRITER
#
# Every field in Pricing Rule's 88 is assigned here - the seven an admin chooses, the
# eleven the system fixes, and explicit safe defaults for the rest. Nothing is left to
# doctype defaults, because a field left unset is exactly how the previous projection
# layer produced rules with no discount at all.
# =========================================
def _coupon_default_company():
    return (
        frappe.defaults.get_user_default("Company")
        or frappe.defaults.get_global_default("company")
    )


def _apply_rule_configuration(pr, config):
    # ---- Admin-chosen ----
    pr.title = config["title"]
    pr.apply_on = config["apply_on"]
    pr.rate_or_discount = DISCOUNT_TYPES[config["discount_type"]]

    if config["discount_type"] == "Percentage":
        pr.discount_percentage = flt(config["discount_value"])
        pr.discount_amount = 0
    else:
        pr.discount_amount = flt(config["discount_value"])
        pr.discount_percentage = 0

    pr.min_amt = flt(config.get("min_amt") or 0)
    pr.valid_from = config.get("valid_from")
    pr.valid_upto = config.get("valid_upto")

    pr.set("items", [])
    pr.set("item_groups", [])
    pr.set("brands", [])
    table = SCOPE_TABLE.get(config["apply_on"])
    if table:
        fieldname, column = table
        for target in config["targets"]:
            pr.append(fieldname, {column: target})

    # Customer targeting. A blank applicable_for means the rule matches any customer;
    # ERPNext filters with `ifnull(customer,'') in (%(customer)s, '')`.
    if config.get("customer"):
        pr.applicable_for = APPLICABLE_FOR_CUSTOMER
        pr.customer = config["customer"]
    else:
        # "" rather than None - see the fieldtype note below.
        pr.applicable_for = ""
        pr.customer = ""

    # ---- System-fixed ----
    pr.disable = 0
    pr.coupon_code_based = 1
    pr.selling = 1
    pr.buying = 0
    pr.price_or_product_discount = "Price"
    pr.for_price_list = COUPON_PRICE_LIST
    pr.priority = config.get("priority") or COUPON_DEFAULT_PRIORITY
    pr.apply_discount_on = "Grand Total"
    pr.currency = COUPON_CURRENCY
    pr.company = config.get("company") or _coupon_default_company()

    # ---- Explicit safe defaults for everything else ----
    #
    # Split by fieldtype, and it matters in both directions.
    #
    # Numeric/Check fields take 0. Select, Link and text fields take "" and NOT None:
    # Document.insert() runs _set_defaults() -> update_if_missing(), which refills any
    # field still holding None from the doctype default. margin_type defaults to
    # "Percentage", so clearing it with None silently put it back.
    #
    # apply_rule_on_other in particular is a Select, not a Check. Writing 0 stores the
    # string "0", which is truthy in Python, and ERPNext reads it as a flag
    # (`fetch_other_item = True if pricing_rule.apply_rule_on_other else False`) - that
    # would send an item-scoped coupon down the apply-rule-on-other-item branch with no
    # other_item_* configured.
    #
    # has_priority is deliberately absent: ERPNext derives it from priority in
    # validate_mandatory(), and priority is always set above.
    for fieldname in (
        "mixed_conditions",
        "is_cumulative",
        "same_item",
        "is_recursive",
        "round_free_qty",
        "dont_enforce_free_item_qty",
        "apply_multiple_pricing_rules",
        "apply_discount_on_rate",
        "validate_applied_rule",
        "min_qty",
        "max_qty",
        "max_amt",
        "rate",
        "margin_rate_or_amount",
        "free_qty",
        "free_item_rate",
        "threshold_percentage",
        "recurse_for",
        "apply_recursion_over",
    ):
        pr.set(fieldname, 0)

    for fieldname in (
        "apply_rule_on_other",
        "margin_type",
        "warehouse",
        "other_item_code",
        "other_item_group",
        "other_brand",
        "free_item",
        "free_item_uom",
        "condition",
        "rule_description",
        "promotional_scheme_id",
        "promotional_scheme",
        "customer_group",
        "territory",
        "sales_partner",
        "campaign",
        "supplier",
        "supplier_group",
    ):
        pr.set(fieldname, "")

    return pr


def _create_pricing_rule(config):
    pr = frappe.new_doc("Pricing Rule")
    pr.naming_series = COUPON_NAMING_SERIES
    _apply_rule_configuration(pr, config)
    pr.insert(ignore_permissions=True)
    return pr


# =========================================
# VALIDATION OF ADMIN INPUT
# =========================================
def _validate_discount(discount_type, discount_value):
    discount_type = cstr(discount_type).strip().title()
    if discount_type not in DISCOUNT_TYPES:
        frappe.throw(
            _("Discount type must be one of: {0}.").format(", ".join(sorted(DISCOUNT_TYPES))),
            CouponConfigError,
        )

    discount_value = flt(discount_value)
    if discount_value <= 0:
        frappe.throw(_("Discount value must be greater than zero."), CouponConfigError)
    if discount_type == "Percentage" and discount_value > 100:
        frappe.throw(_("A percentage discount cannot exceed 100."), CouponConfigError)

    return discount_type, discount_value


def _validate_scope(apply_on, targets):
    apply_on = cstr(apply_on or "Transaction").strip()
    if apply_on not in APPLY_ON_CHOICES:
        frappe.throw(
            _("Scope must be one of: {0}.").format(", ".join(APPLY_ON_CHOICES)),
            CouponConfigError,
        )

    targets = _coerce_list(targets)
    if apply_on == "Transaction":
        return apply_on, []

    if not targets:
        frappe.throw(_("Scope {0} requires at least one target.").format(apply_on), CouponConfigError)

    doctype = "Item" if apply_on == "Item Code" else "Item Group"
    for target in targets:
        if not frappe.db.exists(doctype, target):
            frappe.throw(_("{0} {1} does not exist.").format(doctype, target), CouponConfigError)

    return apply_on, targets


def _validate_dates(valid_from, valid_upto):
    valid_from = _optional_date(valid_from, _("Valid From"))
    valid_upto = _optional_date(valid_upto, _("Valid Up To"))
    if valid_from and valid_upto and valid_upto < valid_from:
        frappe.throw(_("Valid Up To cannot be earlier than Valid From."), CouponConfigError)
    return valid_from, valid_upto


def _validate_customer(customer):
    customer = cstr(customer).strip()
    if not customer:
        return None
    if not frappe.db.exists("Customer", customer):
        frappe.throw(_("Customer {0} does not exist.").format(customer), CouponConfigError)
    return customer


def _has_per_customer_field() -> bool:
    return bool(frappe.get_meta("Coupon Code").get_field(MAX_USES_PER_CUSTOMER_FIELD))


# =========================================
# PROJECTION
# =========================================
def _coupon_payload(coupon, pr=None) -> dict:
    if pr is None and coupon.pricing_rule and CouponRepository.pricing_rule_exists(coupon.pricing_rule):
        pr = CouponRepository.get_pricing_rule(coupon.pricing_rule)

    apply_on, targets = _pricing_rule_scope(pr) if pr else ("Transaction", [])
    discount_type = DISCOUNT_TYPE_BY_RATE_OR_DISCOUNT.get(pr.rate_or_discount) if pr else None
    discount_value = 0.0
    if pr:
        discount_value = flt(
            pr.discount_percentage if discount_type == "Percentage" else pr.discount_amount
        )

    used = cint(coupon.used)
    maximum_use = cint(coupon.maximum_use)

    return {
        "code": coupon.coupon_code,
        "name": coupon.name,
        "coupon_name": coupon.coupon_name,
        "description": coupon.description,
        "pricing_rule": coupon.pricing_rule,
        "discount_type": discount_type,
        "discount_value": discount_value,
        "scope": apply_on,
        "targets": [t for t in targets if t],
        "min_amt": flt(pr.min_amt) if pr else 0.0,
        "valid_from": cstr(coupon.valid_from or ""),
        "valid_upto": cstr(coupon.valid_upto or ""),
        "customer": (pr.customer if pr else None) or coupon.customer,
        "maximum_use": maximum_use,
        "used": used,
        "remaining": (maximum_use - used) if maximum_use else None,
        "max_uses_per_customer": cint(coupon.get(MAX_USES_PER_CUSTOMER_FIELD))
        if _has_per_customer_field()
        else None,
        "active": bool(pr and not cint(pr.disable)),
        "expired": bool(coupon.valid_upto and cstr(coupon.valid_upto) < nowdate()),
        "price_list": pr.for_price_list if pr else None,
        "priority": cint(pr.priority) if pr else None,
    }


# =========================================
# ADMIN ENDPOINTS
# =========================================
LIST_COUPONS_SCAN_CEILING = 2000


@frappe.whitelist()
@standardize_response
def list_coupons(status=None, limit=20, cursor=0, search=None, customer=None):
    """List coupons, newest first.

    `customer` selects coupons TARGETED at one customer (applicable_for = Customer).
    General coupons, which any customer may use, are deliberately not returned by it -
    "belonging to" means assigned to, and a customer-specific list mixed with every
    open promotion would not be answering the question asked.

    `status` and `customer` are evaluated against the projected payload rather than
    pushed into the SQL, because both depend on the linked Pricing Rule (disable, and
    applicable_for/customer). Filtering therefore happens BEFORE pagination - doing it
    after the slice, as this first did, let hasMore and nextCursor describe the
    unfiltered set and hand back short pages.
    """
    _check_permission("read")

    limit = max(1, min(cint(limit or 20), 100))
    offset = max(0, cint(cursor or 0))

    filters = {}
    if search:
        filters["coupon_code"] = ["like", f"%{cstr(search).strip()}%"]

    rows = frappe.get_all(
        "Coupon Code",
        filters=filters,
        fields=["name"],
        order_by="creation desc",
        limit_page_length=LIST_COUPONS_SCAN_CEILING,
        ignore_permissions=True,
    )

    items = [_coupon_payload(frappe.get_doc("Coupon Code", row.name)) for row in rows]

    status = cstr(status or "").strip().lower()
    if status == "active":
        items = [i for i in items if i["active"] and not i["expired"]]
    elif status == "inactive":
        items = [i for i in items if not i["active"]]
    elif status == "expired":
        items = [i for i in items if i["expired"]]

    customer = cstr(customer or "").strip()
    if customer:
        items = [i for i in items if i["customer"] == customer]

    total = len(items)
    page = items[offset : offset + limit]
    has_more = offset + limit < total

    return {
        "items": page,
        "total": total,
        "nextCursor": str(offset + limit) if has_more else None,
        "hasMore": has_more,
    }


@frappe.whitelist()
@standardize_response
def get_coupon(code):
    _check_permission("read")
    coupon = CouponRepository.get_coupon(_normalize_code(code))
    return _coupon_payload(coupon)


@frappe.whitelist()
@standardize_response
def create_coupon(
    coupon_code,
    coupon_name,
    discount_type,
    discount_value,
    apply_on="Transaction",
    targets=None,
    description=None,
    valid_from=None,
    valid_upto=None,
    min_amt=0,
    maximum_use=0,
    max_uses_per_customer=1,
    customer=None,
    priority=None,
    **kwargs,
):
    """Create a Coupon Code and the Pricing Rule that carries its discount.

    The rule is created first so the coupon is never left pointing at nothing; both are
    written inside one transaction and rolled back together on any failure.
    """
    _check_permission("create")

    coupon_code = _normalize_code(coupon_code)
    if not coupon_code:
        frappe.throw(_("Coupon code is required."), CouponConfigError)
    if CouponRepository.exists(coupon_code):
        frappe.throw(_("Coupon code {0} already exists.").format(coupon_code), CouponConfigError)

    coupon_name = cstr(coupon_name).strip()
    if not coupon_name:
        frappe.throw(_("Coupon name is required."), CouponConfigError)
    # Coupon Code autonames from coupon_name, so a duplicate name is a duplicate document.
    if frappe.db.exists("Coupon Code", coupon_name):
        frappe.throw(_("A coupon named {0} already exists.").format(coupon_name), CouponConfigError)

    discount_type, discount_value = _validate_discount(discount_type, discount_value)
    apply_on, targets = _validate_scope(apply_on, targets)
    valid_from, valid_upto = _validate_dates(valid_from, valid_upto)
    customer = _validate_customer(customer)

    _assert_no_conflict(apply_on, targets)

    config = {
        "title": coupon_name,
        "apply_on": apply_on,
        "targets": targets,
        "discount_type": discount_type,
        "discount_value": discount_value,
        "min_amt": min_amt,
        "valid_from": valid_from,
        "valid_upto": valid_upto,
        "customer": customer,
        "priority": cint(priority) or COUPON_DEFAULT_PRIORITY,
    }

    try:
        pr = _create_pricing_rule(config)

        coupon = frappe.new_doc("Coupon Code")
        coupon.coupon_name = coupon_name
        coupon.coupon_code = coupon_code
        coupon.coupon_type = "Promotional"
        coupon.description = cstr(description or "").strip() or None
        coupon.pricing_rule = pr.name
        # Rule and coupon dates are enforced at different layers - get_pricing_rules()
        # reads the rule's, validate_coupon_code() reads the coupon's - so they are kept
        # identical rather than allowed to drift.
        coupon.valid_from = valid_from
        coupon.valid_upto = valid_upto
        coupon.maximum_use = cint(maximum_use)
        if customer:
            coupon.customer = customer
        if _has_per_customer_field():
            coupon.set(MAX_USES_PER_CUSTOMER_FIELD, cint(max_uses_per_customer))
        coupon.flags.ignore_permissions = True
        coupon.insert(ignore_permissions=True)
    except Exception:
        frappe.db.rollback()
        raise

    coupon.reload()
    return _coupon_payload(coupon)


@frappe.whitelist()
@standardize_response
def update_coupon(code, **payload):
    """Field-allowlist update. A whole-document save is not the contract.

    Presentation and limits stay editable for the life of the coupon. The economics -
    discount type, value, and scope - freeze once the coupon has been redeemed, because
    changing what a redeemed coupon was worth rewrites history.
    """
    _check_permission("write")

    coupon = CouponRepository.get_coupon(_normalize_code(code))
    if not coupon.pricing_rule or not CouponRepository.pricing_rule_exists(coupon.pricing_rule):
        frappe.throw(_("Coupon {0} has no Pricing Rule to update.").format(coupon.coupon_code))

    pr = CouponRepository.get_pricing_rule(coupon.pricing_rule)
    redeemed = cint(coupon.used) > 0

    economic_fields = {"discount_type", "discount_value", "apply_on", "targets"}
    always_editable = {
        "coupon_name",
        "description",
        "valid_from",
        "valid_upto",
        "min_amt",
        "maximum_use",
        "max_uses_per_customer",
        "customer",
        "priority",
    }
    allowed = economic_fields | always_editable

    unknown = set(payload) - allowed - {"cmd"}
    if unknown:
        frappe.throw(
            _("These fields cannot be updated: {0}.").format(", ".join(sorted(unknown))),
            CouponConfigError,
        )

    touched_economics = economic_fields & set(payload)
    if redeemed and touched_economics:
        frappe.throw(
            _(
                "Coupon {0} has already been redeemed {1} time(s); {2} can no longer be changed. "
                "Deactivate it and create a replacement instead."
            ).format(coupon.coupon_code, cint(coupon.used), ", ".join(sorted(touched_economics))),
            CouponConfigError,
        )

    # Start from what the rule already says, then overlay only what was sent.
    current_apply_on, current_targets = _pricing_rule_scope(pr)
    current_discount_type = DISCOUNT_TYPE_BY_RATE_OR_DISCOUNT.get(pr.rate_or_discount)
    current_value = flt(
        pr.discount_percentage if current_discount_type == "Percentage" else pr.discount_amount
    )

    discount_type, discount_value = _validate_discount(
        payload.get("discount_type", current_discount_type),
        payload.get("discount_value", current_value),
    )
    apply_on, targets = _validate_scope(
        payload.get("apply_on", current_apply_on),
        payload.get("targets", current_targets) if "targets" in payload or "apply_on" in payload else current_targets,
    )
    valid_from, valid_upto = _validate_dates(
        payload.get("valid_from", pr.valid_from),
        payload.get("valid_upto", pr.valid_upto),
    )
    customer = _validate_customer(payload.get("customer", pr.customer))

    if touched_economics:
        _assert_no_conflict(apply_on, targets, exclude_rule=pr.name)

    config = {
        "title": cstr(payload.get("coupon_name", coupon.coupon_name)).strip() or coupon.coupon_name,
        "apply_on": apply_on,
        "targets": targets,
        "discount_type": discount_type,
        "discount_value": discount_value,
        "min_amt": payload.get("min_amt", pr.min_amt),
        "valid_from": valid_from,
        "valid_upto": valid_upto,
        "customer": customer,
        "priority": cint(payload.get("priority", pr.priority)) or COUPON_DEFAULT_PRIORITY,
    }

    try:
        _apply_rule_configuration(pr, config)
        # An update must never silently re-activate a deactivated coupon.
        pr.disable = cint(frappe.db.get_value("Pricing Rule", pr.name, "disable"))
        pr.save(ignore_permissions=True)

        if "description" in payload:
            coupon.description = cstr(payload["description"]).strip() or None
        if "maximum_use" in payload:
            coupon.maximum_use = cint(payload["maximum_use"])
        if "max_uses_per_customer" in payload and _has_per_customer_field():
            coupon.set(MAX_USES_PER_CUSTOMER_FIELD, cint(payload["max_uses_per_customer"]))
        if customer:
            coupon.customer = customer
        coupon.valid_from = valid_from
        coupon.valid_upto = valid_upto
        coupon.flags.ignore_permissions = True
        coupon.save(ignore_permissions=True)
    except Exception:
        frappe.db.rollback()
        raise

    coupon.reload()
    return _coupon_payload(coupon)


@frappe.whitelist()
@standardize_response
def set_coupon_active(code, active=1):
    """The only writer of Pricing Rule.disable.

    Expiry is expressed through valid_from/valid_upto, not through this flag, so the
    customer gets ERPNext's specific expiry message rather than a generic invalid-coupon
    response.
    """
    _check_permission("write")

    coupon = CouponRepository.get_coupon(_normalize_code(code))
    if not coupon.pricing_rule or not CouponRepository.pricing_rule_exists(coupon.pricing_rule):
        frappe.throw(_("Coupon {0} has no Pricing Rule.").format(coupon.coupon_code))

    active = cint(active)
    if active:
        pr = CouponRepository.get_pricing_rule(coupon.pricing_rule)
        apply_on, targets = _pricing_rule_scope(pr)
        _assert_no_conflict(apply_on, targets, exclude_rule=pr.name)

    frappe.db.set_value("Pricing Rule", coupon.pricing_rule, "disable", 0 if active else 1)
    coupon.reload()
    return _coupon_payload(coupon)


@frappe.whitelist()
@standardize_response
def delete_coupon(code):
    _check_permission("delete")

    coupon = CouponRepository.get_coupon(_normalize_code(code))
    if cint(coupon.used) > 0:
        frappe.throw(
            _("Coupon {0} has been redeemed {1} time(s) and cannot be deleted. Deactivate it instead.").format(
                coupon.coupon_code, cint(coupon.used)
            ),
            CouponConfigError,
        )

    pricing_rule = coupon.pricing_rule
    code_value = coupon.coupon_code
    try:
        coupon.flags.ignore_permissions = True
        coupon.delete(ignore_permissions=True)
        # Deletion is refused above for anything ever redeemed, so the rule carries no
        # history worth keeping. Leaving it behind disabled would accumulate orphans that
        # the conflict scanner then has to reason about.
        if pricing_rule and CouponRepository.pricing_rule_exists(pricing_rule):
            frappe.delete_doc("Pricing Rule", pricing_rule, ignore_permissions=True, force=True)
    except Exception:
        frappe.db.rollback()
        raise

    return {"deleted": code_value, "pricing_rule_deleted": pricing_rule}


@frappe.whitelist()
@standardize_response
def scan_pricing_rule_conflicts(limit=None):
    """Admin-triggered version of the conflict scanner."""
    _check_permission("read")
    return review_active_pricing_rule_conflicts(limit=limit)



# =========================================
# HOOKS
# =========================================

def _is_internal_usage_write(doc) -> bool:
    """ERPNext maintains `used` itself - update_coupon_code_count() calls
    coupon.save() on Sales Order submit and cancel - so those writers must pass."""
    import inspect

    if getattr(doc.flags, "ignore_coupon_used_validation", False):
        return True
    return any(
        frame.function == "update_coupon_code_count" for frame in inspect.stack()
    )


def per_customer_limit_message() -> str:
    return _("You have already used this coupon.")


def enforce_coupon_limits(doc, method=None):
    """Sales Order before_validate: row lock + per-customer redemption cap.

    Bound to the doctype rather than to place_order, because the cap previously lived
    inside the mobile endpoint and so did not exist at all for an order raised in the
    desk, imported, or created by any other code path. Every Sales Order carrying a
    coupon now passes through here.

    before_validate rather than validate: the lock must be held before ERPNext's own
    validate_coupon_code() reads `used`, otherwise a concurrent transaction can slip
    between that read and the increment on submit.
    """
    coupon_name = cstr(doc.get("coupon_code")).strip()
    if not coupon_name:
        return

    # Held for the remainder of the transaction - covers ERPNext's maximum_use check
    # and its increment on submit, as well as the per-customer count below.
    CouponRepository.lock_for_redemption(coupon_name)

    limit = cint(frappe.db.get_value("Coupon Code", coupon_name, MAX_USES_PER_CUSTOMER_FIELD))
    if limit <= 0:
        # 0 means unlimited per customer; the global maximum_use still applies.
        return

    filters = {
        "customer": doc.customer,
        "coupon_code": coupon_name,
        "docstatus": ["!=", 2],
    }
    if not doc.is_new():
        filters["name"] = ["!=", doc.name]

    if frappe.db.count("Sales Order", filters) >= limit:
        frappe.throw(per_customer_limit_message(), CouponConfigError)


def validate(doc, method=None):
    """Minimal server-owned-field guard.

    The previous guard rejected any field present in the raw HTTP payload outside a
    four-field allowlist, including fields sent unchanged, which made a normal form save
    impossible. Configuration now lives on Pricing Rule and is written through the admin
    endpoints, so the only things left to protect on the coupon are its identity, its
    link to the rule, and the counter ERPNext owns.
    """
    if doc.is_new():
        return

    old = frappe.db.get_value(
        "Coupon Code", doc.name, ["used", "pricing_rule", "coupon_code"], as_dict=True
    )
    if not old:
        return

    if not cstr(doc.pricing_rule).strip():
        frappe.throw(_("Pricing Rule is required and cannot be removed."))

    if cstr(doc.pricing_rule) != cstr(old.pricing_rule):
        frappe.throw(_("The linked Pricing Rule cannot be changed."))

    if _normalize_code(doc.coupon_code) != _normalize_code(old.coupon_code):
        frappe.throw(_("Coupon code cannot be changed."))

    if cint(doc.get("used")) != cint(old.used) and not _is_internal_usage_write(doc):
        frappe.throw(_("Coupon usage count cannot be modified manually."))


def on_delete(doc, method=None):
    if doc.pricing_rule and CouponRepository.pricing_rule_exists(doc.pricing_rule):
        CouponRepository.disable_pricing_rule(doc.pricing_rule)
