# Copyright (c) 2026, solvers and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr, flt, getdate, now_datetime

from pet_app.api.permissions import require_doctype_permission
from pet_app.pet_app.doctype.medication.medication import resolve_dose_option_placeholder_uom
from pet_app.utils.medication_stock import resolve_dispense_warehouse
from pet_app.pet_app.doctype.vet_case_sheet.vet_case_sheet import build_case_summary
from pet_app.utils.care_plan_links import assert_no_active_plan_items_linked_to
from pet_app.utils.clinical_options import has_internal_clinical_note, validate_visit_clinical_selections
from pet_app.utils.medical_profile import (
	protect_care_episode_before_visit_delete,
	sync_latest_vitals,
	sync_treatment_from_visit,
	update_profile_for_visit,
)
from pet_app.utils.invoice_reuse import get_or_create_open_invoice
from pet_app.utils.price_list import get_veterinary_selling_price_list
from pet_app.utils.visit_billing import (
	ALL_CHARGES_ALREADY_BILLED_MESSAGE,
	BILLED_ROW_LOCK_MESSAGE,
	BILLED_VISIT_LOCK_MESSAGE,
	SKIP_INVOICE_ROW_STATUSES,
	STRICT_MODE,
	apply_billable_item_amounts,
	billable_row_label,
	billed_row_field_changed,
	get_care_service_doc,
	log_visit_billing_event,
	summarise_visit_billing,
	upsert_visit_billable_item,
)
from pet_app.utils.practitioner import get_practitioner_for_user
from pet_app.workflows import clinical_state
from pet_app.api.response import standardize_response

VISIT_READ_ROLES = ("Healthcare Practitioner", "Doctor", "System Manager", "Accounts User", "Healthcare", "Accounting")
VISIT_BILLING_ROLES = ("Healthcare Practitioner", "Doctor", "System Manager", "Accounts User", "Healthcare", "Accounting")


