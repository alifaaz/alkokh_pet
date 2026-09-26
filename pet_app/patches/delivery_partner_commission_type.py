"""A delivery partner may keep a flat fee per order instead of a percentage.

Talabat takes 25% of every order; another aggregator takes a flat 2,000 per order whatever
it is worth. Only the first could be expressed, so a flat-fee partner had to be entered as
an approximate percentage that was wrong on every order but one.

ADDITIVE ONLY, AND THE EXISTING PARTNERS DO NOT MOVE. `commission_type` defaults to
Percentage, which is exactly what every partner already is, and their `commission_rate` is
untouched. Nothing is recomputed and no past month is restated.

WHY THE TYPE IS SNAPSHOTTED ONTO THE INVOICE
--------------------------------------------
`custom_partner_commission_type` sits beside the rate snapshot for the same reason that one
exists: `create_settlement` works a covered invoice's commission out again from what the
invoice stored, and it has to know WHICH stored figure means something. Reading the live
type instead would silently restate every past order the first time a partner switches from
a percentage to a flat fee - the failure the rate snapshot was introduced to prevent,
reappearing through the field beside it.

Invoices written before this patch carry no type. They are read as Percentage, which is
what they were, so the backfill is a no-op and is deliberately not run: a column left null
is honest about predating the choice, where a backfilled one cannot be told from a decision
somebody made.
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

# Patch-owned, like the six in `delivery_partner_schema`, and for the same reason: it must
# NOT join the Custom Field fixture allow-list in hooks.py, or `test_fixture_allowlist`
# fails the build on a field shipped twice. Kept as a literal mapping bound to a name so
# that test's `ast` walk can see the fieldname at all.
CUSTOM_FIELDS = {
	"Sales Invoice": [
		{
			"fieldname": "custom_partner_commission_type",
			"label": "Partner Commission Type",
			"fieldtype": "Select",
			"options": "\nPercentage\nAmount",
			"insert_after": "custom_partner_customer_name",
			"read_only": 1,
			"description": (
				"A SNAPSHOT taken at sale time: which of the two commission figures below "
				"this order was actually charged on. Empty on orders that predate flat-fee "
				"partners - those are all Percentage."
			),
		},
	],
}

PARTNER_FIELDS = ("commission_type", "commission_amount")


def execute():
	if not frappe.db.table_exists("Delivery Partner"):
		# The creating patch has not run on this site yet; it will ship the new fields.
		return

	# Validate after schema synchronization, for the reason `delivery_partner_schema`
	# gives: on a host with several accessible sites, core's column discovery can see a
	# new field in another DB.
	create_custom_fields(CUSTOM_FIELDS, update=True, ignore_validate=True)
	from frappe.core.doctype.doctype.doctype import validate_fields_for_doctype

	validate_fields_for_doctype("Sales Invoice")

	frappe.reload_doc("pet_app", "doctype", "delivery_partner", force=True)

	missing = [f"Delivery Partner.{f}" for f in PARTNER_FIELDS if not frappe.db.has_column("Delivery Partner", f)]
	missing += [
		f"Sales Invoice.{f['fieldname']}"
		for f in CUSTOM_FIELDS["Sales Invoice"]
		if not frappe.db.has_column("Sales Invoice", f["fieldname"])
	]
	if missing:
		# Loud, like the creating patch: every consumer would otherwise fail one at a time
		# with an obscure SQL error a long way from here.
		frappe.log_error(
			title="DELIVERY_PARTNER_COMMISSION_TYPE_FIELDS_MISSING",
			message="Missing after the reload: " + ", ".join(missing),
		)
		return

	# Existing partners take Percentage from the field default only when they are next
	# saved, and `create_pos_sale` reads the stored value on every sale before then. So the
	# rows are filled here rather than left to the default - a partner reading as neither
	# type would otherwise have its rate ignored the moment the sale path branches on it.
	frappe.db.sql(
		"""update `tabDelivery Partner`
		set commission_type = 'Percentage'
		where commission_type is null or commission_type = ''"""
	)

	frappe.clear_cache(doctype="Delivery Partner")
	frappe.clear_cache(doctype="Sales Invoice")
	frappe.logger("pet_app.migrate").info(
		{
			"event": "DELIVERY_PARTNER_COMMISSION_TYPE_READY",
			"partners": frappe.db.count("Delivery Partner"),
		}
	)
