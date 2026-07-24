from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr, flt

from pet_app.utils.care_service import get_care_service_doc as resolve_care_service_doc


STRICT_MODE = True
BILLED_VISIT_LOCK_MESSAGE = "This visit is already billed and cannot be modified."
ISSUED_INVOICE_CANCEL_MESSAGE = (
	"This order is already on submitted Sales Invoice {0}. The invoice has been issued, "
	"so cancellation is an accounting matter. Please contact the cashier."
)


def log_visit_billing_event(event: str, **context):
	frappe.logger("pet_app.visit_billing").info({"event": event, **context})


def get_care_service_doc(care_service: str):
	return resolve_care_service_doc(care_service)


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
	linked_doctype: str | None = None,
	linked_name: str | None = None,
	order_id: str | None = None,
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

	row = _find_billable_row(
		visit,
		linked_service_id,
		linked_doctype=linked_doctype,
		linked_name=linked_name,
		order_id=order_id,
		item_code=item_code,
		item_type=item_type,
	)
	if row:
		if row.status == "Billed":
			return row
		row.linked_service_id = linked_service_id
		_set_if_field(row, "linked_doctype", linked_doctype)
		_set_if_field(row, "linked_name", linked_name)
		_set_if_field(row, "order_id", order_id)
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
	_set_if_field(row, "linked_doctype", linked_doctype)
	_set_if_field(row, "linked_name", linked_name)
	_set_if_field(row, "order_id", order_id)
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


def _clinical_record_care_service(doc) -> str | None:
	return cstr(doc.get("care_service") or doc.get("care_service_id")).strip() or None


def _clinical_record_rate(doc, service) -> float:
	for fieldname in ("rate", "price"):
		value = doc.get(fieldname)
		if value in (None, ""):
			continue
		value = flt(value)
		if value > 0:
			return value
	return flt(service.default_price or 0)


def _clinical_record_item_name(doc, service=None) -> str:
	service_name = cstr(service.service_name).strip() if service else ""
	service_doc_name = cstr(service.name).strip() if service else ""
	procedure_name = ""
	if doc.doctype == "Pet Procedure" and doc.get("procedure_template"):
		procedure_name = cstr(
			frappe.db.get_value("Procedure Template", doc.get("procedure_template"), "procedure_name")
		).strip()
	return (
		cstr(doc.get("item_name")).strip()
		or cstr(doc.get("pet_service_name")).strip()
		or procedure_name
		or service_name
		or service_doc_name
		or cstr(doc.get("item_code")).strip()
		or cstr(doc.name).strip()
	)


def _clinical_record_note(doc, item_name: str) -> str:
	return cstr(doc.get("note") or doc.get("description") or item_name).strip()


def sync_clinical_record_billable_item(doc, item_type: str, *, visit=None, save: bool = True):
	if not doc.visit:
		frappe.throw(_("Visit is required."))
	care_service = _clinical_record_care_service(doc)

	visit = visit or frappe.get_doc("Vet Visit", doc.visit)
	if visit.sales_invoice:
		frappe.throw(_(BILLED_VISIT_LOCK_MESSAGE))
	if visit.billed:
		frappe.throw(_(BILLED_VISIT_LOCK_MESSAGE))

	if care_service:
		service = get_care_service_doc(care_service)
		item_code = cstr(doc.get("item_code") or service.item_code).strip()
		if not item_code:
			frappe.throw(
				_("Care Service {0} is missing Item Code and cannot be billed.").format(
					frappe.bold(care_service)
				)
			)
		rate = _clinical_record_rate(doc, service)
		if rate <= 0:
			frappe.throw(
				_("Care Service {0} must have a positive billing rate before it can be billed.").format(
					frappe.bold(care_service)
				)
			)
		item_name = _clinical_record_item_name(doc, service)
	else:
		if item_type != "Procedure":
			frappe.throw(_("Care Service is required."))
		item_code = cstr(doc.get("item_code")).strip()
		if not item_code:
			frappe.throw(
				_("Pet Procedure {0} must have an Item Code before it can be billed.").format(
					frappe.bold(doc.name)
				)
			)
		rate = flt(doc.get("rate") or doc.get("price"))
		if rate <= 0:
			frappe.throw(
				_("Pet Procedure {0} must have a positive billing rate before it can be billed.").format(
					frappe.bold(doc.name)
				)
			)
		item_name = _clinical_record_item_name(doc)

	upsert_visit_billable_item(
		visit,
		linked_service_id=f"{doc.doctype}::{doc.name}",
		linked_doctype=doc.doctype,
		linked_name=doc.name,
		order_id=doc.get("order_id"),
		item_code=item_code,
		item_type=item_type,
		qty=1,
		rate=rate,
		item_name=item_name,
		note=_clinical_record_note(doc, item_name),
	)
	visit.total_billable_amount = apply_billable_item_amounts(visit)
	if save:
		visit.save(ignore_permissions=True)
	return visit


