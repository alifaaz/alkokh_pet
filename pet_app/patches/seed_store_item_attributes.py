from __future__ import annotations

import frappe

from pet_app.api.variants import ITEM_ATTRIBUTE_DOCTYPE, STORE_ITEM_ATTRIBUTES


def execute():
    """Seed the storefront Item Attributes used to build variant products.

    Idempotent in both directions: an attribute that already exists is left alone, and
    missing values are appended rather than the whole record being rewritten. That matters
    because abbreviations become permanent variant SKU suffixes - silently rewriting one
    would rename every item code built from it.

    Deliberately does NOT delete ERPNext's stock "Colour"/"Size" attributes. Those were
    removed on this site as a one-off after confirming zero Item Variant Attribute
    references; doing it here would delete another environment's data on migrate, which
    is a different decision from seeding.
    """
    if not frappe.db.exists("DocType", ITEM_ATTRIBUTE_DOCTYPE):
        return

    for spec in STORE_ITEM_ATTRIBUTES:
        name = spec["attribute_name"]
        wanted = [{"attribute_value": v, "abbr": a} for v, a in spec["values"]]

        if not frappe.db.exists(ITEM_ATTRIBUTE_DOCTYPE, name):
            doc = frappe.new_doc(ITEM_ATTRIBUTE_DOCTYPE)
            doc.attribute_name = name
            doc.numeric_values = 0
            for row in wanted:
                doc.append("item_attribute_values", row)
            doc.flags.ignore_permissions = True
            doc.insert()
            continue

        doc = frappe.get_doc(ITEM_ATTRIBUTE_DOCTYPE, name)
        existing = {(r.attribute_value or "").strip().lower() for r in doc.item_attribute_values}
        added = False
        for row in wanted:
            if row["attribute_value"].strip().lower() in existing:
                continue
            doc.append("item_attribute_values", row)
            added = True
        if added:
            doc.flags.ignore_permissions = True
            doc.save()
