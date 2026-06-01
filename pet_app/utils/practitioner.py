from __future__ import annotations

import frappe


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
