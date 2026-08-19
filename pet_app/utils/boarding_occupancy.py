"""Who is on a booking, which room each pet is in, and what that permits.

Three rules live here, and they are deliberately expressed as one:

    A room holds at most `max_pets_per_booking` ACTIVE occupants,
    and every one of them belongs to the same guardian.

At the capacity of 1 this collapses to exactly today's behaviour - one pet, one room -
which is why the setting is the gate. At 7 it becomes the settled model: a guardian's
pets share a room, two guardians never do.

Expressing it as one rule rather than two matters. Holding capacity at 1 does NOT by
itself stop two pets sharing a room: without the occupant count in the rule, a second
BOOKING by the same guardian could be reserved into the same room and the room would hold
two pets while pricing, death and departure still assume one. The count closes that door.

What is deliberately NOT here:

- Pricing. An occupant's nights are its own, but nothing in this module computes money.
- Departure and death. Closing an occupant is Stage 5; this module only describes state.
- The room invariant as a scheduled audit. `GET_LOCK` is advisory and session-scoped: it
  prevents races between callers that take it, and protects nothing against a direct SQL
  write or a second app server that forgets. `find_rooms_with_multiple_guardians` is the
  detector, meant to be run on a schedule, not the preventer.
"""

from __future__ import annotations

from contextlib import contextmanager
from math import ceil

import frappe
from frappe import _
from frappe.utils import cint, cstr, get_datetime, now_datetime


OCCUPANT_DOCTYPE = "Pet Boarding Occupant"
STINT_DOCTYPE = "Pet Boarding Room Stint"

# An occupant that is physically present. Departed/Deceased/Cancelled are not.
ACTIVE_OCCUPANT_STATUS = "Active"
OCCUPANT_STATUSES = ("Active", "Departed", "Deceased", "Cancelled")

# Bookings whose occupants can still be holding a room.
ROOM_HOLDING_DOCSTATUS = 2  # docstatus < this

DEFAULT_MAX_PETS_PER_BOOKING = 1
LOCK_TIMEOUT_SECONDS = 15


def get_max_pets_per_booking() -> int:
	"""Capacity, from settings. Never a constant - the owner moves this, not a developer."""
	try:
		configured = cint(frappe.db.get_single_value("Pet Boarding Settings", "max_pets_per_booking"))
	except Exception:
		configured = 0
	return configured if configured > 0 else DEFAULT_MAX_PETS_PER_BOOKING


@contextmanager
def boarding_lock(boarding_name: str):
	"""Covers the whole read-decide-write window on a booking's occupant set.

	Capacity is counted, compared and then acted on. Two staff adding a pet at the same
	moment both read six and both insert, giving eight, unless the count and the insert
	are inside the same lock. Mirrors _service_room_lock in api/healthcare/boarding.py;
	the key is the BOOKING because capacity is a property of the booking, not the room.
	"""
	yield from _advisory_lock(f"pet_boarding_occupants::{boarding_name}", _("Could not acquire booking lock. Please retry."))


@contextmanager
def room_locks(*service_rooms: str):
	"""Lock one or more rooms, always in sorted order.

	A transfer holds the source and the destination at once. Taking them in name order
	is what stops two simultaneous A->B and B->A transfers deadlocking until the timeout.
	"""
	ordered = sorted({cstr(room).strip() for room in service_rooms if cstr(room).strip()})
	if not ordered:
		yield
		return
	if len(ordered) == 1:
		yield from _advisory_lock(f"service_room_boarding::{ordered[0]}", _("Could not acquire room lock. Please retry."))
		return
	with room_locks(ordered[0]):
		with room_locks(*ordered[1:]):
			yield