class VetVisit(Document):
	def before_insert(self):
		self._set_defaults()
		self._pull_case_sheet_values()
		self._reject_legacy_payload()

	def after_insert(self):
		if self.animal_patient:
			update_profile_for_visit(self)
			if self.get("treatment_plan") or self.get("prescribed_medications"):
				sync_treatment_from_visit(self)
			sync_latest_vitals(self)

		self._notify_doctor()

	def _notify_doctor(self):
		if not self.doctor:
			return

		user_id = frappe.db.get_value(
			"Healthcare Practitioner",
			self.doctor,
			"user_id"
		)

		if not user_id or user_id == "Administrator":
			return

		pet_name = (
			frappe.db.get_value("Pet", self.animal_patient, "pet_name")
			or self.animal_patient
		)

		try:
			log = frappe.new_doc("Notification Log")
			log.for_user = user_id
			log.from_user = frappe.session.user
			log.subject = f"New visit assigned: {pet_name}"
			log.document_type = "Vet Visit"
			log.document_name = self.name
			log.type = "Alert"
			log.insert(ignore_permissions=True)

		except Exception:
			frappe.log_error(
				frappe.get_traceback(),
				"Vet Visit Notification Error"
			)
	
	def validate(self):
		self._set_defaults()
		self._reject_legacy_payload()
		self._pull_case_sheet_values()
		self._validate_identity_consistency()
		self._validate_identity_immutability()
		clinical_state.validate_document_transition(self)
		self._validate_order_id_uniqueness()
		self._validate_order_status_transitions()
		self._sync_latest_vital_signs()
		self._validate_sales_invoice_lock()
		self._validate_billing_lock()
		self._apply_prescribed_medication_dose_options()
		self._audit_prescribed_medication_changes()
		self._validate_prescribed_medication_plan_links()
		self._sync_prescribed_medications_billables()
		self._sync_care_services_billables()
		self._validate_billable_item_rows()
		self._validate_billed_billable_rows_unchanged()
		self._apply_row_pricing()
		self._validate_case_sheet_uniqueness()
		self._validate_follow_up()
		validate_visit_clinical_selections(self)
		self._validate_completion_rules()

	def on_update(self):
		self._sync_case_sheet()
		self._sync_medical_profile_snapshot()
		self._audit_manual_billable_items()

	def on_trash(self):
		protect_care_episode_before_visit_delete(self)

	def _set_defaults(self):
		if not self.visit_datetime:
			self.visit_datetime = now_datetime()

		if not self.status:
			self.status = "Draft"

		if not self.visit_type:
			self.visit_type = "Consultation"

		if self.meta.has_field("primary_practitioner") and self.primary_practitioner and not self.doctor:
			self.doctor = self.primary_practitioner

		if not self.doctor:
			doctor = _get_session_doctor()
			if doctor:
				self.doctor = doctor

		if self.meta.has_field("primary_practitioner"):
			if self.doctor and not self.primary_practitioner:
				self.primary_practitioner = self.doctor
			elif self.primary_practitioner and not self.doctor:
				self.doctor = self.primary_practitioner

		if self.meta.has_field("follow_up_preferred_date"):
			if self.follow_up_date and not self.follow_up_preferred_date:
				self.follow_up_preferred_date = self.follow_up_date
			if self.follow_up_preferred_date and not self.follow_up_date:
				self.follow_up_date = self.follow_up_preferred_date

		if self.meta.has_field("follow_up_status") and not self.follow_up_status:
			self.follow_up_status = "Requested" if self.follow_up_required else "Not Needed"

	def _pull_case_sheet_values(self):
		if not self.case_sheet:
			return

		case_sheet = frappe.get_doc("Vet Case Sheet", self.case_sheet)
		self.guardian = case_sheet.guardian
		self.customer = case_sheet.customer
		self.animal_patient = case_sheet.animal_patient
		if self.meta.has_field("appointment") and case_sheet.meta.has_field("appointment"):
			self.appointment = case_sheet.get("appointment")

		if not self.weight:
			self.weight = case_sheet.weight

		if not self.case_summary:
			self.case_summary = build_case_summary(case_sheet)

	def _validate_identity_consistency(self):
		if self.case_sheet and frappe.db.exists("Vet Case Sheet", self.case_sheet):
			case_sheet = frappe.db.get_value(
				"Vet Case Sheet",
				self.case_sheet,
				["guardian", "customer", "animal_patient"],
				as_dict=True,
			)
			if case_sheet:
				if case_sheet.guardian:
					self.guardian = case_sheet.guardian
				if case_sheet.customer and not self.customer:
					self.customer = case_sheet.customer
				if case_sheet.animal_patient:
					self.animal_patient = case_sheet.animal_patient

		if self.guardian and self.animal_patient and not frappe.db.exists(
			"PetGuardian", {"guardian_id": self.guardian, "pet_id": self.animal_patient}
		):
			frappe.throw(
				_("Pet {0} is not linked to Guardian {1}.").format(
					frappe.bold(self.animal_patient), frappe.bold(self.guardian)
				)
			)

		if self.guardian:
			guardian_customer = frappe.db.get_value("Guardian", self.guardian, "customer_id")
			if guardian_customer and not self.customer:
				self.customer = guardian_customer
			elif guardian_customer and self.customer and guardian_customer != self.customer:
				frappe.throw(
					_("Customer {0} does not match Guardian {1}.").format(
						frappe.bold(self.customer), frappe.bold(self.guardian)
					)
				)

	def _validate_case_sheet_uniqueness(self):
		if not self.case_sheet:
			return

		existing_visit = frappe.db.get_value(
			"Vet Visit",
			{"case_sheet": self.case_sheet, "name": ["!=", self.name]},
			["name", "status"],
			as_dict=True,
		)
		if existing_visit:
			frappe.throw(
				_("Case Sheet {0} is already linked to Vet Visit {1} ({2}).").format(
					frappe.bold(self.case_sheet),
					frappe.bold(existing_visit.name),
					frappe.bold(existing_visit.status),
				),
				title=_("Duplicate Visit"),
			)

	def _validate_follow_up(self):
		preferred_date = self.get("follow_up_preferred_date") or self.get("follow_up_date")
		if self.follow_up_required and not preferred_date:
			frappe.throw(_("Follow-up Preferred Date is required when Follow-up Required is checked."))

	def _reject_legacy_payload(self):
		legacy_fields = {
			"requested_services": _("requested_services is deprecated. Use Lab / Imaging / billable_items."),
			"lab_requests": _("lab_requests is deprecated. Use Lab / Imaging / billable_items."),
		}
		for fieldname, message in legacy_fields.items():
			if fieldname not in self.__dict__:
				continue
			value = self.__dict__.get(fieldname)
			if value in (None, "", []):
				continue
			frappe.throw(message, title=_("Legacy Billing Path Blocked"))

	def _validate_identity_immutability(self):
		previous = self.get_doc_before_save()
		if not previous:
			return

		locked_identity_fields = ("case_sheet", "guardian", "customer", "animal_patient")
		for fieldname in locked_identity_fields:
			if previous.get(fieldname) != self.get(fieldname):
				frappe.throw(
					_("{0} cannot be changed after the Vet Visit is created.").format(
						frappe.bold(self.meta.get_label(fieldname) or fieldname)
					)
				)

	def _validate_completion_rules(self):
		if self.status != "Completed":
			return

		if not self.diagnosis:
			frappe.throw(_("Diagnosis is required before completing the visit."))

		if not has_internal_clinical_note(self):
			frappe.throw(_("Clinical Note is required before completing the visit."))

		if STRICT_MODE:
			self._validate_no_pending_clinical_records()

	def _validate_order_status_transitions(self):
		previous = self.get_doc_before_save()
		if not previous or not self.meta.has_field("orders"):
			return
		previous_by_name = {row.name: row for row in previous.get("orders") or [] if row.name}
		for row in self.get("orders") or []:
			if not row.name or row.name not in previous_by_name:
				continue
			if row.flags.get("allow_status_reconcile"):
				continue
			old_status = previous_by_name[row.name].status
			if old_status != row.status:
				clinical_state.assert_transition("Visit Order", old_status, row.status)

	def _validate_order_id_uniqueness(self):
		if not self.meta.has_field("orders"):
			return
		seen = {}
		for row in self.get("orders") or []:
			if not row.order_id:
				continue
			if row.order_id in seen:
				frappe.throw(
					_("Visit Order ID {0} is duplicated in rows {1} and {2}.").format(
						frappe.bold(row.order_id), seen[row.order_id], row.idx
					)
				)
			seen[row.order_id] = row.idx

	def _sync_latest_vital_signs(self):
		if not self.meta.has_field("vital_signs") or not self.get("vital_signs"):
			return

		for row in self.get("vital_signs") or []:
			if not row.recorded_at:
				row.recorded_at = now_datetime()
			if not row.recorded_by:
				row.recorded_by = frappe.session.user

		latest = max(
			self.get("vital_signs") or [],
			key=lambda row: (cstr(row.recorded_at), row.idx or 0),
		)
		if latest.temperature not in (None, ""):
			self.temperature = latest.temperature
		if latest.heart_rate not in (None, ""):
			self.heart_rate = latest.heart_rate
		if latest.respiratory_rate not in (None, ""):
			self.respiratory_rate = latest.respiratory_rate
		if latest.weight not in (None, ""):
			self.weight = latest.weight

	def _sync_medical_profile_snapshot(self):
		if not self.animal_patient or not frappe.db.exists("DocType", "Pet Medical Profile"):
			return
		update_profile_for_visit(self)
		if self.get("treatment_plan") or self.get("prescribed_medications"):
			sync_treatment_from_visit(self)
		sync_latest_vitals(self)

	def _ensure_care_episode_after_insert(self):
		return self.get("care_episode") if self.meta.has_field("care_episode") else None

	def _validate_billing_lock(self):
		if self.flags.ignore_billing_lock:
			return

		previous = self.get_doc_before_save()
		if not previous or not previous.billed:
			return

		if not _visit_has_locked_changes(self, previous):
			return

		log_visit_billing_event("VISIT_LOCKED", visit=self.name, billed=self.billed, sales_invoice=self.sales_invoice)
		frappe.throw(_(BILLED_VISIT_LOCK_MESSAGE))

	def _validate_sales_invoice_lock(self):
		if self.flags.ignore_billing_lock or self.is_new():
			return
		if self.sales_invoice:
			frappe.throw(_("Visit is locked after billing"))

	def _validate_no_pending_clinical_records(self):
		pending_lab = frappe.db.exists(
			"Lab", {"visit": self.name, "status": ["not in", ["Released", "Completed", "Cancelled"]]}
		)
		if pending_lab:
			frappe.throw(_("Complete all Lab records before completing this visit."))

		pending_imaging = frappe.db.exists(
			"Imaging", {"visit": self.name, "status": ["not in", ["Released", "Completed", "Cancelled"]]}
		)
		if pending_imaging:
			frappe.throw(_("Complete all Imaging records before completing this visit."))

		if frappe.db.exists("DocType", "Pet Procedure"):
			pending_procedure = frappe.db.exists(
				"Pet Procedure",
				{"visit": self.name, "status": ["not in", ["Completed", "Closed", "Cancelled"]]},
			)
			if pending_procedure:
				frappe.throw(_("Complete or cancel all Procedure records before completing this visit."))

	def _apply_row_pricing(self):
		self.total_billable_amount = apply_billable_item_amounts(self)

	def _apply_prescribed_medication_dose_options(self):
		for row in self.get("prescribed_medications") or []:
			dose_option = cstr(row.get("dose_option")).strip()
			if not dose_option:
				continue

			option = frappe.db.get_value(
				"Medication Dose Option",
				dose_option,
				["name", "parent", "label", "qty", "disabled"],
				as_dict=True,
			)
			if not option:
				frappe.throw(_("Dose Option {0} was not found.").format(frappe.bold(dose_option)))
			if cint(option.disabled):
				frappe.throw(_("Dose Option {0} is disabled.").format(frappe.bold(option.label or option.name)))

			medication = cstr(row.get("medication")).strip()
			if medication and medication != option.parent:
				frappe.throw(
					_("Dose Option {0} does not belong to Medication {1}.").format(
						frappe.bold(option.label or option.name), frappe.bold(medication)
					)
				)
			if not medication:
				row.medication = option.parent

			medication_item = cstr(row.get("medication_item")).strip()
			linked_item = frappe.db.get_value("Medication", option.parent, "linked_item")
			if linked_item and medication_item and medication_item != linked_item:
				frappe.throw(
					_("Dose Option {0} belongs to Item {1}, not {2}.").format(
						frappe.bold(option.label or option.name), frappe.bold(linked_item), frappe.bold(medication_item)
					)
				)
			if linked_item and not medication_item:
				row.medication_item = linked_item
				medication_item = linked_item

			if row.get("qty") in (None, ""):
				row.qty = 1
			else:
				dose_count = flt(row.get("qty"))
				if dose_count < 1:
					frappe.throw(
						_("Medication row {0} uses Dose Option {1}; Qty must be at least 1 because it represents the dose count.").format(
							row.idx, frappe.bold(option.label or option.name)
						)
					)
				row.qty = dose_count

			stock_deduction_qty = flt(option.qty)
			if stock_deduction_qty <= 0:
				frappe.throw(_("Dose Option {0} does not have a valid Stock Deduction Qty. Save the Medication master first.").format(frappe.bold(option.label or option.name)))

			stock_uom = frappe.db.get_value("Item", medication_item, "stock_uom") if medication_item else None
			placeholder_uom = resolve_dose_option_placeholder_uom(stock_uom)
			row.dispense_uom = placeholder_uom
			row.stock_uom = stock_uom
			row.conversion_factor = stock_deduction_qty

	def _audit_prescribed_medication_changes(self):
		previous = self.get_doc_before_save()
		previous_rows = {row.name: row for row in previous.get("prescribed_medications") or [] if row.name} if previous else {}
		timestamp = None

		for row in self.get("prescribed_medications") or []:
			before = previous_rows.get(row.name) if row.name else None
			quantity_changed = False
			rate_changed = False

			if before:
				quantity_changed = flt(before.get("qty")) != flt(row.get("qty"))
				rate_changed = flt(before.get("rate")) != flt(row.get("rate"))
			else:
				quantity_changed = row.get("qty") not in (None, "") and not row.get("quantity_modified_by")
				rate_changed = row.get("rate") not in (None, "") and not row.get("rate_modified_by")

			if quantity_changed and row.meta.has_field("quantity_modified_by"):
				timestamp = timestamp or now_datetime()
				row.quantity_modified_by = frappe.session.user
				row.quantity_modified_at = timestamp
			if rate_changed and row.meta.has_field("rate_modified_by"):
				timestamp = timestamp or now_datetime()
				row.rate_modified_by = frappe.session.user
				row.rate_modified_at = timestamp

	def _validate_prescribed_medication_plan_links(self):
		previous = self.get_doc_before_save()
		if not previous:
			return

		current_rows = {row.name: row for row in self.get("prescribed_medications") or [] if row.name}
		for previous_row in previous.get("prescribed_medications") or []:
			if not previous_row.name:
				continue
			current_row = current_rows.get(previous_row.name)
			if not current_row:
				assert_no_active_plan_items_linked_to("Vet Visit Medication Item", previous_row.name, action="remove")
				continue
			was_cancelled = cstr(previous_row.get("dispense_status")).strip() == "Cancelled"
			is_cancelled = cstr(current_row.get("dispense_status")).strip() == "Cancelled"
			if is_cancelled and not was_cancelled:
				assert_no_active_plan_items_linked_to("Vet Visit Medication Item", previous_row.name, action="cancel")

	def _sync_prescribed_medications_billables(self):
		active_linked_ids = set()

		for row in self.prescribed_medications or []:
			if cstr(row.get("dispense_status")).strip() == "Cancelled":
				continue
			if not row.medication_item:
				continue

			item = frappe.db.get_value(
				"Item",
				row.medication_item,
				["name", "item_name", "standard_rate"],
				as_dict=True,
			)
			if not item:
				frappe.throw(_("Medication Item {0} was not found.").format(frappe.bold(row.medication_item)))

			qty = flt(row.qty)
			if qty <= 0:
				frappe.throw(
					_("Prescribed medication row {0} must have Qty greater than zero.").format(row.idx)
				)

			rate = flt(row.rate) if row.rate not in (None, "") else self._get_medication_rate(item)
			if rate < 0:
				frappe.throw(
					_("Prescribed medication row {0} must not have a negative Rate.").format(row.idx)
				)
			amount = qty * rate

			row.rate = rate
			row.amount = amount

			linked_service_id = f"medication::{row.name}"
			active_linked_ids.add(linked_service_id)
			upsert_visit_billable_item(
				self,
				linked_service_id=linked_service_id,
				item_code=item.name,
				item_type="Medication",
				qty=qty,
				rate=rate,
				item_name=item.item_name,
				note=self._build_prescribed_medication_note(row),
			)

		for billable_row in self.billable_items or []:
			if not cstr(billable_row.linked_service_id).startswith("medication::"):
				continue
			if billable_row.linked_service_id in active_linked_ids:
				continue
			if billable_row.status == "Billed":
				continue
			billable_row.status = "Cancelled"

	def _sync_care_services_billables(self):
		# Service orders are billed by PetCareService hooks. The legacy
		# care_services child table is retained for old rows only.
		return

	def _validate_billable_item_rows(self):
		active_keys = set()
		for row in self.billable_items or []:
			if row.status == "Cancelled":
				continue
			if not row.item_code:
				frappe.throw(_("Billable item row {0} is missing Item Code.").format(row.idx))
			if flt(row.qty) <= 0:
				frappe.throw(_("Billable item row {0} must have Qty greater than zero.").format(row.idx))
			if flt(row.rate) < 0:
				frappe.throw(_("Billable item row {0} must not have a negative Rate.").format(row.idx))
			key = (
				row.get("linked_service_id"),
				row.get("linked_doctype"),
				row.get("linked_name"),
				row.get("order_id"),
				row.get("item_code"),
				row.get("item_type"),
			)
			if any(key) and key in active_keys:
				frappe.throw(_("Duplicate active billable item row found for linked record/order {0}.").format(row.get("linked_name") or row.get("order_id") or row.get("linked_service_id")))
			active_keys.add(key)

	def _validate_billed_billable_rows_unchanged(self):
		if self.flags.ignore_billing_lock:
			return
		previous = self.get_doc_before_save()
		if not previous:
			return
		previous_rows = {row.name: row for row in previous.get("billable_items") or [] if row.name}
		locked_fields = (
			"item_code",
			"item_name",
			"item_type",
			"qty",
			"rate",
			"amount",
			"linked_service_id",
			"linked_doctype",
			"linked_name",
			"order_id",
			"status",
			"note",
		)
		for row in self.get("billable_items") or []:
			if not row.name or row.name not in previous_rows:
				continue
			before = previous_rows[row.name]
			if before.status != "Billed":
				continue
			for fieldname in locked_fields:
				# Compared by value, not by text. The client posts the whole document back,
				# and a whole float serialises to a JSON integer - so cstr() saw 1 against
				# a stored 1.0 and refused a save that changed nothing. Orders now bill at
				# their own completion, so a row can be Billed while the visit is still
				# open and being edited, which is what made a latent comparison bug a daily
				# one. The scope is unchanged: an edit to an invoiced charge is refused.
				if billed_row_field_changed(before, row, fieldname):
					frappe.throw(
						_(BILLED_ROW_LOCK_MESSAGE).format(frappe.bold(billable_row_label(before)))
					)

	def _get_medication_rate(self, item) -> float:
		price_list = get_veterinary_selling_price_list()
		rate = frappe.db.get_value(
			"Item Price",
			{"item_code": item.name, "price_list": price_list, "selling": 1},
			"price_list_rate",
		)
		if rate is None:
			rate = item.standard_rate or 0
		return flt(rate)

	def _build_prescribed_medication_note(self, row) -> str:
		parts = []
		if row.dosage:
			parts.append(cstr(row.dosage).strip())
		if row.frequency:
			parts.append(cstr(row.frequency).strip())
		if row.duration_days:
			parts.append(_("Duration: {0} day(s)").format(row.duration_days))
		if row.instructions:
			parts.append(cstr(row.instructions).strip())
		return " | ".join(part for part in parts if part)

	def _audit_manual_billable_items(self):
		for row in self.billable_items or []:
			if row.status == "Cancelled" or row.linked_service_id:
				continue
			message = _("Manual billable item added: {0}").format(
				row.item_code or row.item_name or _("Unknown Item")
			)
			if not frappe.db.exists(
				"Comment",
				{
					"reference_doctype": self.doctype,
					"reference_name": self.name,
					"content": message,
				},
			):
				self.add_comment("Comment", message)
			log_visit_billing_event(
				"MANUAL_BILLABLE_ITEM",
				visit=self.name,
				row=row.idx,
				item_code=row.item_code,
				qty=flt(row.qty),
				rate=flt(row.rate),
			)

	def _sync_case_sheet(self):
		if not self.case_sheet:
			return

		case_sheet = frappe.db.get_value(
			"Vet Case Sheet",
			self.case_sheet,
			["name", "status", "vet_visit"],
			as_dict=True,
		)
		if not case_sheet:
			return

		if case_sheet.vet_visit and case_sheet.vet_visit != self.name:
			frappe.throw(
				_("Case Sheet {0} is already assigned to Vet Visit {1}.").format(
					frappe.bold(self.case_sheet), frappe.bold(case_sheet.vet_visit)
				)
			)

		target_status = _get_case_sheet_status_for_visit(self.status)
		updates = {}
		if case_sheet.vet_visit != self.name:
			updates["vet_visit"] = self.name
		if case_sheet.status != target_status:
			updates["status"] = target_status

		if updates:
			frappe.db.set_value("Vet Case Sheet", self.case_sheet, updates, update_modified=False)


