# Copyright (c) 2026, solvers and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from pet_app.utils.visit_billing import (
	BILLED_VISIT_LOCK_MESSAGE,
	STRICT_MODE,
	sync_clinical_record_billable_item,
)


class Lab(Document):
	def validate(self):
		self.set_values_from_visit()
		self.set_values_from_care_service()

	def after_insert(self):
		sync_clinical_record_billable_item(self, "Lab")

	def on_update(self):
		sync_clinical_record_billable_item(self, "Lab")

	def set_values_from_visit(self):
		if not self.visit:
			frappe.throw(_("Visit is required"))

		visit = frappe.db.get_value(
			"Vet Visit", self.visit, ["animal_patient", "doctor", "billed", "sales_invoice"], as_dict=True
		)
		if not visit:
			frappe.throw(_("Vet Visit {0} was not found.").format(frappe.bold(self.visit)))
		if visit.sales_invoice:
			frappe.throw(_(BILLED_VISIT_LOCK_MESSAGE))
		if visit.billed:
			frappe.throw(_(BILLED_VISIT_LOCK_MESSAGE))

		if not self.pet:
			self.pet = visit.animal_patient
		elif visit.animal_patient and self.pet != visit.animal_patient:
			frappe.throw(_("Lab Pet must match the linked Vet Visit pet."))

		if not self.doctor and visit.doctor:
			self.doctor = visit.doctor
		elif STRICT_MODE and visit.doctor and self.doctor != visit.doctor:
			frappe.throw(_("Lab doctor must match the linked Vet Visit doctor."))

	def set_values_from_care_service(self):
		if not self.care_service:
			frappe.throw(_("Care Service is required"))

		care_service = frappe.get_cached_doc("CareService", self.care_service)
		if not care_service.get("item_code"):
			frappe.throw(_("Care Service {0} must have an Item Code.").format(frappe.bold(self.care_service)))
		if care_service.get("default_price") in (None, ""):
			frappe.throw(_("Care Service {0} must have a Default Price.").format(frappe.bold(self.care_service)))
		self.item_code = care_service.get("item_code")
		self.rate = flt(care_service.get("default_price"))

		if not self.status:
			self.status = "Pending"
