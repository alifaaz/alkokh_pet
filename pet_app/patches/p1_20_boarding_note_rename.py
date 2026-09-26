"""Carry the one orphaned ``checkout_notes`` value over to ``check_out_note``.

Two Pet Boarding fields were renamed in the desk UI on 2026-08-31 - ``boarding_note``
to ``check_in_note`` and ``checkout_notes`` to ``check_out_note``. Frappe adds the new
columns on the DocType save and never drops the old ones, so the site ended up holding
all four: two populated columns no longer reachable from the meta, and two empty ones
that the desk correctly showed as blank. The rename itself is not this patch's business
- it ships in pet_boarding.json - and neither is the Python side, which now names the
new fields. This patch only moves the data the rename stranded.

Only ``check_out_note`` is backfilled, and only one row
------------------------------------------------------
``checkout_notes`` holds a single value on this site: BRD-00042's death-closure note,
written by boarding_death_cascade._death_checkout_note. That is a genuine check-out
note and it is the only thing in either orphaned column that is not recoverable from
somewhere else, so it moves.

``check_in_note`` is deliberately NOT backfilled from ``boarding_note``
----------------------------------------------------------------------
``boarding_note`` was never independent data. _start_visit_boarding_atomic wrote the
same string to ``boarding_note`` and to ``note`` on the same insert, and the site bears
that out exactly: of the 39 rows holding a ``boarding_note``, zero have an empty
``note`` and zero differ from it. Nothing is at risk, so there is nothing to rescue.

Copying it into ``check_in_note`` would also be wrong on the merits rather than merely
unnecessary. That text is what the operator typed when the stay was BOOKED; the field
it would land in is rendered to staff under "Check In Notes" and describes the animal's
arrival. Backfilling would put a reservation note against 39 arrivals that never
recorded one, which is worse than the blank the desk shows today - a blank is honest.

Both orphaned columns are left in place. Frappe does not drop columns and neither does
this patch: after the code change nothing references them, and they are the only record
of what the pre-rename site held.

Idempotent: guarded on the source column still existing, and it only writes rows whose
destination is still empty, so a second run selects nothing.
"""

from __future__ import annotations

import frappe

DOCTYPE = "Pet Boarding"


def execute():
	# A site installed after the rename never had the old column. Nothing to move.
	if not frappe.db.has_column(DOCTYPE, "checkout_notes"):
		print("Pet Boarding.checkout_notes does not exist; nothing to migrate.")
		return

	if not frappe.db.has_column(DOCTYPE, "check_out_note"):
		frappe.throw(
			"Pet Boarding.check_out_note does not exist. This patch runs post_model_sync, "
			"so the renamed field should already be there - check that pet_boarding.json "
			"carries it."
		)

	pending = frappe.db.sql(
		"""
		SELECT name
		FROM `tabPet Boarding`
		WHERE ifnull(checkout_notes, '') != ''
		  AND ifnull(check_out_note, '') = ''
		""",
		pluck="name",
	)

	if not pending:
		print("Orphaned checkout_notes: none pending; already migrated.")
		return

	frappe.db.sql(
		"""
		UPDATE `tabPet Boarding`
		SET check_out_note = checkout_notes
		WHERE ifnull(checkout_notes, '') != ''
		  AND ifnull(check_out_note, '') = ''
		"""
	)

	print(f"Migrated checkout_notes -> check_out_note on {len(pending)} row(s): {', '.join(pending)}")