def assert_visit_billable_item_can_cancel(
	visit_name: str,
	*,
	linked_service_id: str | None = None,
	linked_doctype: str | None = None,
	linked_name: str | None = None,
	order_id: str | None = None,
	item_code: str | None = None,
	item_type: str | None = None,
	allow_legacy_service_fallback: bool = False,
	require_match: bool = False,
):
	if not visit_name or not (linked_service_id or (linked_doctype and linked_name) or order_id):
		return
	visit = frappe.get_doc("Vet Visit", visit_name)
	row = _find_billable_row(
		visit,
		linked_service_id,
		linked_doctype=linked_doctype,
		linked_name=linked_name,
		order_id=order_id,
		item_code=item_code,
		item_type=item_type,
	)
	if not row and allow_legacy_service_fallback:
		row = _find_legacy_service_billable_row(
			visit,
			linked_doctype=linked_doctype,
			linked_name=linked_name,
			item_type=item_type,
			raise_on_failure=require_match,
		)
	if row:
		_assert_parent_billable_row_can_cancel(visit, row, sales_invoice=visit.sales_invoice, billed=visit.billed)
	elif require_match:
		_raise_billable_match_failure(
			"No matching billable item was found.",
			visit=visit,
			linked_doctype=linked_doctype,
			linked_name=linked_name,
			item_type=item_type,
			order_id=order_id,
			linked_service_id=linked_service_id,
		)
	elif visit.sales_invoice or visit.billed:
		_assert_parent_billing_state_can_cancel(visit.sales_invoice, visit.billed)


def assert_boarding_billable_item_can_cancel(
	boarding_name: str,
	*,
	linked_service_id: str | None = None,
	linked_doctype: str | None = None,
	linked_name: str | None = None,
	order_id: str | None = None,
	item_code: str | None = None,
	item_type: str | None = None,
):
	if not boarding_name or not (linked_service_id or (linked_doctype and linked_name) or order_id):
		return
	boarding = frappe.get_doc("Pet Boarding", boarding_name)
	row = _find_billable_row(
		boarding,
		linked_service_id,
		linked_doctype=linked_doctype,
		linked_name=linked_name,
		order_id=order_id,
		item_code=item_code,
		item_type=item_type,
	)
	if row:
		_assert_parent_billable_row_can_cancel(
			boarding,
			row,
			sales_invoice=boarding.sales_invoice,
			billed=boarding.billing_status == "Invoiced",
		)
	elif boarding.sales_invoice or boarding.billing_status == "Invoiced":
		_assert_parent_billing_state_can_cancel(boarding.sales_invoice, boarding.billing_status == "Invoiced")


def cancel_visit_billable_item(visit_name: str, linked_service_id: str):
	cancel_visit_billable_item_by_link(visit_name, linked_service_id=linked_service_id)


def cancel_visit_billable_item_by_link(
	visit_name: str,
	*,
	linked_service_id: str | None = None,
	linked_doctype: str | None = None,
	linked_name: str | None = None,
	order_id: str | None = None,
	item_code: str | None = None,
	item_type: str | None = None,
	visit=None,
	save: bool = True,
	allow_legacy_service_fallback: bool = False,
	require_match: bool = False,
):
	if not visit_name:
		return
	if not (linked_service_id or (linked_doctype and linked_name) or order_id):
		return

	visit = visit or frappe.get_doc("Vet Visit", visit_name)
	row = _find_billable_row(
		visit,
		linked_service_id,
		linked_doctype=linked_doctype,
		linked_name=linked_name,
		order_id=order_id,
		item_code=item_code,
		item_type=item_type,
	)
	if not row and allow_legacy_service_fallback:
		row = _find_legacy_service_billable_row(
			visit,
			linked_doctype=linked_doctype,
			linked_name=linked_name,
			item_type=item_type,
			raise_on_failure=require_match,
		)
	if not row:
		if require_match:
			_raise_billable_match_failure(
				"No matching billable item was found.",
				visit=visit,
				linked_doctype=linked_doctype,
				linked_name=linked_name,
				item_type=item_type,
				order_id=order_id,
				linked_service_id=linked_service_id,
			)
		return
	_cancel_parent_billable_row(
		visit,
		row,
		sales_invoice=visit.sales_invoice,
		billed=visit.billed,
		recalculate=lambda doc: setattr(doc, "total_billable_amount", apply_billable_item_amounts(doc)),
		save=save,
	)
	return visit


