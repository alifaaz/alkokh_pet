"""Give every existing Pet Boarding one occupant row, one room stint, and pet-stamped charges.

Every record on this site genuinely IS a one-pet booking, so the conversion is mechanical
and has no ambiguous cases: `pet` -> the occupant, `check_in`/`check_out` -> its arrival
and departure, `service_room` -> its room, `boarding_type` -> its type.

Two things make this more than a loop.

1. THIRTY BOOKINGS ARE SUBMITTED (docstatus = 1). Child rows cannot be appended to a
   submitted document through the ORM without cancel-and-amend, and amending would ripple
   into the thirty Sales Invoices hanging off them. So occupant rows are INSERTed directly
   into the child table, with parent/parenttype/parentfield/idx/name set by hand. That is
   the only way to add occupancy to history without rewriting the history.

2. STATUS IS NOT DERIVED FROM check_out. A cancelled booking's pet never departed - it
   never arrived. `Cancelled` is its own occupant status precisely so a booking that did
   not happen is not recorded as a stay that ended.

Idempotent throughout: a booking that already has an occupant is skipped, a stint that
already exists is not duplicated, and a billable row that already names a pet is left
alone.
"""

from __future__ import annotations

import frappe
from frappe.utils import cstr


OCCUPANT_DOCTYPE = "Pet Boarding Occupant"
STINT_DOCTYPE = "Pet Boarding Room Stint"

# record_status on the booking -> status on its single occupant
STATUS_MAP = {
	"Pending Room": "Active",
	"Reserved": "Active",
	"Checked In": "Active",
	"Checked Out": "Departed",
	"Cancelled": "Cancelled",
}


def execute():
	if not frappe.db.exists("DocType", OCCUPANT_DOCTYPE):
		# The doctype ships as JSON on disk; if migrate has not synced it yet there is
		# nothing to fill. Bail rather than half-run.
		return

	created_occupants = _backfill_occupants()
	created_stints = _backfill_stints()
	stamped_rows = _backfill_billable_pets()
	frappe.db.commit()
	frappe.logger("pet_app.boarding").info(
		{
			"event": "BOARDING_OCCUPANT_MODEL_BACKFILLED",
			"occupants": created_occupants,
			"stints": created_stints,
			"billable_rows": stamped_rows,
		}
	)


def _backfill_occupants() -> int:
	bookings = frappe.db.sql(
		"""
		SELECT b.name, b.pet, b.guardian, b.service_room, b.boarding_type,
		       b.record_status, b.check_in, b.check_out, b.reserved_at, b.modified
		FROM `tabPet Boarding` b
		WHERE NOT EXISTS (
			SELECT 1 FROM `tabPet Boarding Occupant` o
			WHERE o.parent = b.name AND o.parenttype = 'Pet Boarding'
		)
		""",
		as_dict=True,
	)
	created = 0
	for row in bookings:
		pet = cstr(row.pet).strip()
		if not pet:
			# Never seen on this site (zero null pets), but a booking with no pet has no
			# occupant to create and guessing one would invent an animal.
			continue
		status = STATUS_MAP.get(cstr(row.record_status).strip(), "Active")
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
				"service_room": row.service_room or None,
				# A stay that never started has no arrival. reserved_at is when the room
				# was held, not when the animal walked in, so it is not a substitute.
				"joined_at": row.check_in or None,
				"departed_at": row.check_out or None,
			},
		)
		created += 1
	return created


def _backfill_stints() -> int:
	"""One stint per occupant that has a room. Bookings without a room get none."""
	rows = frappe.db.sql(
		f"""
		SELECT b.name AS boarding, o.pet, o.service_room, o.joined_at, o.departed_at,
		       b.reserved_at, b.creation
		FROM `tab{OCCUPANT_DOCTYPE}` o
		JOIN `tabPet Boarding` b ON b.name = o.parent
		WHERE o.parenttype = 'Pet Boarding'
		  AND IFNULL(o.service_room, '') != ''
		  AND NOT EXISTS (
			SELECT 1 FROM `tab{STINT_DOCTYPE}` s
			WHERE s.boarding = b.name AND s.pet = o.pet AND s.service_room = o.service_room
		  )
		""",
		as_dict=True,
	)
	created = 0
	for row in rows:
		# The room was held from reservation even when the pet arrived later; falling back
		# to creation keeps from_datetime non-null, which the field requires.
		frappe.get_doc(
			{
				"doctype": STINT_DOCTYPE,
				"boarding": row.boarding,
				"pet": row.pet,
				"service_room": row.service_room,
				"from_datetime": row.joined_at or row.reserved_at or row.creation,
				"to_datetime": row.departed_at or None,
				"moved_by": "Administrator",
				"reason": "Backfilled from the single-room booking this stay was created as.",
			}
		).insert(ignore_permissions=True)
		created += 1
	return created


def _backfill_billable_pets() -> int:
	"""Stamp every boarding charge with the booking's pet.

	Scoped to parenttype = 'Pet Boarding' on purpose: Pet Billable Item is also a child of
	Vet Visit, and those rows are not this patch's business.
	"""
	if not frappe.db.has_column("Pet Billable Item", "pet"):
		return 0
	pending = frappe.db.sql(
		"""
		SELECT COUNT(*) FROM `tabPet Billable Item` i
		JOIN `tabPet Boarding` b ON b.name = i.parent
		WHERE i.parenttype = 'Pet Boarding'
		  AND IFNULL(i.pet, '') = '' AND IFNULL(b.pet, '') != ''
		"""
	)[0][0]
	if not pending:
		return 0
	frappe.db.sql(
		"""
		UPDATE `tabPet Billable Item` i
		JOIN `tabPet Boarding` b ON b.name = i.parent
		SET i.pet = b.pet
		WHERE i.parenttype = 'Pet Boarding'
		  AND IFNULL(i.pet, '') = ''
		  AND IFNULL(b.pet, '') != ''
		"""
	)
	return pending