def _get_case_sheet_status_for_visit(visit_status: str) -> str:
	if visit_status == "Completed":
		return "Closed"
	if visit_status == "Cancelled":
		return "Converted to Visit"
	return "In Consultation"


def _get_session_doctor() -> str | None:
	return get_practitioner_for_user(frappe.session.user)


def _has_legacy_visit_role(roles) -> bool:
	return bool(set(frappe.get_roles(frappe.session.user) or []) & set(roles))


def _has_doctype_permission(doctype: str, ptype: str) -> bool:
	try:
		return bool(frappe.has_permission(doctype, ptype=ptype, user=frappe.session.user))
	except Exception:
		return False


def _doc_has_permission(doc, ptype: str) -> bool:
	try:
		return bool(doc.has_permission(ptype))
	except Exception:
		return False


def _require_visit_role(roles, *, allow_docperm=False):
	if frappe.session.user == "Administrator" or _has_legacy_visit_role(roles) or allow_docperm:
		return
	frappe.only_for(roles)


@frappe.whitelist()
@standardize_response
def get_item_billing_details(item_code: str) -> dict:
	_require_visit_role(
		VISIT_READ_ROLES,
		allow_docperm=_has_doctype_permission("Item", "read") and _has_doctype_permission("Item Price", "read"),
	)
	if not item_code:
		return {}

	item = frappe.db.get_value("Item", item_code, ["name", "item_name", "stock_uom", "standard_rate"], as_dict=True)
	if not item:
		frappe.throw(_("Item {0} was not found.").format(frappe.bold(item_code)))

	rate = frappe.db.get_value(
		"Item Price",
		{"item_code": item_code, "price_list": get_veterinary_selling_price_list(), "selling": 1},
		"price_list_rate",
	)
	if rate is None:
		rate = item.standard_rate or 0

	return {
		"item_code": item.name,
		"item_name": item.item_name,
		"uom": item.stock_uom,
		"rate": rate,
	}


