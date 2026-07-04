from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cstr

from pet_app.api.response import fail, ok
from pet_app.utils.case_assignment import (
	DEFAULT_TEAM_ROLE,
	SUPERVISOR_ROLES,
	assert_can_assign_visit_doctor,
	assert_can_manage_episode_team,
	current_user_team_practitioner,
	ensure_episode_practitioner,
	episode_team_practitioners,
	episode_visit_history,
	is_supervisor_user,
	open_visit_assignments_for_practitioner,
	practitioner_user_is_supervisor,
	remove_episode_practitioner,
	set_visit_practitioner,
	team_member_payload,
	validate_doctor_practitioner,
	visit_practitioner,
)


@frappe.whitelist(methods=["POST"])
def add_episode_doctor(episode=None, practitioner=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		episode_doc = _episode_doc(_arg(episode, payload, "episode", "care_episode"))
		practitioner_name = _arg(practitioner, payload, "practitioner", "doctor", "primary_practitioner")
		if not practitioner_name:
			return fail(_("Practitioner is required."), code="VALIDATION_ERROR")

		assert_can_manage_episode_team(episode_doc)
		validate_doctor_practitioner(practitioner_name)
		changed = ensure_episode_practitioner(
			episode_doc,
			practitioner_name,
			role=payload.get("role") or DEFAULT_TEAM_ROLE,
			note=payload.get("note"),
		)
		if changed:
			episode_doc.save(ignore_permissions=True)

		result = _care_team_payload(episode_doc.name)
		result["added"] = bool(changed)
		return ok(result)
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def remove_episode_doctor(episode=None, practitioner=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		episode_doc = _episode_doc(_arg(episode, payload, "episode", "care_episode"))
		practitioner_name = _arg(practitioner, payload, "practitioner", "doctor", "primary_practitioner")
		if not practitioner_name:
			return fail(_("Practitioner is required."), code="VALIDATION_ERROR")

		assert_can_manage_episode_team(episode_doc)
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
		if changed:
			episode_doc.save(ignore_permissions=True)

		result = _care_team_payload(episode_doc.name)
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

		assert_can_assign_visit_doctor(episode_doc)
		validate_doctor_practitioner(practitioner_name)

		previous_practitioner = visit_practitioner(visit_doc)
		set_visit_practitioner(visit_doc, practitioner_name)
		visit_doc.save(ignore_permissions=True)

		episode_doc = frappe.get_doc("Pet Care Episode", episode_doc.name)
		added_to_team = ensure_episode_practitioner(episode_doc, practitioner_name)
		if added_to_team:
			episode_doc.save(ignore_permissions=True)

		result = _care_team_payload(episode_doc.name)
		result["visit"] = _visit_payload(frappe.get_doc("Vet Visit", visit_doc.name), day_index=None)
		result["previous_practitioner"] = previous_practitioner
		result["added_to_team"] = bool(added_to_team)
		return ok(result)
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_episode_care_team(episode=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		episode_doc = _episode_doc(_arg(episode, payload, "episode", "care_episode"))
		if not (is_supervisor_user() or current_user_team_practitioner(episode_doc)):
			frappe.throw(_("Not permitted"), frappe.PermissionError)
		return ok(_care_team_payload(episode_doc.name))
	except Exception as exc:
		return _error_response(exc)


def _care_team_payload(episode_name: str) -> dict:
	episode_doc = frappe.get_doc("Pet Care Episode", episode_name)
	care_team = _team_payload(episode_doc)
	visits = [
		_visit_payload(row, day_index=index + 1)
		for index, row in enumerate(episode_visit_history(episode_doc.name))
	]
	team_practitioner = current_user_team_practitioner(episode_doc)
	supervisor = is_supervisor_user()
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
			"team_practitioner": team_practitioner,
			"can_add_doctor": bool(supervisor or team_practitioner),
			"can_remove_doctor": bool(supervisor or team_practitioner),
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

	for practitioner in episode_team_practitioners(episode_doc):
		if practitioner in seen:
			continue
		rows.append(_team_row_payload(practitioner, None, source="primary_doctor"))
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
		"is_supervisor": role == "Supervisor" or practitioner_user_is_supervisor(practitioner),
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
	visit_doc = frappe.get_doc("Vet Visit", visit_name)
	if not visit_doc.meta.has_field("care_episode") or not visit_doc.get("care_episode"):
		frappe.throw(_("Visit {0} is not linked to a Care Episode.").format(frappe.bold(visit_name)))
	return visit_doc


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
		return fail(_("Not permitted"), code="PERMISSION_DENIED")
	return fail(cstr(exc), code=getattr(exc, "exc_type", None) or exc.__class__.__name__, details=frappe.get_traceback())
