# Copyright (c) 2026, solvers and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from pet_app.utils.care_plan_links import assert_no_active_plan_items_linked_to
from pet_app.utils.visit_billing import (
	BILLED_VISIT_LOCK_MESSAGE,
	STRICT_MODE,
	cancel_boarding_billable_item_by_link,
	cancel_visit_billable_item_by_link,
	get_care_service_doc,
	sync_clinical_record_billable_item,
)
from pet_app.workflows import clinical_state


class Imaging(Document):
	def validate(self):
		self._validate_plan_item_links_before_detach()
		if self.visit:
			self.set_values_from_visit()
		else:
			self.validate_source()
		self.set_values_from_care_service()
		clinical_state.validate_document_transition(self)

	def after_insert(self):
		# Visit-linked imaging auto-bills onto the Vet Visit. Source-linked
		# imaging (e.g. ordered from Pet Boarding) is billed by its own flow.
		if self.visit and self.status != "Cancelled":
			sync_clinical_record_billable_item(self, "Imaging")

	def on_update(self):
		if self.status == "Cancelled":
			self._cancel_linked_billable_item()
			return
		if self.visit:
			sync_clinical_record_billable_item(self, "Imaging")

	def _cancel_linked_billable_item(self):
		if self.visit:
			cancel_visit_billable_item_by_link(
				self.visit,
				linked_service_id=f"Imaging::{self.name}",
				linked_doctype="Imaging",
				linked_name=self.name,
				order_id=self.get("order_id"),
				item_type="Imaging",
			)
		elif self.source_doctype == "Pet Boarding":
			cancel_boarding_billable_item_by_link(
				self.source_name,
				linked_service_id=f"Imaging::{self.name}",
				linked_doctype="Imaging",
				linked_name=self.name,
				order_id=self.get("order_id"),
				item_type="Imaging",
			)

	def on_trash(self):
		assert_no_active_plan_items_linked_to(self.doctype, self.name, action="delete")

	def _validate_plan_item_links_before_detach(self):
		previous = self.get_doc_before_save()
		if previous and previous.get("visit") != self.visit:
			assert_no_active_plan_items_linked_to(self.doctype, self.name, action="move")
		if self.status == "Cancelled" and (not previous or previous.get("status") != "Cancelled"):
			assert_no_active_plan_items_linked_to(self.doctype, self.name, action="cancel")

	def validate_source(self):
		if not (self.source_doctype and self.source_name):
			frappe.throw(_("A Vet Visit or a source record is required."))
		if not frappe.db.exists(self.source_doctype, self.source_name):
			frappe.throw(
				_("Source {0} {1} was not found.").format(
					frappe.bold(self.source_doctype), frappe.bold(self.source_name)
				)
			)
		if not self.pet:
			frappe.throw(_("Pet is required."))

	def set_values_from_visit(self):
		if not self.visit:
			frappe.throw(_("Visit is required"))

		visit = frappe.db.get_value(
			"Vet Visit", self.visit, ["animal_patient", "doctor", "billed", "sales_invoice"], as_dict=True
		)
		if not visit:
			frappe.throw(_("Vet Visit {0} was not found.").format(frappe.bold(self.visit)))
		allow_cancel = self.status == "Cancelled" and self.flags.get("allow_billed_visit_cancellation")
		if visit.sales_invoice and not allow_cancel:
			frappe.throw(_(BILLED_VISIT_LOCK_MESSAGE))
		if visit.billed and not allow_cancel:
			frappe.throw(_(BILLED_VISIT_LOCK_MESSAGE))

		if not self.pet:
			self.pet = visit.animal_patient
		elif visit.animal_patient and self.pet != visit.animal_patient:
			frappe.throw(_("Imaging Pet must match the linked Vet Visit pet."))

		if not self.doctor and visit.doctor:
			self.doctor = visit.doctor
		elif STRICT_MODE and visit.doctor and self.doctor != visit.doctor:
			frappe.throw(_("Imaging practitioner must match the linked Vet Visit practitioner."))

	def set_values_from_care_service(self):
		if not self.care_service:
			frappe.throw(_("Care Service is required"))

		care_service = get_care_service_doc(self.care_service)
		if not care_service.get("item_code"):
			frappe.throw(_("Care Service {0} must have an Item Code.").format(frappe.bold(self.care_service)))
		if care_service.get("default_price") in (None, ""):
			frappe.throw(_("Care Service {0} must have a Default Price.").format(frappe.bold(self.care_service)))
		self.item_code = care_service.get("item_code")
		self.rate = flt(care_service.get("default_price"))

		if not self.status:
			self.status = "Ordered"
