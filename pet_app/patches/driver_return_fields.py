"""Why a delivered order came back (driver report_return). This patch owns these fields."""
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
    create_custom_fields({
        "Sales Order": [
            {"fieldname": "custom_return_reason", "label": "Return Reason", "fieldtype": "Select", "options": "\ndamaged\nwrong_order\nchanged_mind\nother", "read_only": 1, "allow_on_submit": 1, "no_copy": 1},
            {"fieldname": "custom_return_note", "label": "Return Note", "fieldtype": "Small Text", "read_only": 1, "allow_on_submit": 1, "no_copy": 1},
        ],
    }, update=True)
    frappe.clear_cache(doctype="Sales Order")
