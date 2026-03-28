# Copyright (c) 2026, solvers and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, now_datetime

from pet_app.pet_app.doctype.vet_case_sheet.vet_case_sheet import build_case_summary


class VetVisit(Document):
	def before_insert(self):
		self._set_defaults()
		self._pull_case_sheet_values()

	def validate(self):
		self._set_defaults()
		self._pull_case_sheet_values()
		self._apply_row_pricing()
		self._validate_case_sheet_uniqueness()
		self._validate_follow_up()
		self._validate_completion_rules()

	def on_update(self):
		self._sync_case_sheet()

	def _set_defaults(self):
		if not self.visit_datetime:
			self.visit_datetime = now_datetime()

		if not self.status:
			self.status = "Draft"

		if not self.visit_type:
			self.visit_type = "Consultation"

		if not self.doctor:
			practitioner = _get_session_practitioner()
			if practitioner:
				self.doctor = practitioner

	def _pull_case_sheet_values(self):
		if not self.case_sheet:
			return

		case_sheet = frappe.get_doc("Vet Case Sheet", self.case_sheet)
		self.customer = case_sheet.customer
		self.animal_patient = case_sheet.animal_patient

		if not self.weight:
			self.weight = case_sheet.weight

		if not self.case_summary:
			self.case_summary = build_case_summary(case_sheet)

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

	def _validate_completion_rules(self):
		if self.status != "Completed":
			return

		if not self.diagnosis:
			frappe.throw(_("Diagnosis is required before completing the visit."))

		if not self.treatment_plan:
			frappe.throw(_("Treatment Plan is required before completing the visit."))

	def _apply_row_pricing(self):
		total_amount = 0

		for row in self.prescribed_medications or []:
			if not row.medication_item:
				continue
			row.qty = row.qty or 1
			if not row.rate:
				row.rate = get_item_billing_details(row.medication_item).get("rate", 0)
			row.amount = (row.qty or 0) * (row.rate or 0)
			total_amount += row.amount or 0

		for row in self.requested_services or []:
			if not row.care_service:
				continue
			row.qty = row.qty or 1
			service_details = get_care_service_billing_details(row.care_service)
			row.item_code = service_details.get("item_code")
			if not row.rate:
				row.rate = service_details.get("rate", 0)
			row.amount = (row.qty or 0) * (row.rate or 0)
			total_amount += row.amount or 0

		for row in self.lab_requests or []:
			if not row.lab_service:
				continue
			row.qty = row.qty or 1
			lab_details = get_care_service_billing_details(row.lab_service)
			row.item_code = lab_details.get("item_code")
			if not row.rate:
				row.rate = lab_details.get("rate", 0)
			row.amount = (row.qty or 0) * (row.rate or 0)
			total_amount += row.amount or 0

		self.total_billable_amount = total_amount

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


def _get_session_practitioner() -> str | None:
	return frappe.db.get_value("Healthcare Practitioner", {"user_id": frappe.session.user}, "name")


@frappe.whitelist()
def get_item_billing_details(item_code: str) -> dict:
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
	if not visit_name:
		frappe.throw(_("Vet Visit is required."))

	visit = frappe.get_doc("Vet Visit", visit_name)
	visit.check_permission("write")

	if visit.sales_invoice:
		frappe.throw(
			_("Vet Visit {0} is already billed with Sales Invoice {1}.").format(
				frappe.bold(visit.name), frappe.bold(visit.sales_invoice)
			)
		)

	visit._apply_row_pricing()
	items = []

	for row in visit.prescribed_medications or []:
		if not row.medication_item:
			continue
		if not row.rate:
			row.rate = get_item_billing_details(row.medication_item).get("rate", 0)
		items.append(
			{
				"item_code": row.medication_item,
				"qty": row.qty or 1,
				"rate": row.rate or 0,
				"description": _build_medication_description(row),
			}
		)

	for row in visit.requested_services or []:
		if not row.care_service:
			continue
		service_details = get_care_service_billing_details(row.care_service)
		if not service_details.get("item_code"):
			frappe.throw(
				_("Care Service {0} is missing item_code and cannot be billed.").format(
					frappe.bold(row.care_service)
				)
			)
		items.append(
			{
				"item_code": service_details["item_code"],
				"qty": row.qty or 1,
				"rate": row.rate or service_details.get("rate") or 0,
				"description": row.notes or service_details.get("service_name"),
			}
		)

	for row in visit.lab_requests or []:
		if not row.lab_service:
			continue
		lab_details = get_care_service_billing_details(row.lab_service)
		if not lab_details.get("item_code"):
			frappe.throw(
				_("Care Service {0} is missing item_code and cannot be billed.").format(
					frappe.bold(row.lab_service)
				)
			)
		items.append(
			{
				"item_code": lab_details["item_code"],
				"qty": row.qty or 1,
				"rate": row.rate or lab_details.get("rate") or 0,
				"description": row.notes or lab_details.get("service_name"),
			}
		)

	if not items:
		frappe.throw(_("Add at least one billable medication, service, or lab request before invoicing."))

	sales_invoice = frappe.get_doc(
		{
			"doctype": "Sales Invoice",
			"customer": visit.customer,
			"posting_date": getdate(visit.visit_datetime),
			"due_date": getdate(visit.visit_datetime),
			"items": items,
		}
	)
	sales_invoice.insert()

	frappe.db.set_value(
		"Vet Visit",
		visit.name,
		{
			"billed": 1,
			"sales_invoice": sales_invoice.name,
			"total_billable_amount": visit.total_billable_amount,
		},
		update_modified=False,
	)

	return {
		"sales_invoice": sales_invoice.name,
		"total_billable_amount": visit.total_billable_amount,
	}


def _build_medication_description(row) -> str:
	parts = []
	if row.dosage:
		parts.append(_("Dosage: {0}").format(row.dosage))
	if row.frequency:
		parts.append(_("Frequency: {0}").format(row.frequency))
	if row.duration_days:
		parts.append(_("Duration: {0} day(s)").format(row.duration_days))
	if row.instructions:
		parts.append(_("Instructions: {0}").format(row.instructions))
	return "\n".join(parts)
