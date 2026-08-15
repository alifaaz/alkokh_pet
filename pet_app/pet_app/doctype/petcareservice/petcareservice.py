# Copyright (c) 2025, solvers and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr, flt

from pet_app.utils.branch import snapshot_performing_branch
from pet_app.utils.visit_billing import cancel_visit_billable_item_by_link, sync_clinical_record_billable_item
from pet_app.workflows import clinical_state


def _notify_user(for_user, subject, doc_type, doc_name):
	if not for_user or for_user == "Administrator":
		return
	try:
		log = frappe.new_doc("Notification Log")
		log.for_user = for_user
		log.from_user = frappe.session.user
		log.subject = subject
		log.document_type = doc_type
		log.document_name = doc_name
		log.type = "Alert"
		log.insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Notification Error")


class PetCareService(Document):
	def before_validate(self):
		self._set_barcode_on_create()

	def validate(self):
		self._set_guardian_from_pet()
		self._set_category_from_links()
		self._validate_category()
		self._validate_pet_guardian_link()
		self._validate_billing_selection()
		# After _set_category_from_links, so care_service_id is settled before it is read.
		# Set once: see snapshot_performing_branch.
		snapshot_performing_branch(self, care_service=self.care_service_id)
		clinical_state.validate_document_transition(self)

	def after_insert(self):
		self._notify_provider()
		self._notify_guardian()

	def on_update(self):
		self.after_save()

	def on_update_after_submit(self):
		self.after_save()

	def after_save(self):
		self._sync_pet_weight()
		self._sync_visit_side_effects()

	def _set_barcode_on_create(self):
		if not self.is_new() or self.barcode:
			return
		self.barcode = self.name

	def _notify_provider(self):
		provider = self.provider or self.doctor
		if not provider:
			return
		user_id = frappe.db.get_value("Healthcare Practitioner", provider, "user_id")
		if not user_id:
			return
		pet_name = frappe.db.get_value("Pet", self.pet_id, "pet_name") or self.pet_id
		service_name = self.pet_service_name or "Service"
		_notify_user(user_id, f"New service assigned: {service_name} for {pet_name}", "PetCareService", self.name)

	def _notify_guardian(self):
		if not self.guardian_id:
			return
		user_id = frappe.db.get_value("Guardian", self.guardian_id, "user_id")
		if not user_id:
			return
		pet_name = frappe.db.get_value("Pet", self.pet_id, "pet_name") or self.pet_id
		service_name = self.pet_service_name or "Service"
		_notify_user(user_id, f"Service scheduled for {pet_name}: {service_name}", "PetCareService", self.name)

	def _set_guardian_from_pet(self):
		if self.guardian_id or not self.pet_id:
			return
		self.guardian_id = frappe.db.get_value("PetGuardian", {"pet_id": self.pet_id, "role": "primary_owner"}, "guardian_id") or frappe.db.get_value("PetGuardian", {"pet_id": self.pet_id}, "guardian_id")

	def _set_category_from_links(self):
		if self.category:
			return
		if self.care_service_id:
			self.category = frappe.db.get_value("CareService template", self.care_service_id, "category_id")
		if not self.category and self.service_option:
			self.category = frappe.db.get_value("Care Service Billing Option", self.service_option, "category_care_services")

	def _validate_category(self):
		if not self.category:
			frappe.throw(_("Category is required for Pet Care Service."))
		if not frappe.db.exists("CategoryCareServices", self.category):
			frappe.throw(_("Category {0} does not exist.").format(frappe.bold(self.category)))
		if self.care_service_id:
			expected = frappe.db.get_value("CareService template", self.care_service_id, "category_id")
			if expected and expected != self.category:
				frappe.throw(
					_("Category must match Care Service template {0}. Expected {1}.").format(
						frappe.bold(self.care_service_id),
						frappe.bold(expected),
					)
				)
		if self.service_option:
			expected = frappe.db.get_value("Care Service Billing Option", self.service_option, "category_care_services")
			if expected and expected != self.category:
				frappe.throw(
					_("Category must match Care Service Billing Option {0}. Expected {1}.").format(
						frappe.bold(self.service_option),
						frappe.bold(expected),
					)
				)

	def _sync_pet_weight(self):
		if self.flags.get("skip_pet_weight_sync"):
			return

		pet_id = self.pet_id
		weight = flt(self.weight)
		if not pet_id or weight <= 0:
			return

		current_weight = flt(frappe.db.get_value("Pet", pet_id, "weight") or 0)
		if current_weight == weight:
			return

		frappe.db.set_value("Pet", pet_id, "weight", weight, update_modified=False)
		frappe.logger().info(f"Updated Pet {pet_id} weight to {weight}")

	def _sync_visit_side_effects(self):
		if self.flags.get("syncing_service_visit_side_effects") or not self.visit:
			return
		self.flags.syncing_service_visit_side_effects = True
		try:
			visit = frappe.get_doc("Vet Visit", self.visit)
			if self._is_cancelled():
				cancel_visit_billable_item_by_link(
					self.visit,
					linked_service_id=f"PetCareService::{self.name}",
					linked_doctype="PetCareService",
					linked_name=self.name,
					order_id=self.get("order_id"),
					item_type="Service",
					visit=visit,
					save=False,
					allow_legacy_service_fallback=True,
					require_match=True,
				)
				self._sync_visit_order_row("Cancelled", visit=visit, save=False)
			else:
				sync_clinical_record_billable_item(self, "Service", visit=visit, save=False)
				self._sync_visit_order_row(self._visit_order_status(), visit=visit, save=False)
			visit.flags.ignore_billing_lock = True
			visit.save(ignore_permissions=True)
		finally:
			self.flags.syncing_service_visit_side_effects = False

	def _is_cancelled(self):
		return cstr(self.status).strip().casefold() == "cancelled"

	def _visit_order_status(self) -> str:
		status = cstr(self.status).strip().casefold()
		if status == "cancelled":
			return "Cancelled"
		if status == "completed" or self.end_date:
			return "Completed"
		if self.start_date:
			return "In Progress"
		return "Ordered"

	def _sync_visit_order_row(self, status: str, *, visit=None, save: bool = True):
		if not self.visit:
			return False
		from pet_app.api.diagnostics import _sync_order_status

		return _sync_order_status(self, status, visit=visit, save=save)

	def _validate_pet_guardian_link(self):
		if not self.pet_id or not self.guardian_id:
			return
		if not frappe.db.exists("PetGuardian", {"pet_id": self.pet_id, "guardian_id": self.guardian_id}):
			frappe.throw(
				_("Pet {0} is not linked to Guardian {1}.").format(
					frappe.bold(self.pet_id),
					frappe.bold(self.guardian_id),
				)
			)

	def _validate_billing_selection(self):
		if self.price not in (None, "") and flt(self.price) < 0:
			frappe.throw(_("Price must not be negative."))
		if self.item_code:
			item = frappe.db.get_value("Item", self.item_code, ["name", "disabled", "is_sales_item"], as_dict=True)
			if not item:
				frappe.throw(_("Item {0} does not exist.").format(frappe.bold(self.item_code)))
			if item.get("disabled"):
				frappe.throw(_("Item {0} is disabled.").format(frappe.bold(self.item_code)))
			if item.get("is_sales_item") is not None and not item.get("is_sales_item"):
				frappe.throw(_("Item {0} must be a sales item.").format(frappe.bold(self.item_code)))
