"""Driver fulfillment metadata. This patch owns these fields (not fixtures)."""
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
    fields = {
        "Pet App Accounting Settings": [
            {"fieldname": "custom_enable_driver_orders", "label": "Enable Driver Orders", "fieldtype": "Check", "default": "0"},
            {"fieldname": "custom_driver_warehouse_parent", "label": "Driver Warehouse Parent", "fieldtype": "Link", "options": "Warehouse"},
            {"fieldname": "custom_driver_cash_parent", "label": "Driver Cash Account Parent", "fieldtype": "Link", "options": "Account"},
        ],
        "Driver": [
            {"fieldname": "custom_warehouse", "label": "Driver Warehouse", "fieldtype": "Link", "options": "Warehouse", "unique": 1},
            {"fieldname": "custom_delivery_fee", "label": "Direct Delivery Fee", "fieldtype": "Currency", "default": "0"},
        ],
        "Comment": [
            {"fieldname": "custom_driver_operation_key", "label": "Driver Operation Key", "fieldtype": "Data", "unique": 1, "read_only": 1},
            {"fieldname": "custom_driver_request_hash", "label": "Driver Request Hash", "fieldtype": "Data", "read_only": 1},
            {"fieldname": "custom_driver_result", "label": "Driver Operation Result", "fieldtype": "Long Text", "read_only": 1},
        ],
        "Sales Order": [
            {"fieldname": "custom_driver_flow", "label": "Driver Order", "fieldtype": "Check", "read_only": 1},
            {"fieldname": "custom_driver_operation", "label": "Driver Operation", "fieldtype": "Link", "options": "Comment", "read_only": 1},
            {"fieldname": "custom_fulfillment_warehouse", "label": "Fulfillment Warehouse", "fieldtype": "Link", "options": "Warehouse", "read_only": 1, "allow_on_submit": 1, "ignore_user_permissions": 1},
            {"fieldname": "custom_driver_cash_account", "label": "Driver Cash Snapshot", "fieldtype": "Link", "options": "Account", "read_only": 1, "allow_on_submit": 1, "ignore_user_permissions": 1},
            {"fieldname": "custom_pos_profile", "label": "Origin POS Profile", "fieldtype": "Link", "options": "POS Profile", "read_only": 1, "ignore_user_permissions": 1},
            {"fieldname": "custom_payment_arrangement", "label": "Payment Arrangement", "fieldtype": "Select", "options": "Cash on Delivery\nPrepaid\nOn Account\nCard on Delivery\nWallet on Delivery", "read_only": 1},
            {"fieldname": "custom_delivery_state", "label": "Delivery State", "fieldtype": "Select", "options": "Draft\nPreparing\nOut for Delivery\nPartially Delivered\nReturned\nCompleted\nCancelled", "read_only": 1, "allow_on_submit": 1},
            {"fieldname": "custom_driver_fee_earned", "label": "Driver Fee Earned", "fieldtype": "Check", "read_only": 1, "allow_on_submit": 1},
        ],
        "Sales Invoice": [
            {"fieldname": "custom_driver_flow", "label": "Driver Sale", "fieldtype": "Check", "read_only": 1},
            {"fieldname": "custom_driver", "label": "Driver", "fieldtype": "Link", "options": "Driver", "read_only": 1},
            {"fieldname": "custom_driver_operation", "label": "Driver Operation", "fieldtype": "Link", "options": "Comment", "read_only": 1},
            {"fieldname": "custom_fulfillment_warehouse", "label": "Fulfillment Warehouse", "fieldtype": "Link", "options": "Warehouse", "read_only": 1},
            {"fieldname": "custom_driver_cash_account", "label": "Driver Cash Snapshot", "fieldtype": "Link", "options": "Account", "read_only": 1},
            {"fieldname": "custom_direct_delivery_fee", "label": "Fee Paid Directly to Driver", "fieldtype": "Currency", "read_only": 1},
        ],
        "Sales Invoice Item": [
            {"fieldname": "custom_driver_load_row", "label": "Driver Load Row", "fieldtype": "Data", "read_only": 1},
        ],
        "Stock Entry": [
            {"fieldname": "custom_driver_flow", "label": "Driver Stock Movement", "fieldtype": "Check", "read_only": 1},
            {"fieldname": "custom_driver", "label": "Driver", "fieldtype": "Link", "options": "Driver", "read_only": 1},
            {"fieldname": "custom_driver_operation", "label": "Driver Operation", "fieldtype": "Link", "options": "Comment", "read_only": 1},
            {"fieldname": "custom_fulfillment_warehouse", "label": "Fulfillment Warehouse", "fieldtype": "Link", "options": "Warehouse", "read_only": 1},
            {"fieldname": "custom_driver_cash_account", "label": "Driver Cash Snapshot", "fieldtype": "Link", "options": "Account", "read_only": 1},
            {"fieldname": "custom_pos_profile", "label": "Origin POS Profile", "fieldtype": "Link", "options": "POS Profile", "read_only": 1},
            {"fieldname": "custom_driver_movement", "label": "Driver Movement", "fieldtype": "Select", "options": "Load\nReturn", "read_only": 1},
        ],
        "Stock Entry Detail": [
            {"fieldname": "custom_driver_load_row", "label": "Original Driver Load Row", "fieldtype": "Data", "read_only": 1},
            {"fieldname": "custom_driver_order_item", "label": "Sales Order Item", "fieldtype": "Data", "read_only": 1},
        ],
        "Payment Entry": [
            {"fieldname": "custom_driver_order", "label": "Original Driver Order", "fieldtype": "Link", "options": "Sales Order", "read_only": 1},
            {"fieldname": "custom_driver_flow", "label": "Driver Payment", "fieldtype": "Check", "read_only": 1},
            {"fieldname": "custom_driver", "label": "Driver", "fieldtype": "Link", "options": "Driver", "read_only": 1},
            {"fieldname": "custom_driver_operation", "label": "Driver Operation", "fieldtype": "Link", "options": "Comment", "read_only": 1},
            {"fieldname": "custom_fulfillment_warehouse", "label": "Fulfillment Warehouse", "fieldtype": "Link", "options": "Warehouse", "read_only": 1},
            {"fieldname": "custom_driver_cash_account", "label": "Driver Cash Snapshot", "fieldtype": "Link", "options": "Account", "read_only": 1},
            {"fieldname": "custom_pos_profile", "label": "Acting POS Profile", "fieldtype": "Link", "options": "POS Profile", "read_only": 1},
            {"fieldname": "custom_driver_payment_kind", "label": "Driver Payment Kind", "fieldtype": "Select", "options": "Receipt\nRefund\nHandover", "read_only": 1},
            {"fieldname": "custom_driver_refund_against", "label": "Advance Refund Against", "fieldtype": "Link", "options": "Payment Entry", "read_only": 1},
        ],
    }
    # Validate after schema synchronization: on hosts with multiple accessible
    # sites, core's column discovery can see a new unique field in another DB.
    create_custom_fields(fields, update=True, ignore_validate=True)
    from frappe.core.doctype.doctype.doctype import validate_fields_for_doctype
    for doctype in fields:
        validate_fields_for_doctype(doctype)
    # Existing delivery fields remain fixture-owned; update their fixture metadata too.
    if frappe.db.exists("Custom Field", "Sales Order-custom_delivery_fee"):
        frappe.db.set_value("Custom Field", "Sales Order-custom_delivery_fee", "allow_on_submit", 1)
    if frappe.db.exists("Workflow", "orders"):
        frappe.db.set_value("Workflow", "orders", "is_active", 0)
    company = frappe.db.get_single_value("Pet App Accounting Settings", "default_company")
    if company:
        parent = frappe.db.get_value("Warehouse", {"company": company, "warehouse_name": "مخزون في الطريق"})
        cash_parent = frappe.db.get_value("Account", {"company": company, "account_number": "531000", "is_group": 1, "root_type": "Asset"})
        settings = "Pet App Accounting Settings"
        if parent and not frappe.db.get_single_value(settings, "custom_driver_warehouse_parent"):
            if not frappe.db.exists("Stock Ledger Entry", {"warehouse": parent}) and not frappe.db.exists("Bin", {"warehouse": parent, "actual_qty": ["!=", 0]}):
                doc = frappe.get_doc("Warehouse", parent)
                doc.is_group = 1
                doc.save(ignore_permissions=True)
                frappe.db.set_single_value(settings, "custom_driver_warehouse_parent", parent)
        if cash_parent and not frappe.db.get_single_value(settings, "custom_driver_cash_parent"):
            frappe.db.set_single_value(settings, "custom_driver_cash_parent", cash_parent)
        if parent and cash_parent and frappe.db.get_single_value(settings, "custom_driver_warehouse_parent"):
            from pet_app.utils.driver_orders import provision_driver
            for name in frappe.get_all("Driver", pluck="name"):
                provision_driver(name, company, repair=True)
    frappe.db.add_unique("Driver", ["custom_cash_account"], "uniq_driver_cash_account")
    frappe.clear_cache()
