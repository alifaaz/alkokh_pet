from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cstr, now_datetime

from pet_app.api.permissions import require_doctype_permission
from pet_app.api.response import fail, ok
from pet_app.api.workspace import _assert_record_access, _has_field
from pet_app.utils.medical_profile import sync_latest_vitals
from pet_app.workflows import clinical_state


VITAL_FIELDS = {
	"recorded_at",
	"recorded_by",
	"temperature",
	"heart_rate",
	"respiratory_rate",
	"weight",
	"body_condition_score",
	"hydration_status",
	"mucous_membrane",
	"capillary_refill_time",
	"pain_score",
	"blood_pressure",
	"spo2",
	"notes",
}


@frappe.whitelist(methods=["POST"])
def add_visit_vital(visit=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		visit_name = cstr(visit or payload.get("visit") or payload.get("visit_id")).strip()
		if not visit_name:
			return fail(_("Visit is required."), code="VALIDATION_ERROR")

		require_doctype_permission("Vet Visit", "write")
		_assert_record_access("Vet Visit", visit_name, write=True, action="save_clinical_note")
		visit_doc = frappe.get_doc("Vet Visit", visit_name)
		_assert_visit_accepts_vitals(visit_doc)
		_assert_vitals_table()

		row_data = _vital_row_data(payload)
		row = visit_doc.append("vital_signs", row_data)
		visit_doc.save(ignore_permissions=True)
		visit_doc.reload()
		row = _find_vital_row(visit_doc, row.name) or _latest_vital_row(visit_doc)
		sync_latest_vitals(visit_doc, row.as_dict() if row else None)
		return ok({"vital": _vital_payload(row), "vitals": [_vital_payload(item) for item in visit_doc.get("vital_signs") or []]})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def update_visit_vital(visit=None, vital=None, vital_name=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		visit_name = cstr(visit or payload.get("visit") or payload.get("visit_id")).strip()
		row_name = cstr(vital or vital_name or payload.get("vital") or payload.get("vital_name") or payload.get("name")).strip()
		if not row_name:
			return fail(_("Vital row is required."), code="VALIDATION_ERROR")
		if not visit_name:
			visit_name = frappe.db.get_value("Vet Visit Vital Sign", row_name, "parent")
		if not visit_name:
			return fail(_("Visit is required."), code="VALIDATION_ERROR")

		require_doctype_permission("Vet Visit", "write")
		_assert_record_access("Vet Visit", visit_name, write=True, action="save_clinical_note")
		visit_doc = frappe.get_doc("Vet Visit", visit_name)
		_assert_visit_accepts_vitals(visit_doc)
		row = _find_vital_row(visit_doc, row_name)
		if not row:
			return fail(_("Vital row was not found."), code="NOT_FOUND")

		for fieldname, value in payload.items():
			if fieldname in VITAL_FIELDS:
				row.set(fieldname, value)
		if not row.recorded_at:
			row.recorded_at = now_datetime()
		if not row.recorded_by:
			row.recorded_by = frappe.session.user
		visit_doc.save(ignore_permissions=True)
		visit_doc.reload()
		row = _find_vital_row(visit_doc, row_name)
		sync_latest_vitals(visit_doc, row.as_dict() if row else None)
		return ok({"vital": _vital_payload(row), "vitals": [_vital_payload(item) for item in visit_doc.get("vital_signs") or []]})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def list_visit_vitals(visit=None, visit_id=None):
	try:
		visit_name = cstr(visit or visit_id).strip()
		if not visit_name:
			return fail(_("Visit is required."), code="VALIDATION_ERROR")
		if not frappe.db.exists("Vet Visit", visit_name):
			return fail(_("Visit {0} was not found.").format(visit_name), code="NOT_FOUND")

		require_doctype_permission("Vet Visit", "read")
		_assert_record_access("Vet Visit", visit_name)
		_assert_vitals_table()
		visit_doc = frappe.get_doc("Vet Visit", visit_name)
		rows = [_vital_payload(row) for row in visit_doc.get("vital_signs") or []]
		rows.sort(key=lambda row: (cstr(row.get("recorded_at")), row.get("idx") or 0), reverse=True)
		return ok({"items": rows}, meta={"total": len(rows)})
	except Exception as exc:
		return _error_response(exc)


def _assert_visit_accepts_vitals(visit_doc):
	if cstr(visit_doc.get("status")) == "Cancelled":
		frappe.throw(_("Cancelled visits cannot accept vital signs."))
	clinical_state.assert_visit_not_billed(visit_doc)


def _assert_vitals_table():
	if not _has_field("Vet Visit", "vital_signs"):
		frappe.throw(_("Vet Visit vital signs table is missing. Run migrations first."))


def _vital_row_data(payload: dict) -> dict:
	row = {fieldname: payload.get(fieldname) for fieldname in VITAL_FIELDS if fieldname in payload}
	row.setdefault("recorded_at", now_datetime())
	row.setdefault("recorded_by", frappe.session.user)
	return row


def _find_vital_row(visit_doc, row_name: str):
	for row in visit_doc.get("vital_signs") or []:
		if row.name == row_name:
			return row
	return None


def _latest_vital_row(visit_doc):
	rows = list(visit_doc.get("vital_signs") or [])
	if not rows:
		return None
	return max(rows, key=lambda row: (cstr(row.get("recorded_at")), row.idx or 0))


def _vital_payload(row) -> dict:
	if not row:
		return {}
	return {
		"name": row.name,
		"idx": row.idx,
		"recorded_at": row.get("recorded_at"),
		"recorded_by": row.get("recorded_by"),
		"temperature": row.get("temperature"),
		"heart_rate": row.get("heart_rate"),
		"respiratory_rate": row.get("respiratory_rate"),
		"weight": row.get("weight"),
		"body_condition_score": row.get("body_condition_score"),
		"hydration_status": row.get("hydration_status"),
		"mucous_membrane": row.get("mucous_membrane"),
		"capillary_refill_time": row.get("capillary_refill_time"),
		"pain_score": row.get("pain_score"),
		"blood_pressure": row.get("blood_pressure"),
		"spo2": row.get("spo2"),
		"notes": row.get("notes"),
	}


def _payload(data, kwargs) -> dict:
	if isinstance(data, str) and data:
		return json.loads(data)
	if isinstance(data, dict):
		return data
	return kwargs or {}


def _error_response(exc: Exception) -> dict:
	code = "PERMISSION_DENIED" if isinstance(exc, frappe.PermissionError) else "ERROR"
	return fail(cstr(exc), code=code, details=frappe.get_traceback())
