"""Move the two existing "when" values onto `scheduled_datetime`.

Deliberately narrow, and the narrowness is the whole point.

`Pet Procedure.scheduled_at` is a real schedule and moves wholesale: four rows, every one
of them sitting exactly on an hour boundary, which is what a person choosing a time looks
like. `scheduled_at` is left in place and is still written alongside for now - retiring a
column the client still sends is a second change, not this one.

`PetCareService.due_date` is NOT a schedule and is mostly NOT moved. It is required, it
defaults to today on all four of its writers, and 2,900 of 2,931 live rows hold exactly
their own creation date. Copying all of them would manufacture 2,900 schedules nobody
chose, and then nothing could tell a scheduled order from an unscheduled one - which is
the single thing this field exists to make possible. Only rows whose `due_date` differs
from their creation date carry a decision, and only those move: 31 of them.

Those 31 land at 00:00:00, and that is honest rather than convenient. A Date has no time,
so there is none to migrate. Midnight is chosen over the 09:00 the care-plan code uses as
its default precisely because nobody books a procedure at midnight: it reads as
"date known, time not", where 09:00 would read as a real morning appointment.
"""

import frappe


def execute():
	_migrate_procedure_scheduled_at()
	_migrate_care_service_due_date()


def _migrate_procedure_scheduled_at():
	if not _has_columns("Pet Procedure", "scheduled_at", "scheduled_datetime"):
		return
	moved = frappe.db.sql(
		"""
		UPDATE `tabPet Procedure`
		SET scheduled_datetime = scheduled_at
		WHERE scheduled_at IS NOT NULL AND scheduled_datetime IS NULL
		"""
	)
	frappe.logger().info("converge_scheduled_datetime: Pet Procedure rows updated=%s" % (moved,))


def _migrate_care_service_due_date():
	if not _has_columns("PetCareService", "due_date", "scheduled_datetime"):
		return
	# `DATE(creation)` is the default any of the four writers would have produced, so a
	# due_date equal to it carries no decision and is left alone.
	frappe.db.sql(
		"""
		UPDATE `tabPetCareService`
		SET scheduled_datetime = TIMESTAMP(due_date, '00:00:00')
		WHERE due_date IS NOT NULL
		  AND scheduled_datetime IS NULL
		  AND due_date <> DATE(creation)
		"""
	)


def _has_columns(doctype: str, *fieldnames: str) -> bool:
	# `table_exists` takes the DOCTYPE, not the table name - passing "tabX" returns False
	# and turns this whole guard into a silent no-op, which is how the first run of this
	# patch migrated nothing and still logged as applied.
	if not frappe.db.table_exists(doctype):
		return False
	table = "tab%s" % doctype
	existing = {
		row[0]
		for row in frappe.db.sql(
			"""
			SELECT column_name FROM information_schema.columns
			WHERE table_schema = DATABASE() AND table_name = %s
			""",
			table,
		)
	}
	return all(f in existing for f in fieldnames)
