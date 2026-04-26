# =========================================
# IMPORTS
# =========================================
import frappe
from frappe import _
from frappe.utils import nowdate, getdate


def _parse_apply_on_values(raw_value):
    import json

    if not raw_value:
        return []

    values = []
    if isinstance(raw_value, str):
        try:
            parsed_value = json.loads(raw_value)
            if isinstance(parsed_value, list):
                values = parsed_value
            else:
                values = [raw_value]
        except Exception:
            values = [raw_value]
    elif isinstance(raw_value, (list, tuple, set)):
        values = list(raw_value)
    else:
        values = [raw_value]

    cleaned_values = []
    seen_values = set()
    for value in values:
        value = (value or "").strip() if isinstance(value, str) else value
        if not value or value in seen_values:
            continue
        seen_values.add(value)
        cleaned_values.append(value)

    return cleaned_values


def _get_request_payload():
    import json

    request = getattr(frappe.local, "request", None)
    if not request:
        return {}

    payload = None

    try:
        payload = request.get_json(force=False, silent=True)
    except Exception:
        payload = None

    if not isinstance(payload, dict):
        try:
            raw_data = request.get_data(as_text=True)
        except Exception:
            raw_data = None

        if raw_data:
            try:
                payload = json.loads(raw_data)
            except Exception:
                payload = None

    if not isinstance(payload, dict):
        form_dict = getattr(frappe.local, "form_dict", None) or {}
        raw_data = form_dict.get("data")
        if raw_data:
            try:
                payload = json.loads(raw_data)
            except Exception:
                payload = None

    return payload if isinstance(payload, dict) else {}


def _item_group_matches_targets(item_group, target_groups):
    if not item_group or not target_groups:
        return False

    item_group_bounds = frappe.get_cached_value("Item Group", item_group, ["lft", "rgt"])
    if not item_group_bounds:
        return False

    item_lft, item_rgt = item_group_bounds
    for target_group in target_groups:
        target_group_bounds = frappe.get_cached_value("Item Group", target_group, ["lft", "rgt"])
        if not target_group_bounds:
            continue

        target_lft, target_rgt = target_group_bounds
        if target_lft <= item_lft <= item_rgt <= target_rgt:
            return True

    return False


def _pricing_rule_scope_signature(pr):
    if pr.apply_on == "Item Code":
        return tuple(sorted(row.item_code for row in pr.items if row.item_code))
    if pr.apply_on == "Item Group":
        return tuple(sorted(row.item_group for row in pr.item_groups if row.item_group))
    if pr.apply_on == "Brand":
        return tuple(sorted(row.brand for row in pr.brands if row.brand))
    return ("Transaction",)


