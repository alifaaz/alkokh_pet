"""A delivery partner may bill the REAL app customer instead of its own Customer.

`Delivery Partner.is_inside` ("Bill the App Customer"): the order is billed to the customer
the cashier picks, while the partner still collects the money, keeps its commission and
settles with us later. Two pieces of schema follow from that, and this patch adds both.

`Sales Invoice.custom_partner_is_inside`
----------------------------------------
A SNAPSHOT of the flag at sale time, beside the commission snapshots. The guard that stops
the till collecting money the partner already took
(`delivery_partners.refuse_partner_collected_invoices`) reads it off the invoice, so
switching a partner's flag later cannot quietly re-expose - or re-lock - orders that
already exist. Empty on everything sold before this: none of those were inside orders.

`Delivery Partner Settlement.journal_entry`
-------------------------------------------
A settlement posts ONE Payment Entry whose party is the partner's Customer, and ERPNext
will not let it clear an invoice billed to anybody else. Inside orders are spread over many
customers, so their settlement posts a Journal Entry that credits each customer's invoice
instead, and this is where it is linked. Settlements of ordinary partners are unaffected and
keep using `payment_entry`.

ADDITIVE ONLY. Nothing existing is read, migrated or altered.
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

# Patch-owned: must NOT join the Custom Field fixture allow-list in hooks.py, or
# `test_fixture_allowlist` fails the build on a field shipped twice. A literal mapping bound
# to a name so that test's `ast` walk can see the fieldname at all.
CUSTOM_FIELDS = {
	"Sales Invoice": [
		{
			"fieldname": "custom_partner_is_inside",
			"label": "Partner Order Billed to App Customer",
			"fieldtype": "Check",
			"default": "0",
			"insert_after": "custom_partner_commission_type",
			"read_only": 1,
			"description": (
				"A SNAPSHOT taken at sale time. Set when this partner order is billed to the real "
				"customer while the partner holds the money - so the till must never collect it; "
				"it is cleared by a partner settlement."
			),
		},
	],
}


def execute():
	if not frappe.db.table_exists("Delivery Partner"):
		# The creating patch has not run on this site yet; it will ship the new fields.
		return

	# Validate after schema synchronization, for the reason `delivery_partner_schema` gives.
	create_custom_fields(CUSTOM_FIELDS, update=True, ignore_validate=True)
	from frappe.core.doctype.doctype.doctype import validate_fields_for_doctype

	validate_fields_for_doctype("Sales Invoice")

	frappe.reload_doc("pet_app", "doctype", "delivery_partner", force=True)
	frappe.reload_doc("pet_app", "doctype", "delivery_partner_settlement", force=True)

	missing = []
	if not frappe.db.has_column("Delivery Partner", "is_inside"):
		missing.append("Delivery Partner.is_inside")
	if not frappe.db.has_column("Delivery Partner Settlement", "journal_entry"):
		missing.append("Delivery Partner Settlement.journal_entry")
	missing += [
		f"Sales Invoice.{f['fieldname']}"
		for f in CUSTOM_FIELDS["Sales Invoice"]
		if not frappe.db.has_column("Sales Invoice", f["fieldname"])
	]
	if missing:
		# Loud: every consumer would otherwise fail one at a time with an obscure SQL error.
		frappe.log_error(
			title="DELIVERY_PARTNER_INSIDE_FIELDS_MISSING",
			message="Missing after the reload: " + ", ".join(missing),
		)
		return

	frappe.clear_cache(doctype="Delivery Partner")
	frappe.clear_cache(doctype="Delivery Partner Settlement")
	frappe.clear_cache(doctype="Sales Invoice")
	frappe.logger("pet_app.migrate").info(
		{
			"event": "DELIVERY_PARTNER_INSIDE_READY",
			"inside_partners": frappe.db.count("Delivery Partner", {"is_inside": 1}),
		}
	)
