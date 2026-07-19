from __future__ import annotations

from collections.abc import Iterable

import frappe
from frappe import _
from frappe.utils import cstr, now_datetime

from pet_app.api.permissions import get_user_roles, user_has_full_access
from pet_app.utils.practitioner import (
	PRACTITIONER_DOCTYPE,
	get_practitioner_for_user,
	practitioner_payload,
)


TEAM_FIELD = "assigned_practitioners"
VISIT_PRIMARY_FIELD = "primary_practitioner"
DEFAULT_TEAM_ROLE = "Treating Doctor"
SUPERVISOR_ROLES = {"Visit Admin", "System Manager", "Pet App Admin", "Healthcare Administrator"}
DIRECT_ASSIGN_ROLES = {"Coordinator", "Visit Admin", "Healthcare Administrator", "Pet App Admin", "System Manager"}
OPEN_VISIT_STATUSES = {"Draft", "In Progress", "Follow-up Needed"}
VISIT_DOCTOR_TEAM_ERROR = _("Only this visit's doctor can change the care team.")


def validate_doctor_practitioner(practitioner: str | None) -> str:
	practitioner = cstr(practitioner).strip()
	if not practitioner or not frappe.db.exists(PRACTITIONER_DOCTYPE, practitioner):
		frappe.throw(_("Practitioner {0} was not found.").format(frappe.bold(practitioner or "")))
	row = frappe.db.get_value(
		PRACTITIONER_DOCTYPE,
		practitioner,
		["disabled", "practitioner_type"],
		as_dict=True,
	)
	if row.get("disabled"):
		frappe.throw(_("Practitioner {0} is disabled.").format(frappe.bold(practitioner)))
	if row.get("practitioner_type") != "Doctor":
		frappe.throw(_("Practitioner {0} must be a Doctor.").format(frappe.bold(practitioner)))
	return practitioner


def ensure_episode_practitioner(episode, practitioner: str | None, *, role: str | None = None, note: str | None = None) -> bool:
	practitioner = cstr(practitioner).strip()
	if not practitioner or not episode or not episode.meta.has_field(TEAM_FIELD):
		return False

	for row in episode.get(TEAM_FIELD) or []:
		if row.get("practitioner") == practitioner:
			return False

	row = episode.append(
		TEAM_FIELD,
		{
			"practitioner": practitioner,
			"role": role or DEFAULT_TEAM_ROLE,
			"added_by": frappe.session.user,
			"added_at": now_datetime(),
		},
	)
	if note and row.meta.has_field("note"):
		row.note = note
	return True


def remove_episode_practitioner(episode, practitioner: str | None) -> bool:
	practitioner = cstr(practitioner).strip()
	if not practitioner or not episode or not episode.meta.has_field(TEAM_FIELD):
		return False
	removed = False
	for row in list(episode.get(TEAM_FIELD) or []):
		if row.get("practitioner") == practitioner:
			episode.remove(row)
			removed = True

	return removed


def first_episode_team_practitioner(episode, *, exclude: Iterable[str] | None = None) -> str | None:
	excluded = set(exclude or [])
	for practitioner in episode_team_practitioners(episode, include_primary=False):
		if practitioner not in excluded:
			return practitioner
	return None


def episode_team_practitioners(episode, *, include_primary: bool = False) -> list[str]:
	seen: set[str] = set()
	team: list[str] = []
	if episode and episode.meta.has_field(TEAM_FIELD):
		for row in episode.get(TEAM_FIELD) or []:
			practitioner = cstr(row.get("practitioner")).strip()
			if practitioner and practitioner not in seen:
				team.append(practitioner)
				seen.add(practitioner)

	if include_primary:
		primary_doctor = cstr(episode.get("primary_doctor")).strip() if episode else ""
		if primary_doctor and primary_doctor not in seen:
			team.append(primary_doctor)
	return team


def is_supervisor_user(user: str | None = None) -> bool:
	user = user or frappe.session.user
	if user_has_full_access(user):
		return True
	return bool(get_user_roles(user) & SUPERVISOR_ROLES)