def _log_pricing_rule_conflict_review(pr):
    if not pr or pr.disable or not pr.selling:
        return

    signature = _pricing_rule_scope_signature(pr)
    candidates = frappe.get_all(
        "Pricing Rule",
        filters={
            "selling": 1,
            "disable": 0,
            "coupon_code_based": 1,
            "for_price_list": pr.for_price_list or "Standard Selling",
            "apply_on": pr.apply_on,
            "name": ["!=", pr.name],
        },
        fields=["name"],
        limit_page_length=50,
    )

    overlaps = []
    for row in candidates:
        try:
            candidate = frappe.get_doc("Pricing Rule", row.name)
            if _pricing_rule_scope_signature(candidate) == signature:
                overlaps.append(candidate.name)
        except Exception:
            continue

    if overlaps:
        frappe.log_error(
            title="PRICING_RULE_CONFLICT_REVIEW",
            message=frappe.as_json(
                {
                    "event": "PRICING_RULE_CONFLICT_REVIEW",
                    "pricing_rule": pr.name,
                    "apply_on": pr.apply_on,
                    "for_price_list": pr.for_price_list,
                    "scope": signature,
                    "overlapping_rules": overlaps,
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
    def increment_usage(coupon):
        previous_used = frappe.db.get_value("Coupon Code", coupon.name, "used") or 0
        updated = frappe.db.sql(
            """
            UPDATE `tabCoupon Code`
            SET used = COALESCE(used, 0) + 1
            WHERE name = %s
              AND (maximum_use IS NULL OR maximum_use = 0 OR COALESCE(used, 0) < maximum_use)
            """,
            (coupon.name,),
        )
        current_used = frappe.db.get_value("Coupon Code", coupon.name, "used") or 0
        if current_used <= previous_used:
            frappe.throw(_("Usage limit reached"))


# =========================================
# SERVICE — creates / updates Pricing Rule
# =========================================
class CouponService:

    def __init__(self, doc):
        self.doc = doc

    def _get(self, field, default=None):
        return getattr(self.doc, field, default)

    # ---------- CREATE ----------
    def create_pricing_rule(self):
        if self.doc.pricing_rule:
            return CouponRepository.get_pricing_rule(self.doc.pricing_rule)

        pr = frappe.new_doc("Pricing Rule")
        pr.naming_series = "COUPON-.####"
        pr.title = self._get("coupon_name") or self._get("coupon_code")

        # Apply On
        apply_map = {
            "All":         "Transaction",
            "Item":        "Item Code",
            "Group":       "Item Group",
            "Brand":       "Brand",
            "Transaction": "Transaction",
        }
        pr.apply_on = apply_map.get(self._get("apply_on"), "Transaction")

        if pr.apply_on == "Item Code" and self._get("apply_on_value"):
            for item_code in _parse_apply_on_values(self._get("apply_on_value")):
                if item_code:
                    pr.append("items", {"item_code": item_code})

        elif pr.apply_on == "Item Group" and self._get("apply_on_value"):
            for item_group in _parse_apply_on_values(self._get("apply_on_value")):
                if item_group:
                    pr.append("item_groups", {"item_group": item_group})

        elif pr.apply_on == "Brand" and self._get("apply_on_value"):
            for brand in _parse_apply_on_values(self._get("apply_on_value")):
                if brand:
                    pr.append("brands", {"brand": brand})

        # Discount
        discount_type = self._get("discount_type")
        if discount_type in ("Percentage", "Fixed", "Free Shipping"):
            pr.price_or_product_discount = "Price"
        elif discount_type == "Free Item":
            pr.price_or_product_discount = "Product"

        if discount_type == "Percentage":
            pr.rate_or_discount    = "Discount Percentage"
            pr.discount_percentage = self._get("discount_percentage") or 0
        elif discount_type == "Fixed":
            pr.rate_or_discount  = "Discount Amount"
            pr.discount_amount   = self._get("discount_amount") or 0
        elif discount_type == "Free Item":
            pr.free_item      = self._get("free_item")
            pr.free_qty       = 1
            pr.free_item_rate = 0
        elif discount_type == "Free Shipping":
            pr.rate_or_discount    = "Discount Percentage"
            pr.discount_percentage = 100
            pr.remarks             = "FREE_SHIPPING"

        # Party
        pr.selling = 1
        pr.buying  = 0

        # Amounts
        pr.min_amt = self._get("min_order_amount") or 0
        pr.min_qty = 0
        pr.max_qty = 0
        pr.max_amt = 0

        # Dates
        pr.valid_from = self._get("valid_from") or None
        pr.valid_upto = self._get("valid_upto") or None

        pr.currency          = "IQD"
        pr.coupon_code_based = 1
        pr.disable           = 1
        pr.for_price_list    = self._get("price_list") or "Standard Selling"

        pr.insert(ignore_permissions=True)
        return pr

    # ---------- UPDATE ----------
    def update_pricing_rule(self):
        if not self.doc.pricing_rule:
            return
        if not CouponRepository.pricing_rule_exists(self.doc.pricing_rule):
            return

        pr = CouponRepository.get_pricing_rule(self.doc.pricing_rule)
        pr.title = self._get("coupon_name") or self._get("coupon_code")
        pr.set("items", [])
        pr.set("item_groups", [])
        pr.set("brands", [])

        apply_map = {
            "All":         "Transaction",
            "Item":        "Item Code",
            "Group":       "Item Group",
            "Brand":       "Brand",
            "Transaction": "Transaction",
        }
        pr.apply_on = apply_map.get(self._get("apply_on"), "Transaction")

        if pr.apply_on == "Item Code" and self._get("apply_on_value"):
            for item_code in _parse_apply_on_values(self._get("apply_on_value")):
                if item_code:
                    pr.append("items", {"item_code": item_code})

        elif pr.apply_on == "Item Group" and self._get("apply_on_value"):
            for item_group in _parse_apply_on_values(self._get("apply_on_value")):
                if item_group:
                    pr.append("item_groups", {"item_group": item_group})

        elif pr.apply_on == "Brand" and self._get("apply_on_value"):
            for brand in _parse_apply_on_values(self._get("apply_on_value")):
                if brand:
                    pr.append("brands", {"brand": brand})

        discount_type = self._get("discount_type")
        if discount_type == "Percentage":
            pr.price_or_product_discount = "Price"
            pr.rate_or_discount          = "Discount Percentage"
            pr.discount_percentage       = self._get("discount_percentage") or 0
        elif discount_type == "Fixed":
            pr.price_or_product_discount = "Price"
            pr.rate_or_discount          = "Discount Amount"
            pr.discount_amount           = self._get("discount_amount") or 0
        elif discount_type == "Free Item":
            pr.price_or_product_discount = "Product"
            pr.free_item                 = self._get("free_item")
        elif discount_type == "Free Shipping":
            pr.price_or_product_discount = "Price"
            pr.rate_or_discount          = "Discount Percentage"
            pr.discount_percentage       = 100
            pr.remarks                   = "FREE_SHIPPING"

        pr.min_amt    = self._get("min_order_amount") or 0
        pr.valid_from = self._get("valid_from") or None
        pr.valid_upto = self._get("valid_upto") or None
        pr.disable    = 1

        pr.save(ignore_permissions=True)


    # ---------- DELETE ----------
    def disable_pricing_rule(self):
        if self.doc.pricing_rule:
            CouponRepository.disable_pricing_rule(self.doc.pricing_rule)


# =========================================
# VALIDATOR
# Only validates — ERPNext handles discount calculation
# =========================================
class CouponValidator:

    @staticmethod
    def validate(code, cart, customer=None):
        """
        Validates coupon against cart.
        Returns pricing rule details when valid so callers can decide whether to
        let ERPNext handle the coupon at transaction level or apply row-level
        discounts manually.
        """
        code = code.strip().upper()
    
        if not CouponRepository.exists(code):
            return {"valid": False, "message": "Invalid coupon"}
    
        coupon = CouponRepository.get_coupon(code)
    
        # Expiry
        if coupon.valid_upto and str(coupon.valid_upto) < nowdate():
            return {"valid": False, "message": "Coupon has expired"}
    
        # Usage limit
        if coupon.maximum_use and coupon.used >= coupon.maximum_use:
            return {"valid": False, "message": "Usage limit reached"}
    
        if customer and frappe.db.exists(
            "Sales Order",
            {
                "customer": customer,
                "coupon_code": coupon.name,
                "docstatus": ["!=", 2],
            },
        ):
            return {"valid": False, "message": "You have already used this coupon"}
    
        if not coupon.pricing_rule:
            return {"valid": False, "message": "Coupon has no linked Pricing Rule"}
    
        pr = CouponRepository.get_pricing_rule(coupon.pricing_rule)
        pricing_rule_details = {
            "name": pr.name,
            "apply_on": pr.apply_on,
            "rate_or_discount": pr.rate_or_discount,
            "discount_percentage": frappe.utils.flt(pr.discount_percentage or 0),
            "discount_amount": frappe.utils.flt(pr.discount_amount or 0),
            "items": [],
            "item_groups": [],
            "brands": [],
        }
    
        # Min amount
        if pr.min_amt and cart["total"] < pr.min_amt:
            return {"valid": False, "message": f"Minimum order amount is {pr.min_amt}"}
    
        # Item Code check
        if pr.apply_on == "Item Code" and pr.items:
            target_items = [row.item_code for row in pr.items if row.item_code]
            pricing_rule_details["items"] = target_items
    
            if not any(i.get("item_code") in target_items for i in cart["items"]):
                return {"valid": False, "message": "Required item not in cart"}
    
        # Item Group check
        if pr.apply_on == "Item Group" and pr.item_groups:
            target_item_groups = [row.item_group for row in pr.item_groups if row.item_group]
            pricing_rule_details["item_groups"] = target_item_groups
            if not any(
                _item_group_matches_targets(i.get("item_group"), target_item_groups)
                for i in cart["items"]
            ):
                return {"valid": False, "message": "Required item group not in cart"}

        # Brand check
        if pr.apply_on == "Brand" and pr.brands:
            target_brands = [row.brand for row in pr.brands if row.brand]
            pricing_rule_details["brands"] = target_brands
            if not any(i.get("brand") in target_brands for i in cart["items"]):
                return {"valid": False, "message": "Required brand not in cart"}
    
        return {
            "valid": True,
            "coupon_name": coupon.name,
            "coupon_code": coupon.coupon_code,
            "pricing_rule": pricing_rule_details,
        }
    
# =========================================
# USAGE
# =========================================
class CouponUsage:

    @staticmethod
    def apply(coupon_code):
        coupon = CouponRepository.get_coupon(coupon_code)
        CouponRepository.increment_usage(coupon)


# =========================================
# HOOKS
# =========================================

def validate(doc, method=None):
    import inspect

    if doc.is_new():
        return

    if not doc.pricing_rule or not str(doc.pricing_rule).strip():
        frappe.throw("Pricing Rule is required and cannot be removed")

    old_doc = frappe.get_doc("Coupon Code", doc.name)
    internal_used_update = any(
        frame.function in {"update_coupon_code_count", "increment_usage"} for frame in inspect.stack()
    )
    ignore_used_validation = getattr(doc.flags, "ignore_coupon_used_validation", False)
    internal_usage_save = internal_used_update or ignore_used_validation

    if doc.get("used") != old_doc.get("used") and not internal_usage_save:
        frappe.throw("used cannot be modified manually")

    if internal_usage_save:
        doc.flags.coupon_pricing_rule_fields_changed = False
        return

    doc.flags.coupon_pricing_rule_fields_changed = any(
        doc.get(field) != old_doc.get(field) for field in ("valid_from", "valid_upto")
    )

    allowed_fields = {
        "description",
        "valid_from",
        "valid_upto",
        "maximum_use",
    }
    blocked_fields = {
        "coupon_code",
        "coupon_name",
        "pricing_rule",
        "apply_on",
        "apply_on_value",
        "discount_type",
        "discount_percentage",
        "discount_amount",
    }

    ignore_fields = {
        "modified", "modified_by", "owner", "creation",
        "idx", "docstatus", "doctype", "name"
    }

    request_payload = _get_request_payload()
    for field in request_payload:
        if field in allowed_fields or field in ignore_fields:
            continue

        if field in blocked_fields or field not in doc.meta.get_valid_columns() or field == "used":
            frappe.throw(f"{field} cannot be modified")

    transient_fields = (
        set(doc.__dict__.keys())
        - set(old_doc.__dict__.keys())
        - ignore_fields
        - {"meta", "flags"}
    )
    for field in transient_fields:
        if field.startswith("_") or field in allowed_fields:
            continue

        if field in blocked_fields or field not in doc.meta.get_valid_columns():
            frappe.throw(f"{field} cannot be modified")
    
    for field in doc.meta.get_valid_columns():
        if field in allowed_fields or field in ignore_fields:
            continue

        if field == "used":
            continue
        
        if doc.get(field) != old_doc.get(field):
            frappe.throw(f"{field} cannot be modified")

def after_insert(doc, method=None):
    pr = CouponService(doc).create_pricing_rule()

    if not pr or not pr.name:
        frappe.throw(_("Pricing Rule is required for Coupon Code"))

    frappe.db.set_value("Coupon Code", doc.name, "pricing_rule", pr.name)

    doc.reload()

    if not doc.pricing_rule:
        frappe.throw(_("Coupon must be linked to a Pricing Rule"))

    _log_pricing_rule_conflict_review(pr)


def on_update(doc, method=None):
    if getattr(doc.flags, "ignore_coupon_pricing_rule_update", False) or getattr(
        frappe.flags, "ignore_coupon_pricing_rule_update", False
    ):
        return

    if not getattr(doc.flags, "coupon_pricing_rule_fields_changed", False):
        return

    CouponService(doc).update_pricing_rule()
    if doc.pricing_rule and CouponRepository.pricing_rule_exists(doc.pricing_rule):
        _log_pricing_rule_conflict_review(CouponRepository.get_pricing_rule(doc.pricing_rule))


def on_delete(doc, method=None):
    CouponService(doc).disable_pricing_rule()
