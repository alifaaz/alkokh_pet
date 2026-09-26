"""The driver's own screen: the "driver sees the customer's phone" switch.
Ran 2026-09-19; the rest of the screen's schema is in driver_self_screen_fields.
This patch owns the field."""
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
    create_custom_fields({
        "Pet App Accounting Settings": [
            {"fieldname": "custom_driver_sees_customer_phone", "label": "Drivers See Customer Phone", "fieldtype": "Check", "default": "1", "insert_after": "custom_driver_settlement_start"},
        ],
    }, update=True)
    frappe.clear_cache()
