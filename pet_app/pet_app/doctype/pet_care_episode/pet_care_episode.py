from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, nowdate

from pet_app.utils.case_assignment import ensure_episode_practitioner


ACTIVE_EPISODE_STATUSES = {
	"Open",
	"Under Diagnosis",
	"Pending Diagnostics",
	"Under Treatment",
	"Monitoring",
	"Follow-up Scheduled",
	"Follow-up Due",
	"Referred",
}


class PetCareEpisode(Document):
	def before_insert(self):
		self._set_defaults()
		self._ensure_primary_doctor_team_member()

	def validate(self):
		self._set_defaults()
		self._validate_identity()
		self._validate_single_primary_active_episode()

	def _set_defaults(self):
		if not self.pet:
			frappe.throw(_("Pet is required."))
		if not self.guardian:
			self.guardian = frappe.db.get_value("PetGuardian", {"pet_id": self.pet, "role": "primary_owner"}, "guardian_id") or frappe.db.get_value("PetGuardian", {"pet_id": self.pet}, "guardian_id")
		if not self.customer and self.guardian:
			self.customer = frappe.db.get_value("Guardian", self.guardian, "customer_id")
		if not self.episode_status:
			self.episode_status = "Open"
		if not self.episode_type:
			self.episode_type = "General Wellness"
		if not self.priority:
			self.priority = "Normal"
		if not self.severity:
			self.severity = "Mild"
		if not self.started_on:
			self.started_on = getdate(nowdate())
		if not self.episode_title:
			self.episode_title = self.chief_complaint or _("Active Case")
		if not self.created_by:
			self.created_by = frappe.session.user

	def _ensure_primary_doctor_team_member(self):
		if self.primary_doctor:
			ensure_episode_practitioner(self, self.primary_doctor)

	def _validate_identity(self):
		if self.guardian and self.pet and not frappe.db.exists("PetGuardian", {"guardian_id": self.guardian, "pet_id": self.pet}):
			frappe.throw(_("Pet {0} is not linked to Guardian {1}.").format(frappe.bold(self.pet), frappe.bold(self.guardian)))

	def _validate_single_primary_active_episode(self):
		if self.episode_status not in ACTIVE_EPISODE_STATUSES:
			return
		existing = frappe.db.get_value(
			"Pet Care Episode",
			{"pet": self.pet, "episode_status": ["in", list(ACTIVE_EPISODE_STATUSES)], "name": ["!=", self.name]},
			"name",
		)
		if existing:
			frappe.throw(_("This pet already has an open case ({0}). Close it before opening a new one.").format(frappe.bold(existing)))
