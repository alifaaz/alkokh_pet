"""Give every active occupant of a roomed booking the room its booking holds.

`boarding_occupant_model` created the occupant rows but filled `service_room` only from the
booking's own field at the moment it ran. Two runtime paths then created active occupants
with a blank room: `_reserve_existing_pending_boarding`, which assigned a room to the
BOOKING and left the occupants untouched, and any row backfilled before check-in stamped
one. Stage 5 fixed both. This closes what they already left.

It matters because the occupant's room, not the booking's, is what every reader consults.
`room_occupancy` - and therefore `assert_room_available`, the hub's per-room count and the
roster - all read `Pet Boarding Occupant.service_room`. A blank one means the animal is in a
kennel that no row names: the pet is missing from its room's card and the room can read
Available with something asleep in it.

Deliberately conservative, and the restrictions are the point:

- **Active occupants only.** A Departed, Deceased or Cancelled pet keeps whatever room it
  actually left from. Filling one in now would invent a location for an animal that is gone.
- **Only bookings in ROOM_ASSIGNED_ACTIVE_BOARDING_STATUSES** ("Reserved", "Checked In"),
  and only `docstatus < 2`. This is the safety rule, not a tidiness one: `room_occupancy`
  filters on occupant status and docstatus but NOT on record_status, so stamping a room onto
  an active occupant of a Checked Out or Cancelled booking would make that room read
  occupied by an animal that went home - re-stranding exactly what
  `boarding_close_stranded_occupants` was written to free. A blank room is inert; a wrong
  one holds a kennel shut.
- **Only where the booking actually has a room.** A Pending Room booking has none, so there
  is nothing to copy and NULL -> NULL is not a write. Those occupants get their room from
  the assignment path when the room is finally assigned.
- **No billable row is touched, and none should be.** Rates resolve from the pet's
  `animal_type` and the occupant's boarding type and from nothing else - the room is not a
  pricing input - so no figure can move as a result of this patch. That is the property to
  re-assert if it is ever edited.
- **No room stint is opened.** Stints are append-only history and the moment each of these
  animals entered its room is not recorded anywhere. A gap in the history is honest; a
  stint stamped with a fabricated `from_datetime` would not be.

Idempotent: re-running finds nothing to fill.
"""

from __future__ import annotations

import frappe


OCCUPANT_DOCTYPE = "Pet Boarding Occupant"

# The booking states in which a room is genuinely held. Mirrors
# ROOM_ASSIGNED_ACTIVE_BOARDING_STATUSES in pet_boarding.py, restated locally so the patch
# keeps meaning what it meant on the day it ran even if that constant moves.
ROOM_HOLDING_STATUSES = ("Reserved", "Checked In")


def execute():
	if not frappe.db.exists("DocType", OCCUPANT_DOCTYPE) or not frappe.db.table_exists(OCCUPANT_DOCTYPE):
		return

	unroomed = frappe.db.sql(
		f"""
		SELECT o.name, o.pet, o.parent AS boarding, b.service_room, b.record_status
		FROM `tab{OCCUPANT_DOCTYPE}` o
		JOIN `tabPet Boarding` b ON b.name = o.parent
		WHERE o.parenttype = 'Pet Boarding'
		  AND o.status = 'Active'
		  AND IFNULL(o.service_room, '') = ''
		  AND IFNULL(b.service_room, '') != ''
		  AND b.record_status IN %(room_holding)s
		  AND b.docstatus < 2
		""",
		{"room_holding": ROOM_HOLDING_STATUSES},
		as_dict=True,
	)
	if not unroomed:
		return

	for row in unroomed:
		# set_value on the child row directly, for the same reason as
		# boarding_occupant_model and boarding_close_stranded_occupants: some of these
		# parents are submitted, and loading one to edit a child would demand
		# cancel-and-amend, which would ripple into the Sales Invoices hanging off it.
		# update_modified=False keeps the booking's own modified stamp honest - nothing
		# about the stay changed, only a column that should have been written at the time.
		frappe.db.set_value(
			OCCUPANT_DOCTYPE,
			row.name,
			"service_room",
			row.service_room,
			update_modified=False,
		)

	frappe.logger("pet_app.boarding").info(
		{
			"event": "BOARDING_OCCUPANT_ROOM_BACKFILLED",
			"count": len(unroomed),
			"bookings": sorted({row.boarding for row in unroomed}),
		}
	)
