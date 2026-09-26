"""Driver screen schema, and two settings the 2026-09-19 patches meant to seed.

- Sales Order: the driver's note, a refusal's reason/note, and the delivery-attempt log
  (child table Driver Delivery Attempt).
- Pet App Accounting Settings.custom_driver_role_profile: the Role Profile a driver login
  holds; linking a driver to an existing User adds it (roles come from profiles here).
- Seeds, written only where NO value is stored (checked in tabSingles directly:
  get_single_value returns a Date/Check placeholder for a missing row, which is why the
  first patches skipped them):
    custom_driver_settlement_start = 2026-09-19 (the migration day; older orders were
        handed over through Handover and must not be offered to settlement),
    custom_driver_sees_customer_phone = 1.
This patch owns these fields.
"""
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

SETTINGS = "Pet App Accounting Settings"
SETTLEMENT_START = "2026-09-19"


def execute():
    create_custom_fields({
        "Sales Order": [
            {"fieldname": "custom_driver_note", "label": "Driver Note", "fieldtype": "Small Text", "read_only": 1, "allow_on_submit": 1, "no_copy": 1},
            {"fieldname": "custom_refusal_reason", "label": "Refusal Reason", "fieldtype": "Select", "options": "\nrefused\nchanged_mind\ndamaged\nwrong_order", "read_only": 1, "allow_on_submit": 1, "no_copy": 1},
            {"fieldname": "custom_refusal_note", "label": "Refusal Note", "fieldtype": "Small Text", "read_only": 1, "allow_on_submit": 1, "no_copy": 1},
            {"fieldname": "custom_delivery_attempts", "label": "Delivery Attempts", "fieldtype": "Table", "options": "Driver Delivery Attempt", "read_only": 1, "allow_on_submit": 1, "no_copy": 1},
        ],
        "Pet App Accounting Settings": [
            {"fieldname": "custom_driver_role_profile", "label": "Driver Role Profile", "fieldtype": "Link", "options": "Role Profile", "insert_after": "custom_driver_sees_customer_phone"},
        ],
    }, update=True)
    frappe.clear_cache()

    def stored(field):
        return frappe.db.sql("select value from `tabSingles` where doctype=%s and field=%s", (SETTINGS, field))

    if not stored("custom_driver_settlement_start"):
        frappe.db.set_single_value(SETTINGS, "custom_driver_settlement_start", SETTLEMENT_START)
    if not stored("custom_driver_sees_customer_phone"):
        frappe.db.set_single_value(SETTINGS, "custom_driver_sees_customer_phone", 1)
    if not stored("custom_driver_role_profile"):
        # The profile the existing driver logins already hold.
        users = frappe.get_all("Driver", filters={"user": ["is", "set"]}, pluck="user")
        held = frappe.get_all("User Role Profile", filters={"parent": ["in", users or [""]]}, pluck="role_profile")
        if "السائق" in held:
            frappe.db.set_single_value(SETTINGS, "custom_driver_role_profile", "السائق")
    frappe.clear_cache()