@contextmanager
def pet_locks(*pets: str):
	"""Lock one or more pets, always in sorted order.

	The room lock does not cover this. Two simultaneous reservations of the SAME pet into
	DIFFERENT rooms contend on the pet, not on a room, so each takes a different room lock
	and both pass the "is this pet already boarding" check. Locking the pets closes that,
	and sorted order keeps a two-pet request from deadlocking against its mirror image.
	"""
	ordered = sorted({cstr(pet).strip() for pet in pets if cstr(pet).strip()})
	if not ordered:
		yield
		return
	if len(ordered) == 1:
		yield from _advisory_lock(f"pet_boarding_pet::{ordered[0]}", _("Could not acquire pet lock. Please retry."))
		return
	with pet_locks(ordered[0]):
		with pet_locks(*ordered[1:]):
			yield


def find_active_boarding_for_pet(pet: str, *, exclude_boarding: str | None = None) -> dict | None:
	"""The active booking this pet is already on, if any.

	Reads BOTH shapes on purpose. Post-migrate an occupant row is the truth; pre-migrate,
	and for any booking the backfill has not reached, the legacy `Pet Boarding.pet` field
	is. Checking only one would let a pet be reserved twice during the window between the
	code landing and migrate running.
	"""
	pet = cstr(pet).strip()
	if not pet:
		return None

	active = ("Pending Room", "Reserved", "Checked In")
	params = {"pet": pet, "cancelled": ROOM_HOLDING_DOCSTATUS, "active": active}
	exclude_sql = ""
	if exclude_boarding:
		exclude_sql = " AND b.name != %(exclude)s"
		params["exclude"] = exclude_boarding

	rows = frappe.db.sql(
		f"""
		SELECT b.name, b.record_status, b.guardian, b.service_room
		FROM `tabPet Boarding` b
		WHERE b.pet = %(pet)s AND b.record_status IN %(active)s
		  AND b.docstatus < %(cancelled)s{exclude_sql}
		LIMIT 1
		""",
		params,
		as_dict=True,
	)
	if rows:
		return rows[0]

	if not frappe.db.table_exists(OCCUPANT_DOCTYPE):
		return None
	rows = frappe.db.sql(
		f"""
		SELECT b.name, b.record_status, b.guardian, o.service_room
		FROM `tab{OCCUPANT_DOCTYPE}` o
		JOIN `tabPet Boarding` b ON b.name = o.parent
		WHERE o.parenttype = 'Pet Boarding' AND o.pet = %(pet)s
		  AND o.status = %(occ_active)s AND b.record_status IN %(active)s
		  AND b.docstatus < %(cancelled)s{exclude_sql}
		LIMIT 1
		""",
		{**params, "occ_active": ACTIVE_OCCUPANT_STATUS},
		as_dict=True,
	)
	return rows[0] if rows else None


def active_boardings_for_pets(pets) -> dict[str, dict]:
	"""`{pet: {name, record_status, guardian, service_room}}` for a whole list of pets.

	The batched form of `find_active_boarding_for_pet`, and deliberately built to give the
	same answer: both shapes are read, and the legacy `Pet Boarding.pet` row wins the
	tie-break, so a list and a detail read can never disagree about which booking a pet is
	on. Two queries whatever the list length, against two per pet resolved one at a time -
	measured at 76 queries for 40 visits, which is an N+1 a worklist should not pay.

	Pets with no active booking are simply absent from the result.
	"""
	wanted = {cstr(pet).strip() for pet in (pets or []) if cstr(pet).strip()}
	if not wanted:
		return {}

	active = ("Pending Room", "Reserved", "Checked In")
	params = {"pets": tuple(wanted), "cancelled": ROOM_HOLDING_DOCSTATUS, "active": active}

	found: dict[str, dict] = {}
	if frappe.db.table_exists(OCCUPANT_DOCTYPE):
		for row in frappe.db.sql(
			f"""
			SELECT o.pet AS pet, b.name, b.record_status, b.guardian, o.service_room
			FROM `tab{OCCUPANT_DOCTYPE}` o
			JOIN `tabPet Boarding` b ON b.name = o.parent
			WHERE o.parenttype = 'Pet Boarding' AND o.pet IN %(pets)s
			  AND o.status = %(occ_active)s AND b.record_status IN %(active)s
			  AND b.docstatus < %(cancelled)s
			""",
			{**params, "occ_active": ACTIVE_OCCUPANT_STATUS},
			as_dict=True,
		):
			found.setdefault(row.pop("pet"), row)

	# Written second and unconditionally overwriting: legacy wins, as it does in the
	# single-pet function.
	for row in frappe.db.sql(
		"""
		SELECT b.pet AS pet, b.name, b.record_status, b.guardian, b.service_room
		FROM `tabPet Boarding` b
		WHERE b.pet IN %(pets)s AND b.record_status IN %(active)s
		  AND b.docstatus < %(cancelled)s
		""",
		params,
		as_dict=True,
	):
		found[row.pop("pet")] = row

	return found


