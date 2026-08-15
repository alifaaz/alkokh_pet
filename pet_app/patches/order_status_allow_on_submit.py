from __future__ import annotations

import frappe

# Sales Order.custom_order_status is a Custom Field with allow_on_submit = 0, and
# place_order submits the order immediately. That combination made the whole transition
# machinery in pet_app.api.order unreachable: before_update_after_submit /
# on_update_after_submit only fire on an update-after-submit, which the field itself
# forbade. No order in the system had ever left "Draft".
#
# Applied as a Property Setter rather than by editing the Custom Field row so the override
# is a separate, inspectable record - deleting this one document restores the original
# behaviour without touching the field definition.

DOC_TYPE = "Sales Order"
FIELD_NAME = "custom_order_status"
PROPERTY = "allow_on_submit"


def execute():
    if not frappe.db.exists("Custom Field", {"dt": DOC_TYPE, "fieldname": FIELD_NAME}):
        return

    frappe.make_property_setter(
        {
            "doctype": DOC_TYPE,
            "fieldname": FIELD_NAME,
            "property": PROPERTY,
            "value": "1",
            "property_type": "Check",
        },
        is_system_generated=False,
    )
    frappe.clear_cache(doctype=DOC_TYPE)
