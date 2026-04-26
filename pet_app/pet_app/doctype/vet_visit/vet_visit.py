# Copyright (c) 2026, solvers and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate, now_datetime

from pet_app.pet_app.doctype.vet_case_sheet.vet_case_sheet import build_case_summary
from pet_app.utils.visit_billing import (
	BILLED_VISIT_LOCK_MESSAGE,
	STRICT_MODE,
	apply_billable_item_amounts,
	log_visit_billing_event,
	upsert_visit_billable_item,
)

VISIT_READ_ROLES = ("Doctor", "System Manager", "Accounts User", "Healthcare", "Accounting")
VISIT_BILLING_ROLES = ("Doctor", "System Manager", "Accounts User", "Healthcare", "Accounting")


class VetVisit(Document):
	def before_insert(self):
		self._set_defaults()
		self._pull_case_sheet_values()
		self._reject_legacy_payload()

	def validate(self):
		self._set_defaults()
		self._reject_legacy_payload()
		self._pull_case_sheet_values()
		self._validate_identity_consistency()
		self._validate_identity_immutability()
		self._validate_sales_invoice_lock()
		self._validate_billing_lock()
		self._sync_prescribed_medications_billables()
		self._sync_care_services_billables()
		self._validate_billable_item_rows()
		self._apply_row_pricing()
		self._validate_case_sheet_uniqueness()
		self._validate_follow_up()
		self._validate_completion_rules()

	def on_update(self):
		self._sync_case_sheet()
		self._audit_manual_billable_items()

	def _set_defaults(self):
		if not self.visit_datetime:
			self.visit_datetime = now_datetime()

		if not self.status:
			self.status = "Draft"

		if not self.visit_type:
			self.visit_type = "Consultation"

		if not self.doctor:
			doctor = _get_session_doctor()
			if doctor:
				self.doctor = doctor

	def _pull_case_sheet_values(self):
		if not self.case_sheet:
			return

		case_sheet = frappe.get_doc("Vet Case Sheet", self.case_sheet)
		self.guardian = case_sheet.guardian
		self.customer = case_sheet.customer
		self.animal_patient = case_sheet.animal_patient

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
		if self.follow_up_required and not self.follow_up_date:
			frappe.throw(_("Follow-up Date is required when Follow-up Required is checked."))

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

		if not self.illness:
			frappe.throw(_("Illness is required before completing the visit."))

		if not self.diagnosis:
			frappe.throw(_("Diagnosis is required before completing the visit."))

		if not self.treatment_plan:
			frappe.throw(_("Treatment Plan is required before completing the visit."))

		if not self.doctor_notes:
			frappe.throw(_("Clinical Note is required before completing the visit."))

		if STRICT_MODE:
			self._validate_no_pending_clinical_records()

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
		pending_lab = frappe.db.exists("Lab", {"visit": self.name, "status": ["!=", "Completed"]})
		if pending_lab:
			frappe.throw(_("Complete all Lab records before completing this visit."))

		pending_imaging = frappe.db.exists("Imaging", {"visit": self.name, "status": ["!=", "Completed"]})
		if pending_imaging:
			frappe.throw(_("Complete all Imaging records before completing this visit."))

	def _apply_row_pricing(self):
		self.total_billable_amount = apply_billable_item_amounts(self)

	def _sync_prescribed_medications_billables(self):
		active_linked_ids = set()

		for row in self.prescribed_medications or []:
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
		active_linked_ids = set()

		for row in self.care_services or []:
			if not row.care_service_id:
				continue

			service = frappe.db.get_value(
				"CareService",
				row.care_service_id,
				["service_name", "item_code", "default_price", "category_id"],
				as_dict=True,
			)
			if not service:
				frappe.throw(_("Care Service {0} was not found.").format(frappe.bold(row.care_service_id)))
			if not service.item_code:
				frappe.throw(_("Care Service {0} must have an Item Code.").format(frappe.bold(row.care_service_id)))
			if service.default_price in (None, ""):
				frappe.throw(_("Care Service {0} must have a Default Price.").format(frappe.bold(row.care_service_id)))
			if flt(service.default_price) < 0:
				frappe.throw(
					_("Care Service {0} must not have a negative Default Price.").format(
						frappe.bold(row.care_service_id)
					)
				)

			category_name = None
			if service.category_id:
				category_name = frappe.db.get_value("CategoryCareServices", service.category_id, "category_name")
			if cstr(category_name).strip().lower() in {"lab", "imaging"}:
				frappe.throw(
					_("Care Service {0} belongs to category {1} and must be added through the {1} tab.").format(
						frappe.bold(row.care_service_id), frappe.bold(category_name)
					)
				)

			linked_service_id = f"visit-care-service::{row.name}"
			active_linked_ids.add(linked_service_id)
			upsert_visit_billable_item(
				self,
				linked_service_id=linked_service_id,
				item_code=service.item_code,
				item_type="Service",
				qty=1,
				rate=service.default_price,
				item_name=service.service_name,
				note=service.service_name,
			)

		for billable_row in self.billable_items or []:
			if not cstr(billable_row.linked_service_id).startswith("visit-care-service::"):
				continue
			if billable_row.linked_service_id in active_linked_ids:
				continue
			if billable_row.status == "Billed":
				continue
			billable_row.status = "Cancelled"

	def _validate_billable_item_rows(self):
		for row in self.billable_items or []:
			if row.status == "Cancelled":
				continue
			if not row.item_code:
				frappe.throw(_("Billable item row {0} is missing Item Code.").format(row.idx))
			if flt(row.qty) <= 0:
				frappe.throw(_("Billable item row {0} must have Qty greater than zero.").format(row.idx))
			if flt(row.rate) < 0:
				frappe.throw(_("Billable item row {0} must not have a negative Rate.").format(row.idx))

	def _get_medication_rate(self, item) -> float:
		rate = frappe.db.get_value(
			"Item Price",
			{"item_code": item.name, "price_list": "Clinic", "selling": 1},
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
	return frappe.db.get_value("Doctor", {"user": frappe.session.user}, "name")


def _require_visit_role(roles):
	if frappe.session.user == "Administrator":
		return
	frappe.only_for(roles)


@frappe.whitelist()
def get_item_billing_details(item_code: str) -> dict:
	_require_visit_role(VISIT_READ_ROLES)
	if not item_code:
		return {}

	item = frappe.db.get_value("Item", item_code, ["name", "item_name", "stock_uom", "standard_rate"], as_dict=True)
	if not item:
		frappe.throw(_("Item {0} was not found.").format(frappe.bold(item_code)))

	rate = frappe.db.get_value(
		"Item Price",
		{"item_code": item_code, "price_list": "Standard Selling", "selling": 1},
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
def get_care_service_billing_details(care_service_name: str) -> dict:
	_require_visit_role(VISIT_READ_ROLES)
	if not care_service_name:
		return {}

	service = frappe.db.get_value(
		"CareService",
		care_service_name,
		["name", "service_name", "item_code", "default_price", "price_list"],
		as_dict=True,
	)
	if not service:
		frappe.throw(_("Care Service {0} was not found.").format(frappe.bold(care_service_name)))

	return {
		"care_service": service.name,
		"service_name": service.service_name,
		"item_code": service.item_code,
		"rate": service.default_price or 0,
		"price_list": service.price_list,
	}


@frappe.whitelist()
def create_sales_invoice(visit_name: str) -> dict:
	_require_visit_role(VISIT_BILLING_ROLES)
	if not visit_name:
		frappe.throw(_("Vet Visit is required."))
	if not frappe.has_permission("Sales Invoice", ptype="create"):
		raise frappe.PermissionError(_("Not permitted to create Sales Invoice."))

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

	sales_invoice = frappe.get_doc(
		{
			"doctype": "Sales Invoice",
			"customer": visit.customer,
			"posting_date": getdate(),
			"due_date": getdate(),
			"ignore_pricing_rule": 1,
			"selling_price_list": _get_clinic_price_list(),
			"items": items,
		}
	)
	guardian_field = None
	if visit.guardian:
		for fieldname in ("guardian_id", "guardian", "custom_guardian_id", "custom_guardian"):
			if sales_invoice.meta.has_field(fieldname):
				sales_invoice.set(fieldname, visit.guardian)
				guardian_field = fieldname
				break
	sales_invoice.flags.from_custom_flow = True
	sales_invoice.insert()
	sales_invoice.submit()
	sales_invoice.add_comment(
		"Comment",
		_("Sales Invoice created from Vet Visit {0} by {1}.").format(visit.name, frappe.session.user),
	)
	log_visit_billing_event(
		"INVOICE_CREATED",
		visit=visit.name,
		sales_invoice=sales_invoice.name,
		customer=visit.customer,
		total_billable_amount=total_amount,
	)

	for row in visit.billable_items or []:
		if row.status != "Cancelled":
			row.status = "Billed"

	visit.sales_invoice = sales_invoice.name
	visit.billed = 1
	visit.total_billable_amount = total_amount
	visit.flags.ignore_billing_lock = True
	visit.save()
	visit.add_comment(
		"Comment",
		_("Sales Invoice {0} created for this visit by {1}.").format(sales_invoice.name, frappe.session.user),
	)

	return {
		"sales_invoice": sales_invoice.name,
		"customer": visit.customer,
		"total_billable_amount": total_amount,
		"guardian_reference_field": guardian_field,
	}


def get_billable_invoice_items(visit) -> tuple[list[dict], float]:
	if not visit.billable_items:
		frappe.throw(_("Add at least one billable item before invoicing."))

	items = []
	total_amount = 0
	for row in visit.billable_items or []:
		if row.status == "Cancelled":
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

		items.append(
			{
				"item_code": row.item_code,
				"qty": qty,
				"rate": rate,
				"amount": amount,
				"description": row.item_name or row.item_code,
			}
		)

	if not items:
		frappe.throw(_("Add at least one active billable item before invoicing."))
	if total_amount <= 0:
		frappe.throw(_("Total billable amount must be greater than zero before invoicing."))

	return items, total_amount


def _validate_visit_ready_for_invoice(visit):
	pending_lab = frappe.db.exists("Lab", {"visit": visit.name, "status": ["!=", "Completed"]})
	if pending_lab:
		frappe.throw(_("Complete all Lab records before creating the invoice."))

	pending_imaging = frappe.db.exists("Imaging", {"visit": visit.name, "status": ["!=", "Completed"]})
	if pending_imaging:
		frappe.throw(_("Complete all Imaging records before creating the invoice."))


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


def _get_clinic_price_list() -> str:
	if frappe.db.exists("Price List", "Clinic"):
		return "Clinic"
	frappe.throw(_("Clinic Price List is required for veterinary invoices."))


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
		if field.fieldtype == "Table":
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
