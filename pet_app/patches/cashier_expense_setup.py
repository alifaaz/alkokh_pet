"""Custom fields the cashier expense voucher is stamped with.

Journal Entry carried no pet_app custom fields before this - the driver cash handovers
that already post Cash Entries against till accounts identify themselves by parsing
user_remark, which is not something a reporting query should have to do.

`branch` is a plain Link, matching the six `*-branch` reporting fields pet_app already adds
to Sales Invoice, Payment Entry, Sales Order, Stock Entry, POS Profile and Appointment. It
tags the voucher, not the GL Entry, so ERPNext's own P&L cannot filter on it; that is the
accepted trade against registering Branch as a site-wide Accounting Dimension, which would
add the field to every accounting doctype and leave every existing voucher blank.
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	_create_expense_custom_fields()
	frappe.clear_cache(doctype="Journal Entry")


def _create_expense_custom_fields():
	custom_fields = {
		"Journal Entry": [
			{
				"fieldname": "branch",
				"fieldtype": "Link",
				"label": "Branch",
				"options": "Branch",
				"insert_after": "company",
				# Front-desk users are User-Permission-scoped to their own clinic but must
				# be able to book a payout attributed to another - the endpoint checks the
				# write with assert_can_write_to_branch instead.
				"ignore_user_permissions": 1,
				"in_standard_filter": 1,
			},
			{
				"fieldname": "custom_pos_profile",
				"fieldtype": "Link",
				"label": "POS Profile",
				"options": "POS Profile",
				"insert_after": "voucher_type",
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
				"read_only": 1,
			},
			{
				"fieldname": "custom_expense_category",
				"fieldtype": "Data",
				"label": "Expense Category",
				"insert_after": "custom_cashier_user",
				"allow_on_submit": 1,
				"in_standard_filter": 1,
				"read_only": 1,
				# Data, not a Link to the child table: categories live in a settings grid
				# with no independent identity, and a historic voucher must keep reporting
				# the category it was booked under even after that row is renamed or removed.
				"description": "Key of the cashier expense category this entry was booked under.",
			},
			{
				"fieldname": "custom_idempotency_key",
				"fieldtype": "Data",
				"label": "Idempotency Key",
				"insert_after": "custom_expense_category",
				"read_only": 1,
				"no_copy": 1,
				# Indexed, deliberately not unique. Every other Journal Entry on the site -
				# driver cash handovers included - leaves this blank, and a unique index over
				# a column full of empty strings would refuse the second one.
				"search_index": 1,
				"description": "Set by record_cashier_expense so a double-tapped till button books one payout, not two.",
			},
		],
	}
	create_custom_fields(custom_fields, ignore_validate=True)
