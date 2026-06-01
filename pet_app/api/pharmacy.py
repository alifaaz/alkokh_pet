from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import flt, now_datetime, cstr

from pet_app.api.permissions import require_restriction_value
from pet_app.api.response import fail, ok
from pet_app.api.link_aliases import enrich_link_aliases


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
				m.dispensed_qty,
				m.return_qty,
				m.dispense_status,
				m.warehouse,
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
		target_warehouse = _resolve_warehouse(row, warehouse or payload.get("warehouse"))
		require_restriction_value("warehouse", target_warehouse)
		if dispense_qty <= 0:
			dispense_qty = max(flt(row.qty) - flt(row.dispensed_qty), 0)
		if dispense_qty <= 0:
			return fail(_("Dispense Qty must be greater than zero."), code="VALIDATION_ERROR")
		if flt(row.dispensed_qty) + dispense_qty > flt(row.qty):
			return fail(_("Dispensed Qty cannot exceed prescribed Qty."), code="VALIDATION_ERROR")

		row.warehouse = target_warehouse
		row.dispensed_qty = flt(row.dispensed_qty) + dispense_qty
		row.dispensed_by = frappe.session.user
		row.dispensed_at = now_datetime()
		row.batch_no = payload.get("batch_no") or row.batch_no
		row.expiry_date = payload.get("expiry_date") or row.expiry_date
		row.stock_entry = payload.get("stock_entry") or row.stock_entry
		row.dispense_status = "Dispensed" if flt(row.dispensed_qty) >= flt(row.qty) else "Partially Dispensed"
		doc.save(ignore_permissions=True)
		return ok({"medication": _row_payload(row), "visit": doc.name})
	except Exception as exc:
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
		if row.warehouse:
			require_restriction_value("warehouse", row.warehouse)
		if return_qty <= 0:
			return fail(_("Return Qty must be greater than zero."), code="VALIDATION_ERROR")
		if flt(row.return_qty) + return_qty > flt(row.dispensed_qty):
			return fail(_("Return Qty cannot exceed dispensed Qty."), code="VALIDATION_ERROR")
		row.return_qty = flt(row.return_qty) + return_qty
		row.dispense_status = "Returned" if flt(row.return_qty) >= flt(row.dispensed_qty) else "Partially Dispensed"
		doc.save(ignore_permissions=True)
		return ok({"medication": _row_payload(row), "visit": doc.name})
	except Exception as exc:
		return _error_response(exc)


def _resolve_warehouse(row, requested=None):
	warehouse = cstr(requested or row.warehouse).strip()
	if warehouse:
		return warehouse
	if row.medication:
		warehouse = frappe.db.get_value("Medication", row.medication, "default_warehouse")
	if not warehouse:
		warehouse = frappe.db.get_single_value("Stock Settings", "default_warehouse")
	if not warehouse:
		frappe.throw(_("Warehouse is required to dispense medication."))
	return warehouse


def _find_medication_row(doc, row_id):
	for row in doc.get("prescribed_medications") or []:
		if row.name == row_id or cstr(row.idx) == row_id:
			return row
	return None


def _row_payload(row) -> dict:
	return {
		"name": row.name,
		"medication": row.medication,
		"medication_item": row.medication_item,
		"qty": row.qty,
		"warehouse": row.warehouse,
		"dispense_status": row.dispense_status,
		"dispensed_qty": row.dispensed_qty,
		"return_qty": row.return_qty,
		"dispensed_by": row.dispensed_by,
		"dispensed_at": row.dispensed_at,
		"batch_no": row.batch_no,
		"expiry_date": row.expiry_date,
		"stock_entry": row.stock_entry,
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
