from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cstr, now_datetime

from pet_app.api.response import ok
from pet_app.utils.case_assignment import (
	DEFAULT_TEAM_ROLE,
	ensure_episode_practitioner,
	is_supervisor_user,
	set_visit_practitioner,
	team_member_payload,
	validate_doctor_practitioner,
	VISIT_DOCTOR_TEAM_ERROR,
	visit_practitioner,
)
from pet_app.utils.practitioner import get_practitioner_for_user
from pet_app.workflows import clinical_state


REFERRAL_FIELD = "referrals"


def create_visit_referral(visit=None, to_practitioner=None, note=None, data=None, **kwargs):
	savepoint = f"visit_referral_{frappe.generate_hash(length=10)}"
	frappe.db.savepoint(savepoint)
	try:
		result = _create_visit_referral_atomic(
			visit=visit,
			to_practitioner=to_practitioner,
			note=note,
			data=data,
			**kwargs,
		)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise
	else:
		frappe.db.release_savepoint(savepoint)
		return ok(result)


def _create_visit_referral_atomic(visit=None, to_practitioner=None, note=None, data=None, **kwargs):
	payload = _payload(data, kwargs)
	visit_doc = _visit_doc(_arg(visit, payload, "visit", "vet_visit"))
	_assert_referral_table(visit_doc)
	_assert_visit_can_receive_referral(visit_doc)

	referral_note = cstr(note or payload.get("note")).strip()
	if not referral_note:
		frappe.throw(_("Referral note is required."))

	episode_doc = _episode_doc(visit_doc.get("care_episode")) if visit_doc.get("care_episode") else None
	actor = _referral_actor(visit_doc, episode_doc=episode_doc)
	from_practitioner = actor["from_practitioner"]

	to_practitioner_name = _arg(to_practitioner, payload, "to_practitioner", "practitioner", "doctor")
	if not to_practitioner_name:
		frappe.throw(_("Receiving practitioner is required."))
	to_practitioner_name = validate_doctor_practitioner(to_practitioner_name)
	if to_practitioner_name == visit_practitioner(visit_doc):
		frappe.throw(_("Cannot refer a visit to its current practitioner."))
	_require_practitioner_user(to_practitioner_name)

	row = visit_doc.append(
		REFERRAL_FIELD,
		{
			"from_practitioner": from_practitioner,
			"to_practitioner": to_practitioner_name,
			"note": referral_note,
			"referred_by": frappe.session.user,
			"referred_at": now_datetime(),
		},
	)
	set_visit_practitioner(visit_doc, to_practitioner_name)
	visit_doc.save(ignore_permissions=True)

	if episode_doc:
		episode_doc = frappe.get_doc("Pet Care Episode", episode_doc.name)
		team_changed = ensure_episode_practitioner(episode_doc, to_practitioner_name, role=DEFAULT_TEAM_ROLE)
		if team_changed:
			episode_doc.save(ignore_permissions=True)

	_notify_practitioner(
		to_practitioner_name,
		_("Visit referred to you: {0} - {1}").format(_visit_pet_label(visit_doc), referral_note),
		visit_doc,
	)
	if actor["admin_initiated"]:
		_notify_practitioner(
			from_practitioner,
			_("Visit transferred from you: {0} - {1}").format(_visit_pet_label(visit_doc), referral_note),
			visit_doc,
		)
	visit_doc = frappe.get_doc("Vet Visit", visit_doc.name)
	return {"visit": visit_doc.name, "referral": _referral_payload(row), "referrals": visit_referrals_payload(visit_doc)}


def visit_referrals_payload(visit_doc) -> list[dict]:
	if not visit_doc or not visit_doc.meta.has_field(REFERRAL_FIELD):
		return []
	rows = sorted(
		visit_doc.get(REFERRAL_FIELD) or [],
		key=lambda row: (cstr(row.get("referred_at") or row.get("creation")), row.get("idx") or 0),
	)
	return [_referral_payload(row) for row in rows]


