"""Set every Service Room's capacity to 7, as data on the row.

The owner's decision: every room starts at 7 and is then edited down to whatever each one
physically holds. This is a DATA change - the number lives in `Service Room.capacity`, one
value per row, and every room is editable afterwards.

Deliberately NOT a default. `Service Room.capacity` keeps its DocType default of 1, which is
the safe floor for a room created later by someone who has not thought about it. And 7 is
not hardcoded anywhere in the reading path: `_serialize_room` publishes whatever the column
says, so changing a room to 3 in the Desk changes the hub immediately with no deploy.

`INITIAL_CAPACITY` sits here, in the patch, rather than in the API: it is the value this
one-time initialisation writes, not a constant the application consults. Nothing reads it
after this runs.

Every room is written, unconditionally
--------------------------------------
An earlier draft skipped rooms that already held a positive capacity, to protect a number
the owner had set by hand. That guard cannot work on the run that matters, and finding out
why is the reason this note exists: `Service Room.capacity` carries a DocType default of 1,
and when Frappe creates the column it backfills that default into every existing row. So by
the time this patch reads the table, all 37 rooms hold 1 - not 0 - and a
"skip anything already positive" rule skips all of them and writes nothing, while still
reporting success.

There is no value to protect on a column that has just been created, so the write is
unconditional. Once-only is enforced where it belongs - the Patch Log, which runs a
registered patch exactly once - rather than by a data condition that cannot tell the
field default apart from a decision.

The DocType default stays 1 deliberately. It is the safe floor for a room created later by
someone who has not thought about capacity, and it is not this number.

Inactive rooms are included. There are two, and a room that is reactivated later with a
capacity nobody set would be the same bug arriving on a day nobody is looking for it.

Not enforced
------------
This writes a number the hub displays. Nothing refuses a pet for exceeding it:
`assert_room_available` still gates on `max_pets_per_booking`. Capacity and that setting
both read 7 today, which is a coincidence of this decision and not a relationship - see the
note in that function.

Idempotent: re-running finds nothing unset.
"""

from __future__ import annotations

import frappe


# What this initialisation writes. Not read by the application - the row is the truth.
INITIAL_CAPACITY = 7


def execute():
	if not frappe.db.table_exists("Service Room"):
		return

	# Sync the DocType before reading the column it introduced. Without this the patch is
	# ordering-dependent: it works when the DocType sync happens to run first in the same
	# migrate and silently no-ops when it does not, which is the failure mode where a patch
	# reports success and writes nothing. One doctype, not a migrate.
	frappe.reload_doc("pet_app", "doctype", "service_room")

	if not frappe.db.has_column("Service Room", "capacity"):
		return

	rooms = frappe.db.sql("SELECT name, status FROM `tabService Room`", as_dict=True)
	if not rooms:
		return

	for room in rooms:
		# set_value on the row: Service Room has no validate worth running for this, and
		# update_modified=False keeps `modified` honest - nothing about these rooms changed
		# except a column that had never been filled in.
		frappe.db.set_value("Service Room", room.name, "capacity", INITIAL_CAPACITY, update_modified=False)

	frappe.logger("pet_app.boarding").info(
		{
			"event": "SERVICE_ROOM_CAPACITY_INITIALISED",
			"capacity": INITIAL_CAPACITY,
			"count": len(rooms),
			"active": sum(1 for r in rooms if r.status == "Active"),
			"inactive": sum(1 for r in rooms if r.status != "Active"),
		}
	)
