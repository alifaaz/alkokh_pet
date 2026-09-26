"""Delivery partners: two doctypes, one child table, and six Sales Invoice stamps.

SCHEMA ONLY, AND ADDITIVE ONLY. Nothing that exists is read, migrated or altered. After
this runs, every existing screen behaves exactly as it does today - the new tables are
simply there, and empty, and Sales Invoice carries six more nullable columns that only a
partner sale ever fills.

THE CUSTOM FIELDS COME FIRST, deliberately. `DeliveryPartner._validate_customer_ownership`
filters Sales Invoice on `custom_delivery_partner`, and Frappe fails the whole query when
one requested field does not exist. Creating the doctype before the column it reads would
leave a window - a single interrupted migrate - in which saving a partner throws an
obscure SQL error instead of validating.

These six are PATCH-OWNED and must NOT be added to the Custom Field fixture allow-list in
hooks.py; `tests/test_fixture_allowlist.py` fails the build on a field owned by both. That
guard reads this file with `ast`, so `CUSTOM_FIELDS` has to stay a literal mapping bound to
a name - inlining it into the call, or building it at runtime, makes these six invisible to
the check rather than making them pass it.

`reload_doc(force=True)` rather than relying on the migrate sync, for the reason
`preventive_care_record_schema` gives: a doctype JSON whose `modified` is not newer than
the row in the database is skipped, and a fresh doctype that silently failed to appear
would surface later as a missing-table error on a screen rather than here.

THE VERIFICATION IS LOUD. Every consumer built on top of this would otherwise fail one at
a time with an obscure SQL error, so a missing table or column logs an Error Log naming it.
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

DOCTYPES = {
	"Delivery Partner": "delivery_partner",
	"Delivery Partner Settlement Invoice": "delivery_partner_settlement_invoice",
	"Delivery Partner Settlement": "delivery_partner_settlement",
}

# The columns the API layer depends on. Not the whole field list - just the ones whose
# absence would be silently survivable at insert and fatal later.
REQUIRED_FIELDS = {
	"Delivery Partner": (
		"partner_name", "partner_name_ar", "is_active", "commission_rate", "settlement_cycle",
		"customer", "logo", "brand_color", "commission_expense_account", "contact_person",
		"contact_number", "notes",
	),
	"Delivery Partner Settlement": (
		"delivery_partner", "customer", "posting_date", "gross_amount", "commission_amount",
		"adjustment_amount", "received_amount", "received_in", "payment_entry", "idempotency_key",
	),
	"Delivery Partner Settlement Invoice": ("sales_invoice", "gross_amount", "commission_amount"),
}

CUSTOM_FIELDS = {
	"Sales Invoice": [
	{
		"fieldname": "custom_delivery_partner",
		"label": "Delivery Partner",
		"fieldtype": "Link",
		"options": "Delivery Partner",
		"insert_after": "customer_name",
		"read_only": 1,
		"in_standard_filter": 1,
		"description": "Set only on a partner order. Its presence is what makes this a partner sale rather than a counter sale.",
	},
	{
		"fieldname": "custom_partner_order_ref",
		"label": "Partner Order Ref",
		"fieldtype": "Data",
		"insert_after": "custom_delivery_partner",
		"read_only": 1,
		"description": "The aggregator's own order number. Required on every partner sale - it is the only handle the partner's statement can be matched on.",
	},
	{
		"fieldname": "custom_partner_customer_name",
		"label": "Partner Customer Name",
		"fieldtype": "Data",
		"insert_after": "custom_partner_order_ref",
		"read_only": 1,
		"description": "Who ordered it in the app. The invoice is billed to the PARTNER, so without this the end customer's name appears nowhere.",
	},
	{
		"fieldname": "custom_partner_commission_rate",
		"label": "Partner Commission Rate (%)",
		"fieldtype": "Percent",
		"insert_after": "custom_partner_customer_name",
		"read_only": 1,
		"description": "A SNAPSHOT taken at sale time, never re-read from the partner. The first time a partner renegotiates, reading the live rate would silently restate every past month.",
	},
	{
		"fieldname": "custom_partner_commission_amount",
		"label": "Partner Commission Amount",
		"fieldtype": "Currency",
		"insert_after": "custom_partner_commission_rate",
		"read_only": 1,
		"description": "grand_total x the snapshotted rate, computed at sale time.",
	},
	{
		"fieldname": "custom_partner_settlement",
		"label": "Partner Settlement",
		"fieldtype": "Link",
		"options": "Delivery Partner Settlement",
		"insert_after": "custom_partner_commission_amount",
		"read_only": 1,
		"allow_on_submit": 1,
		"in_standard_filter": 1,
		"description": "The settlement that collected this invoice. Empty means unsettled - this field IS the settled state, so there is one place to look and no flag that can disagree with it. allow_on_submit because an invoice is submitted long before it is settled.",
	},
	],
}


def execute():
	# Validate after schema synchronization: on hosts with multiple accessible sites,
	# core's column discovery can see a new field in another DB. Same reason
	# driver_orders_schema passes it.
	create_custom_fields(CUSTOM_FIELDS, update=True, ignore_validate=True)
	from frappe.core.doctype.doctype.doctype import validate_fields_for_doctype

	validate_fields_for_doctype("Sales Invoice")

	for doctype, path in DOCTYPES.items():
		frappe.reload_doc("pet_app", "doctype", path, force=True)
		if not frappe.db.exists("DocType", doctype):
			frappe.log_error(
				title="DELIVERY_PARTNER_DOCTYPE_MISSING",
				message=f"{doctype} was not created by the doctype reload. Nothing downstream will work.",
			)
			return

	missing_columns = []
	for doctype, fields in REQUIRED_FIELDS.items():
		missing_columns += [f"{doctype}.{f}" for f in fields if not frappe.db.has_column(doctype, f)]
	missing_columns += [
		f"Sales Invoice.{f['fieldname']}"
		for f in CUSTOM_FIELDS["Sales Invoice"]
		if not frappe.db.has_column("Sales Invoice", f["fieldname"])
	]
	if missing_columns:
		frappe.log_error(
			title="DELIVERY_PARTNER_FIELDS_MISSING",
			message="Missing after the reload: " + ", ".join(missing_columns),
		)
		return

	frappe.clear_cache()
	frappe.logger("pet_app.migrate").info(
		{
			"event": "DELIVERY_PARTNER_SCHEMA_READY",
			"partners": frappe.db.count("Delivery Partner"),
			"settlements": frappe.db.count("Delivery Partner Settlement"),
		}
	)