def cancel_boarding_billable_item_by_link(
	boarding_name: str,
	*,
	linked_service_id: str | None = None,
	linked_doctype: str | None = None,
	linked_name: str | None = None,
	order_id: str | None = None,
	item_code: str | None = None,
	item_type: str | None = None,
):
	if not boarding_name:
		return
	if not (linked_service_id or (linked_doctype and linked_name) or order_id):
		return

	boarding = frappe.get_doc("Pet Boarding", boarding_name)
	row = _find_billable_row(
		boarding,
		linked_service_id,
		linked_doctype=linked_doctype,
		linked_name=linked_name,
		order_id=order_id,
		item_code=item_code,
		item_type=item_type,
	)
	if not row:
		return

	def recalculate(doc):
		doc.run_method("_apply_billable_item_amounts")
		doc.run_method("_compute_totals")
		if not doc.sales_invoice and doc.meta.has_field("billing_status"):
			doc.billing_status = "Unbilled"

	_cancel_parent_billable_row(
		boarding,
		row,
		sales_invoice=boarding.sales_invoice,
		billed=boarding.billing_status == "Invoiced",
		recalculate=recalculate,
	)


def _assert_parent_billing_state_can_cancel(sales_invoice: str | None, billed: bool):
	invoice = frappe.get_doc("Sales Invoice", sales_invoice) if sales_invoice else None
	if invoice and invoice.docstatus == 1:
		frappe.throw(_(ISSUED_INVOICE_CANCEL_MESSAGE).format(frappe.bold(invoice.name)))
	if invoice and invoice.docstatus == 0:
		return
	if invoice:
		frappe.throw(_(ISSUED_INVOICE_CANCEL_MESSAGE).format(frappe.bold(invoice.name)))
	if billed:
		frappe.throw(_(BILLED_VISIT_LOCK_MESSAGE))


def _assert_parent_billable_row_can_cancel(parent, row, *, sales_invoice: str | None, billed: bool):
	if row.status == "Cancelled":
		return
	invoice = frappe.get_doc("Sales Invoice", sales_invoice) if sales_invoice else None
	if invoice and invoice.docstatus == 1:
		frappe.throw(_(ISSUED_INVOICE_CANCEL_MESSAGE).format(frappe.bold(invoice.name)))
	if invoice and invoice.docstatus == 0:
		if not _find_draft_invoice_item_for_billable(parent, invoice, row):
			frappe.throw(
				_(
					"Could not safely match this order to a line on draft Sales Invoice {0}. "
					"Please contact the cashier."
				).format(frappe.bold(invoice.name))
			)
		return
	if invoice:
		frappe.throw(_(ISSUED_INVOICE_CANCEL_MESSAGE).format(frappe.bold(invoice.name)))
	if billed or row.status == "Billed":
		frappe.throw(_(BILLED_VISIT_LOCK_MESSAGE))


def _cancel_parent_billable_row(parent, row, *, sales_invoice: str | None, billed: bool, recalculate, save: bool = True):
	_assert_parent_billable_row_can_cancel(parent, row, sales_invoice=sales_invoice, billed=billed)
	if row.status == "Cancelled":
		return

	invoice = frappe.get_doc("Sales Invoice", sales_invoice) if sales_invoice else None
	if invoice and invoice.docstatus == 0:
		_remove_draft_invoice_item_for_billable(parent, invoice, row)

	row.status = "Cancelled"
	recalculate(parent)
	parent.flags.ignore_billing_lock = True
	if save:
		parent.save(ignore_permissions=True)


def _remove_draft_invoice_item_for_billable(parent, invoice, billable_row):
	invoice_item = _find_draft_invoice_item_for_billable(parent, invoice, billable_row)
	if not invoice_item:
		frappe.throw(
			_(
				"Could not safely match this order to a line on draft Sales Invoice {0}. "
				"Please contact the cashier."
			).format(frappe.bold(invoice.name))
		)

	if len(invoice.items or []) == 1:
		frappe.delete_doc("Sales Invoice", invoice.name, force=True, ignore_permissions=True)
		if parent.meta.has_field("sales_invoice"):
			parent.sales_invoice = None
		if parent.meta.has_field("billed"):
			parent.billed = 0
		if parent.meta.has_field("billing_status"):
			parent.billing_status = "Unbilled"
		return

	invoice.remove(invoice_item)
	invoice.flags.ignore_permissions = True
	invoice.save(ignore_permissions=True)


def _find_draft_invoice_item_for_billable(parent, invoice, billable_row):
	active_rows = [row for row in parent.billable_items or [] if row.status != "Cancelled"]
	for index, row in enumerate(active_rows):
		if row.name == billable_row.name and index < len(invoice.items or []):
			candidate = invoice.items[index]
			if _invoice_item_matches_billable(candidate, billable_row):
				return candidate
			break

	matches = [row for row in invoice.items or [] if _invoice_item_matches_billable(row, billable_row)]
	if len(matches) == 1:
		return matches[0]
	return None


