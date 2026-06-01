import frappe

def execute():
    """Remove Website Item dependencies from Product doctype and clean up orphaned records."""

    # Clear website_item field from all Product records
    frappe.db.sql("UPDATE `tabProduct` SET website_item = NULL WHERE website_item IS NOT NULL")

    # Check if Website Item table exists
    if frappe.db.table_exists("Website Item"):
        # Remove orphaned Website Item records that are linked to Products
        # First, get all Website Items linked to Products
        linked_website_items = frappe.db.sql("""
            SELECT DISTINCT wi.name
            FROM `tabWebsite Item` wi
            INNER JOIN `tabProduct` p ON p.website_item = wi.name
            WHERE p.website_item IS NOT NULL
        """, as_dict=False)

        if linked_website_items:
            linked_names = [row[0] for row in linked_website_items]
            # Delete them
            frappe.db.sql("DELETE FROM `tabWebsite Item` WHERE name IN %(names)s", {"names": linked_names})

        # Optionally truncate the entire Website Item table if no other dependencies
        # Uncomment the following line if you want to remove ALL Website Items
        # frappe.db.sql("TRUNCATE TABLE `tabWebsite Item`")

    frappe.db.commit()