@frappe.whitelist()
@standardize_response
def get_care_service_billing_details(care_service_name: str) -> dict:
	if not care_service_name:
		_require_visit_role(
			VISIT_READ_ROLES,
			allow_docperm=_has_doctype_permission("CareService template", "read") or _has_doctype_permission("CareService", "read"),
		)
		return {}

	service = get_care_service_doc(care_service_name)
	_require_visit_role(VISIT_READ_ROLES, allow_docperm=_doc_has_permission(service, "read"))

	return {
		"care_service": service.name,
		"service_name": service.service_name,
		"item_code": service.item_code,
		"rate": service.default_price or 0,
		"price_list": service.price_list,
	}


@frappe.whitelist()
@standardize_response
def create_sales_invoice(visit_name: str) -> dict:
	_require_visit_role(VISIT_BILLING_ROLES, allow_docperm=True)

	# Now the common case, not an edge case: every order on this visit billed at its own
	# completion, so there is no remainder to invoice. That is success, not an error - the
	# work IS billed - so it answers with where the money went instead of throwing.
	# `sales_invoice` is None here; callers must read `status` before using it.
	already = _already_fully_billed_response(visit_name)
	if already:
		return already

	result = _create_sales_invoice_for_visit(visit_name)
	visit = result["visit"]
	sales_invoice = result["sales_invoice"]

	_mark_visit_invoiced(visit, sales_invoice, result["total_billable_amount"])
	visit.add_comment(
		"Comment",
		_("Draft Sales Invoice {0} created for this visit by {1}.").format(sales_invoice.name, frappe.session.user),
	)

	return {
		"status": "invoiced",
		"sales_invoice": sales_invoice.name,
		"customer": visit.customer,
		"total_billable_amount": result["total_billable_amount"],
		"guardian_reference_field": result["guardian_reference_field"],
	}


