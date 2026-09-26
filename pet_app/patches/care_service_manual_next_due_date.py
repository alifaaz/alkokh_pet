"""The next dose date is a date somebody chooses, not a date the template computes.

Completing a Vaccination or Deworming service used to read `CareService template.frequency`
- annul/annual 12 months, biannual 6, quarterly 3, monthly 1 - and stamp
`administered_on + N months` onto the clinical record. One rule for every pet on that
template. The owner's decision is that the next dose belongs to this pet's service: a puppy
on a primary course and an adult on an annual booster share the template and do not share a
schedule, and a rule that answers for both answers wrongly for one of them.

So `PetCareService` gains `next_due_date`, the operator sets it on the service, and
utils/care_service_clinical_record copies it verbatim onto the record. The calculation is
deleted, not demoted to a fallback or a suggestion - a computed date sitting in the field
is indistinguishable from a chosen one, and the operator would have no way to tell whether
a date in front of them is a decision or a guess. No date set means no date written and no
reminder, and that is accepted.

`frequency` IS NOT TOUCHED. The column stays on `CareService template` with its rows
intact; this only stops one code path reading it. Removing a stored field is a separate
decision and nothing here forecloses it.

THE FOUR STALE ROWS. Four `Pet Vaccination Record`s carry dates this calculation produced
(VAC-00001 and VAC-00002 on 2027-09-06, VAC-00003 and VAC-00004 on 2027-09-07 - all
administered_on + 12 months off `annul`). They are cleared, because a date nobody chose is
exactly what this change is removing, and leaving them would fire four reminders on a
schedule the clinic never set.

They are identified by two independent predicates that must BOTH hold, never by a name list
and never by the date value:

  1. `notes` carry this app's provenance marker for a PetCareService. Only
     `write_clinical_record` emits that string, so the row was written by the calculation
     rather than entered by a human.
  2. The row has never been saved since it was inserted - `modified == creation` and zero
     `Version` rows. Any date in the field is therefore still the one the writer put there.

Predicate 2 is what protects an operator: the moment somebody opens one of these records
and sets a date by hand, it stops matching and this patch leaves it alone. Re-deriving the
date instead (administered_on + the template's frequency) was rejected because it cannot be
done for VAC-00003 at all - its source service, PetCareService-03514, has since been
deleted, so there is no template left to read a frequency off.

`update_modified=False` on the clear, deliberately. Stamping these rows as modified would
make them look operator-touched, which is the exact signal predicate 2 reads, and would
misreport who decided the field should be empty. The clear is recorded here and in the log
instead.
"""

from __future__ import annotations

import frappe

RECORD_DOCTYPES = ("Pet Vaccination Record", "Pet Deworming Record")

MARKER_LIKE = "%[alkokh-source-group:PetCareService:%"


def execute():
	_add_service_field()
	_clear_calculated_dates()


def _add_service_field():
	"""`PetCareService.next_due_date`, declared in petcareservice.json."""
	if not frappe.db.exists("DocType", "PetCareService"):
		return

	frappe.reload_doc("pet_app", "doctype", "petcareservice", force=True)

	if not frappe.db.has_column("PetCareService", "next_due_date"):
		frappe.log_error(
			title="CARE_SERVICE_MANUAL_NEXT_DUE_DATE_FIELD_MISSING",
			message="PetCareService is missing next_due_date after the doctype reload.",
		)


def _clear_calculated_dates():
	cleared = []

	for doctype in RECORD_DOCTYPES:
		if not frappe.db.exists("DocType", doctype):
			continue

		for row in frappe.get_all(
			doctype,
			filters={"next_due_date": ["is", "set"], "notes": ["like", MARKER_LIKE]},
			fields=["name", "creation", "modified", "next_due_date"],
			limit_page_length=0,
			ignore_permissions=True,
		):
			# Predicate 2. `modified == creation` and no Version row: nobody has saved this
			# record since the writer inserted it, so the date is still the computed one.
			if row.modified != row.creation:
				continue
			if frappe.db.count("Version", {"ref_doctype": doctype, "docname": row.name}):
				continue

			frappe.db.set_value(doctype, row.name, "next_due_date", None, update_modified=False)
			cleared.append(f"{doctype} {row.name}: {row.next_due_date} -> None")

	if cleared:
		frappe.log_error(
			title="CARE_SERVICE_MANUAL_NEXT_DUE_DATE_CLEARED",
			message="Calculated next due dates cleared:\n" + "\n".join(cleared),
		)
