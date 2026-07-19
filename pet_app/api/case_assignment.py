from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cstr

from pet_app.api.permissions import get_user_roles, user_has_full_access
from pet_app.api.response import fail, ok
from pet_app.api.visit_referral import create_visit_referral as _create_visit_referral
from pet_app.pet_app.doctype.pet_care_episode.pet_care_episode import ACTIVE_EPISODE_STATUSES
from pet_app.utils.case_assignment import (
	DEFAULT_TEAM_ROLE,
	DIRECT_ASSIGN_ROLES,
	SUPERVISOR_ROLES,
	assert_can_manage_episode_team,
	can_manage_episode_team,
	current_user_team_practitioner,
	current_user_visit_practitioner,
	ensure_episode_practitioner,
	episode_visit_history,
	is_supervisor_user,
	open_visit_assignments_for_practitioner,
	remove_episode_practitioner,
	set_visit_practitioner,
	team_member_payload,
	validate_doctor_practitioner,
	visit_practitioner,
)


@frappe.whitelist(methods=["POST"])
def add_episode_doctor(episode=None, practitioner=None, visit=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		episode_doc = _episode_doc(_arg(episode, payload, "episode", "care_episode"))
		visit_doc = _required_visit_context_doc(_arg(visit, payload, "visit", "vet_visit", "source_visit"), episode_doc)
		practitioner_name = _arg(practitioner, payload, "practitioner", "doctor", "primary_practitioner")
		if not practitioner_name:
			return fail(_("Practitioner is required."), code="VALIDATION_ERROR")

		assert_can_manage_episode_team(episode_doc, visit_doc)
		validate_doctor_practitioner(practitioner_name)
		changed = ensure_episode_practitioner(
			episode_doc,
			practitioner_name,
			role=payload.get("role") or DEFAULT_TEAM_ROLE,
			note=payload.get("note"),
		)
		if changed:
			episode_doc.save(ignore_permissions=True)

		result = _care_team_payload(episode_doc.name, visit_doc.name)
		result["added"] = bool(changed)
		return ok(result)
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def remove_episode_doctor(episode=None, practitioner=None, visit=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		episode_doc = _episode_doc(_arg(episode, payload, "episode", "care_episode"))
		visit_doc = _required_visit_context_doc(_arg(visit, payload, "visit", "vet_visit", "source_visit"), episode_doc)
		practitioner_name = _arg(practitioner, payload, "practitioner", "doctor", "primary_practitioner")
		if not practitioner_name:
			return fail(_("Practitioner is required."), code="VALIDATION_ERROR")

		assert_can_manage_episode_team(episode_doc, visit_doc)
		validate_doctor_practitioner(practitioner_name)

		blocking_visits = open_visit_assignments_for_practitioner(episode_doc.name, practitioner_name)
		if blocking_visits:
			return fail(
				_("Cannot remove practitioner {0}; they are assigned to open visit(s).").format(
					frappe.bold(practitioner_name)
				),
				code="PRACTITIONER_HAS_OPEN_VISITS",
				data={"blocking_visits": [_visit_blocker_payload(row) for row in blocking_visits]},
			)

		changed = remove_episode_practitioner(episode_doc, practitioner_name)
		if not changed:
			return fail(
				_("Practitioner {0} is not on the care team.").format(frappe.bold(practitioner_name)),
				code="PRACTITIONER_NOT_ON_TEAM",
			)
		episode_doc.save(ignore_permissions=True)

		result = _care_team_payload(episode_doc.name, visit_doc.name)
		result["removed"] = bool(changed)
		return ok(result)
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def assign_visit_doctor(visit=None, practitioner=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		visit_doc = _visit_doc(_arg(visit, payload, "visit", "vet_visit"))
		episode_doc = _episode_doc(visit_doc.get("care_episode"))
		practitioner_name = _arg(practitioner, payload, "practitioner", "doctor", "primary_practitioner")
		if not practitioner_name:
			return fail(_("Practitioner is required."), code="VALIDATION_ERROR")

		permission_error = _direct_assign_permission_error()
		if permission_error:
			return fail(permission_error, code="PERMISSION_DENIED")
		validate_doctor_practitioner(practitioner_name)

		previous_practitioner = visit_practitioner(visit_doc)
		set_visit_practitioner(visit_doc, practitioner_name)
		visit_doc.save(ignore_permissions=True)

		episode_doc = frappe.get_doc("Pet Care Episode", episode_doc.name)
		added_to_team = ensure_episode_practitioner(episode_doc, practitioner_name)
		if added_to_team:
			episode_doc.save(ignore_permissions=True)

		result = _care_team_payload(episode_doc.name, visit_doc.name)
		result["visit"] = _visit_payload(frappe.get_doc("Vet Visit", visit_doc.name), day_index=None)
		result["previous_practitioner"] = previous_practitioner
		result["added_to_team"] = bool(added_to_team)
		return ok(result)
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def create_visit_referral(visit=None, to_practitioner=None, note=None, data=None, **kwargs):
	try:
		return _create_visit_referral(visit=visit, to_practitioner=to_practitioner, note=note, data=data, **kwargs)
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_episode_care_team(episode=None, visit=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		episode_doc = _episode_doc(_arg(episode, payload, "episode", "care_episode"))
		visit_doc = _optional_visit_context_doc(_arg(visit, payload, "visit", "vet_visit", "source_visit"), episode_doc)
		if not (is_supervisor_user() or current_user_team_practitioner(episode_doc) or current_user_visit_practitioner(visit_doc)):
			frappe.throw(_("Not permitted"), frappe.PermissionError)
		return ok(_care_team_payload(episode_doc.name, visit_doc.name if visit_doc else None))
	except Exception as exc:
		return _error_response(exc)


def _care_team_payload(episode_name: str, visit_name: str | None = None) -> dict:
	episode_doc = frappe.get_doc("Pet Care Episode", episode_name)
	visit_doc = _optional_visit_context_doc(visit_name, episode_doc)
	care_team = _team_payload(episode_doc)
	visits = [
		_visit_payload(row, day_index=index + 1)
		for index, row in enumerate(episode_visit_history(episode_doc.name))
	]
	team_practitioner = current_user_team_practitioner(episode_doc)
	visit_doctor = visit_practitioner(visit_doc)
	visit_user_practitioner = current_user_visit_practitioner(visit_doc)
	supervisor = is_supervisor_user()
	can_manage_team = can_manage_episode_team(episode_doc, visit_doc)
	return {
		"episode": {
			"name": episode_doc.name,
			"episode_status": episode_doc.get("episode_status"),
			"episode_title": episode_doc.get("episode_title"),
			"primary_doctor": episode_doc.get("primary_doctor"),
		},
		"care_team": care_team,
		"team": care_team,
		"visits": visits,
		"visit_doctors": visits,
		"permissions": {
			"is_supervisor": supervisor,
			"is_team_doctor": bool(team_practitioner),
			"is_visit_doctor": bool(visit_user_practitioner),
			"team_practitioner": team_practitioner,
			"visit_practitioner": visit_doctor,
			"control_visit": visit_doc.name if visit_doc else None,
			"episode_primary_doctor": episode_doc.get("primary_doctor"),
			"can_add_doctor": can_manage_team,
			"can_remove_doctor": can_manage_team,
			"can_assign_visit_doctor": bool(frappe.session.user == "Administrator" or team_practitioner),
			"supervisor_roles": sorted(SUPERVISOR_ROLES),
		},
	}


def _team_payload(episode_doc) -> list[dict]:
	seen: set[str] = set()
	rows: list[dict] = []
	for row in episode_doc.get("assigned_practitioners") or []:
		practitioner = cstr(row.get("practitioner")).strip()
		if not practitioner or practitioner in seen:
			continue
		rows.append(_team_row_payload(practitioner, row))
		seen.add(practitioner)

	return rows


def _team_row_payload(practitioner: str, row=None, *, source: str = "assigned_practitioners") -> dict:
	member = team_member_payload(practitioner)
	role = (row.get("role") if row else None) or DEFAULT_TEAM_ROLE
	return {
		"name": practitioner,
		"practitioner": practitioner,
		"practitioner_name": member.get("name_label") or practitioner,
		"name_label": member.get("name_label") or practitioner,
		"practitioner_type": member.get("practitioner_type"),
		"specialization": member.get("specialization"),
		"user": member.get("user"),
		"user_id": member.get("user_id"),
		"image": member.get("image"),
		"role": role,
		"row_name": row.name if row else None,
		"added_by": row.get("added_by") if row else None,
		"added_at": row.get("added_at") if row else None,
		"note": row.get("note") if row else None,
		"source": source,
	}


def _visit_payload(visit, *, day_index: int | None) -> dict:
	assigned_practitioner = visit_practitioner(visit)
	member = team_member_payload(assigned_practitioner)
	return {
		"name": visit.get("name"),
		"visit": visit.get("name"),
		"day_index": day_index,
		"visit_datetime": visit.get("visit_datetime"),
		"status": visit.get("status"),
		"visit_type": visit.get("visit_type"),
		"case_sheet": visit.get("case_sheet"),
		"primary_practitioner": assigned_practitioner,
		"practitioner": assigned_practitioner,
		"doctor": visit.get("doctor"),
		"practitioner_name": member.get("name_label") or assigned_practitioner,
		"doctor_name": member.get("name_label") or assigned_practitioner,
	}


def _visit_blocker_payload(row: dict) -> dict:
	return {
		"name": row.get("name"),
		"visit": row.get("name"),
		"visit_datetime": row.get("visit_datetime"),
		"status": row.get("status"),
		"primary_practitioner": row.get("primary_practitioner") or row.get("doctor"),
		"doctor": row.get("doctor"),
	}


def _direct_assign_permission_error():
	if user_has_full_access():
		return None
	roles = get_user_roles()
	if roles & DIRECT_ASSIGN_ROLES:
		return None
	if "Doctor" in roles:
		return _("Doctors must transfer visits using referral with a note.")
	return _("Not permitted to assign visit doctor.")



def _required_visit_context_doc(visit_name: str | None, episode_doc):
	visit_name = cstr(visit_name).strip()
	if not visit_name:
		frappe.throw(_("Visit is required to change the care team."))
	return _visit_context_doc(visit_name, episode_doc)


def _optional_visit_context_doc(visit_name: str | None, episode_doc):
	visit_name = cstr(visit_name).strip()
	if not visit_name:
		return None
	return _visit_context_doc(visit_name, episode_doc)


def _visit_context_doc(visit_name: str, episode_doc):
	visit_doc = _visit_doc(visit_name)
	visit_episode = cstr(visit_doc.get("care_episode")).strip()
	if visit_episode == episode_doc.name:
		return visit_doc
	if not visit_episode and visit_doc.get("animal_patient") == episode_doc.get("pet") and episode_doc.get("episode_status") in ACTIVE_EPISODE_STATUSES:
		return visit_doc
	frappe.throw(
		_("Visit {0} is not linked to Care Episode {1}.").format(
			frappe.bold(visit_doc.name), frappe.bold(episode_doc.name)
		)
	)

def _episode_doc(episode_name: str | None):
	episode_name = cstr(episode_name).strip()
	if not episode_name:
		frappe.throw(_("Care Episode is required."))
	if not frappe.db.exists("Pet Care Episode", episode_name):
		frappe.throw(_("Care Episode {0} was not found.").format(frappe.bold(episode_name)))
	return frappe.get_doc("Pet Care Episode", episode_name)


def _visit_doc(visit_name: str | None):
	visit_name = cstr(visit_name).strip()
	if not visit_name:
		frappe.throw(_("Visit is required."))
	if not frappe.db.exists("Vet Visit", visit_name):
		frappe.throw(_("Visit {0} was not found.").format(frappe.bold(visit_name)))
	return frappe.get_doc("Vet Visit", visit_name)


def _payload(data, kwargs) -> dict:
	if isinstance(data, str) and data:
		return json.loads(data)
	if isinstance(data, dict):
		return data
	return kwargs or {}


def _arg(explicit, payload: dict, *keys):
	if explicit:
		return cstr(explicit).strip()
	for key in keys:
		if payload.get(key):
			return cstr(payload.get(key)).strip()
	return None


def _error_response(exc):
	if isinstance(exc, frappe.PermissionError):
		return fail(cstr(exc) or _("Not permitted"), code="PERMISSION_DENIED")
	return fail(cstr(exc), code=getattr(exc, "exc_type", None) or exc.__class__.__name__, details=frappe.get_traceback())