def _already_fully_billed_response(visit_name: str) -> dict | None:
	"""A clean answer for a visit whose charges were all billed at order completion.

	Returns None when there is real work for the invoicing path to do, so the normal route
	is untouched. Only claims "already billed" when at least one row actually reached an
	invoice - a visit with no rows at all, or only cancelled ones, still falls through to
	the existing throw, which is truthful for that case.
	"""
	if not visit_name or not frappe.db.exists("Vet Visit", visit_name):
		return None

	visit = frappe.get_doc("Vet Visit", visit_name)
	if visit.sales_invoice:
		# Its own invoice exists; the normal path reports that, and says which one.
		return None

	rows = [row for row in visit.billable_items or [] if row.status != "Cancelled"]
	if not rows or any(row.status != "Billed" for row in rows):
		return None

	summary = summarise_visit_billing(visit)
	return {
		"status": "already_billed",
		"sales_invoice": None,
		"customer": visit.customer,
		"total_billable_amount": summary.total,
		"guardian_reference_field": None,
		"message": _(ALL_CHARGES_ALREADY_BILLED_MESSAGE).format(visit.name),
		"invoices": [
			{
				"sales_invoice": inv.get("name"),
				"branch": inv.get("branch"),
				"docstatus": inv.get("docstatus"),
				"status": inv.get("status"),
				"outstanding_amount": flt(inv.get("outstanding_amount")),
			}
			for inv in summary.invoices
		],
	}


