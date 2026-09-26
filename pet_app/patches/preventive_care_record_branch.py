"""`Preventive Care Record` gains its own `branch`, and is scoped by it.

WHY IT WAS MISSING. The doctype was modelled on `Lab`, and `Lab` has no `branch` - it has
`performing_branch` only. That is correct for Lab because a Lab always hangs off a Vet Visit
and the visit carries the branch. A preventive dose does not: it is routinely given straight
on a pet's record with no visit at all, and 28 of the 29 preventive rows on this site have
none. With nothing to inherit from, the record was left with no branch.

WHAT THAT ACTUALLY BROKE. `performing_branch` is blank on every catalogue row on this site,
so billing resolved a branch of None, and `get_or_create_open_invoice` refuses that for any
user who is not restricted to a single clinic:

    MandatoryError: Select a branch for this Sales Invoice. Your user is not restricted to
    one clinic, so the branch cannot be inferred.

Which is every back-office account, Administrator included. The path this replaces did not
have the problem because `PetCareService` carries `branch` as well - it is in
`utils.branch.SCOPED_DOCTYPES` - and `plan_care_service_billing` read
`performing_branch or branch`. The new record now carries both fields and reads them in the
same order.

TWO BRANCH FIELDS, TWO DIFFERENT FACTS, and they are adjacent in the form so nobody merges
them by accident:

    `branch`             which clinic this record BELONGS to. Stamped from the acting user
                         at insert by `stamp_branch_on_insert`, and what worklists filter on.
    `performing_branch`  where the catalogue says the service is physically CARRIED OUT.
                         Set once from `CareService template.performing_branch`, blank on
                         every row today, and the reason it still wins is that a service
                         performed at another branch should bill to that branch.

A NULL BRANCH IS A LEAK, NOT A DEFAULT - it is visible to every clinic
(`frappe/model/db_query.py`, `add_user_permissions`). That is why the doctype joins
`SCOPED_DOCTYPES` and gets the `before_insert` stamp in the same change rather than later:
the field alone would just produce NULLs.

Schema only, and there is nothing to backfill - the table is empty. Verified by this patch
rather than assumed, because a backfill-free claim that turns out to be wrong leaves rows no
clinic can see.
"""

from __future__ import annotations

import frappe

DOCTYPE = "Preventive Care Record"
MODULE_PATH = "preventive_care_record"
FIELDNAME = "branch"


def execute():
	if not frappe.db.exists("DocType", DOCTYPE):
		frappe.log_error(
			title="PREVENTIVE_CARE_RECORD_BRANCH_NO_DOCTYPE",
			message=f"{DOCTYPE} does not exist; run preventive_care_record_schema first.",
		)
		return

	frappe.reload_doc("pet_app", "doctype", MODULE_PATH, force=True)

	if not frappe.db.has_column(DOCTYPE, FIELDNAME):
		frappe.log_error(
			title="PREVENTIVE_CARE_RECORD_BRANCH_MISSING",
			message=f"{DOCTYPE}.{FIELDNAME} was not created by the doctype reload.",
		)
		return

	# Nothing to backfill is a claim, so it is checked. If rows somehow exist without a
	# branch, say so loudly - they would be visible to every clinic.
	orphans = frappe.db.sql(
		f"select count(*) from `tab{DOCTYPE}` where ifnull(`{FIELDNAME}`, '') = ''"
	)[0][0]
	if orphans:
		frappe.log_error(
			title="PREVENTIVE_CARE_RECORD_BRANCH_ORPHANS",
			message=(
				f"{orphans} {DOCTYPE} row(s) have no branch and are visible to every clinic. "
				"They pre-date this patch and need a branch set by hand."
			),
		)

	frappe.logger("pet_app.migrate").info(
		{"event": "PREVENTIVE_CARE_RECORD_BRANCH_READY", "doctype": DOCTYPE, "rows_without_branch": orphans}
	)
