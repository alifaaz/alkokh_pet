from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	_create_cashier_custom_fields()
	frappe.clear_cache(doctype="POS Profile")
	frappe.clear_cache(doctype="Payment Entry")
	frappe.clear_cache(doctype="Sales Invoice")


def _create_cashier_custom_fields():
	custom_fields = {
		"POS Profile": [
			{
				"fieldname": "custom_cash_account",
				"fieldtype": "Link",
				"label": "Cash Account",
				"options": "Account",
				"insert_after": "payments",
			},
		],
		"Payment Entry": [
			{
				"fieldname": "custom_pos_profile",
				"fieldtype": "Link",
				"label": "POS Profile",
				"options": "POS Profile",
				"insert_after": "mode_of_payment",
				"allow_on_submit": 1,
				"in_standard_filter": 1,
				"read_only": 1,
			},
			{
				"fieldname": "custom_cashier_user",
				"fieldtype": "Link",
				"label": "Cashier User",
				"options": "User",
				"insert_after": "custom_pos_profile",
				"allow_on_submit": 1,
				"in_standard_filter": 1,
				"read_only": 1,
			},
			{
				"fieldname": "custom_cashier_employee",
				"fieldtype": "Link",
				"label": "Cashier Employee",
				"options": "Employee",
				"insert_after": "custom_cashier_user",
				"allow_on_submit": 1,
				"read_only": 1,
			},
			{
				"fieldname": "custom_cashier_cash_account",
				"fieldtype": "Link",
				"label": "Cashier Cash Account",
				"options": "Account",
				"insert_after": "custom_cashier_employee",
				"allow_on_submit": 1,
				"in_standard_filter": 1,
				"read_only": 1,
			},
			{
				"fieldname": "custom_cashier_settlement",
				"fieldtype": "Link",
				"label": "Cashier Settlement",
				"options": "Pet App Cashier Settlement",
				"insert_after": "custom_cashier_cash_account",
				"allow_on_submit": 1,
				"in_standard_filter": 1,
				"read_only": 1,
			},
		],
		"Sales Invoice": [
			{
				"fieldname": "custom_pos_profile",
				"fieldtype": "Link",
				"label": "POS Profile",
				"options": "POS Profile",
				"insert_after": "pos_profile",
				"allow_on_submit": 1,
				"in_standard_filter": 1,
				"read_only": 1,
			},
			{
				"fieldname": "custom_cashier_user",
				"fieldtype": "Link",
				"label": "Cashier User",
				"options": "User",
				"insert_after": "custom_pos_profile",
				"allow_on_submit": 1,
				"in_standard_filter": 1,
				"read_only": 1,
			},
		],
	}
	create_custom_fields(custom_fields, update=True)