def _create_sales_invoice_for_visit(visit_name: str) -> dict:
	_require_visit_role(VISIT_BILLING_ROLES, allow_docperm=True)
	require_doctype_permission("Sales Invoice", "create")
	if not visit_name:
		frappe.throw(_("Vet Visit is required."))

	visit = _get_locked_visit_for_invoice(visit_name)
	visit.check_permission("write")

	if visit.sales_invoice:
		frappe.throw(
			_("Vet Visit {0} is already billed with Sales Invoice {1}.").format(
				frappe.bold(visit.name), frappe.bold(visit.sales_invoice)
			)
		)

	if not visit.customer:
		frappe.throw(_("Customer is required before invoicing this visit."))

	if STRICT_MODE:
		_validate_visit_ready_for_invoice(visit)

	items, total_amount = get_billable_invoice_items(visit)
	updates_stock = any(item.get("warehouse") for item in items)

	# A line carrying the warehouse key with an empty value is one whose goods were
	# already issued at dispense. ERPNext's update_stock is a DOCUMENT flag with no
	# per-row equivalent, so an invoice cannot deduct one stock line while leaving
	# another alone: with the flag on it demands a warehouse for every stock item
	# and refuses to save without one. Of the three possible outcomes - deduct
	# twice, block the invoice, or let this invoice stop being a stock document -
	# only the third neither corrupts stock nor stops the clinic billing. It can
	# under-deduct a second medication that is not yet opted in and happens to
	# share the invoice; that is recorded on the invoice rather than left silent.
	already_issued = [item for item in items if "warehouse" in item and not item["warehouse"]]
	stock_flag_dropped = bool(already_issued and updates_stock)
	if stock_flag_dropped:
		updates_stock = False

	# Appends to this customer's open Draft when one exists for the same company, branch
	# and stock kind; creates one only when it does not. The invoice belongs to the clinic
	# that did the work, not to whoever bills it, so the visit's branch is passed rather
	# than the acting user's - and it is part of the match key, so two branches never
	# share an invoice.
	result = get_or_create_open_invoice(
		customer=visit.customer,
		items=items,
		source_doctype="Vet Visit",
		source_name=visit.name,
		branch=visit.get("branch"),
		posting_date=getdate(),
		due_date=getdate(),
		selling_price_list=get_veterinary_selling_price_list(),
		ignore_pricing_rule=1,
		remarks=_("Vet Visit {0} billed.").format(visit.name),
		guardian=visit.guardian,
		requires_stock=updates_stock,
	)
	sales_invoice = result.invoice
	guardian_field = result.guardian_reference_field
	sales_invoice.add_comment(
		"Comment",
		_("Draft Sales Invoice {0} from Vet Visit {1} by {2}.").format(
			_("created") if result.created else _("extended"), visit.name, frappe.session.user
		),
	)
	if stock_flag_dropped:
		sales_invoice.add_comment(
			"Comment",
			_(
				"Update Stock was turned off on this invoice: {0} medication line(s) were already issued from stock at dispense. "
				"Any other stock line on this invoice is therefore not deducted here."
			).format(len(already_issued)),
		)
	log_visit_billing_event(
		"INVOICE_CREATED",
		visit=visit.name,
		sales_invoice=sales_invoice.name,
		customer=visit.customer,
		total_billable_amount=total_amount,
	)

	return {
		"visit": visit,
		"sales_invoice": sales_invoice,
		"total_billable_amount": total_amount,
		"guardian_reference_field": guardian_field,
	}