def current_user_team_practitioner(episode, user: str | None = None) -> str | None:
	user = user or frappe.session.user
	practitioner = get_practitioner_for_user(user, include_disabled=False)
	if not practitioner or practitioner not in set(episode_team_practitioners(episode)):
		return None
	if not practitioner_is_doctor(practitioner):
		return None
	return practitioner


def current_user_visit_practitioner(visit, user: str | None = None) -> str | None:
	user = user or frappe.session.user
	visit_doctor = visit_practitioner(visit)
	if not visit_doctor:
		return None
	practitioner = get_practitioner_for_user(user, include_disabled=False)
	if practitioner != visit_doctor or not practitioner_is_doctor(practitioner):
		return None
	return practitioner


def can_manage_episode_team(episode, visit=None, user: str | None = None) -> bool:
	return bool(is_supervisor_user(user) or current_user_visit_practitioner(visit, user=user))


def practitioner_is_doctor(practitioner: str | None) -> bool:
	if not practitioner:
		return False
	return frappe.db.get_value(PRACTITIONER_DOCTYPE, practitioner, "practitioner_type") == "Doctor"


def assert_can_manage_episode_team(episode, visit=None):
	if can_manage_episode_team(episode, visit):
		return
	frappe.throw(VISIT_DOCTOR_TEAM_ERROR, frappe.PermissionError)


def assert_can_assign_visit_doctor(episode):
	if user_has_full_access() or current_user_team_practitioner(episode):
		return
	frappe.throw(_("Not permitted"), frappe.PermissionError)


def visit_practitioner(visit) -> str | None:
	if not visit:
		return None
	if hasattr(visit, "get"):
		return cstr(visit.get(VISIT_PRIMARY_FIELD) or visit.get("doctor")).strip() or None
	return None


def set_visit_practitioner(visit, practitioner: str):
	if visit.meta.has_field(VISIT_PRIMARY_FIELD):
		visit.set(VISIT_PRIMARY_FIELD, practitioner)
	visit.set("doctor", practitioner)


def open_visit_assignments_for_practitioner(episode_name: str, practitioner: str) -> list[dict]:
	if not _has_doctype_field("Vet Visit", "care_episode"):
		return []

	fields = ["name", "status", "visit_datetime", "doctor", "visit_type"]
	if _has_doctype_field("Vet Visit", VISIT_PRIMARY_FIELD):
		fields.append(VISIT_PRIMARY_FIELD)

	rows = frappe.get_all(
		"Vet Visit",
		filters={"care_episode": episode_name, "status": ["in", sorted(OPEN_VISIT_STATUSES)]},
		fields=fields,
		order_by="visit_datetime asc, creation asc",
		ignore_permissions=True,
	)
	return [dict(row) for row in rows if _row_visit_practitioner(row) == practitioner or row.get("doctor") == practitioner]


def episode_visit_history(episode_name: str) -> list[dict]:
	if not _has_doctype_field("Vet Visit", "care_episode"):
		return []

	fields = ["name", "status", "visit_datetime", "doctor", "visit_type", "case_sheet"]
	if _has_doctype_field("Vet Visit", VISIT_PRIMARY_FIELD):
		fields.append(VISIT_PRIMARY_FIELD)

	rows = frappe.get_all(
		"Vet Visit",
		filters={"care_episode": episode_name},
		fields=fields,
		order_by="visit_datetime asc, creation asc",
		ignore_permissions=True,
	)
	return [dict(row) for row in rows]


def team_member_payload(practitioner: str | None) -> dict:
	return practitioner_payload(practitioner)


def _row_visit_practitioner(row) -> str | None:
	return cstr(row.get(VISIT_PRIMARY_FIELD) or row.get("doctor")).strip() or None


def _has_doctype_field(doctype: str, fieldname: str) -> bool:
	try:
		return bool(frappe.get_meta(doctype).has_field(fieldname))
	except Exception:
		return False
