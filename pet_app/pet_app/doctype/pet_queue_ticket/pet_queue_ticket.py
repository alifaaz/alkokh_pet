from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, now_datetime


ACTIVE_STATUSES = {"Checked In", "Waiting", "Called", "In Consultation"}
TERMINAL_STATUSES = {"Completed", "No Show", "Cancelled"}


class PetQueueTicket(Document):
	def before_insert(self):
		self._set_defaults()
		self._set_ticket_no()

	def validate(self):
		self._set_defaults()
		self._validate_identity()
		self._validate_active_uniqueness()

	def _set_defaults(self):
		if not self.queue_date:
			self.queue_date = getdate()
		if not self.status:
			self.status = "Checked In"
		if self.status == "Checked In" and not self.checked_in_at:
			self.checked_in_at = now_datetime()

	def _set_ticket_no(self):
		if self.ticket_no:
			return
		date_token = getdate(self.queue_date).strftime("%Y%m%d")
		count = frappe.db.count("Pet Queue Ticket", {"queue_date": self.queue_date}) + 1
		while True:
			ticket_no = f"Q-{date_token}-{count:03d}"
			if not frappe.db.exists("Pet Queue Ticket", {"ticket_no": ticket_no}):
				self.ticket_no = ticket_no
				return
			count += 1

	def _validate_identity(self):
		if self.appointment and not frappe.db.exists("Appointment", self.appointment):
			frappe.throw(_("Appointment {0} was not found.").format(frappe.bold(self.appointment)))
		if self.case_sheet and not frappe.db.exists("Vet Case Sheet", self.case_sheet):
			frappe.throw(_("Case Sheet {0} was not found.").format(frappe.bold(self.case_sheet)))
		if self.visit and not frappe.db.exists("Vet Visit", self.visit):
			frappe.throw(_("Vet Visit {0} was not found.").format(frappe.bold(self.visit)))
		if self.guardian and self.pet and not frappe.db.exists(
			"PetGuardian", {"guardian_id": self.guardian, "pet_id": self.pet}
		):
			frappe.throw(
				_("Pet {0} is not linked to Guardian {1}.").format(
					frappe.bold(self.pet), frappe.bold(self.guardian)
				)
			)

	def _validate_active_uniqueness(self):
		if self.status in TERMINAL_STATUSES:
			return
		for fieldname in ("appointment", "visit"):
			value = self.get(fieldname)
			if not value:
				continue
			existing = frappe.db.get_value(
				"Pet Queue Ticket",
				{
					fieldname: value,
					"status": ["in", list(ACTIVE_STATUSES)],
					"name": ["!=", self.name],
				},
				"name",
			)
			if existing:
				frappe.throw(
					_("Active Queue Ticket {0} already exists for {1} {2}.").format(
						frappe.bold(existing),
						frappe.bold(self.meta.get_label(fieldname) or fieldname),
						frappe.bold(value),
					)
				)