def _mark_visit_invoiced(visit, sales_invoice, total_amount: float):
	for row in visit.billable_items or []:
		if row.status != "Cancelled":
			row.status = "Billed"

	visit.sales_invoice = sales_invoice.name
	visit.billed = 1
	# The visit's full clinical value, NOT this invoice's total. Since orders bill at their
	# own completion, `total_amount` is only the remainder that reached this invoice -
	# writing it here would make the field silently understate the work done, and it is
	# summed as per-visit revenue by clinical_reports and analytics. apply_billable_item_
	# amounts stays authoritative; what is still OWED is computed by summarise_visit_billing
	# from the invoices the rows actually landed on.
	visit.total_billable_amount = apply_billable_item_amounts(visit)
	visit.flags.ignore_billing_lock = True
	visit.save()


def get_billable_invoice_items(visit, *, allow_non_invoiceable: bool = False) -> tuple[list[dict], float]:
	if not visit.billable_items:
		if allow_non_invoiceable:
			return [], 0
		frappe.throw(_("Add at least one billable item before invoicing."))

	items = []
	medication_rows = {row.name: row for row in visit.get("prescribed_medications") or [] if row.name}
	total_amount = 0
	for row in visit.billable_items or []:
		# "Billed" is now reachable BEFORE this visit is invoiced. An order whose service
		# is performed at another branch bills at its own completion, to its own branch,
		# and marks its row Billed on the way out. Without this skip that row would be
		# emitted again here and the customer would pay for one X-ray twice - once on the
		# hotel invoice at release, once on the clinic's invoice at visit close.
		#
		# Nothing else sets Billed before invoicing: _mark_visit_invoiced sets it only
		# after the invoice exists, and the visit is locked from that point, so this skip
		# is a no-op for every visit that does not use per-order billing.
		if row.status in SKIP_INVOICE_ROW_STATUSES:
			continue

		if not row.item_code:
			frappe.throw(_("Billable item row {0} is missing Item Code.").format(row.idx))

		qty = flt(row.qty)
		rate = flt(row.rate)
		if qty <= 0:
			frappe.throw(_("Billable item row {0} must have Qty greater than zero.").format(row.idx))
		if rate < 0:
			frappe.throw(_("Billable item row {0} must not have a negative Rate.").format(row.idx))

		amount = qty * rate
		row.amount = amount
		row.item_name = row.item_name or frappe.db.get_value("Item", row.item_code, "item_name") or row.item_code
		total_amount += amount

		invoice_item = {
			"item_code": row.item_code,
			"qty": qty,
			"rate": rate,
			"amount": amount,
			"description": row.item_name or row.item_code,
		}
		invoice_item.update(_get_stock_invoice_context(row, medication_rows))
		items.append(invoice_item)

	if not items:
		if allow_non_invoiceable:
			return [], 0
		# Distinguish "there is nothing to bill" from "it has all been billed already".
		# Telling a cashier to add a billable item when every charge is already on an
		# invoice sends them to create a duplicate.
		if any(row.status == "Billed" for row in visit.billable_items or []):
			frappe.throw(_(ALL_CHARGES_ALREADY_BILLED_MESSAGE).format(frappe.bold(visit.name)))
		frappe.throw(_("Add at least one active billable item before invoicing."))
	if total_amount <= 0:
		if allow_non_invoiceable:
			return items, total_amount
		frappe.throw(_("Total billable amount must be greater than zero before invoicing."))

	return items, total_amount


