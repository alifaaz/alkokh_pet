"""Create `Preventive Care Record`, the execution record for vaccination and deworming.

SCHEMA ONLY, AND ADDITIVE ONLY. This patch creates one new doctype and touches nothing that
exists. It does not migrate a row, does not read `Pet Vaccination Record` or
`Pet Deworming Record`, and does not alter `PetCareService` or any catalogue doctype. After
it runs, every existing screen and every existing record behaves exactly as it does today -
the new table is simply there, and empty.

NOTHING IS MIGRATED, DELIBERATELY. There is no preventive history on this site to migrate:
all 29 `PetCareService` rows in the two preventive categories, and all 26 rows across the two
record doctypes, are on a single test pet (PET-00128 / GUARDIAN-00107), were created inside a
four-day window, carry the machine provenance marker rather than a human's entry, have never
been edited since insert, and sit on two invoices with no payment against either. The removal
of that test data is a separate, destructive, self-verifying patch that runs at the END of
this piece of work, not here.

`reload_doc(force=True)` rather than relying on the migrate sync, for the reason
`care_service_billing_option_stock_qty` gives: a doctype JSON whose `modified` is not newer
than the row in the database is skipped, and a fresh doctype that silently failed to appear
would be discovered later as a missing-table error on a screen rather than here.

THE VERIFICATION IS LOUD. If the reload does not produce the table, this logs an Error Log
naming the doctype rather than returning quietly, because every consumer built on top of it
in the following stages would otherwise fail one at a time with an obscure SQL error.
"""

from __future__ import annotations

import frappe

DOCTYPE = "Preventive Care Record"
MODULE_PATH = "preventive_care_record"

# The columns the following stages depend on. Not the whole field list - just the ones whose
# absence would be silently survivable at insert and fatal later.
REQUIRED_FIELDS = (
	"pet",
	"visit",
	"source_doctype",
	"source_name",
	"order_id",
	"care_service",
	"service_option",
	"kind",
	"category",
	"medication",
	"medication_name",
	"qty",
	"status",
	"administered_on",
	"next_due_date",
	"reminder_enabled",
	"reminder_status",
	"item_code",
	"rate",
	"performing_branch",
	"billed",
	"sales_invoice",
	"stock_issued_qty",
	"stock_entry",
	"stock_warehouse",
)


def execute():
	frappe.reload_doc("pet_app", "doctype", MODULE_PATH, force=True)

	if not frappe.db.exists("DocType", DOCTYPE):
		frappe.log_error(
			title="PREVENTIVE_CARE_RECORD_DOCTYPE_MISSING",
			message=f"{DOCTYPE} was not created by the doctype reload. Nothing downstream will work.",
		)
		return

	missing = [f for f in REQUIRED_FIELDS if not frappe.db.has_column(DOCTYPE, f)]
	if missing:
		frappe.log_error(
			title="PREVENTIVE_CARE_RECORD_FIELDS_MISSING",
			message=f"{DOCTYPE} is missing {', '.join(missing)} after the doctype reload.",
		)
		return

	frappe.logger("pet_app.migrate").info(
		{
			"event": "PREVENTIVE_CARE_RECORD_SCHEMA_READY",
			"doctype": DOCTYPE,
			"existing_rows": frappe.db.count(DOCTYPE),
		}
	)
