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


class _UpdatePetError(Exception):
	pass


def _resolve_update_pet(boarding_doc, requested):
	"""Which animal this update is about.

	Named explicitly, or inferred only when there is exactly one animal it could be.
	A booking holding several and no `pet` is refused rather than defaulted: guessing
	there is how every update on a three-pet stay ended up on one pet's record.

	Both membership shapes are handled by `pet_is_on_boarding` / `boarding_pets`: a booking
	with no occupant rows - a Pending Room stay from a visit, or one the backfill never
	reached - falls back to the legacy scalar, and holds one animal by construction, so
	there is nothing to guess there either.
	"""
	from pet_app.utils.boarding_occupancy import boarding_pets, pet_is_on_boarding

	requested = cstr(requested).strip()
	active = boarding_pets(boarding_doc, active_only=True)

	if requested:
		if not pet_is_on_boarding(boarding_doc, requested, active_only=True):
			raise _UpdatePetError(
				_("Pet {0} is not an active occupant of Pet Boarding {1}.").format(requested, boarding_doc.name)
			)
		return requested

	if len(active) == 1:
		return active[0]
	if len(active) > 1:
		raise _UpdatePetError(
			_("Pet Boarding {0} holds {1} animals ({2}). Send `pet` to say which one this update is about.").format(
				boarding_doc.name, len(active), ", ".join(active)
			)
		)
	return cstr(boarding_doc.pet).strip() or None


@frappe.whitelist(methods=["POST"])
def add_boarding_update(boarding=None, update_type="Daily Log", summary=None, file=None, data=None, pet=None, **kwargs):
	"""Post a daily log, photo or incident report against ONE animal on a stay.

	`pet` is new and optional. It used to be absent entirely and every update was filed
	against `Pet Boarding.pet` - the legacy single-pet scalar - so on a booking holding
	three animals every photo, log and incident report was attributed to whichever one
	that scalar named. Nothing failed; the wrong animal simply went on the record, and
	these are guardian-visible. A misattributed incident report is worse than a refused
	one because nobody finds out.
	"""
	try:
		payload = _payload(data, kwargs)
		boarding_name = boarding or payload.get("boarding")
		if not boarding_name:
			return fail(_("Boarding is required."), code="VALIDATION_ERROR")
		boarding_doc = frappe.get_doc("Pet Boarding", boarding_name)
		try:
			update_pet = _resolve_update_pet(boarding_doc, pet or payload.get("pet") or payload.get("pet_id"))
		except _UpdatePetError as exc:
			return fail(str(exc), code="VALIDATION_ERROR")
		doctype = {
			"Daily Log": "Pet Boarding Daily Log",
			"Media": "Pet Boarding Media Update",
			"Incident": "Pet Boarding Incident Report",
		}.get(update_type or payload.get("update_type"), "Pet Boarding Daily Log")
		if doctype == "Pet Boarding Media Update":
			doc = frappe.get_doc({"doctype": doctype, "boarding": boarding_name, "pet": update_pet, "file": file or payload.get("file"), "caption": summary or payload.get("summary"), "guardian_visible": cint(payload.get("guardian_visible", 1)), "posted_at": now_datetime()})
		elif doctype == "Pet Boarding Incident Report":
			doc = frappe.get_doc({"doctype": doctype, "boarding": boarding_name, "pet": update_pet, "incident_title": summary or payload.get("summary"), "incident_datetime": now_datetime(), "guardian_visible_summary": payload.get("guardian_visible_summary"), "internal_note": payload.get("internal_note"), "status": "Reported"})
		else:
			doc = frappe.get_doc({"doctype": doctype, "boarding": boarding_name, "pet": update_pet, "log_datetime": now_datetime(), "summary": summary or payload.get("summary"), "internal_note": payload.get("internal_note")})
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
