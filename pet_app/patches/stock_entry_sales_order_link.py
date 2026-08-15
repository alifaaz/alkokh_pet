from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_field

# pet_app.api.order._create_stock_issue has always written se.custom_sales_order, but the
# field was never created on Stock Entry. Frappe drops attributes that are not meta fields,
# so the value silently vanished and both dependents broke in different ways:
#
#   * the idempotency guard uses frappe.db.exists(), which swallows the SQL error and
#     returns None - so it never detected an existing issue and would happily create a
#     duplicate Material Issue.
#   * _reverse_stock_issue uses frappe.db.get_value(), which does not swallow - it raised
#     OperationalError 1054, making it impossible to cancel an order and return its stock.
#
# read_only because the link is set by the order flow, never by hand; no_copy so an amended
# Stock Entry does not silently claim the original's Sales Order.

DOC_TYPE = "Stock Entry"
FIELD_NAME = "custom_sales_order"


def execute():
    create_custom_field(
        DOC_TYPE,
        {
            "fieldname": FIELD_NAME,
            "label": "Sales Order",
            "fieldtype": "Link",
            "options": "Sales Order",
            "insert_after": "sales_invoice_no",
            "read_only": 1,
            "no_copy": 1,
            "print_hide": 1,
        },
    )
    frappe.clear_cache(doctype=DOC_TYPE)
