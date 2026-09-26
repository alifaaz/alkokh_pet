"""Driver orders are scoped by Branch, not by the till that created them.

Sales Order.custom_pos_profile (origin till), custom_fulfillment_warehouse (the driver's
warehouse) and custom_driver_cash_account are provenance snapshots. As ordinary Link
fields they made Frappe hide an order from any user holding a POS Profile, Warehouse or
Account User Permission that doesn't match, so a cashier restricted to one till never
saw orders rung up at another till in the same branch. Branch stays enforced.
"""
import frappe

FIELDS = (
    "Sales Order-custom_pos_profile",
    "Sales Order-custom_fulfillment_warehouse",
    "Sales Order-custom_driver_cash_account",
)


def execute():
    for name in FIELDS:
        if frappe.db.exists("Custom Field", name):
            frappe.db.set_value("Custom Field", name, "ignore_user_permissions", 1)
    frappe.clear_cache(doctype="Sales Order")
