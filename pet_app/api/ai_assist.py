from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr

from pet_app.api.response import fail, ok
from pet_app.api.link_aliases import with_link_aliases
from pet_app.utils.clinical_options import clinical_selection_text


@frappe.whitelist()
def summarize_visit(visit):
	try:
		doc = frappe.get_doc("Vet Visit", visit)
		return ok({"summary": _visit_summary(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def generate_owner_friendly_discharge_instructions(visit):
	try:
		doc = frappe.get_doc("Vet Visit", visit)
		home_instructions = _owner_instruction_text(doc)
		parts = [
			"Your pet was seen today.",
			f"Diagnosis: {_plain(doc.diagnosis)}" if doc.diagnosis else None,
			f"Care plan: {_plain(doc.treatment_plan)}" if doc.treatment_plan else None,
			f"Home instructions: {_plain(home_instructions)}" if home_instructions else None,
		]
		return ok({"instructions": "\n".join(part for part in parts if part)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def translate_arabic_english_instructions(text=None, target_language="en"):
	try:
		return ok({"text": text or "", "target_language": target_language, "translated_text": text or "", "note": "Translation adapter placeholder; no clinical changes were made."})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def summarize_pet_timeline(pet):
	try:
		from pet_app.api.medical_file import get_pet_medical_timeline

		timeline = get_pet_medical_timeline(pet=pet, limit=20)
		events = timeline.get("data", {}).get("events", []) if timeline.get("ok") else []
		return ok({"summary": f"{len(events)} recent medical timeline events found.", "events": events[:5]})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def detect_missing_fields(source_type, name):
	try:
		doctype = {"visit": "Vet Visit", "case_sheet": "Vet Case Sheet"}.get(cstr(source_type).lower(), source_type)
		doc = frappe.get_doc(doctype, name)
		missing = [field.fieldname for field in doc.meta.fields if field.reqd and not doc.get(field.fieldname)]
		return ok({"missing_fields": missing}, meta={"total": len(missing)})
	except Exception as exc:
		return _error_response(exc)


def _visit_summary(doc):
	payload = {
		"visit": doc.name,
		"pet": doc.animal_patient,
		"status": doc.status,
		"diagnosis": _plain(doc.diagnosis),
		"treatment_plan": _plain(doc.treatment_plan),
	}
	return with_link_aliases(payload, pet_field="pet", include_guardian=False, include_doctor=False, include_provider=False)


def _owner_instruction_text(doc):
	parts = [clinical_selection_text(doc, "owner_instruction_items"), doc.get("owner_instruction_note")]
	return "\n".join(_plain(part) for part in parts if _plain(part))


def _plain(value):
	return cstr(value).replace("<p>", "").replace("</p>", "").strip()


def _error_response(exc):
	return fail(cstr(exc), code=getattr(exc, "exc_type", None) or exc.__class__.__name__)
