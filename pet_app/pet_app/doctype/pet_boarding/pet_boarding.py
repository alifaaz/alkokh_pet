# Copyright (c) 2026, solvers and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from math import ceil

from frappe.utils import flt, get_datetime, now_datetime

from pet_app.utils.guardian_customer import get_or_create_customer_from_guardian


PENDING_ROOM_STATUS = "Pending Room"
ROOM_ASSIGNED_ACTIVE_BOARDING_STATUSES = ("Reserved", "Checked In")
ACTIVE_BOARDING_STATUSES = (PENDING_ROOM_STATUS, *ROOM_ASSIGNED_ACTIVE_BOARDING_STATUSES)
CLOSED_BOARDING_STATUSES = ("Checked Out", "Cancelled")


class PetBoarding(Document):
	def before_insert(self):
		self._set_defaults()

	def validate(self):
		self._set_defaults()
		self._validate_room()
		self._validate_pet_guardian_link()
		self._validate_status_consistency()
		self._validate_single_active_room_boarding()
		self._apply_billable_item_amounts()
		self._compute_stay_days()
		self._compute_totals()

	def before_submit(self):
		if self.record_status != "Checked Out" or self.status != "Closed":
			frappe.throw(_("Only checked-out boarding records can be submitted."))
		if not self.sales_invoice:
			frappe.throw(_("Sales Invoice is required before submitting Pet Boarding."))

	def _set_defaults(self):
		if not self.record_status:
			self.record_status = "Reserved"
		if not self.status:
			self.status = "Open"
		if not self.billing_status:
			self.billing_status = "Unbilled"
		if not self.boarding_type:
			self.boarding_type = _get_default_boarding_type()
		if not self.reserved_at:
			self.reserved_at = now_datetime()
		if self.guardian and not self.customer:
			self.customer = get_or_create_customer_from_guardian(self.guardian)
		self.workflow_state = _workflow_state_for_record_status(self.record_status)

	def _validate_room(self):
		if not self.service_room:
			if self.record_status not in (PENDING_ROOM_STATUS, "Cancelled"):
				frappe.throw(_("Service Room is required unless boarding is Pending Room or Cancelled."))
			return

		room = frappe.db.get_value("Service Room", self.service_room, ["name", "status"], as_dict=True)
		if not room:
			frappe.throw(_("Service Room {0} was not found.").format(frappe.bold(self.service_room)))
		if room.status != "Active":
			frappe.throw(_("Service Room {0} is inactive.").format(frappe.bold(self.service_room)))

	def _validate_pet_guardian_link(self):
		if not self.pet or not self.guardian:
			return

		if not frappe.db.exists("Pet", self.pet):
			frappe.throw(_("Pet {0} was not found.").format(frappe.bold(self.pet)))
		if not frappe.db.exists("Guardian", self.guardian):
			frappe.throw(_("Guardian {0} was not found.").format(frappe.bold(self.guardian)))

		if not frappe.db.exists("PetGuardian", {"pet_id": self.pet, "guardian_id": self.guardian}):
			frappe.throw(
				_("Pet {0} is not linked to Guardian {1}.").format(
					frappe.bold(self.pet), frappe.bold(self.guardian)
				)
			)

	def _validate_status_consistency(self):
		if self.record_status in ACTIVE_BOARDING_STATUSES:
			self.status = "Open"
			return

		if self.record_status == "Checked Out":
			self.status = "Closed"
			return

		if self.record_status == "Cancelled":
			self.status = "Cancelled"
			return

		frappe.throw(_("Invalid boarding record status {0}.").format(self.record_status))

	def _validate_single_active_room_boarding(self):
		"""One guardian per room, and no more pets in it than capacity allows.

		This replaced "one active booking per room". The old rule cannot survive the
		occupant model: a guardian's pets share a room, and a booking mid-transfer holds
		two. The new rule is weaker in one direction and stricter in another, and at a
		capacity of 1 the two are equivalent - which is what makes the swap safe to ship
		before per-occupant pricing and departure exist.
		"""
		if self.record_status not in ROOM_ASSIGNED_ACTIVE_BOARDING_STATUSES or not self.service_room:
			return

		# Until `bench migrate` creates the occupant table, the new rule has nothing to
		# read. Falling through to the old check rather than to "no opinion": an absent
		# table must not be the reason two guardians end up in one room. This branch dies
		# with the legacy `pet` field in Stage 6.
		if not frappe.db.table_exists("Pet Boarding Occupant"):
			self._validate_legacy_single_active_room_boarding()
			return

		from pet_app.utils.boarding_occupancy import assert_room_available

		# Occupants of THIS booking are excluded and counted separately: on a new booking
		# they are not yet persisted, so they cannot be read back from the room.
		assert_room_available(
			self.service_room,
			self.guardian,
			exclude_boarding=self.name,
			adding=self._pets_entering_room(),
		)

	def _validate_legacy_single_active_room_boarding(self):
		existing = get_active_boarding_for_room(self.service_room, exclude_name=self.name)
		if existing:
			frappe.throw(
				_("Service Room {0} already has active boarding {1} ({2}).").format(
					frappe.bold(self.service_room),
					frappe.bold(existing.name),
					frappe.bold(existing.record_status),
				)
			)

	def _pets_entering_room(self) -> int:
		"""How many of this booking's pets will be in `service_room`.

		Falls back to 1 while occupants are unpopulated, which is every record until the
		backfill patch runs and every new booking until reserve_room writes its rows.
		"""
		occupants = [
			row
			for row in (self.get("occupants") or [])
			if row.status == "Active" and (not row.service_room or row.service_room == self.service_room)
		]
		return len(occupants) or 1

	def _apply_billable_item_amounts(self):
		for row in self.billable_items or []:
			row.qty = flt(row.qty or 1)
			row.rate = flt(row.rate or 0)
			row.amount = row.qty * row.rate
			if row.item_code and not row.item_name:
				row.item_name = frappe.db.get_value("Item", row.item_code, "item_name")
			if not row.status:
				row.status = "Billable"
			if row.item_type == "Medication" and row.meta.has_field("dispense_status") and not row.get("dispense_status"):
				row.dispense_status = "Pending Dispense"

	def _compute_stay_days(self):
		self._compute_stay_duration()

	def _compute_stay_duration(self):
		if not self.check_in:
			self.stay_days = 0
			self.stay_hours = 0
			return

		# Shared with each occupant's billed nights so the two definitions cannot drift.
		# This figure is now DISPLAY ONLY - ten readers want a booking-level stay length -
		# and no longer feeds any price.
		from pet_app.utils.boarding_occupancy import stay_duration

		stay_hours, stay_days = stay_duration(self.check_in, self.check_out or now_datetime())
		self.stay_hours = stay_hours
		self.stay_days = stay_days

	# Charges that never reach an invoice. Included is a medication absorbed by the medical
	# boarding rate: real, given, priced - and not owed.
	NON_INVOICED_STATUSES = ("Cancelled", "Included")

	def _compute_totals(self):
		total = 0
		for row in self.billable_items or []:
			if row.status in self.NON_INVOICED_STATUSES:
				continue
			total += flt(row.amount)

		self.total_cost = total
		self.balance = total - flt(self.deposit or 0)


def get_active_boarding_for_room(service_room: str, exclude_name: str | None = None):
	if not service_room:
		return None

	filters = {
		"service_room": service_room,
		"record_status": ["in", ROOM_ASSIGNED_ACTIVE_BOARDING_STATUSES],
		"docstatus": ["<", 2],
	}
	if exclude_name:
		filters["name"] = ["!=", exclude_name]

	rows = frappe.get_all(
		"Pet Boarding",
		filters=filters,
		fields=["name", "record_status", "service_room", "pet", "guardian", "customer"],
		order_by="modified desc",
		limit_page_length=1,
	)
	return rows[0] if rows else None


def _get_default_boarding_type() -> str:
	try:
		return frappe.db.get_single_value("Pet Boarding Settings", "default_boarding_type") or "Travel"
	except Exception:
		return "Travel"


def _workflow_state_for_record_status(record_status: str) -> str:
	if record_status == "Checked Out":
		return "Closed"
	if record_status == "Cancelled":
		return "Cancelled"
	return record_status or "Reserved"
