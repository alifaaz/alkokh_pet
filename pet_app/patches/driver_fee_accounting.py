"""The driver's delivery fee is booked: income on the customer's invoice, and an expense
the shop owes the driver on his own fee account. This patch owns these fields."""
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

SETTINGS = "Pet App Accounting Settings"


def execute():
    fields = {
        "Pet App Accounting Settings": [
            {"fieldname": "custom_driver_fee_income_account", "label": "Driver Fee Income Account", "fieldtype": "Link", "options": "Account", "insert_after": "custom_driver_cash_parent"},
            {"fieldname": "custom_driver_fee_expense_account", "label": "Driver Fee Expense Account", "fieldtype": "Link", "options": "Account", "insert_after": "custom_driver_fee_income_account"},
            {"fieldname": "custom_driver_fee_payable_parent", "label": "Driver Fee Payable Parent", "fieldtype": "Link", "options": "Account", "insert_after": "custom_driver_fee_expense_account"},
            {"fieldname": "custom_driver_shortage_account", "label": "Driver Settlement Shortage Account", "fieldtype": "Link", "options": "Account", "insert_after": "custom_driver_fee_payable_parent"},
            {"fieldname": "custom_driver_overage_account", "label": "Driver Settlement Overage Account", "fieldtype": "Link", "options": "Account", "insert_after": "custom_driver_shortage_account"},
        ],
        "Driver": [
            {"fieldname": "custom_fee_account", "label": "Driver Fee Account", "fieldtype": "Link", "options": "Account", "read_only": 1, "insert_after": "custom_cash_account"},
        ],
        "Sales Invoice": [
            {"fieldname": "custom_driver_fee_booked", "label": "Driver Fee Booked", "fieldtype": "Currency", "read_only": 1},
        ],
        "Journal Entry": [
            {"fieldname": "custom_driver_flow", "label": "Driver Order", "fieldtype": "Check", "read_only": 1},
            {"fieldname": "custom_driver_operation", "label": "Driver Operation", "fieldtype": "Link", "options": "Comment", "read_only": 1},
            {"fieldname": "custom_driver", "label": "Driver", "fieldtype": "Link", "options": "Driver", "read_only": 1},
            {"fieldname": "custom_driver_order", "label": "Driver Order", "fieldtype": "Link", "options": "Sales Order", "read_only": 1},
            {"fieldname": "custom_driver_invoice", "label": "Driver Invoice", "fieldtype": "Link", "options": "Sales Invoice", "read_only": 1},
            {"fieldname": "custom_driver_journal_kind", "label": "Driver Journal Kind", "fieldtype": "Select", "options": "\nFee\nHistoric Fee\nSettlement", "read_only": 1},
        ],
    }
    create_custom_fields(fields, update=True)
    frappe.clear_cache()

    company = frappe.db.get_single_value(SETTINGS, "default_company")
    if not company:
        return
    defaults = {
        "custom_driver_fee_income_account": {"account_number": "708500"},
        "custom_driver_fee_expense_account": {"account_number": "624200"},
        "custom_driver_shortage_account": {"account_number": "658000"},
        "custom_driver_overage_account": {"account_number": "758000"},
    }
    for field, filters in defaults.items():
        if not frappe.db.get_single_value(SETTINGS, field):
            account = frappe.db.get_value("Account", {"company": company, "is_group": 0, "disabled": 0, **filters})
            if account:
                frappe.db.set_single_value(SETTINGS, field, account)

    if not frappe.db.get_single_value(SETTINGS, "custom_driver_fee_payable_parent"):
        parent = frappe.db.get_value("Account", {"company": company, "account_number": "42", "is_group": 1, "root_type": "Liability"})
        if parent:
            group = frappe.db.get_value("Account", {"company": company, "account_name": "Driver Fees Payable", "is_group": 1})
            if not group:
                group = frappe.get_doc({"doctype": "Account", "account_name": "Driver Fees Payable", "parent_account": parent,
                    "company": company, "is_group": 1, "root_type": "Liability"}).insert(ignore_permissions=True).name
            frappe.db.set_single_value(SETTINGS, "custom_driver_fee_payable_parent", group)

    if frappe.db.get_single_value(SETTINGS, "custom_driver_fee_payable_parent"):
        from pet_app.utils.driver_orders import provision_fee_account
        for name in frappe.get_all("Driver", filters={"status": "Active"}, pluck="name"):
            provision_fee_account(name, company)
    frappe.db.add_unique("Driver", ["custom_fee_account"], "uniq_driver_fee_account")
    frappe.clear_cache()
