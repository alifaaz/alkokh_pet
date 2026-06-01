from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, cstr, now_datetime

from pet_app.api.response import fail, ok


@frappe.whitelist()
def list_templates(visit_type=None, species=None, active=1):
	try:
		filters = {}
		if cint(active):
			filters["active"] = 1
		if visit_type:
			filters["visit_type"] = visit_type
		rows = frappe.get_all(
			"Vet Case Sheet Template",
			filters=filters,
			fields=["name", "template_name", "visit_type", "species", "active", "is_default", "description"],
			order_by="visit_type asc, is_default desc, template_name asc",
			ignore_permissions=False,
		)
		if species:
			species_text = cstr(species).strip().lower()
			rows = [row for row in rows if not cstr(row.species).strip() or cstr(row.species).strip().lower() == species_text]
		return ok({"templates": [dict(row) for row in rows]}, meta={"total": len(rows)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_template(template=None, name=None):
	try:
		template_name = cstr(template or name).strip()
		if not template_name:
			return fail(_("Template is required."), code="VALIDATION_ERROR")
		doc = frappe.get_doc("Vet Case Sheet Template", template_name)
		doc.check_permission("read")
		return ok({"template": _template_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def apply_template_to_case_sheet(case_sheet=None, template=None, visit_type=None, species=None):
	try:
		case_sheet_name = cstr(case_sheet).strip()
		if not case_sheet_name:
			return fail(_("Case Sheet is required."), code="VALIDATION_ERROR")
		case_doc = frappe.get_doc("Vet Case Sheet", case_sheet_name)
		case_doc.check_permission("write")
		template_doc = _resolve_template(template, visit_type, species or case_doc.get("species"))
		if not template_doc:
			return fail(_("No matching active template was found."), code="NOT_FOUND")

		responses = []
		changed_case = False
		for item in _sorted_items(template_doc):
			response = _upsert_response(
				case_sheet=case_doc.name,
				template=template_doc.name,
				item=item.name,
				field_key=item.field_key,
				label=item.label,
				value=item.default_value,
			)
			responses.append(_response_payload(response))
			if item.default_value not in (None, "") and case_doc.meta.has_field(item.field_key):
				if case_doc.get(item.field_key) in (None, ""):
					case_doc.set(item.field_key, item.default_value)
					changed_case = True
		if changed_case:
			case_doc.save(ignore_permissions=True)
		return ok({"template": _template_payload(template_doc), "responses": responses})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def save_case_sheet_responses(case_sheet=None, template=None, responses=None, data=None):
	try:
		payload = _coerce_dict(data)
		case_sheet_name = cstr(case_sheet or payload.get("case_sheet")).strip()
		template_name = cstr(template or payload.get("template")).strip()
		rows = _coerce_list(responses if responses is not None else payload.get("responses"))
		if not case_sheet_name:
			return fail(_("Case Sheet is required."), code="VALIDATION_ERROR")
		if not rows:
			return fail(_("At least one response is required."), code="VALIDATION_ERROR")

		case_doc = frappe.get_doc("Vet Case Sheet", case_sheet_name)
		case_doc.check_permission("write")
		template_doc = frappe.get_doc("Vet Case Sheet Template", template_name) if template_name else None
		required = _required_field_keys(template_doc) if template_doc else set()
		seen = set()
		saved = []
		for raw in rows:
			row = _coerce_dict(raw)
			field_key = cstr(row.get("field_key") or row.get("key")).strip()
			if not field_key:
				return fail(_("Each response needs a field_key."), code="VALIDATION_ERROR")
			seen.add(field_key)
			value_json = row.get("value_json")
			value = row.get("value")
			if isinstance(value, (dict, list)) and not value_json:
				value_json = json.dumps(value, default=str)
				value = None
			response = _upsert_response(
				case_sheet=case_sheet_name,
				template=template_name,
				item=row.get("item"),
				field_key=field_key,
				label=row.get("label"),
				value=value,
				value_json=value_json,
			)
			saved.append(_response_payload(response))

		missing = sorted(required - seen)
		if missing:
			return fail(_("Required case sheet response(s) are missing."), code="VALIDATION_ERROR", details={"missing": missing})
		return ok({"responses": saved}, meta={"total": len(saved)})
	except Exception as exc:
		return _error_response(exc)


def _resolve_template(template=None, visit_type=None, species=None):
	if template:
		doc = frappe.get_doc("Vet Case Sheet Template", template)
		doc.check_permission("read")
		return doc
	filters = {"active": 1}
	if visit_type:
		filters["visit_type"] = visit_type
	rows = frappe.get_all(
		"Vet Case Sheet Template",
		filters=filters,
		fields=["name", "species", "is_default"],
		order_by="is_default desc, modified desc",
		ignore_permissions=False,
	)
	species_text = cstr(species).strip().lower()
	for row in rows:
		if species_text and cstr(row.species).strip() and cstr(row.species).strip().lower() != species_text:
			continue
		return frappe.get_doc("Vet Case Sheet Template", row.name)
	return None


def _template_payload(doc) -> dict:
	return {
		"name": doc.name,
		"template_name": doc.template_name,
		"visit_type": doc.visit_type,
		"species": doc.species,
		"active": cint(doc.active),
		"is_default": cint(doc.is_default),
		"description": doc.description,
		"items": [_item_payload(row) for row in _sorted_items(doc)],
	}


def _item_payload(row) -> dict:
	return {
		"name": row.name,
		"section": row.section,
		"label": row.label,
		"field_key": row.field_key,
		"fieldtype": row.fieldtype,
		"required": cint(row.required),
		"options": row.options,
		"link_doctype": row.link_doctype,
		"default_value": row.default_value,
		"sort_order": row.sort_order,
		"help_text": row.help_text,
	}


def _response_payload(doc) -> dict:
	return {
		"name": doc.name,
		"case_sheet": doc.case_sheet,
		"template": doc.template,
		"item": doc.item,
		"field_key": doc.field_key,
		"label": doc.label,
		"value": doc.value,
		"value_json": doc.value_json,
		"recorded_by": doc.recorded_by,
		"recorded_at": doc.recorded_at,
	}


def _sorted_items(doc):
	return sorted(doc.get("items") or [], key=lambda row: (cint(row.sort_order), row.idx))


def _required_field_keys(template_doc) -> set[str]:
	return {row.field_key for row in _sorted_items(template_doc) if cint(row.required)}


def _upsert_response(*, case_sheet, template=None, item=None, field_key=None, label=None, value=None, value_json=None):
	filters = {"case_sheet": case_sheet, "field_key": field_key}
	if template:
		filters["template"] = template
	name = frappe.db.get_value("Vet Case Sheet Response", filters, "name")
	if name:
		doc = frappe.get_doc("Vet Case Sheet Response", name)
	else:
		doc = frappe.new_doc("Vet Case Sheet Response")
		doc.case_sheet = case_sheet
		doc.field_key = field_key
	doc.template = template
	doc.item = item or doc.item
	doc.label = label or doc.label or field_key
	if value is not None:
		doc.value = cstr(value)
	if value_json is not None:
		doc.value_json = cstr(value_json)
	doc.recorded_by = frappe.session.user
	doc.recorded_at = now_datetime()
	doc.save(ignore_permissions=True)
	return doc


def _coerce_dict(value: Any) -> dict:
	if not value:
		return {}
	if isinstance(value, str):
		return json.loads(value)
	return dict(value)


def _coerce_list(value: Any) -> list:
	if not value:
		return []
	if isinstance(value, str):
		return json.loads(value)
	return list(value)


def _error_response(exc):
	if isinstance(exc, frappe.PermissionError):
		return fail(_("Not permitted"), code="PERMISSION_ERROR")
	return fail(cstr(exc), code=getattr(exc, "exc_type", None) or exc.__class__.__name__)
