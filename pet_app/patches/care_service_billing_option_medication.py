"""The weight band names the drug it deducts, not just how much of it.

`care_service_stock_relief_fields` put `medication` on `CareService template` and
`stock_deduction_qty` on `Care Service Billing Option`, on the reasoning that the drug is
named once per service rather than repeated on all 22 weight bands. That reasoning assumed
a band knows which template it belongs to. It does not: `Care Service Billing Option` has
no link to a template at all - its only parent field, `category_care_services`, points at a
`CategoryCareServices`, and the three lookups in its controller that read it as a template
name return None for all 45 rows on this site.

So a band could say HOW MUCH but had no reachable place to say WHAT. On this site Deworming
holds a band with `stock_deduction_qty = 1.0` and no template anywhere in its category,
while Vaccination holds a template naming Rabisin and no band. Neither half could relieve
stock, and a drug chosen against a band was discarded because it had nowhere to be written.

THE DRUG IS NOW NAMED ON THE BAND. The cost is accepted and is real: a service with several
weight bands repeats the same drug on every one of them, and changing it means changing
every band. That is the owner's decision, taken against the alternative of first inventing
a band-to-template link.

THE TEMPLATE'S FIELD STAYS, AND STAYS AUTHORITATIVE WHERE THE BAND IS SILENT. Vaccination
works today entirely through it - CareService-00022 names Rabisin and has no band at all -
so `plan_care_service_stock` reads the band first and falls back to the template. Removing
the template field would break the one configuration on this site that currently works.

Schema only, nothing backfilled. Every existing band's `medication` starts empty, so every
one of them resolves exactly as it does today: through its template, or not at all. The
band that needs the new field is CSBO-00050, and filling it is a data decision for the
operator, not a guess this patch is entitled to make.
"""

from __future__ import annotations

import frappe


DOCTYPE = "Care Service Billing Option"
MODULE_PATH = "care_service_billing_option"
FIELDNAME = "medication"


def execute():
	if not frappe.db.exists("DocType", DOCTYPE):
		return

	frappe.reload_doc("pet_app", "doctype", MODULE_PATH, force=True)

	if not frappe.db.has_column(DOCTYPE, FIELDNAME):
		frappe.log_error(
			title="CARE_SERVICE_BILLING_OPTION_MEDICATION_MISSING",
			message=f"{DOCTYPE}.{FIELDNAME} was not created by the doctype reload.",
		)