def _advisory_lock(lock_name: str, failure_message: str):
	acquired = False
	try:
		result = frappe.db.sql("SELECT GET_LOCK(%s, %s)", (lock_name, LOCK_TIMEOUT_SECONDS))
		acquired = bool(result and result[0] and cint(result[0][0]) == 1)
		if not acquired:
			frappe.throw(failure_message)
		yield
	finally:
		if acquired:
			frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_name,))


def stay_duration(start, end) -> tuple[int, int]:
	"""(hours, nights) between two moments. THE definition of a night, in one place.

	Both the booking-level display figure and each occupant's billed nights call this, so
	they cannot drift apart. A night is 24 hours from arrival, rounded up, minimum one -
	which is a commercial policy sitting in code, and is on the audit list to move into
	settings. It is written once here so that move is a one-function change.
	"""
	elapsed_seconds = max((get_datetime(end) - get_datetime(start)).total_seconds(), 0)
	hours = max(ceil(elapsed_seconds / 3600), 1)
	return hours, max(ceil(hours / 24), 1)


def billable_occupants(boarding) -> list:
	"""Occupants that owe nights.

	Everything except Cancelled. A cancelled occupant never arrived; Departed and Deceased
	did, and owe the nights they were here. Today every occupant on an open booking is
	Active, so this is the same set - but it is written for the world Stage 5 creates,
	where a pet can leave while the booking runs on.
	"""
	return [row for row in (boarding.get("occupants") or []) if cstr(row.status).strip() != "Cancelled"]


def occupant_nights(occupant, *, reserve_placeholder: int = 1) -> int:
	"""Nights this occupant owes.

	`joined_at` empty means the room is reserved and the animal has not arrived. That is
	not zero nights and it is not "now minus nothing" - it is a placeholder, stated here
	rather than falling out of `flt(0 or 1)` somewhere downstream, and it is replaced by
	the real figure at check-out.
	"""
	joined_at = occupant.get("joined_at")
	if not joined_at:
		return reserve_placeholder
	_hours, nights = stay_duration(joined_at, occupant.get("departed_at") or now_datetime())
	return nights


def active_occupants(boarding) -> list:
	"""Occupant rows on an in-memory Pet Boarding doc that are physically present."""
	return [row for row in (boarding.get("occupants") or []) if cstr(row.status) == ACTIVE_OCCUPANT_STATUS]


def active_occupant_pets(boarding) -> set[str]:
	return {cstr(row.pet).strip() for row in active_occupants(boarding) if cstr(row.pet).strip()}


def is_occupant(boarding, pet: str, *, active_only: bool = False) -> bool:
	"""Whether a pet is on this booking.

	`active_only=False` by default, and that is the useful default for clinical work: an
	order or a result may legitimately be recorded against a pet that has since gone home.
	Blocking on the departure window would deadlock the front desk for no safety gain.
	"""
	pet = cstr(pet).strip()
	if not pet:
		return False
	rows = active_occupants(boarding) if active_only else (boarding.get("occupants") or [])
	return any(cstr(row.pet).strip() == pet for row in rows)


