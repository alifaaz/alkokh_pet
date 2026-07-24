from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, cstr

from pet_app.api.response import fail, ok
from pet_app.api.link_aliases import with_link_aliases
from pet_app.api import workspace
from pet_app.utils.clinical_options import visit_clinical_payload


@frappe.whitelist()
def get_visit_summary(visit=None, visit_name=None):
	try:
		visit_id = cstr(visit or visit_name).strip()
		if not visit_id:
			return fail(_("Visit is required."), code="VALIDATION_ERROR")
		return ok({"visit": workspace.get_record("Vet Visit", visit_id)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_prescription(visit=None, visit_name=None):
	try:
		doc = _get_doc("Vet Visit", visit or visit_name)
		return ok(
			{
				"visit": _visit_header(doc),
				"medications": [
					{
						"name": row.name,
						"medication": row.medication,
						"medication_item": row.medication_item,
						"qty": row.qty,
						"dosage": row.dosage,
						"frequency": row.frequency,
						"duration_days": row.duration_days,
						"instructions": row.instructions,
						"dispense_status": row.get("dispense_status"),
						"dispensed_qty": row.get("dispensed_qty"),
					}
					for row in doc.get("prescribed_medications") or []
				],
			}
		)
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_lab_result(lab=None, name=None):
	try:
		doc = _get_doc("Lab", lab or name)
		return ok({"lab": _diagnostic_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_imaging_report(imaging=None, name=None):
	try:
		doc = _get_doc("Imaging", imaging or name)
		return ok({"imaging": _diagnostic_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_procedure_report(procedure=None, name=None):
	try:
		doc = _get_doc("Pet Procedure", procedure or name)
		procedure = {
			"name": doc.name,
			"visit": doc.visit,
			"pet": doc.pet,
			"guardian": doc.guardian,
			"doctor": doc.doctor,
			"provider": doc.provider,
			"procedure_template": doc.procedure_template,
			"care_service": doc.care_service,
			"status": doc.status,
			"scheduled_at": doc.scheduled_at,
			"started_at": doc.started_at,
			"completed_at": doc.completed_at,
			"closed_at": doc.closed_at,
			"indication": doc.indication,
			"consent_obtained": cint(doc.consent_obtained),
			"anesthesia_used": cint(doc.anesthesia_used),
			"procedure_note": doc.procedure_note,
			"findings": doc.findings,
			"outcome": doc.outcome,
			"complications": doc.complications,
			"aftercare_instructions": doc.aftercare_instructions,
			"checklist": [
				{"step_title": row.step_title, "required": cint(row.required), "done": cint(row.done), "note": row.note}
				for row in doc.get("checklist") or []
			],
			"consents": _signed_consents(doc),
		}
		return ok(
			{
				"procedure": with_link_aliases(
					procedure,
					pet_field="pet",
					guardian_field="guardian",
					doctor_field="doctor",
					provider_field="provider",
				)
			}
		)
	except Exception as exc:
		return _error_response(exc)


def _get_doc(doctype, name):
	name = cstr(name).strip()
	if not name:
		frappe.throw(_("{0} is required.").format(doctype))
	doc = frappe.get_doc(doctype, name)
	doc.check_permission("read")
	return doc


def _visit_header(doc) -> dict:
	clinical_note = visit_clinical_payload(doc)
	payload = {
		"name": doc.name,
		"visit_datetime": doc.visit_datetime,
		"pet": doc.animal_patient,
		"guardian": doc.guardian,
		"customer": doc.customer,
		"doctor": doc.doctor,
		"diagnosis": doc.diagnosis,
		"treatment_plan": doc.treatment_plan,
		"doctor_note": doc.get("doctor_note"),
		"owner_instruction_note": doc.get("owner_instruction_note"),
		"clinical_note": clinical_note,
	}
	return with_link_aliases(payload, pet_field="pet", guardian_field="guardian", doctor_field="doctor", include_provider=False)


def _diagnostic_payload(doc) -> dict:
	visit = frappe.get_doc("Vet Visit", doc.visit) if doc.visit and frappe.db.exists("Vet Visit", doc.visit) else None
	payload = {
		"name": doc.name,
		"doctype": doc.doctype,
		"visit": doc.visit,
		"visit_header": _visit_header(visit) if visit else {},
		"order_id": doc.get("order_id"),
		"pet": doc.pet,
		"doctor": doc.doctor,
		"care_service": doc.care_service,
		"item_code": doc.get("item_code"),
		"body_part": doc.get("body_part"),
		"modality": doc.get("modality"),
		"status": doc.status,
		"result": doc.get("result"),
		"report": doc.get("report"),
		"image": doc.get("image"),
		"released_by": doc.get("released_by"),
		"released_at": doc.get("released_at"),
		"doctor_reviewed": cint(doc.get("doctor_reviewed")),
		"doctor_reviewed_at": doc.get("doctor_reviewed_at"),
		"result_visibility": doc.get("result_visibility"),
		"attachments": workspace._attachments_for(doc.doctype, doc.name),
	}
	return with_link_aliases(payload, pet_field="pet", doctor_field="doctor", include_guardian=False, include_provider=False)


def _signed_consents(procedure):
	if not frappe.db.exists("DocType", "Pet Consent Form"):
		return []
	rows = frappe.get_all(
		"Pet Consent Form",
		filters={"procedure": procedure.name, "status": "Signed"},
		fields=["name", "template", "signed_by", "signed_at", "witness", "signature"],
		order_by="signed_at desc",
		ignore_permissions=True,
	)
	return [dict(row) for row in rows]


def _error_response(exc):
	if isinstance(exc, frappe.PermissionError):
		return fail(_("Not permitted"), code="PERMISSION_ERROR")
	return fail(cstr(exc), code=getattr(exc, "exc_type", None) or exc.__class__.__name__)



