from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class PetMedicalProfile(Document):
	def before_insert(self):
		self._set_defaults()

	def validate(self):
		self._set_defaults()
		self._validate_one_profile_per_pet()

	def _set_defaults(self):
		if not self.pet:
			frappe.throw(_("Pet is required."))
		if not self.primary_guardian:
			self.primary_guardian = get_primary_guardian(self.pet)
		if self.meta.has_field("customer") and not self.customer and self.primary_guardian:
			self.customer = frappe.db.get_value("Guardian", self.primary_guardian, "customer_id")
		if self.active is None:
			self.active = 1
		if self.meta.has_field("profile_status") and not self.profile_status:
			self.profile_status = "Active"
		if self.meta.has_field("current_clinical_status") and not self.current_clinical_status:
			self.current_clinical_status = "No Active Case"
		if self.meta.has_field("current_case_status") and not self.current_case_status:
			self.current_case_status = "No Active Case"
		if self.meta.has_field("treatment_plan_status") and not self.treatment_plan_status:
			self.treatment_plan_status = "No Active Plan"

	def _validate_one_profile_per_pet(self):
		existing = frappe.db.get_value(
			"Pet Medical Profile",
			{"pet": self.pet, "name": ["!=", self.name]},
			"name",
		)
		if existing:
			frappe.throw(
				_("Pet {0} already has Medical Profile {1}.").format(
					frappe.bold(self.pet), frappe.bold(existing)
				)
			)


def get_primary_guardian(pet: str) -> str | None:
	guardian = frappe.db.get_value("PetGuardian", {"pet_id": pet, "role": "primary_owner"}, "guardian_id")
	if guardian:
		return guardian
	return frappe.db.get_value("PetGuardian", {"pet_id": pet}, "guardian_id")


def ensure_pet_medical_profile(pet: str, guardian: str | None = None, patient: str | None = None) -> str:
	if not pet:
		frappe.throw(_("Pet is required."))
	existing = frappe.db.get_value("Pet Medical Profile", {"pet": pet}, "name")
	if existing:
		doc = frappe.get_doc("Pet Medical Profile", existing)
		changed = False
		if guardian and not doc.primary_guardian:
			doc.primary_guardian = guardian
			changed = True
		if doc.meta.has_field("customer") and guardian and not doc.customer:
			doc.customer = frappe.db.get_value("Guardian", guardian, "customer_id")
			changed = True
		if changed:
			doc.save(ignore_permissions=True)
		return doc.name

	doc = frappe.get_doc(
		{
			"doctype": "Pet Medical Profile",
			"pet": pet,
			"primary_guardian": guardian,
			"customer": frappe.db.get_value("Guardian", guardian, "customer_id") if guardian else None,
			"active": 1,
			"profile_status": "Active",
			"current_clinical_status": "No Active Case",
			"current_case_status": "No Active Case",
			"treatment_plan_status": "No Active Plan",
			"last_synced_at": now_datetime(),
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name
