from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cstr, flt, now_datetime

from pet_app.api.link_aliases import enrich_link_aliases
from pet_app.api.permissions import require_restriction_value
from pet_app.api.response import fail, ok
from pet_app.utils.medication_stock import (
	assert_row_can_record_stock,
	is_opted_in,
	issue_for_dispense,
	receive_for_return,
	returnable_stock_qty,
)


PENDING_STATUSES = ("", "Prescribed", "Pending Dispense", "Partially Dispensed")
FINAL_STATUSES = {"Cancelled", "Returned"}


@frappe.whitelist()
def list_pending_dispense(visit=None, warehouse=None, limit=50):
	try:
		if warehouse:
			require_restriction_value("warehouse", warehouse)
		limit = max(min(int(limit or 50), 200), 1)
		conditions = ["v.docstatus < 2"]
		values = {"limit": limit}
		conditions.append("(coalesce(m.dispense_status, '') in %(statuses)s)")
		values["statuses"] = PENDING_STATUSES
		if visit:
			conditions.append("v.name = %(visit)s")
			values["visit"] = visit
		if warehouse:
			conditions.append("(m.warehouse = %(warehouse)s or coalesce(m.warehouse, '') = '')")
			values["warehouse"] = warehouse
		rows = frappe.db.sql(
			f"""
			select
				m.name as row_name,
				m.parent as visit,
				m.medication,
				m.medication_item,
				m.qty,
				m.dispense_uom,
				m.stock_uom,
				m.conversion_factor,
				m.dispensed_qty,
				m.return_qty,
				m.dispense_status,
				m.warehouse,
				m.batch_no,
				m.expiry_date,
				m.dosage,
				m.frequency,
				m.duration_days,
				m.instructions,
				v.animal_patient as pet,
				v.guardian,
				v.customer,
				v.doctor,
				v.visit_datetime
			from `tabVet Visit Medication Item` m
			inner join `tabVet Visit` v on v.name = m.parent
			where {" and ".join(conditions)}
			order by v.visit_datetime asc, m.idx asc
			limit %(limit)s
			""",
			values,
			as_dict=True,
		)
		medications = [dict(row) for row in rows]
		enrich_link_aliases(medications, pet_field="pet", guardian_field="guardian", doctor_field="doctor", include_provider=False)
		return ok({"medications": medications}, meta={"total": len(medications)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def dispense_visit_medication(visit=None, row_name=None, medication_row=None, qty=None, warehouse=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		visit_name = cstr(visit or payload.get("visit")).strip()
		row_id = cstr(row_name or medication_row or payload.get("row_name") or payload.get("medication_row")).strip()
		dispense_qty = flt(qty if qty is not None else payload.get("qty"))
		if not visit_name or not row_id:
			return fail(_("Visit and medication row are required."), code="VALIDATION_ERROR")
		doc = frappe.get_doc("Vet Visit", visit_name)
		doc.check_permission("write")
		row = _find_medication_row(doc, row_id)
		if not row:
			return fail(_("Medication row was not found."), code="NOT_FOUND")
		if row.dispense_status in FINAL_STATUSES:
			return fail(_("Medication row cannot be dispensed from its current status."), code="INVALID_STATUS")

		target_warehouse = _resolve_warehouse(row, warehouse or payload.get("warehouse"), required=False)
		if dispense_qty <= 0:
			dispense_qty = max(flt(row.qty) - flt(row.dispensed_qty), 0)
		if dispense_qty <= 0:
			return fail(_("Dispense Qty must be greater than zero."), code="VALIDATION_ERROR")
		if flt(row.dispensed_qty) + dispense_qty > flt(row.qty):
			return fail(_("Dispensed Qty cannot exceed prescribed Qty."), code="VALIDATION_ERROR")

		now = now_datetime()
		source_status = row.dispense_status
		if target_warehouse:
			row.warehouse = target_warehouse
		_apply_invoice_metadata(row, payload)

		# The goods leave here, before anything is written, because this is the step
		# that can legitimately refuse - no stock, a fractional qty against a
		# whole-number UOM, an unresolvable warehouse. Returns None and changes
		# nothing for a medication with no dose option, which is all but four of
		# them today. Row `qty` is a dose COUNT when a dose option is in play, so
		# `dispense_qty` is the number of doses being handed over, never a stock
		# quantity - the conversion happens once, inside issue_for_dispense.
		if is_opted_in(row.medication):
			assert_row_can_record_stock(row.meta, row.medication)
		issued = issue_for_dispense(
			medication=row.medication,
			medication_item=row.medication_item,
			dose_count=dispense_qty,
			dose_option=row.get("dose_option"),
			row_warehouse=row.get("warehouse"),
			reference=_("Vet Visit {0} row {1}").format(doc.name, row.idx),
			label=row.medication or row.medication_item,
		)
		if issued:
			row.warehouse = issued["warehouse"]
			row.stock_issued_qty = flt(row.get("stock_issued_qty")) + flt(issued["qty"])
			row.stock_entry = issued["stock_entry"]

		row.dispensed_qty = flt(row.dispensed_qty) + dispense_qty
		row.dispensed_by = frappe.session.user
		row.dispensed_at = now
		row.dispense_status = "Dispensed" if flt(row.dispensed_qty) >= flt(row.qty) else "Partially Dispensed"
		target_status = row.dispense_status
		doc.flags.ignore_billing_lock = True
		doc.save(ignore_permissions=True)
		ledger = _create_dispense_ledger(
			operation="Dispense",
			visit=doc.name,
			row=row,
			qty=dispense_qty,
			source_status=source_status,
			target_status=target_status,
			notes=payload.get("notes") or payload.get("note"),
		)
		payload_row = _row_payload(row)
		return ok({"medication": payload_row, "visit": doc.name, "ledger": ledger.name})
	except Exception as exc:
		frappe.db.rollback()
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def return_dispensed_medication(visit=None, row_name=None, medication_row=None, qty=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		visit_name = cstr(visit or payload.get("visit")).strip()
		row_id = cstr(row_name or medication_row or payload.get("row_name") or payload.get("medication_row")).strip()
		return_qty = flt(qty if qty is not None else payload.get("qty"))
		if not visit_name or not row_id:
			return fail(_("Visit and medication row are required."), code="VALIDATION_ERROR")
		doc = frappe.get_doc("Vet Visit", visit_name)
		doc.check_permission("write")
		row = _find_medication_row(doc, row_id)
		if not row:
			return fail(_("Medication row was not found."), code="NOT_FOUND")
		if return_qty <= 0:
			return fail(_("Return Qty must be greater than zero."), code="VALIDATION_ERROR")
		if flt(row.return_qty) + return_qty > flt(row.dispensed_qty):
			return fail(_("Return Qty cannot exceed dispensed Qty."), code="VALIDATION_ERROR")

		now = now_datetime()
		source_status = row.dispense_status

		# Put back what this row actually took out, at the rate it took it out at.
		# Computed from the row's own issue history rather than from the dose
		# option, so an option corrected between dispense and return cannot make
		# the reversal a different size than the original movement.
		restore_qty = returnable_stock_qty(
			stock_issued_qty=row.get("stock_issued_qty"),
			dispensed_qty=row.get("dispensed_qty"),
			return_doses=return_qty,
		)
		if restore_qty > 0:
			returned_entry = receive_for_return(
				medication_item=row.medication_item,
				qty=restore_qty,
				warehouse=row.get("warehouse"),
				medication=row.medication,
				reference=_("Vet Visit {0} row {1}").format(doc.name, row.idx),
				label=row.medication or row.medication_item,
			)
			if returned_entry:
				row.stock_issued_qty = max(flt(row.get("stock_issued_qty")) - restore_qty, 0)
				row.stock_entry = returned_entry

		row.return_qty = flt(row.return_qty) + return_qty
		row.returned_by = frappe.session.user
		row.returned_at = now
		row.dispense_status = "Returned" if flt(row.return_qty) >= flt(row.dispensed_qty) else "Partially Dispensed"
		target_status = row.dispense_status
		doc.flags.ignore_billing_lock = True
		doc.save(ignore_permissions=True)
		ledger = _create_dispense_ledger(
			operation="Return",
			visit=doc.name,
			row=row,
			qty=return_qty,
			source_status=source_status,
			target_status=target_status,
			notes=payload.get("notes") or payload.get("note"),
		)
		payload_row = _row_payload(row)
		return ok({"medication": payload_row, "visit": doc.name, "ledger": ledger.name})
	except Exception as exc:
		frappe.db.rollback()
		return _error_response(exc)


def _resolve_warehouse(row, requested=None, required=True):
	warehouse = cstr(requested or row.get("warehouse")).strip()
	if warehouse:
		require_restriction_value("warehouse", warehouse)
		return warehouse
	if required:
		frappe.throw(_("Warehouse is required for medication billing."))
	return None


def _find_medication_row(doc, row_id):
	for row in doc.get("prescribed_medications") or []:
		if row.name == row_id or cstr(row.idx) == row_id:
			return row
	return None


def _apply_invoice_metadata(row, payload: dict):
	dispense_uom = cstr(payload.get("uom") or payload.get("dispense_uom")).strip()
	if dispense_uom:
		row.dispense_uom = dispense_uom
	stock_uom = cstr(payload.get("stock_uom")).strip()
	if stock_uom:
		row.stock_uom = stock_uom
	if payload.get("conversion_factor") not in (None, ""):
		row.conversion_factor = flt(payload.get("conversion_factor"))
	batch_no = cstr(payload.get("batch_no")).strip()
	if batch_no:
		row.batch_no = batch_no
	if payload.get("expiry_date"):
		row.expiry_date = payload.get("expiry_date")


def _create_dispense_ledger(operation, visit, row, qty, source_status, target_status, notes=None):
	ledger = frappe.new_doc("Medication Dispense Ledger")
	ledger.operation = operation
	ledger.visit = visit
	ledger.medication_row = row.name
	ledger.medication = row.medication
	ledger.medication_item = row.medication_item
	ledger.warehouse = row.get("warehouse")
	ledger.qty = qty
	ledger.uom = row.get("dispense_uom")
	ledger.batch_no = row.get("batch_no")
	ledger.expiry_date = row.get("expiry_date")
	ledger.performed_by = frappe.session.user
	ledger.performed_at = now_datetime()
	ledger.source_status = source_status
	ledger.target_status = target_status
	ledger.notes = cstr(notes).strip() or None
	ledger.flags.ignore_permissions = True
	ledger.insert()
	return ledger


def _row_payload(row) -> dict:
	return {
		"name": row.name,
		"medication": row.medication,
		"medication_item": row.medication_item,
		"qty": row.qty,
		"dispense_uom": row.get("dispense_uom"),
		"stock_uom": row.get("stock_uom"),
		"conversion_factor": row.get("conversion_factor"),
		"warehouse": row.warehouse,
		"dispense_status": row.dispense_status,
		"dispensed_qty": row.dispensed_qty,
		"return_qty": row.return_qty,
		"dispensed_by": row.dispensed_by,
		"dispensed_at": row.dispensed_at,
		"returned_by": row.get("returned_by"),
		"returned_at": row.get("returned_at"),
		"batch_no": row.batch_no,
		"expiry_date": row.expiry_date,
		"stock_issued_qty": row.get("stock_issued_qty"),
		"stock_entry": row.get("stock_entry"),
	}


def _payload(data, kwargs) -> dict:
	if isinstance(data, str) and data:
		return json.loads(data)
	if isinstance(data, dict):
		return data
	return kwargs or {}


def _error_response(exc):
	if isinstance(exc, frappe.PermissionError):
		return fail(_("Not permitted"), code="PERMISSION_ERROR")
	return fail(cstr(exc), code=getattr(exc, "exc_type", None) or exc.__class__.__name__)
