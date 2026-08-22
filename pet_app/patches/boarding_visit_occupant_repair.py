"""Give every booking that `start_visit_boarding` created before the fix its occupant row.

`_start_visit_boarding_atomic` set the legacy `Pet Boarding.pet` scalar and inserted, with
no occupant row - the only booking-creation path in the app that did. Six bookings reached
that state after `boarding_occupant_model` had already run and moved on:

    BRD-00104  Cancelled   PET-02307   (no room)
    BRD-00105  Checked In  PET-02307   B1   - a real 50,000 deposit sitting on it
    BRD-00107  Cancelled   PET-00129   (no room)
    BRD-00108  Cancelled   PET-00129   (no room)
    BRD-00116  Checked In  PET-02360   A6
    BRD-00117  Checked In  PET-02071   A2

Three of them hold a kennel that reads Available on the hub, because `room_occupancy` and
the hub's per-room count both read the OCCUPANT's room and there was no occupant to read.

WHY THIS IS RAW SQL AND NOT `doc.save()`. Not the reason `boarding_occupant_model` had -
none of these is submitted. Three of them are LIVE Checked In stays, and `validate()`
recomputes `stay_days`/`stay_hours` against `now` on any open stay, so an ORM save would
silently re-date three occupied kennels as a side effect of adding a child row. It would
also abort the whole run on any unrelated validation the parent happens to fail today. The
row is the repair; the parent is not being changed and must not be.

WHAT IS DELIBERATELY NOT DONE HERE:

- **No pet is guessed.** A booking with no `pet`, or a `pet` that is not a Pet record, is
  SKIPPED and named in the log. A wrong animal on a stay is worse than an empty one - it
  is the only outcome here that cannot be spotted later by looking at the roster.
- **Status is not derived from `check_out`.** Same rule `boarding_occupant_model` set:
  a cancelled booking's pet never departed, it never arrived. `Cancelled` is its own
  occupant status so that a stay that was called off is not recorded as a stay that ended -
  and so that "booked and cancelled" stays distinguishable from "no occupant row at all",
  which is what these six looked like and is a different fact entirely.
- **No arrival time is invented.** `joined_at` is the booking's own `check_in` or nothing.
  `reserved_at` is when the room was held, not when the animal walked in.
- **No stints.** `Pet Boarding Room Stint` rows are room HISTORY; nothing reads them for
  occupancy (`room_occupancy` reads occupants, by design) and `close_open_stint` no-ops
  when there is no open stint, so check-out is unaffected. These six have no stint history
  and this patch does not manufacture one.

Idempotent: a booking that already has any occupant row is skipped, so a re-run is a no-op.
Written to be general rather than to name the six, so it also catches anything created
between this landing and the workers being restarted.
"""

from __future__ import annotations

import frappe
from frappe.utils import cstr


OCCUPANT_DOCTYPE = "Pet Boarding Occupant"

# record_status on the booking -> status on its reconstructed occupant.
# Same map as boarding_occupant_model, deliberately: two patches disagreeing about what a
# cancelled booking's pet is would be worse than either rule on its own.
STATUS_MAP = {
	"Pending Room": "Active",
	"Reserved": "Active",
	"Checked In": "Active",
	"Checked Out": "Departed",
	"Cancelled": "Cancelled",
}


def execute():
	if not frappe.db.exists("DocType", OCCUPANT_DOCTYPE):
		return

	bookings = frappe.db.sql(
		"""
		SELECT b.name, b.pet, b.guardian, b.service_room, b.boarding_type,
		       b.record_status, b.check_in, b.check_out, b.visit
		FROM `tabPet Boarding` b
		WHERE NOT EXISTS (
			SELECT 1 FROM `tabPet Boarding Occupant` o
			WHERE o.parent = b.name AND o.parenttype = 'Pet Boarding'
		)
		ORDER BY b.creation
		""",
		as_dict=True,
	)

	repaired: list[str] = []
	skipped: list[dict] = []

	for row in bookings:
		pet = cstr(row.pet).strip()
		if not pet:
			skipped.append({"boarding": row.name, "reason": "no pet on the booking"})
			continue
		if not frappe.db.exists("Pet", pet):
			skipped.append({"boarding": row.name, "reason": f"pet {pet} is not a Pet record"})
			continue

		status = STATUS_MAP.get(cstr(row.record_status).strip(), "Active")
		# The booking's room goes on the row whatever the status, matching
		# `boarding_occupant_model`. A non-Active row does NOT hold the kennel - every
		# reader that counts occupancy (`room_occupancy`, `_get_room_occupancy_state`,
		# `assert_room_available`, `room_is_free`) filters `status = 'Active'` first - so
		# there is no stranded-room risk here, and stripping it would throw away the one
		# record of which kennel a cancelled stay had been going to use.
		service_room = row.service_room

		frappe.db.sql(
			f"""
			INSERT INTO `tab{OCCUPANT_DOCTYPE}`
				(name, creation, modified, modified_by, owner, docstatus,
				 parent, parentfield, parenttype, idx,
				 pet, status, boarding_type, service_room, joined_at, departed_at)
			VALUES
				(%(name)s, %(now)s, %(now)s, %(user)s, %(user)s, 0,
				 %(parent)s, 'occupants', 'Pet Boarding', 1,
				 %(pet)s, %(status)s, %(boarding_type)s, %(service_room)s, %(joined_at)s, %(departed_at)s)
			""",
			{
				"name": frappe.generate_hash(length=10),
				"now": frappe.utils.now(),
				"user": "Administrator",
				"parent": row.name,
				"pet": pet,
				"status": status,
				"boarding_type": cstr(row.boarding_type).strip() or "Travel",
				"service_room": service_room or None,
				"joined_at": row.check_in or None,
				"departed_at": row.check_out or None,
			},
		)
		repaired.append(row.name)

		# Said on the record, not just in a log line. Someone reading this stay in a month
		# needs to know the roster was reconstructed rather than entered at the counter.
		frappe.get_doc("Pet Boarding", row.name).add_comment(
			"Comment",
			"Occupant row reconstructed by boarding_visit_occupant_repair: "
			f"{pet} ({status})"
			+ (f" in {service_room}" if service_room else "")
			+ ". The booking was created without one by start_visit_boarding.",
		)

	frappe.db.commit()
	frappe.logger("pet_app.boarding").info(
		{
			"event": "BOARDING_VISIT_OCCUPANT_REPAIRED",
			"repaired": repaired,
			"repaired_count": len(repaired),
			"skipped": skipped,
			"skipped_count": len(skipped),
		}
	)
	return {"repaired": repaired, "skipped": skipped}