def _referral_payload(row) -> dict:
	from_member = team_member_payload(row.get("from_practitioner"))
	to_member = team_member_payload(row.get("to_practitioner"))
	return {
		"name": row.get("name"),
		"note": row.get("note"),
		"from_practitioner": row.get("from_practitioner"),
		"from_practitioner_name": from_member.get("name_label") or row.get("from_practitioner"),
		"to_practitioner": row.get("to_practitioner"),
		"to_practitioner_name": to_member.get("name_label") or row.get("to_practitioner"),
		"referred_by": row.get("referred_by"),
		"referred_by_name": _user_display_name(row.get("referred_by")),
		"referred_at": row.get("referred_at"),
	}


def can_refer_visit(visit_doc) -> bool:
	if not visit_doc or _visit_locked_for_referral(visit_doc):
		return False
	current_practitioner = visit_practitioner(visit_doc)
	if not current_practitioner:
		return False

	if is_supervisor_user():
		return True
	practitioner = get_practitioner_for_user(frappe.session.user, include_disabled=False)
	if not practitioner or practitioner != current_practitioner:
		return False
	return frappe.db.get_value("Healthcare Practitioner", practitioner, "practitioner_type") == "Doctor"


def _referral_actor(visit_doc, *, episode_doc=None) -> dict:
	current_practitioner = visit_practitioner(visit_doc)
	if not current_practitioner:
		frappe.throw(_("Visit has no current practitioner."))

	if is_supervisor_user():
		return {"from_practitioner": current_practitioner, "admin_initiated": True}

	practitioner = _current_doctor_practitioner()
	if practitioner != current_practitioner:
		frappe.throw(VISIT_DOCTOR_TEAM_ERROR, frappe.PermissionError)
	return {"from_practitioner": practitioner, "admin_initiated": False}



def _current_doctor_practitioner() -> str:
	practitioner = get_practitioner_for_user(frappe.session.user, include_disabled=False)
	if not practitioner:
		frappe.throw(_("Only a linked Doctor user may create a referral."), frappe.PermissionError)
	return validate_doctor_practitioner(practitioner)


def _require_practitioner_user(practitioner: str | None) -> str:
	member = team_member_payload(practitioner)
	user = member.get("user") or member.get("user_id")
	if not user:
		frappe.throw(_("Practitioner {0} does not have a linked user.").format(frappe.bold(practitioner or "")))
	return user


def _notify_practitioner(practitioner: str | None, subject: str, visit_doc):
	user = _require_practitioner_user(practitioner)
	log = frappe.new_doc("Notification Log")
	log.for_user = user
	log.from_user = frappe.session.user
	log.subject = subject
	log.document_type = "Vet Visit"
	log.document_name = visit_doc.name
	log.type = "Alert"
	log.insert(ignore_permissions=True)


def _assert_visit_can_receive_referral(visit_doc):
	if cstr(visit_doc.get("status")).strip() == "Completed":
		frappe.throw(_("Completed visits cannot be referred."))
	if clinical_state.is_billed_visit(visit_doc):
		frappe.throw(_("Visit is locked after billing."))


def _visit_locked_for_referral(visit_doc) -> bool:
	return cstr(visit_doc.get("status")).strip() == "Completed" or clinical_state.is_billed_visit(visit_doc)


def _assert_referral_table(visit_doc):
	if not visit_doc.meta.has_field(REFERRAL_FIELD):
		frappe.throw(_("Vet Visit referral table is missing. Run migrations first."))


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


def _visit_pet_label(visit_doc) -> str:
	return frappe.db.get_value("Pet", visit_doc.get("animal_patient"), "pet_name") or visit_doc.get("animal_patient") or visit_doc.name


def _user_display_name(user: str | None) -> str | None:
	if not user:
		return None
	return frappe.db.get_value("User", user, "full_name") or user


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
