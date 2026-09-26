"""The weight band gains the quantity it deducts, alongside the price it charges.

`Care Service Billing Option` already carries the money side of a band - `default_rate`
against `min_weight`/`max_weight`. It carried nothing about stock, so a deworming tablet
band could say "0-10 kg costs X" but not "0-10 kg consumes one tablet", and the dispensing
side had no per-band quantity to read.

`stock_deduction_qty` is Float, not Int, on purpose: a sixth of a tablet is a real dose and
0.1667 must survive. `precision: 4` is explicit because this site's System Settings
`float_precision` is 2, which would round a sixth to 0.17 and drift a box of tablets out of
count over a few hundred doses.

ZERO MEANS NOT CONFIGURED, NOT "DEDUCT NOTHING". Every one of the existing rows starts at 0
and this patch deliberately backfills none of them - there is no safe value to guess, since
the correct quantity depends on the drug and the band. A consumer that reads 0 must refuse
to deduct and name the band that needs configuring, rather than silently issue a service
that takes nothing off the shelf. Vaccination is the one case with a fixed answer (exactly
one whole vial, no band) and that is still a deliberate 1, entered, not inferred.

Schema only. `reload_doc(force=True)` rather than relying on the migrate sync, because a
doctype JSON whose `modified` is not newer than the row in the database is skipped.
"""

from __future__ import annotations

import frappe


DOCTYPE = "Care Service Billing Option"
FIELDNAME = "stock_deduction_qty"


def execute():
	if not frappe.db.exists("DocType", DOCTYPE):
		return

	frappe.reload_doc("pet_app", "doctype", "care_service_billing_option", force=True)

	if not frappe.db.has_column(DOCTYPE, FIELDNAME):
		frappe.log_error(
			title="CARE_SERVICE_BILLING_OPTION_STOCK_QTY_MISSING",
			message=f"{DOCTYPE}.{FIELDNAME} was not created by the doctype reload.",
		)
