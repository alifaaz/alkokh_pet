from __future__ import annotations

import frappe
from frappe import _


PRACTITIONER_DOCTYPE = "Healthcare Practitioner"


def get_practitioner_for_user(user: str | None = None, include_disabled: bool = True) -> str | None:
	user = user or frappe.session.user
	if not user or not frappe.db.exists("DocType", PRACTITIONER_DOCTYPE):
		return None

	name = frappe.db.get_value(PRACTITIONER_DOCTYPE, {"user_id": user, "disabled": 0}, "name")
	if name or not include_disabled:
		return name
	return frappe.db.get_value(PRACTITIONER_DOCTYPE, {"user_id": user}, "name")


def practitioner_exists(name: str | None) -> bool:
	return bool(name and frappe.db.exists(PRACTITIONER_DOCTYPE, name))


def practitioner_is_active(name: str | None) -> bool:
	if not name or not frappe.db.exists("DocType", PRACTITIONER_DOCTYPE):
		return False
	disabled = frappe.db.get_value(PRACTITIONER_DOCTYPE, name, "disabled")
	if disabled is None:  # record does not exist
		return False
	return not disabled


def resolve_practitioner(explicit: str | None = None, case_sheet=None) -> str:
	"""Resolve the practitioner for a visit.

	Priority: explicit arg > case sheet ``practitioner`` field > session user.
	Validates the explicit/field choice exists and is enabled. Throws when the
	chosen practitioner is unknown/disabled, or when nothing can be resolved.
	"""
	chosen = explicit.strip() if isinstance(explicit, str) else explicit
	source_was_explicit = bool(chosen)
	if not chosen and case_sheet is not None and hasattr(case_sheet, "get"):
		field_value = case_sheet.get("practitioner")
		if field_value:
			chosen, source_was_explicit = field_value, True

	if chosen and source_was_explicit:
		if not practitioner_is_active(chosen):
			frappe.throw(_("Practitioner {0} not found or is disabled.").format(chosen))
		return chosen

	fallback = get_practitioner_for_user(frappe.session.user)
	if not fallback:
		frappe.throw(_("Healthcare Practitioner is required to convert to a visit."))
	return fallback


def practitioner_payload(name: str | None) -> dict:
	if not practitioner_exists(name):
		return {}
	row = frappe.db.get_value(
		PRACTITIONER_DOCTYPE,
		name,
		["name", "practitioner_name", "practitioner_type", "user_id", "specialization", "photo"],
		as_dict=True,
	)
	return {
		"id": row.name,
		"name": row.name,
		"name_label": row.practitioner_name or row.name,
		"practitioner_type": row.practitioner_type,
		"user": row.user_id,
		"user_id": row.user_id,
		"specialization": row.specialization,
		"image": row.photo,
	}
