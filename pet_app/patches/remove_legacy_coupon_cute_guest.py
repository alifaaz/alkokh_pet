from __future__ import annotations

import frappe

COUPON_CODE = "CUTE-GUEST-V9R4H"


def execute():
    """Remove the one pre-rebuild coupon and its Pricing Rule.

    CUTE-GUEST-V9R4H was expired, its rule disabled, and it had never been redeemed. Its
    Pricing Rule carried a discount_amount that the old projection layer could not
    produce - it was typed in by hand - so it is not representative of anything the new
    admin API creates, and leaving it in place would make the first real coupon harder to
    reason about.

    Guarded rather than unconditional: this runs on every site, and another environment
    may hold a coupon of the same code that has actually been used. Anything with a
    redemption, or referenced by a Sales Order, is left alone.
    """
    name = frappe.db.get_value("Coupon Code", {"coupon_code": COUPON_CODE}, "name")
    if not name:
        return

    used = frappe.db.get_value("Coupon Code", name, "used") or 0
    if used:
        return

    if frappe.db.exists("Sales Order", {"coupon_code": name, "docstatus": ["!=", 2]}):
        return

    pricing_rule = frappe.db.get_value("Coupon Code", name, "pricing_rule")

    # on_trash disables the rule; delete it outright instead, since nothing references it.
    frappe.delete_doc("Coupon Code", name, ignore_permissions=True, force=True)

    if pricing_rule and frappe.db.exists("Pricing Rule", pricing_rule):
        frappe.delete_doc("Pricing Rule", pricing_rule, ignore_permissions=True, force=True)

    frappe.db.commit()