def pet_is_on_boarding(boarding, pet: str, *, active_only: bool = True) -> bool:
	"""Is this pet on this booking? THE membership test - both shapes, one answer.

	Every gate that asks this question must come through here, because the answer lives in
	two places and always has:

	  - The occupant table, which is the truth for any booking that has rows.
	  - `Pet Boarding.pet`, the legacy single-pet scalar, which is the ONLY membership
	    record a booking with no occupant rows has.

	Both halves are load-bearing today, and skipping either is a real outage rather than a
	tidiness point. Reading only the scalar refused every occupant but one on a multi-pet
	stay - the roster showed three animals and two of them could not be ordered for.
	Reading only the table refuses EVERYTHING on a booking that has no rows yet, and those
	exist: `start_visit_boarding` creates a Pending Room stay with a `pet` and no
	occupants, and this site has a Checked In booking in exactly that state right now.

	`active_only=True` by default, matching `add_occupant`, `depart_occupant` and
	`transfer_occupant_room`, so every gate agrees. The scalar branch cannot honour it -
	there is no per-pet status to read - which is correct: a booking with no occupant rows
	has no departures to exclude.
	"""
	pet = cstr(pet).strip()
	if not pet:
		return False
	if boarding.get("occupants"):
		return is_occupant(boarding, pet, active_only=active_only)
	return pet == cstr(boarding.get("pet")).strip()


def boarding_pets(boarding, *, active_only: bool = True) -> list[str]:
	"""Every pet on this booking, across both shapes. The list form of `pet_is_on_boarding`."""
	rows = boarding.get("occupants") or []
	if rows:
		source = active_occupants(boarding) if active_only else rows
		return sorted({cstr(row.pet).strip() for row in source if cstr(row.pet).strip()})
	scalar = cstr(boarding.get("pet")).strip()
	return [scalar] if scalar else []


def assert_capacity(boarding, *, adding: int = 0):
	"""Refuse a booking that would hold more active pets than the setting allows."""
	limit = get_max_pets_per_booking()
	count = len(active_occupants(boarding)) + max(cint(adding), 0)
	if count > limit:
		frappe.throw(
			_("This booking would hold {0} pets. The limit is {1}.").format(count, limit)
		)


def room_occupancy(service_room: str, *, exclude_boarding: str | None = None) -> list[dict]:
	"""Active occupants physically in a room right now, with their guardian.

	Reads occupants, not stints: `occupant.service_room` is the current room and stints
	are history. Excludes cancelled bookings via docstatus, and the caller's own booking
	when it is being re-validated.
	"""
	service_room = cstr(service_room).strip()
	if not service_room:
		return []
	conditions = ["o.service_room = %(room)s", "o.status = %(active)s", "b.docstatus < %(cancelled)s"]
	values = {"room": service_room, "active": ACTIVE_OCCUPANT_STATUS, "cancelled": ROOM_HOLDING_DOCSTATUS}
	if exclude_boarding:
		conditions.append("b.name != %(exclude)s")
		values["exclude"] = exclude_boarding
	return frappe.db.sql(
		f"""
		SELECT b.name AS boarding, b.guardian, o.pet
		FROM `tab{OCCUPANT_DOCTYPE}` o
		JOIN `tabPet Boarding` b ON b.name = o.parent
		WHERE o.parenttype = 'Pet Boarding' AND {" AND ".join(conditions)}
		""",
		values,
		as_dict=True,
	)


