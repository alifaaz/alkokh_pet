"""One-step driver settlement: the Sales Order stamp and the settlement start date.

Orders delivered before the start date were already handed over through the old
Handover (receive_driver_cash) and are not offered to settlement again; any cash still
recorded against the driver from them keeps using Handover. This patch owns these fields.
"""
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import nowdate


def execute():
    fields = {
        "Sales Order": [
            {"fieldname": "custom_driver_settlement", "label": "Driver Settlement", "fieldtype": "Link", "options": "Driver Settlement", "read_only": 1, "allow_on_submit": 1, "ignore_user_permissions": 1, "no_copy": 1},
        ],
        "Pet App Accounting Settings": [
            {"fieldname": "custom_driver_settlement_start", "label": "Driver Settlement Start Date", "fieldtype": "Date", "insert_after": "custom_driver_overage_account", "description": "Orders delivered before this date are settled through Handover, not Driver Settlement."},
        ],
    }
    create_custom_fields(fields, update=True)
    frappe.clear_cache()
    if not frappe.db.get_single_value("Pet App Accounting Settings", "custom_driver_settlement_start"):
        frappe.db.set_single_value("Pet App Accounting Settings", "custom_driver_settlement_start", nowdate())
