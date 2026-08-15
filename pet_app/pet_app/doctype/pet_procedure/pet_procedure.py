# Copyright (c) 2026, solvers and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr, flt

from pet_app.utils.branch import snapshot_performing_branch
from pet_app.utils.care_plan_links import assert_no_active_plan_items_linked_to
from pet_app.utils.visit_billing import (
	BILLED_VISIT_LOCK_MESSAGE,
	STRICT_MODE,
	cancel_visit_billable_item_by_link,
	get_care_service_doc,
	sync_clinical_record_billable_item,
)
from pet_app.workflows import clinical_state


class PetProcedure(Document):
	def before_insert(self):
		self.set_values_from_visit()
		self.set_values_from_template()
		self.set_values_from_care_service()
		self.copy_checklist_from_template()

	def validate(self):
		self._validate_plan_item_links_before_detach()
		self.set_values_from_visit()
		self.set_values_from_template()
		self.set_values_from_care_service()
		clinical_state.validate_document_transition(self)
		self.validate_completion_requirements()

	def after_insert(self):
		if self.status != "Cancelled":
			sync_clinical_record_billable_item(self, "Procedure")

	def on_update(self):
		if self.flags.get("syncing_procedure_visit_side_effects") or not self.visit:
			return
		self.flags.syncing_procedure_visit_side_effects = True
		try:
			visit = frappe.get_doc("Vet Visit", self.visit)
			if self.status == "Cancelled":
				cancel_visit_billable_item_by_link(
					self.visit,
					linked_service_id=f"Pet Procedure::{self.name}",
					linked_doctype="Pet Procedure",
					linked_name=self.name,
					order_id=self.get("order_id"),
					item_type="Procedure",
					visit=visit,
					save=False,
				)
				self._sync_visit_order_row("Cancelled", visit=visit, save=False)
			else:
				sync_clinical_record_billable_item(self, "Procedure", visit=visit, save=False)
				self._sync_visit_order_row(self._visit_order_status(), visit=visit, save=False)
			visit.flags.ignore_billing_lock = True
			visit.save(ignore_permissions=True)
		finally:
			self.flags.syncing_procedure_visit_side_effects = False

	def _visit_order_status(self) -> str:
		status = cstr(self.status).strip()
		if status == "Cancelled":
			return "Cancelled"
		if status in {"Completed", "Closed"}:
			return "Completed"
		if status == "In Progress":
			return "In Progress"
		return "Ordered"

	def _sync_visit_order_row(self, status: str, *, visit=None, save: bool = True):
		if not self.visit:
			return False
		from pet_app.api.diagnostics import _sync_order_status

		return _sync_order_status(self, status, visit=visit, save=save, allow_rewind=status == "Ordered")

	def on_trash(self):
		assert_no_active_plan_items_linked_to(self.doctype, self.name, action="delete")

	def _validate_plan_item_links_before_detach(self):
		previous = self.get_doc_before_save()
		if previous and previous.get("visit") != self.visit:
			assert_no_active_plan_items_linked_to(self.doctype, self.name, action="move")
		if self.status == "Cancelled" and (not previous or previous.get("status") != "Cancelled"):
			assert_no_active_plan_items_linked_to(self.doctype, self.name, action="cancel")

	def set_values_from_visit(self):
		if not self.visit:
			frappe.throw(_("Visit is required."))

		visit = frappe.db.get_value(
			"Vet Visit",
			self.visit,
			["animal_patient", "guardian", "doctor", "billed", "sales_invoice"],
			as_dict=True,
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
			frappe.throw(_("Procedure Pet must match the linked Vet Visit pet."))

		if not self.guardian and visit.guardian:
			self.guardian = visit.guardian
		elif STRICT_MODE and visit.guardian and self.guardian != visit.guardian:
			frappe.throw(_("Procedure Guardian must match the linked Vet Visit guardian."))

		if not self.doctor and visit.doctor:
			self.doctor = visit.doctor
		elif STRICT_MODE and visit.doctor and self.doctor != visit.doctor:
			frappe.throw(_("Procedure practitioner must match the linked Vet Visit practitioner."))

	def set_values_from_template(self):
		if not self.procedure_template:
			frappe.throw(_("Procedure Template is required."))
		if not frappe.db.exists("Procedure Template", self.procedure_template):
			frappe.throw(_("Procedure Template {0} was not found.").format(frappe.bold(self.procedure_template)))

		template = frappe.get_cached_doc("Procedure Template", self.procedure_template)
		if not cint(template.get("active", 1)):
			frappe.throw(_("Procedure Template {0} is inactive.").format(frappe.bold(self.procedure_template)))
		if template.get("anesthesia_required") and not self.anesthesia_used:
			self.anesthesia_used = 1

	def copy_checklist_from_template(self):
		if self.checklist:
			return
		if not self.procedure_template:
			return
		template = frappe.get_cached_doc("Procedure Template", self.procedure_template)
		steps = sorted(template.get("steps") or [], key=lambda row: (cint(row.get("sort_order")), row.idx))
		for step in steps:
			self.append(
				"checklist",
				{
					"step_title": step.step_title,
					"required": cint(step.required),
					"done": 0,
					"note": step.default_note,
				},
			)

	def set_values_from_care_service(self):
		if self.care_service:
			care_service = get_care_service_doc(self.care_service)
			if not care_service.get("item_code"):
				frappe.throw(_("Care Service {0} must have an Item Code.").format(frappe.bold(self.care_service)))
			if care_service.get("default_price") in (None, ""):
				frappe.throw(_("Care Service {0} must have a Default Price.").format(frappe.bold(self.care_service)))
			self.item_code = care_service.get("item_code")
			self.rate = flt(care_service.get("default_price"))
		else:
			template = frappe.get_cached_doc("Procedure Template", self.procedure_template)
			item_code = cstr(template.get("item_code")).strip()
			if not item_code:
				frappe.throw(
					_("Procedure Template {0} must have an Item Code before it can be billed.").format(
						frappe.bold(self.procedure_template)
					)
				)
			rate = flt(template.get("price"))
			if rate <= 0:
				frappe.throw(
					_("Procedure Template {0} must have a positive Price before it can be billed.").format(
						frappe.bold(self.procedure_template)
					)
				)
			self.item_code = item_code
			self.rate = rate

		# Set once, unlike item_code and rate above. care_service wins where there is one,
		# and the Procedure Template answers for procedures billed straight off a template.
		snapshot_performing_branch(
			self, care_service=self.care_service, procedure_template=self.procedure_template
		)

		if not self.status:
			self.status = "Pending"

	def validate_completion_requirements(self):
		if self.status not in {"Completed", "Closed"}:
			return
		template = frappe.get_cached_doc("Procedure Template", self.procedure_template)
		if template.get("consent_required") and not self.consent_obtained:
			if not _has_signed_consent(self):
				frappe.throw(_("Signed consent is required before completing this procedure."))
			self.consent_obtained = 1

		for row in self.checklist or []:
			if cint(row.required) and not cint(row.done):
				frappe.throw(_("Required procedure checklist item {0} is not done.").format(frappe.bold(row.step_title)))


def _has_signed_consent(procedure) -> bool:
	if not frappe.db.exists("DocType", "Pet Consent Form"):
		return False
	filters = {
		"status": "Signed",
		"procedure": procedure.name,
	}
	if frappe.db.exists("Pet Consent Form", filters):
		return True
	fallback_filters = {
		"status": "Signed",
		"visit": procedure.visit,
		"pet": procedure.pet,
	}
	if procedure.guardian:
		fallback_filters["guardian"] = procedure.guardian
	return bool(frappe.db.exists("Pet Consent Form", fallback_filters))
