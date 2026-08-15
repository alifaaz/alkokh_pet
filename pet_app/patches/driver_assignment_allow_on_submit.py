from __future__ import annotations

import frappe

# Sales Order.custom_driver is a Custom Field with allow_on_submit = 0, and place_order
# submits the order immediately. That combination made driver assignment impossible after
# the fact: assign_driver_to_order writes custom_driver on a submitted order, and
# update_after_submit refuses any field the doctype has not opened. It is the same trap
# custom_order_status was in before pet_app.patches.order_status_allow_on_submit.
#
# Applied as a Property Setter rather than by editing the Custom Field row so the override
# is a separate, inspectable record - deleting this one document restores the original
# behaviour without touching the field definition.

DOC_TYPE = "Sales Order"
FIELD_NAME = "custom_driver"
PROPERTY = "allow_on_submit"

# Removed in favour of per-cashier till resolution: the driver hands cash to a person,
# and that person's own POS Profile decides the receiving account. A single site-wide
# account cannot express that, so the field is dropped rather than left to rot.
RETIRED_SETTING = "driver_cash_handover_account"


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

    # The doctype JSON no longer declares the field, but a value written into tabSingles
    # while it existed would survive; clear it so nothing reads a stale account.
    frappe.db.delete(
        "Singles", {"doctype": "Pet App Accounting Settings", "field": RETIRED_SETTING}
    )
    frappe.db.commit()