def _invoice_item_matches_billable(invoice_item, billable_row) -> bool:
	if invoice_item.item_code != billable_row.item_code:
		return False
	if flt(invoice_item.qty) != flt(billable_row.qty):
		return False
	if flt(invoice_item.rate) != flt(billable_row.rate):
		return False
	if flt(invoice_item.amount) != flt(billable_row.amount):
		return False
	billable_description = cstr(billable_row.get("item_name") or billable_row.get("item_code")).strip()
	invoice_description = cstr(invoice_item.get("description")).strip()
	return not invoice_description or invoice_description == billable_description


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


def _find_billable_row(
	visit,
	linked_service_id: str | None,
	*,
	linked_doctype: str | None = None,
	linked_name: str | None = None,
	order_id: str | None = None,
	item_code: str | None = None,
	item_type: str | None = None,
):
	for row in visit.billable_items or []:
		if row.linked_service_id == linked_service_id:
			return row
		if row.get("linked_name") == linked_service_id:
			return row
		if linked_name and row.linked_service_id == linked_name:
			return row
		if linked_doctype and linked_name and row.get("linked_doctype") == linked_doctype and row.get("linked_name") == linked_name:
			return row
		if (
			order_id
			and item_code
			and item_type
			and row.get("order_id") == order_id
			and row.item_code == item_code
			and row.item_type == item_type
			and (not linked_doctype or not row.get("linked_doctype") or row.get("linked_doctype") == linked_doctype)
		):
			return row
	return None


def _find_legacy_service_billable_row(
	visit,
	*,
	linked_doctype: str | None,
	linked_name: str | None,
	item_type: str | None,
	raise_on_failure: bool = False,
):
	if linked_doctype != "PetCareService" or not linked_name or item_type != "Service":
		return None
	service = frappe.db.get_value(
		"PetCareService",
		linked_name,
		["name", "visit", "care_service_id"],
		as_dict=True,
	)
	if not service or service.visit != visit.name or not service.care_service_id:
		if raise_on_failure:
			_raise_billable_match_failure(
				"PetCareService does not match this visit or has no Care Service template.",
				visit=visit,
				linked_doctype=linked_doctype,
				linked_name=linked_name,
				item_type=item_type,
			)
		return None
	service_matches = frappe.get_all(
		"PetCareService",
		filters={"visit": visit.name, "care_service_id": service.care_service_id},
		pluck="name",
		ignore_permissions=True,
	)
	if service_matches != [linked_name]:
		if raise_on_failure:
			_raise_billable_match_failure(
				"Legacy service record match was not unique.",
				visit=visit,
				linked_doctype=linked_doctype,
				linked_name=linked_name,
				item_type=item_type,
				care_service=service.care_service_id,
				matches=len(service_matches),
			)
		return None
	legacy_rows = frappe.get_all(
		"custom services",
		filters={
			"parenttype": "Vet Visit",
			"parentfield": "care_services",
			"parent": visit.name,
			"care_service_id": service.care_service_id,
		},
		fields=["name"],
		order_by="creation asc",
		ignore_permissions=True,
	)
	if len(legacy_rows) != 1:
		if raise_on_failure:
			_raise_billable_match_failure(
				"Legacy service billable match was not unique.",
				visit=visit,
				linked_doctype=linked_doctype,
				linked_name=linked_name,
				item_type=item_type,
				care_service=service.care_service_id,
				matches=len(legacy_rows),
			)
		return None
	legacy_key = f"visit-care-service::{legacy_rows[0].name}"
	matches = [
		row
		for row in visit.billable_items or []
		if row.linked_service_id == legacy_key and row.item_type == "Service"
	]
	if len(matches) == 1:
		return matches[0]
	if raise_on_failure:
		_raise_billable_match_failure(
			"Legacy service billable row was not unique.",
			visit=visit,
			linked_doctype=linked_doctype,
			linked_name=linked_name,
			item_type=item_type,
			care_service=service.care_service_id,
			legacy_key=legacy_key,
			matches=len(matches),
		)
	return None


def _raise_billable_match_failure(message: str, **context):
	visit = context.pop("visit", None)
	if visit is not None:
		context["visit"] = getattr(visit, "name", visit)
	log_visit_billing_event(
		"LEGACY_SERVICE_BILLABLE_MATCH_FAILED",
		reason=message,
		**context,
	)
	frappe.throw(
		_(
			"Could not safely match this service to exactly one billable line. "
			"Cancellation was not applied. Please contact the cashier."
		)
	)


def _set_if_field(row, fieldname: str, value):
	if value in (None, ""):
		return
	if row.meta.has_field(fieldname):
		row.set(fieldname, value)
