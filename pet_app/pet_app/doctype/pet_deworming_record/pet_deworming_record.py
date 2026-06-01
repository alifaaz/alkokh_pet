from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, nowdate


class PetDewormingRecord(Document):
	def validate(self):
		self._set_identity_from_visit()
		if not self.pet:
			frappe.throw(_("Pet is required."))
		if not self.medication_name:
			frappe.throw(_("Medication Name is required."))
		if not self.administered_on:
			self.administered_on = nowdate()
		if self.next_due_date and getdate(self.next_due_date) < getdate(self.administered_on):
			frappe.throw(_("Next Due Date cannot be before Administered On."))

	def _set_identity_from_visit(self):
		if not self.visit:
			return
		visit = frappe.db.get_value("Vet Visit", self.visit, ["animal_patient", "guardian", "doctor"], as_dict=True)
		if not visit:
			frappe.throw(_("Vet Visit {0} was not found.").format(frappe.bold(self.visit)))
		self.pet = self.pet or visit.animal_patient
		self.guardian = self.guardian or visit.guardian
		self.doctor = self.doctor or visit.doctor

