from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, cstr, now_datetime

from pet_app.api.link_aliases import with_link_aliases
from pet_app.api.permissions import GUARDIAN_ROLES, get_user_roles, is_clinical_user, require_doctype_permission
from pet_app.api.response import fail, ok


MANAGER_ROLES = {"System Manager", "Pet App Admin", "Healthcare Administrator", "Clinic Manager"}
OWNER_SAFE_ADDENDUM_TYPES = {"Clinical Correction", "Medication Correction", "Follow-up Note", "Clinical Note", "Correction", "Clarification"}


@frappe.whitelist(methods=["POST"])
def add_visit_addendum(data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		visit_name = cstr(payload.get("visit")).strip()
		if not visit_name:
			return fail(_("Visit is required."), code="VALIDATION_ERROR")
		_assert_visit_access(visit_name, write=True)
		require_doctype_permission("Vet Visit Addendum", "create")

		doc = frappe.get_doc(
			{
				"doctype": "Vet Visit Addendum",
				"visit": visit_name,
				"addendum_type": payload.get("addendum_type") or "Clinical Correction",
				"note": payload.get("note"),
				"reason": payload.get("reason"),
				"requires_manager_approval": cint(payload.get("requires_manager_approval")),
				"status": payload.get("status") or "Submitted",
			}
		)
		doc.insert(ignore_permissions=True)
		return ok({"addendum": _payload_for_addendum(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def list_visit_addendums(visit=None, owner_safe=None, limit=50):
	try:
		visit_name = cstr(visit).strip()
		if not visit_name:
			return fail(_("Visit is required."), code="VALIDATION_ERROR")
		_assert_visit_access(visit_name, write=False)
		owner_safe = _is_guardian_user() if owner_safe is None else cint(owner_safe)
		filters = {"visit": visit_name}
		if owner_safe:
			filters["addendum_type"] = ["in", list(OWNER_SAFE_ADDENDUM_TYPES)]
			filters["status"] = ["!=", "Cancelled"]
		rows = frappe.get_all(
			"Vet Visit Addendum",
			filters=filters,
			fields=["name"],
			order_by="addendum_datetime desc, creation desc",
			limit_page_length=max(min(cint(limit or 50), 200), 1),
			ignore_permissions=True,
		)
		addendums = [_payload_for_addendum(frappe.get_doc("Vet Visit Addendum", row.name), owner_safe=owner_safe) for row in rows]
		return ok({"addendums": addendums}, meta={"total": len(addendums)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def approve_visit_addendum(addendum=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		addendum_name = cstr(addendum or payload.get("addendum")).strip()
		if not addendum_name:
			return fail(_("Addendum is required."), code="VALIDATION_ERROR")
		if not frappe.db.exists("Vet Visit Addendum", addendum_name):
			return fail(_("Addendum was not found."), code="NOT_FOUND")
		require_doctype_permission("Vet Visit Addendum", "write")
		doc = frappe.get_doc("Vet Visit Addendum", addendum_name)
		_assert_visit_access(doc.visit, write=True)
		doc.status = payload.get("status") or "Approved"
		doc.approved_by = frappe.session.user
		doc.approved_at = now_datetime()
		doc.save(ignore_permissions=True)
		return ok({"addendum": _payload_for_addendum(doc)})
	except Exception as exc:
		return _error_response(exc)


def _assert_visit_access(visit_name: str, write=False):
	if not frappe.db.exists("Vet Visit", visit_name):
		frappe.throw(_("Vet Visit {0} was not found.").format(frappe.bold(visit_name)))
	if is_clinical_user("write" if write else "read"):
		return
	guardian = frappe.db.get_value("Guardian", {"user_id": frappe.session.user}, "name")
	if guardian:
		row = frappe.db.get_value("Vet Visit", visit_name, ["guardian", "animal_patient"], as_dict=True)
		if row and row.guardian == guardian and frappe.db.exists("PetGuardian", {"guardian_id": guardian, "pet_id": row.animal_patient}):
			if write:
				frappe.throw(_("Not permitted"), frappe.PermissionError)
			return
	frappe.throw(_("Not permitted"), frappe.PermissionError)


def _payload_for_addendum(doc, owner_safe=False) -> dict:
	data = {
		"name": doc.name,
		"visit": doc.visit,
		"addendum_datetime": doc.addendum_datetime,
		"created_at": doc.get("created_at") or doc.addendum_datetime,
		"addendum_type": doc.addendum_type,
		"status": doc.get("status"),
		"author": doc.get("author") or doc.get("authored_by"),
		"note": doc.note,
		"reason": doc.reason,
	}
	if owner_safe:
		data.pop("author", None)
		return data
	data.update(
		{
			"authored_by": doc.get("authored_by"),
			"author_role": doc.get("author_role"),
			"doctor": doc.doctor,
			"guardian": doc.guardian,
			"customer": doc.customer,
			"pet": doc.pet,
			"requires_manager_approval": cint(doc.get("requires_manager_approval")),
			"approved_by": doc.get("approved_by"),
			"approved_at": doc.get("approved_at"),
		}
	)
	return with_link_aliases(data, pet_field="pet", guardian_field="guardian", doctor_field="doctor", include_provider=False)


def _is_guardian_user() -> bool:
	# Clinical status is decided by is_clinical_user, not by the absence of a guardian
	# role: staff on this site are routinely granted "Pet"/"Guardian" alongside their
	# clinical roles, and the old role-exclusion therefore served doctors the
	# owner-safe payload.
	roles = get_user_roles()
	return bool(roles & GUARDIAN_ROLES and not roles & MANAGER_ROLES and not is_clinical_user())


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
