from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, cstr, getdate

from pet_app.api import medical_file
from pet_app.api.link_aliases import enrich_link_aliases, with_link_aliases
from pet_app.api.permissions import get_user_roles, require_doctype_permission, user_has_full_access
from pet_app.api.response import fail, ok, standardize_response
from pet_app.pet_app.doctype.pet_care_episode.pet_care_episode import ACTIVE_EPISODE_STATUSES
from pet_app.utils.medical_profile import get_visit_case_context, set_visit_case_choice, update_profile_for_visit


CLINICAL_ROLES = {"Doctor", "Physician", "Healthcare", "Healthcare Practitioner", "Healthcare Administrator"}
GUARDIAN_ROLES = {"Guardian", "Guardians", "Pet"}


@frappe.whitelist()
def get_pet_medical_profile(pet=None, pet_id=None):
	try:
		pet_name = cstr(pet or pet_id).strip()
		if not pet_name:
			return fail(_("Pet is required."), code="VALIDATION_ERROR")
		_assert_pet_access(pet_name)
		summary = medical_file.get_pet_medical_summary(pet=pet_name)
		if not summary.get("ok"):
			return summary
		profile_name = summary["data"]["profile"]["name"]
		profile = frappe.get_doc("Pet Medical Profile", profile_name)
		return ok(
			{
				"profile": _doc_payload(profile),
				"active_episode": _active_episode_payload(pet_name),
				"active_plan_items": _plan_items(pet_name),
				"latest_visits": _latest_visits(pet_name),
				"latest_vitals": summary["data"].get("latest_vitals") or {},
			}
		)
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_pet_current_case(pet=None, pet_id=None):
	try:
		pet_name = cstr(pet or pet_id).strip()
		if not pet_name:
			return fail(_("Pet is required."), code="VALIDATION_ERROR")
		_assert_pet_access(pet_name)
		return ok({"active_episode": _active_episode_payload(pet_name), "active_plan_items": _plan_items(pet_name)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
@standardize_response
def get_pet_medical_timeline(pet=None, pet_id=None, limit=50, cursor=0):
	result = medical_file.get_pet_medical_timeline(pet=pet or pet_id, limit=limit)
	if result.get("ok"):
		result.setdefault("meta", {})["cursor"] = cint(cursor)
	return result


@frappe.whitelist(methods=["POST"])
def recalculate_pet_medical_profile(pet=None, pet_id=None):
	try:
		pet_name = cstr(pet or pet_id).strip()
		if not pet_name:
			return fail(_("Pet is required."), code="VALIDATION_ERROR")
		_assert_pet_access(pet_name, write=True)
		visit_name = frappe.db.get_value("Vet Visit", {"animal_patient": pet_name}, "name", order_by="visit_datetime desc, creation desc")
		if visit_name:
			visit = frappe.get_doc("Vet Visit", visit_name)
			update_profile_for_visit(visit)
		return get_pet_medical_profile(pet=pet_name)
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def close_care_episode(episode=None, outcome=None, closure_reason=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		episode_name = cstr(episode or payload.get("episode")).strip()
		if not episode_name:
			return fail(_("Care Episode is required."), code="VALIDATION_ERROR")
		require_doctype_permission("Pet Care Episode", "write")
		doc = frappe.get_doc("Pet Care Episode", episode_name)
		_assert_pet_access(doc.pet, write=True)
		doc.episode_status = "Resolved" if (outcome or payload.get("outcome")) not in {"Death", "Euthanasia"} else "Deceased"
		doc.outcome = outcome or payload.get("outcome") or doc.outcome
		doc.closure_reason = closure_reason or payload.get("closure_reason") or doc.closure_reason
		doc.closed_on = doc.closed_on or getdate()
		doc.closed_by = frappe.session.user
		doc.save(ignore_permissions=True)
		profile_name = frappe.db.get_value("Pet Medical Profile", {"pet": doc.pet}, "name")
		if profile_name:
			profile = frappe.get_doc("Pet Medical Profile", profile_name)
			updates = {
				"active_care_episode": None,
				"current_case_status": "Deceased" if doc.episode_status == "Deceased" else "No Active Case",
				"current_clinical_status": "Deceased" if doc.episode_status == "Deceased" else "Stable",
				"treatment_plan_status": "Completed",
			}
			frappe.db.set_value("Pet Medical Profile", profile.name, updates, update_modified=False)
		return ok({"episode": _doc_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def create_or_update_care_episode_from_visit(visit=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		visit_name = cstr(visit or payload.get("visit")).strip()
		if not visit_name:
			return fail(_("Visit is required."), code="VALIDATION_ERROR")
		require_doctype_permission("Pet Care Episode", "create")
		visit_doc = frappe.get_doc("Vet Visit", visit_name)
		_assert_pet_access(visit_doc.animal_patient, write=True)
		choice = payload.get("doctor_case_choice") or payload.get("case_choice")
		if not choice:
			context = get_visit_case_context(visit_doc)
			choice = "continue_case" if context.get("can_continue_case") else "new_case"
		context = set_visit_case_choice(
			visit_doc,
			choice,
			episode=payload.get("care_episode") or payload.get("episode"),
			note=payload.get("case_choice_note") or payload.get("note"),
		)
		episode_name = context.get("visit_care_episode")
		episode = _doc_payload(frappe.get_doc("Pet Care Episode", episode_name)) if episode_name else {}
		return ok({"episode": episode, "visit": visit_name, "case_context": context})
	except Exception as exc:
		return _error_response(exc)


def _active_episode_payload(pet: str) -> dict:
	name = frappe.db.get_value("Pet Care Episode", {"pet": pet, "episode_status": ["in", list(ACTIVE_EPISODE_STATUSES)]}, "name", order_by="modified desc")
	return _doc_payload(frappe.get_doc("Pet Care Episode", name)) if name else {}


def _plan_items(pet: str) -> list[dict]:
	if not frappe.db.exists("DocType", "Pet Care Plan Item"):
		return []
	rows = frappe.get_all(
		"Pet Care Plan Item",
		filters={"pet": pet, "status": ["not in", ["Done", "Cancelled", "Converted To Visit"]]},
		fields=["*"],
		order_by="due_date asc, modified desc",
		ignore_permissions=True,
	)
	items = [dict(row) for row in rows]
	enrich_link_aliases(items, pet_field="pet", guardian_field="guardian", doctor_field="doctor", include_provider=False)
	return items


def _latest_visits(pet: str, limit=5) -> list[dict]:
	rows = frappe.get_all(
		"Vet Visit",
		filters={"animal_patient": pet},
		fields=["name", "visit_datetime", "status", "doctor", "animal_patient", "guardian", "diagnosis", "follow_up_date", "follow_up_status"],
		order_by="visit_datetime desc, creation desc",
		limit_page_length=limit,
		ignore_permissions=True,
	)
	visits = [dict(row) for row in rows]
	enrich_link_aliases(visits, pet_field="animal_patient", guardian_field="guardian", doctor_field="doctor", include_provider=False)
	return visits


def _doc_payload(doc) -> dict:
	return with_link_aliases(doc.as_dict(no_nulls=False))


def _assert_pet_access(pet: str, write=False):
	if not frappe.db.exists("Pet", pet):
		frappe.throw(_("Pet {0} was not found.").format(frappe.bold(pet)))
	if user_has_full_access() or get_user_roles() & CLINICAL_ROLES:
		return
	guardian = frappe.db.get_value("Guardian", {"user_id": frappe.session.user}, "name")
	if guardian and frappe.db.exists("PetGuardian", {"guardian_id": guardian, "pet_id": pet}):
		if write:
			frappe.throw(_("Not permitted"), frappe.PermissionError)
		return
	frappe.throw(_("Not permitted"), frappe.PermissionError)


def _payload(data, kwargs) -> dict:
	if isinstance(data, str) and data:
		return json.loads(data)
	if isinstance(data, dict):
		return data
	return kwargs or {}


def _error_response(exc):
	if isinstance(exc, frappe.PermissionError):
		return fail(_("Not permitted"), code="PERMISSION_DENIED")
	return fail(cstr(exc), code=getattr(exc, "exc_type", None) or exc.__class__.__name__, details=frappe.get_traceback())
