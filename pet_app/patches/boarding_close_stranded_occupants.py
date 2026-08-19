"""Close occupants left Active on bookings that already ended.

`boarding_occupant_model` set every historical occupant's status from its booking, so the
site was consistent the day it ran. Nothing at runtime kept it that way: `check_out_boarding`
and `_cancel_boarding_atomic` closed the booking and left the occupant rows Active. Five
rows are in that state today and every future check-out would have added another.

It matters because `room_occupancy` - and therefore `assert_room_available`, which gates
every reservation - reads OCCUPANT status, not booking status. An Active occupant on a
Checked Out booking is, to that query, an animal still in the kennel. The room is then
refused to any other guardian forever, with an error naming a guardian whose pet went home
weeks ago. At a capacity of 1 it also consumed the room's only slot.

Stage 5 fixed the two runtime paths. This closes what they already stranded.

Deliberately conservative:

- Only bookings in a terminal record_status are touched. An Active occupant on an open
  booking is an animal that is actually there.
- `departed_at` is filled from the booking's own check_out, falling back to its
  modification time, so a closed occupant carries a plausible departure rather than
  `now` - these stays ended weeks ago and stamping today would make the nights wrong if
  anything ever re-reads them.
- The status mirrors `boarding_occupant_model`: a checked-out booking's pet Departed, a
  cancelled booking's pet Cancelled, because a cancelled stay never happened.
- Open room stints on those bookings are closed to the same moment, so the stint history
  does not keep a stay open either.
- No billable row is touched. These bookings are invoiced and settled; recomputing nights
  now would move figures on documents the customer has already been given.

Idempotent: re-running finds nothing to close.
"""

from __future__ import annotations

import frappe


OCCUPANT_DOCTYPE = "Pet Boarding Occupant"
STINT_DOCTYPE = "Pet Boarding Room Stint"

# Terminal booking status -> the status its remaining occupants should carry.
TERMINAL_STATUS_MAP = {
	"Checked Out": "Departed",
	"Cancelled": "Cancelled",
}


def execute():
	if not frappe.db.exists("DocType", OCCUPANT_DOCTYPE) or not frappe.db.table_exists(OCCUPANT_DOCTYPE):
		return

	stranded = frappe.db.sql(
		f"""
		SELECT o.name, o.pet, o.parent AS boarding, b.record_status, b.check_out, b.modified
		FROM `tab{OCCUPANT_DOCTYPE}` o
		JOIN `tabPet Boarding` b ON b.name = o.parent
		WHERE o.parenttype = 'Pet Boarding'
		  AND o.status = 'Active'
		  AND b.record_status IN %(terminal)s
		""",
		{"terminal": tuple(TERMINAL_STATUS_MAP)},
		as_dict=True,
	)
	if not stranded:
		return

	stints_exist = frappe.db.table_exists(STINT_DOCTYPE)

	for row in stranded:
		closed_at = row.check_out or row.modified
		# set_value on the child table directly: thirty of these bookings are submitted,
		# and loading the parent to edit a child would demand cancel-and-amend, which
		# would ripple into the Sales Invoices hanging off them. Same reasoning, and the
		# same technique, as boarding_occupant_model.
		frappe.db.set_value(
			OCCUPANT_DOCTYPE,
			row.name,
			{"status": TERMINAL_STATUS_MAP[row.record_status], "departed_at": closed_at},
			update_modified=False,
		)

		if stints_exist and row.pet:
			open_stints = frappe.get_all(
				STINT_DOCTYPE,
				filters={"boarding": row.boarding, "pet": row.pet, "to_datetime": ["is", "not set"]},
				pluck="name",
			)
			for stint in open_stints:
				frappe.db.set_value(STINT_DOCTYPE, stint, "to_datetime", closed_at, update_modified=False)

	frappe.logger("pet_app.boarding").info(
		{
			"event": "BOARDING_STRANDED_OCCUPANTS_CLOSED",
			"count": len(stranded),
			"bookings": sorted({row.boarding for row in stranded}),
		}
	)
