from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	if not frappe.db.exists("DocType", "Pet App Cashier Settlement"):
		return

	create_custom_fields(
		{
			"Payment Entry": [
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
		},
		update=True,
	)
	frappe.clear_cache(doctype="Payment Entry")

