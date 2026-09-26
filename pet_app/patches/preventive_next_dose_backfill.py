"""Raise the next dose for doses already administered before the rule existed.

`api.preventive_care._create_next_dose` runs at administration. Every dose administered
BEFORE it existed carries its `next_due_date` and has no record for it, which is the state
the change was made to end: the date drives the reminder and the compliance standing, and
the worklist shows nothing owed. This raises the missing ones, once.

USES THE SAME PLANNER THE LIVE PATH USES, rather than a second copy of the rules. Both
guards therefore apply here unchanged - a dose that already spawned a child is skipped, and
two doses of one protocol due the same day raise one recall between them - so running this
twice raises nothing the second time and it is safe to leave in the patch list.

DELIBERATELY NOT A FIX-UP OF EVERY OLD ROW. It skips:

  * Cancelled doses - a dose not given owes no next one, the rule the reminder sweep already
    applies.
  * History (`kind` alone, no catalogue row) - a child built from one would have no Item Code
    and could never be administered. The date still reminds off the parent.
  * Anything whose child would land on a slot an open dose already holds.

Failures are per-record and non-fatal, for the reason the live path gives: this is a
convenience being backfilled, and one unresolvable pet must not stop the other rows or the
migration.
"""

from __future__ import annotations

import frappe

from pet_app.api.preventive_care import DOCTYPE, _create_next_dose


def execute():
	if not frappe.db.exists("DocType", DOCTYPE):
		return

	names = frappe.get_all(
		DOCTYPE,
		filters={
			"status": "Administered",
			"next_due_date": ["is", "set"],
		},
		pluck="name",
		ignore_permissions=True,
	)

	created, skipped = [], 0
	for name in names:
		record = frappe.get_doc(DOCTYPE, name)
		child = _create_next_dose(record)
		if child:
			created.append((name, child))
		else:
			skipped += 1

	frappe.logger("pet_app.migrate").info(
		{
			"event": "PREVENTIVE_NEXT_DOSE_BACKFILL",
			"considered": len(names),
			"created": created,
			"skipped": skipped,
		}
	)
