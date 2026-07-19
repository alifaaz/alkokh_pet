from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr, getdate, nowdate

from pet_app.utils.care_plan_links import plan_requires_link, validate_plan_item_link_target


class PetCarePlanItem(Document):
	def before_insert(self):
		self._set_defaults()

	def validate(self):
		self._set_defaults()
		self._validate_identity()
		self._validate_dates()
		self._validate_linked_target()

	def _set_defaults(self):
		if not self.status:
			self.status = "Planned"
		if not self.priority:
			self.priority = "Normal"
		if not self.plan_type:
			self.plan_type = "Other"
		if self.source_visit and not self.source_doctype:
			self.source_doctype = "Vet Visit"
			self.source_name = self.source_visit
		if self.source_visit:
			visit = frappe.db.get_value(
				"Vet Visit",
				self.source_visit,
				["animal_patient", "guardian", "customer", "doctor", "care_episode"],
				as_dict=True,
			)
			if visit:
				self.pet = self.pet or visit.animal_patient
				self.guardian = self.guardian or visit.guardian
				self.customer = self.customer or visit.customer
				self.doctor = self.doctor or visit.doctor
				self.care_episode = self.care_episode or visit.get("care_episode")
		if self.care_episode and not (self.pet and self.guardian):
			episode = frappe.db.get_value("Pet Care Episode", self.care_episode, ["pet", "guardian", "customer", "primary_doctor"], as_dict=True)
			if episode:
				self.pet = self.pet or episode.pet
				self.guardian = self.guardian or episode.guardian
				self.customer = self.customer or episode.customer
				self.doctor = self.doctor or episode.primary_doctor
		if not self.start_date:
			self.start_date = getdate(nowdate())
		if not self.title:
			self.title = self.plan_type

	def _validate_identity(self):
		if not self.pet:
			frappe.throw(_("Pet is required."))
		if self.guardian and not frappe.db.exists("PetGuardian", {"guardian_id": self.guardian, "pet_id": self.pet}):
			frappe.throw(_("Pet {0} is not linked to Guardian {1}.").format(frappe.bold(self.pet), frappe.bold(self.guardian)))
		if self.plan_type == "Follow-up Visit" and not cint(self.requires_appointment):
			frappe.throw(_("Follow-up Visit plan items require an appointment."))

	def _validate_dates(self):
		if self.end_date and self.start_date and getdate(self.end_date) < getdate(self.start_date):
			frappe.throw(_("End Date cannot be before Start Date."))

	def _validate_linked_target(self):
		require_link = False
		if plan_requires_link(self.plan_type):
			if self.is_new():
				require_link = True
			else:
				previous = self.get_doc_before_save()
				if previous:
					previous_had_link = bool(previous.get("linked_doctype") or previous.get("linked_name"))
					plan_type_changed = cstr(previous.get("plan_type")).strip() != cstr(self.plan_type).strip()
					require_link = previous_had_link or plan_type_changed

		validate_plan_item_link_target(self, require_link=require_link)
