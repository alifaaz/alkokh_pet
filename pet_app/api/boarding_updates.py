from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, cstr, now_datetime

from pet_app.api.link_aliases import with_link_aliases
from pet_app.api.response import fail, ok


@frappe.whitelist()
def get_owner_boarding_updates(boarding=None, pet=None):
	try:
		guardian = _current_guardian()
		boardings = _allowed_boardings(guardian, boarding=boarding, pet=pet)
		updates = []
		for boarding_name in boardings:
			updates.extend(_updates_for_boarding(boarding_name))
		updates.sort(key=lambda row: cstr(row.get("at")), reverse=True)
		return ok({"updates": updates}, meta={"total": len(updates)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def add_boarding_update(boarding=None, update_type="Daily Log", summary=None, file=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		boarding_name = boarding or payload.get("boarding")
		if not boarding_name:
			return fail(_("Boarding is required."), code="VALIDATION_ERROR")
		boarding_doc = frappe.get_doc("Pet Boarding", boarding_name)
		doctype = {
			"Daily Log": "Pet Boarding Daily Log",
			"Media": "Pet Boarding Media Update",
			"Incident": "Pet Boarding Incident Report",
		}.get(update_type or payload.get("update_type"), "Pet Boarding Daily Log")
		if doctype == "Pet Boarding Media Update":
			doc = frappe.get_doc({"doctype": doctype, "boarding": boarding_name, "pet": boarding_doc.pet, "file": file or payload.get("file"), "caption": summary or payload.get("summary"), "guardian_visible": cint(payload.get("guardian_visible", 1)), "posted_at": now_datetime()})
		elif doctype == "Pet Boarding Incident Report":
			doc = frappe.get_doc({"doctype": doctype, "boarding": boarding_name, "pet": boarding_doc.pet, "incident_title": summary or payload.get("summary"), "incident_datetime": now_datetime(), "guardian_visible_summary": payload.get("guardian_visible_summary"), "internal_note": payload.get("internal_note"), "status": "Reported"})
		else:
			doc = frappe.get_doc({"doctype": doctype, "boarding": boarding_name, "pet": boarding_doc.pet, "log_datetime": now_datetime(), "summary": summary or payload.get("summary"), "internal_note": payload.get("internal_note")})
		doc.insert(ignore_permissions=True)
		return ok({"update": _owner_update_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


def _current_guardian():
	guardian = frappe.db.get_value("Guardian", {"user_id": frappe.session.user}, "name")
	if not guardian:
		frappe.throw(_("No Guardian is linked to the current user."), frappe.PermissionError)
	return guardian


def _allowed_boardings(guardian, boarding=None, pet=None):
	filters = {"guardian": guardian}
	if boarding:
		filters["name"] = boarding
	if pet:
		filters["pet"] = pet
	return frappe.get_all("Pet Boarding", filters=filters, pluck="name", ignore_permissions=True)


def _updates_for_boarding(boarding_name):
	updates = []
	for doctype, fields in {
		"Pet Boarding Daily Log": ["name", "boarding", "pet", "log_datetime", "summary"],
		"Pet Boarding Media Update": ["name", "boarding", "pet", "posted_at", "caption", "file", "guardian_visible"],
		"Pet Boarding Incident Report": ["name", "boarding", "pet", "incident_datetime", "incident_title", "guardian_visible_summary", "status"],
	}.items():
		if not frappe.db.exists("DocType", doctype):
			continue
		rows = frappe.get_all(doctype, filters={"boarding": boarding_name}, fields=fields, ignore_permissions=True)
		for row in rows:
			if "guardian_visible" in row and not cint(row.guardian_visible):
				continue
			updates.append(_owner_update_payload(row, doctype=doctype))
	return updates


def _owner_update_payload(doc, doctype=None):
	doctype = doctype or doc.doctype
	payload = {
		"source_doctype": doctype,
		"name": doc.get("name"),
		"boarding": doc.get("boarding"),
		"pet": doc.get("pet"),
		"at": doc.get("log_datetime") or doc.get("posted_at") or doc.get("incident_datetime"),
		"title": doc.get("summary") or doc.get("caption") or doc.get("incident_title"),
		"summary": doc.get("summary") or doc.get("caption") or doc.get("guardian_visible_summary"),
		"file": doc.get("file"),
		"status": doc.get("status"),
	}
	return with_link_aliases(payload, pet_field="pet", include_guardian=False, include_doctor=False, include_provider=False)


def _payload(data, kwargs):
	if isinstance(data, str) and data:
		return json.loads(data)
	if isinstance(data, dict):
		return data
	return kwargs or {}


def _error_response(exc):
	if isinstance(exc, frappe.PermissionError):
		return fail(_("Not permitted"), code="PERMISSION_ERROR")
	return fail(cstr(exc), code=getattr(exc, "exc_type", None) or exc.__class__.__name__)