def _get_stock_invoice_context(billable_row, medication_rows: dict) -> dict:
	item = frappe.db.get_value(
		"Item",
		billable_row.item_code,
		["name", "is_stock_item", "stock_uom"],
		as_dict=True,
	)
	if not item or not cint(item.is_stock_item):
		return {}

	medication_row = _get_billable_medication_row(billable_row, medication_rows)

	# The goods for this line already left the warehouse when it was dispensed.
	# Invoicing it with a warehouse would deduct the same medication twice - once
	# at the bedside and once at the till - and the invoice is the wrong one of the
	# two to keep, because it bills the PRESCRIBED qty while the dispense issued
	# what was actually handed over. An explicit empty string, not a missing key:
	# ERPNext backfills `warehouse` from Item Default when the value is None, which
	# would silently reinstate the second deduction.
	if medication_row and flt(medication_row.get("stock_issued_qty")) > 0:
		return {"warehouse": ""}

	medication_defaults = {}
	medication_label = (
		cstr(medication_row.get("medication") if medication_row else "").strip()
		or cstr(billable_row.get("item_name")).strip()
		or item.name
	)
	if medication_row:
		medication_defaults = _get_medication_invoice_defaults(medication_row)
	warehouse = resolve_dispense_warehouse(
		medication=medication_row.get("medication") if medication_row else None,
		# Left None for a non-medication stock line so the chain falls straight
		# through to Stock Settings, exactly as it always has for those rows.
		medication_item=medication_row.get("medication_item") if medication_row else None,
		row_warehouse=medication_row.get("warehouse") if medication_row else None,
		label=medication_label,
		purpose=_("invoice"),
	)

	stock_uom = cstr(item.stock_uom).strip()
	uom = stock_uom
	conversion_factor = 1 if stock_uom else 0

	if medication_row:
		stock_uom = cstr(medication_row.get("stock_uom") or stock_uom).strip()
		uom = cstr(medication_row.get("dispense_uom") or medication_defaults.get("default_dispense_uom") or uom).strip()
		conversion_factor = flt(medication_row.get("conversion_factor"))
		if conversion_factor <= 0:
			conversion_factor = flt(medication_defaults.get("default_conversion_factor"))
		if conversion_factor <= 0 and uom and stock_uom and uom == stock_uom:
			conversion_factor = 1

	context = {"warehouse": warehouse}
	if uom:
		context["uom"] = uom
	if stock_uom:
		context["stock_uom"] = stock_uom
	if conversion_factor > 0:
		context["conversion_factor"] = flt(conversion_factor)
	if medication_row and medication_row.get("batch_no"):
		context["batch_no"] = medication_row.get("batch_no")
		context["use_serial_batch_fields"] = 1
	return context


def _get_medication_invoice_defaults(medication_row) -> dict:
	medication = cstr(medication_row.get("medication")).strip()
	if medication:
		defaults = frappe.db.get_value(
			"Medication",
			medication,
			["default_warehouse", "default_dispense_uom", "default_conversion_factor"],
			as_dict=True,
		)
		if defaults:
			return defaults

	medication_item = cstr(medication_row.get("medication_item")).strip()
	if medication_item:
		return frappe.db.get_value(
			"Medication",
			{"linked_item": medication_item},
			["default_warehouse", "default_dispense_uom", "default_conversion_factor"],
			as_dict=True,
		) or {}

	return {}


def _get_billable_medication_row(billable_row, medication_rows: dict):
	linked_service_id = cstr(billable_row.get("linked_service_id"))
	prefix = "medication::"
	if not linked_service_id.startswith(prefix):
		return None
	return medication_rows.get(linked_service_id[len(prefix):])


def _validate_visit_ready_for_invoice(visit):
	pending_lab = frappe.db.exists(
		"Lab", {"visit": visit.name, "status": ["not in", ["Released", "Completed", "Cancelled"]]}
	)
	if pending_lab:
		frappe.throw(_("Complete all Lab records before creating the invoice."))

	pending_imaging = frappe.db.exists(
		"Imaging", {"visit": visit.name, "status": ["not in", ["Released", "Completed", "Cancelled"]]}
	)
	if pending_imaging:
		frappe.throw(_("Complete all Imaging records before creating the invoice."))

	if frappe.db.exists("DocType", "Pet Procedure"):
		pending_procedure = frappe.db.exists(
			"Pet Procedure",
			{"visit": visit.name, "status": ["not in", ["Completed", "Closed", "Cancelled"]]},
		)
		if pending_procedure:
			frappe.throw(_("Complete or cancel all Procedure records before creating the invoice."))


def _get_locked_visit_for_invoice(visit_name: str):
	locked_visit = frappe.db.sql(
		"""
		SELECT name, sales_invoice
		FROM `tabVet Visit`
		WHERE name = %s
		FOR UPDATE
		""",
		(visit_name,),
		as_dict=True,
	)
	if not locked_visit:
		frappe.throw(_("Vet Visit {0} was not found.").format(frappe.bold(visit_name)))
	if locked_visit[0].get("sales_invoice"):
		frappe.throw(
			_("Vet Visit {0} is already billed with Sales Invoice {1}.").format(
				frappe.bold(visit_name), frappe.bold(locked_visit[0]["sales_invoice"])
			)
		)
	return frappe.get_doc("Vet Visit", visit_name)



def _visit_has_locked_changes(visit, previous) -> bool:
	if STRICT_MODE:
		fieldnames = [
			field.fieldname
			for field in visit.meta.fields
			if field.fieldtype not in {"Section Break", "Column Break", "Tab Break", "HTML", "Button"}
		]
	else:
		fieldnames = ["billable_items", "prescribed_medications", "diagnosis", "treatment_plan"]

	for fieldname in fieldnames:
		field = visit.meta.get_field(fieldname)
		if not field:
			continue
		if field.fieldtype in {"Table", "Table MultiSelect"}:
			if _table_signature(visit.get(fieldname), field.options) != _table_signature(
				previous.get(fieldname), field.options
			):
				return True
		elif visit.get(fieldname) != previous.get(fieldname):
			return True

	return False


def _table_signature(rows, child_doctype: str):
	meta = frappe.get_meta(child_doctype)
	fieldnames = [
		field.fieldname
		for field in meta.fields
		if field.fieldtype not in {"Section Break", "Column Break", "Tab Break", "HTML", "Button"}
	]
	return [
		{fieldname: row.get(fieldname) for fieldname in fieldnames}
		for row in rows or []
	]


def cstr(value) -> str:
	return str(value or "")