def assert_room_available(service_room: str, guardian: str, *, exclude_boarding: str | None = None, adding: int = 1):
	"""The one rule: one guardian per room, and no more pets in it than capacity allows.

	Call inside room_locks(service_room). Outside the lock this is a suggestion.
	"""
	service_room = cstr(service_room).strip()
	if not service_room:
		return

	occupants = room_occupancy(service_room, exclude_boarding=exclude_boarding)
	other_guardians = {cstr(row["guardian"]).strip() for row in occupants} - {cstr(guardian).strip()}
	if other_guardians:
		frappe.throw(
			_("Service Room {0} is occupied by another guardian ({1}).").format(
				frappe.bold(service_room), frappe.bold(", ".join(sorted(other_guardians)))
			)
		)

	# This limit is `max_pets_per_booking`, and on a room check that is a known conflation,
	# left standing on purpose until enforcement is decided. Read the next paragraph before
	# touching it.
	#
	# DO NOT COLLAPSE THESE TWO NUMBERS. `Service Room.capacity` and
	# `Pet Boarding Settings.max_pets_per_booking` both read 7 today and they are not the
	# same quantity:
	#
	#   max_pets_per_booking  a POLICY about one BOOKING - how many pets one guardian may
	#                         put on one reservation. One value for the whole facility.
	#   Service Room.capacity a FACT about one ROOM - how many animals physically fit in
	#                         it. One value per room, and the owner edits it per room.
	#
	# They agree at 7 by coincidence: the owner set a booking policy of 7, and separately
	# chose 7 as the starting capacity for every room before editing them down. Two
	# unrelated decisions landing on the same digit. The moment a Kennel is set to 2 - which
	# is the entire point of the field - they diverge, and this check starts permitting
	# seven animals into a room the hub is drawing as `5 / 2`.
	#
	# When capacity is enforced, the limit here becomes min(max_pets_per_booking, capacity)
	# and THIS is the line that changes. Deleting either number in favour of the other
	# reintroduces the bug the field was added to fix.
	limit = get_max_pets_per_booking()
	total = len(occupants) + max(cint(adding), 0)
	if total > limit:
		frappe.throw(
			_("Service Room {0} would hold {1} pets. The limit is {2}.").format(
				frappe.bold(service_room), total, limit
			)
		)


def room_is_free(service_room: str, *, exclude_boarding: str | None = None) -> bool:
	"""A room is free when NO PET REMAINS IN IT - not when a booking ends.

	One pet leaving a shared room frees nothing; the last one leaving frees it.
	"""
	return not room_occupancy(service_room, exclude_boarding=exclude_boarding)


def find_rooms_with_multiple_guardians() -> list[dict]:
	"""The invariant as a data query. Empty is the only acceptable result.

	Meant for CI and a scheduled check. The application-level assertion cannot see a
	violation it did not itself cause; this can.
	"""
	return frappe.db.sql(
		f"""
		SELECT o.service_room, COUNT(DISTINCT b.guardian) AS guardians, COUNT(*) AS pets
		FROM `tab{OCCUPANT_DOCTYPE}` o
		JOIN `tabPet Boarding` b ON b.name = o.parent
		WHERE o.parenttype = 'Pet Boarding'
		  AND o.status = %(active)s
		  AND b.docstatus < %(cancelled)s
		  AND IFNULL(o.service_room, '') != ''
		GROUP BY o.service_room
		HAVING guardians > 1
		""",
		{"active": ACTIVE_OCCUPANT_STATUS, "cancelled": ROOM_HOLDING_DOCSTATUS},
		as_dict=True,
	)


CLOSED_OCCUPANT_STATUSES = ("Departed", "Deceased", "Cancelled")


def active_occupant_count(boarding) -> int:
	return len(active_occupants(boarding))


def find_occupant(boarding, pet: str, *, active_only: bool = False):
	"""The occupant row for this pet, or None."""
	pet = cstr(pet).strip()
	if not pet:
		return None
	rows = active_occupants(boarding) if active_only else (boarding.get("occupants") or [])
	for row in rows:
		if cstr(row.pet).strip() == pet:
			return row
	return None


def is_last_active_occupant(boarding, pet: str) -> bool:
	"""Whether closing this pet would empty the booking.

	The question every Stage 5 path asks first, because the answer changes what the
	operation IS. One of several pets leaving is an occupant event and touches nothing on
	the booking; the last one leaving is a booking event, and pretending otherwise leaves
	a room held by nobody with its stay length still accruing against `now`.
	"""
	pet = cstr(pet).strip()
	active = active_occupant_pets(boarding)
	return bool(pet) and active == {pet}


