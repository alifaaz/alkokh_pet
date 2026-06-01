from __future__ import annotations

from collections import OrderedDict

import frappe
from frappe.utils import cstr


STANDARD_USERS = {"Administrator", "Guest"}


def before_validate_user_role_profiles(doc, method=None):
	if doc.name in STANDARD_USERS:
		return
	if _has_field(doc, "role_profile_name"):
		doc.role_profile_name = None


def before_save_user_role_profiles(doc, method=None):
	if doc.name in STANDARD_USERS:
		return
	if _has_field(doc, "role_profile_name"):
		doc.role_profile_name = None

	current_profiles = _profile_names_from_user_doc(doc)
	previous_profiles = _profile_names_from_user_doc(doc.get_doc_before_save(), include_legacy=True)
	removed_profiles = previous_profiles - current_profiles
	if not removed_profiles:
		return

	roles_to_keep = _roles_from_role_profiles(current_profiles)
	roles_to_remove = _roles_from_role_profiles(removed_profiles) - roles_to_keep
	if not roles_to_remove:
		return

	doc.set("roles", [row for row in doc.get("roles") or [] if _row_value(row, "role") not in roles_to_remove])


def _profile_names_from_user_doc(doc, include_legacy: bool = False) -> set[str]:
	if not doc:
		return set()

	profiles = OrderedDict()
	for row in doc.get("role_profiles") or []:
		role_profile = cstr(_row_value(row, "role_profile")).strip()
		if role_profile:
			profiles[role_profile] = None

	if include_legacy:
		role_profile_name = cstr(doc.get("role_profile_name")).strip()
		if role_profile_name:
			profiles[role_profile_name] = None

	return set(profiles)


def _roles_from_role_profiles(role_profiles: set[str]) -> set[str]:
	roles = set()
	for role_profile in role_profiles:
		if not frappe.db.exists("Role Profile", role_profile):
			continue
		profile_doc = frappe.get_doc("Role Profile", role_profile)
		roles.update(row.role for row in profile_doc.roles if row.role)
	return roles


def _row_value(row, fieldname: str):
	if isinstance(row, dict):
		return row.get(fieldname)
	return getattr(row, fieldname, None)


def _has_field(doc, fieldname: str) -> bool:
	return bool(getattr(doc, "meta", None) and doc.meta.has_field(fieldname))
