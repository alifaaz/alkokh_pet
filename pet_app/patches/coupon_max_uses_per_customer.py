from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_field

FIELDNAME = "custom_max_uses_per_customer"

DEFINITION = {
    "fieldname": FIELDNAME,
    "label": "Max Uses Per Customer",
    "fieldtype": "Int",
    "default": "1",
    "insert_after": "maximum_use",
    "non_negative": 1,
    "description": (
        "How many times one customer may redeem this coupon. "
        "0 means unlimited; the global limit in Maximum Use still applies."
    ),
    "module": "Pet App",
    "no_copy": 0,
    "read_only": 0,
    "translatable": 0,
}


def execute():
    """Make the per-customer redemption limit configurable.

    The limit was hardcoded as once-per-customer-forever inside CouponValidator, with no
    field to relax it and no effect on Sales Orders created outside the mobile API. This
    field is where the policy lives; Phase 5 relocates enforcement into a Sales Order
    validate hook so the desk path is covered too.

    Backfilled to 1 only on the run that creates the field, so re-running can never
    tighten a limit an admin deliberately widened.
    """
    if frappe.db.exists("Custom Field", {"dt": "Coupon Code", "fieldname": FIELDNAME}):
        return

    create_custom_field("Coupon Code", DEFINITION)

    # The ALTER carries the default for future INSERTs only; existing coupons are filled
    # explicitly so none is left at NULL, which would read as "unlimited".
    frappe.db.sql(
        "UPDATE `tabCoupon Code` SET `{0}` = 1 WHERE `{0}` IS NULL".format(FIELDNAME)
    )
    frappe.clear_cache(doctype="Coupon Code")
    frappe.db.commit()
