from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cstr

from pet_app.api.response import fail, ok


SUPPORTED_IMPORTS = {
	"Guardians": "Guardian",
	"Pets": "Pet",
	"PetGuardian links": "PetGuardian",
	"Products": "Product",
	"Medications": "Medication",
	"Services": "CareService template",
}


@frappe.whitelist(methods=["POST"])
def create_import_job(import_type=None, rows=None, dry_run=1, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		import_type = import_type or payload.get("import_type")
		rows = _rows(rows if rows is not None else payload.get("rows"))
		if import_type not in SUPPORTED_IMPORTS and import_type not in ("Medical History", "Opening Stock", "Outstanding Balances"):
			return fail(_("Unsupported import type."), code="VALIDATION_ERROR")
		job = frappe.get_doc(
			{
				"doctype": "Pet App Import Job",
				"import_type": import_type,
				"dry_run": int(dry_run if dry_run is not None else payload.get("dry_run", 1)),
				"status": "Draft",
				"payload_json": json.dumps(rows, default=str),
			}
		).insert(ignore_permissions=True)
		result = validate_import_job(job.name)
		if not result.get("ok"):
			return result
		if not job.dry_run:
			return commit_import_job(job.name)
		return result
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def validate_import_job(job):
	try:
		doc = frappe.get_doc("Pet App Import Job", job)
		_clear_errors(doc.name)
		rows = _rows(doc.payload_json)
		errors = []
		target = SUPPORTED_IMPORTS.get(doc.import_type)
		for index, row in enumerate(rows, start=1):
			if not isinstance(row, dict):
				errors.append(_error(doc.name, index, None, "Row must be an object.", row))
				continue
			if target and not row.get("doctype"):
				row["doctype"] = target
			if target and row.get("doctype") != target:
				errors.append(_error(doc.name, index, "doctype", f"Expected DocType {target}.", row))
			if target and target in {"Guardian", "Pet", "Product", "Medication", "CareService template"}:
				required = {
					"Guardian": "phone",
					"Pet": "pet_name",
					"Product": "product_name",
					"Medication": "medication_name",
					"CareService template": "service_name",
				}[target]
				if not row.get(required):
					errors.append(_error(doc.name, index, required, f"{required} is required.", row))
		doc.status = "Validated" if not errors else "Failed"
		doc.summary_json = json.dumps({"rows": len(rows), "errors": len(errors), "dry_run": bool(doc.dry_run)}, default=str)
		doc.save(ignore_permissions=True)
		return ok({"job": _job_payload(doc), "errors": errors}, meta={"total_errors": len(errors)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def commit_import_job(job):
	try:
		doc = frappe.get_doc("Pet App Import Job", job)
		validation = validate_import_job(job)
		if not validation.get("ok") or validation["meta"].get("total_errors"):
			return validation
		doc.reload()
		rows = _rows(doc.payload_json)
		created = []
		for row in rows:
			target = SUPPORTED_IMPORTS.get(doc.import_type)
			if target:
				row["doctype"] = target
				created.append(frappe.get_doc(row).insert(ignore_permissions=True).name)
		doc.status = "Committed"
		doc.dry_run = 0
		doc.summary_json = json.dumps({"created": created, "rows": len(rows)}, default=str)
		doc.save(ignore_permissions=True)
		return ok({"job": _job_payload(doc), "created": created}, meta={"total_created": len(created)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_import_errors(job):
	try:
		rows = frappe.get_all("Pet App Import Error", filters={"import_job": job}, fields=["name", "row_no", "fieldname", "message", "raw_json"], order_by="row_no asc", ignore_permissions=True)
		return ok({"errors": [dict(row) for row in rows]}, meta={"total": len(rows)})
	except Exception as exc:
		return _error_response(exc)


def _error(job, row_no, fieldname, message, raw):
	doc = frappe.get_doc({"doctype": "Pet App Import Error", "import_job": job, "row_no": row_no, "fieldname": fieldname, "message": message, "raw_json": json.dumps(raw, default=str)})
	doc.insert(ignore_permissions=True)
	return {"row_no": row_no, "fieldname": fieldname, "message": message}


def _clear_errors(job):
	for name in frappe.get_all("Pet App Import Error", filters={"import_job": job}, pluck="name", ignore_permissions=True):
		frappe.delete_doc("Pet App Import Error", name, force=True, ignore_permissions=True)


def _job_payload(doc):
	return {"name": doc.name, "import_type": doc.import_type, "dry_run": doc.dry_run, "status": doc.status, "summary_json": doc.summary_json}


def _rows(value):
	if not value:
		return []
	if isinstance(value, str):
		return json.loads(value)
	return list(value)


def _payload(data, kwargs):
	if isinstance(data, str) and data:
		return json.loads(data)
	if isinstance(data, dict):
		return data
	return kwargs or {}


def _error_response(exc):
	return fail(cstr(exc), code=getattr(exc, "exc_type", None) or exc.__class__.__name__)
