from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_field

FIELDNAME = "custom_store_published"

DEFINITION = {
    "fieldname": FIELDNAME,
    "label": "Store Published",
    "fieldtype": "Check",
    "default": "1",
    "insert_after": "variant_of",
    # Only rendered for items that ARE variants. A template is not purchasable, so its
    # own value is meaningless, and a simple item's visibility is Product.status - showing
    # the checkbox on either would invite an admin to set a flag nothing reads.
    "depends_on": "eval:doc.variant_of",
    "description": (
        "Uncheck to hide this variant from the storefront. "
        "Ignored on templates and on non-variant items."
    ),
    "module": "Pet App",
    "no_copy": 0,
    "read_only": 0,
    "translatable": 0,
}


def execute():
    """Add Item.custom_store_published so variants can be published individually.

    Backfills to 1 ONLY on the run that creates the field. Re-running must never
    re-publish a variant an admin deliberately hid, so the backfill is tied to creation
    rather than to "any row that is currently 0".
    """
    if frappe.db.exists("Custom Field", {"dt": "Item", "fieldname": FIELDNAME}):
        return

    create_custom_field("Item", DEFINITION)

    # The ALTER carries the column default, but that governs future INSERTs; rows that
    # already existed are filled explicitly so nothing silently disappears from the store.
    frappe.db.sql(
        "UPDATE `tabItem` SET `{0}` = 1 WHERE `{0}` IS NULL OR `{0}` = 0".format(FIELDNAME)
    )
    frappe.db.commit()
