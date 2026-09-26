"""Persist the scope of a native Sales Invoice accommodation discount."""
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
    create_custom_fields({"Sales Invoice": [{
        "fieldname": "custom_boarding_discount_booking",
        "fieldtype": "Link",
        "options": "Pet Boarding",
        "label": "Boarding Discount Booking",
        "insert_after": "discount_amount",
        "read_only": 1,
        "hidden": 1,
        "no_copy": 1,
        "description": "Restricts invoice discount distribution to this booking's accommodation rows.",
    }]})