def close_occupant(boarding, pet: str, *, status: str, when=None, note: str | None = None, death_record: str | None = None, row=None):
	"""Close one occupant IN MEMORY. The caller saves.

	Stamps `departed_at`, which is what makes this occupant's room-stay row price to its
	own nights rather than to the booking's: `occupant_nights` reads departed_at and falls
	back to `now` only while it is empty. So a pet that went home on Tuesday stops
	accruing on Tuesday even though the booking runs to Friday.

	Idempotent - an already-closed occupant is returned untouched rather than re-stamped,
	so a retried request cannot move a departure time that has already been billed.

	Selects the ACTIVE row, not the first one. A pet that left and was re-added has several
	rows, and picking the first match found the DEPARTED one, hit the idempotency guard
	below, and returned it untouched - so the caller was handed a truthy row, reported
	success, and changed nothing. On BRD-00103 an operator pressed depart three times
	against a row that was already closed while the open one stayed Active, and the stint
	got closed each time because that call sits outside this guard.

	`row` lets a caller pass the occupant it has ALREADY selected and validated, so
	validation and mutation act on the same object and cannot drift apart again. That is
	the real fix; preferring the active row is the safety net for callers that pass a pet.
	"""
	if status not in CLOSED_OCCUPANT_STATUSES:
		frappe.throw(_("{0} is not a closing occupant status.").format(status))

	if row is None:
		# Active first. The plain lookup is the fallback so a genuinely already-closed
		# occupant still returns untouched rather than None, which is what makes a retried
		# request idempotent instead of an error.
		row = find_occupant(boarding, pet, active_only=True) or find_occupant(boarding, pet)
	if row is None:
		return None
	if cstr(row.status).strip() in CLOSED_OCCUPANT_STATUSES:
		return row

	row.status = status
	row.departed_at = when or now_datetime()
	if note and row.meta.has_field("departure_note"):
		existing = cstr(row.get("departure_note")).strip()
		row.departure_note = f"{existing}\n{note}".strip() if existing else note
	if death_record and row.meta.has_field("death_record"):
		row.death_record = death_record
	return row


def close_all_active_occupants(boarding, *, when=None, status: str = "Departed", note: str | None = None) -> list:
	"""Close every remaining occupant, for a BOOKING-level end.

	Check-out and cancellation call this. Without it the occupant rows stay Active after
	the booking closes, and because `room_occupancy` reads occupant status rather than
	booking status, the room stays occupied forever - the booking is finished and the room
	can never be reserved by another guardian again.

	That is not hypothetical: the backfill patch set 35 historical occupants to Departed,
	and every booking checked out since has left its occupants Active because nothing at
	runtime closed them.
	"""
	when = when or now_datetime()
	closed = []
	for row in list(active_occupants(boarding)):
		result = close_occupant(boarding, cstr(row.pet).strip(), status=status, when=when, note=note)
		if result is not None:
			closed.append(result)
	return closed


def open_stint(boarding: str, pet: str, service_room: str, from_datetime, *, reason: str | None = None):
	"""Start a room stint. Append-only: nothing here ever edits an existing row."""
	if not cstr(service_room).strip():
		return None
	doc = frappe.get_doc(
		{
			"doctype": STINT_DOCTYPE,
			"boarding": boarding,
			"pet": pet,
			"service_room": service_room,
			"from_datetime": from_datetime,
			"moved_by": frappe.session.user,
			"reason": reason,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc


def close_open_stint(boarding: str, pet: str, to_datetime):
	"""Close whichever stint is still open for this pet on this booking."""
	names = frappe.get_all(
		STINT_DOCTYPE,
		filters={"boarding": boarding, "pet": pet, "to_datetime": ["is", "not set"]},
		pluck="name",
	)
	for name in names:
		frappe.db.set_value(STINT_DOCTYPE, name, "to_datetime", to_datetime, update_modified=False)
	return names
