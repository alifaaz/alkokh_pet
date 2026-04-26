from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt


STRICT_MODE = True
BILLED_VISIT_LOCK_MESSAGE = "This visit is already billed and cannot be modified."


def log_visit_billing_event(event: str, **context):
	frappe.logger("pet_app.visit_billing").info({"event": event, **context})


def apply_billable_item_amounts(visit) -> float:
	total = 0
	for row in visit.billable_items or []:
		row.qty = flt(row.qty or 1)
		row.rate = flt(row.rate or 0)
		row.amount = row.qty * row.rate
		if row.item_code and not row.item_name:
			row.item_name = frappe.db.get_value("Item", row.item_code, "item_name")
		if not row.status:
			row.status = "Billable"
		if row.status != "Cancelled":
			total += flt(row.amount)
	return total


def upsert_visit_billable_item(
	visit,
	*,
	linked_service_id: str,
	item_code: str,
	item_type: str,
	qty: float = 1,
	rate: float = 0,
	item_name: str | None = None,
	note: str | None = None,
	status: str = "Billable",
):
	if not linked_service_id:
		frappe.throw(_("Linked Service ID is required for billable items."))
	if not item_code:
		frappe.throw(_("Item Code is required for billable items."))
	if flt(qty) <= 0:
		frappe.throw(_("Billable item Qty must be greater than zero."))
	if flt(rate) < 0:
		frappe.throw(_("Billable item Rate must not be negative."))

	row = _find_billable_row(visit, linked_service_id)
	if row:
		if row.status == "Billed":
			return row
		row.item_code = item_code
		row.item_name = item_name or frappe.db.get_value("Item", item_code, "item_name")
		row.item_type = item_type
		row.qty = flt(qty or 1)
		row.rate = flt(rate or 0)
		row.amount = row.qty * row.rate
		row.note = note
		if row.status in (None, "", "Draft"):
			row.status = status
		return row

	row = visit.append("billable_items", {})
	row.linked_service_id = linked_service_id
	row.item_code = item_code
	row.item_name = item_name or frappe.db.get_value("Item", item_code, "item_name")
	row.item_type = item_type
	row.qty = flt(qty or 1)
	row.rate = flt(rate or 0)
	row.amount = row.qty * row.rate
	row.status = row.status or status
	row.note = note
	log_visit_billing_event(
		"BILLING_MODIFIED",
		visit=visit.name,
		linked_service_id=linked_service_id,
		item_code=item_code,
		item_type=item_type,
		amount=row.amount,
	)
	return row


def sync_clinical_record_billable_item(doc, item_type: str):
	if not doc.visit:
		frappe.throw(_("Visit is required."))
	if not doc.care_service:
		frappe.throw(_("Care Service is required."))

	visit = frappe.get_doc("Vet Visit", doc.visit)
	if visit.sales_invoice:
		frappe.throw(_(BILLED_VISIT_LOCK_MESSAGE))
	if visit.billed:
		frappe.throw(_(BILLED_VISIT_LOCK_MESSAGE))

	service = frappe.get_cached_doc("CareService", doc.care_service)
	if not service.item_code:
		frappe.throw(
			_("Care Service {0} is missing Item Code and cannot be billed.").format(
				frappe.bold(doc.care_service)
			)
		)
	if service.get("default_price") in (None, ""):
		frappe.throw(
			_("Care Service {0} is missing Default Price and cannot be billed.").format(
				frappe.bold(doc.care_service)
			)
		)

	upsert_visit_billable_item(
		visit,
		linked_service_id=doc.name,
		item_code=doc.item_code or service.item_code,
		item_type=item_type,
		qty=1,
		rate=doc.rate if doc.rate is not None else (service.default_price or 0),
		item_name=doc.get("item_name") or service.service_name,
		note=doc.get("note") or service.service_name,
	)
	visit.total_billable_amount = apply_billable_item_amounts(visit)
	visit.save(ignore_permissions=True)


def on_sales_invoice_cancel(doc, method=None):
	visit_name = frappe.db.get_value("Vet Visit", {"sales_invoice": doc.name}, "name")
	if not visit_name:
		return

	visit = frappe.get_doc("Vet Visit", visit_name)
	for row in visit.billable_items or []:
		if row.status == "Billed":
			row.status = "Billable"

	visit.billed = 0
	visit.sales_invoice = None
	visit.total_billable_amount = apply_billable_item_amounts(visit)
	visit.flags.ignore_billing_lock = True
	visit.save(ignore_permissions=True)
	log_visit_billing_event("INVOICE_CANCELLED", visit=visit.name, sales_invoice=doc.name)


def _find_billable_row(visit, linked_service_id: str):
	for row in visit.billable_items or []:
		if row.linked_service_id == linked_service_id:
			return row
	return None
