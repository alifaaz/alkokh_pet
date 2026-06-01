from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, cstr, get_datetime, now_datetime

from pet_app.api.link_aliases import with_link_aliases
from pet_app.api.permissions import get_user_roles, user_has_full_access
from pet_app.api.response import fail, ok
from pet_app.utils.audit import log_event
from pet_app.utils.guardian_customer import get_or_create_customer_from_guardian
from pet_app.utils.mortality import guardian_for_pet, source_pet


SOURCE_DOCTYPES = {"Vet Visit", "Pet Procedure", "Pet Boarding", "PetCareService", "Appointment", "Lab", "Imaging"}
MANAGER_REVIEW_CATEGORIES = {
	"Procedure Complication",
	"Anesthesia Complication",
	"Boarding Incident",
	"Unknown / Found Dead",
}
MANAGER_ROLES = {"System Manager", "Pet App Admin", "Healthcare Administrator", "Clinic Manager"}
DOCTOR_ROLES = {"Doctor", "Physician", "Healthcare Practitioner"}
REPORT_ROLES = {"Visit", "Healthcare", "Nursing User", "Healthcare Administrator", "Clinic Reception"}
GUARDIAN_ROLES = {"Guardian", "Guardians", "Pet"}


@frappe.whitelist()
def list_death_reasons(species=None, category=None, active=1):
	try:
		filters = {}
		if cint(active):
			filters["active"] = 1
		if category:
			filters["category"] = category
		rows = frappe.get_all(
			"Pet Death Reason",
			filters=filters,
			fields=[
				"name",
				"reason_name",
				"reason_code",
				"category",
				"species",
				"description",
				"active",
				"requires_doctor_confirmation",
				"requires_manager_review",
				"requires_incident_report",
				"guardian_visible_label",
				"sort_order",
			],
			order_by="sort_order asc, reason_name asc",
			ignore_permissions=True,
		)
		if species:
			species_text = cstr(species).strip().lower()
			rows = [row for row in rows if not cstr(row.species).strip() or cstr(row.species).strip().lower() == species_text]
		return ok({"reasons": [dict(row) for row in rows]}, meta={"total": len(rows)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def report_pet_death(data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		pet = _resolve_pet(payload)
		if not pet:
			return fail(_("Pet is required."), code="VALIDATION_ERROR")
		_assert_pet_access(pet, write=True)
		_validate_single_active_death_record(pet)
		source_doctype = cstr(payload.get("source_doctype")).strip()
		source_name = cstr(payload.get("source_name")).strip()
		if source_doctype or source_name:
			_validate_source(source_doctype, source_name, pet)

		reason = _reason_payload(payload.get("death_reason"))
		category = payload.get("death_reason_category") or reason.get("category")
		requires_manager_review = _requires_manager_review(reason, category, payload)
		status = "Pending Confirmation" if reason.get("requires_doctor_confirmation") else "Reported"
		guardian = payload.get("guardian") or guardian_for_pet(pet)
		doc = frappe.get_doc(
			{
				"doctype": "Pet Death Record",
				"pet": pet,
				"guardian": guardian,
				"customer": payload.get("customer") or (get_or_create_customer_from_guardian(guardian) if guardian else None),
				"death_datetime": payload.get("death_datetime") or now_datetime(),
				"reported_datetime": now_datetime(),
				"reported_by": frappe.session.user,
				"death_reason": payload.get("death_reason"),
				"death_reason_category": category,
				"cause_of_death_text": payload.get("cause_of_death_text"),
				"clinical_summary": payload.get("clinical_summary"),
				"guardian_visible_summary": payload.get("guardian_visible_summary") or reason.get("guardian_visible_label") or payload.get("cause_of_death_text"),
				"internal_note": payload.get("internal_note"),
				"source_doctype": source_doctype or None,
				"source_name": source_name or None,
				"source_title": payload.get("source_title") or _source_title(source_doctype, source_name),
				"death_location": payload.get("death_location"),
				"death_location_detail": payload.get("death_location_detail"),
				"was_under_clinic_care": cint(payload.get("was_under_clinic_care") or bool(source_doctype)),
				"was_unexpected": cint(payload.get("was_unexpected")),
				"requires_manager_review": cint(requires_manager_review),
				"manager_review_status": "Pending" if requires_manager_review else "Not Required",
				"body_handling_option": payload.get("body_handling_option") or "Pending Guardian Decision",
				"necropsy_requested": cint(payload.get("necropsy_requested")),
				"necropsy_status": payload.get("necropsy_status") or ("Pending" if cint(payload.get("necropsy_requested")) else "Not Requested"),
				"status": status,
			}
		)
		doc.insert(ignore_permissions=True)
		if not source_doctype:
			frappe.db.set_value("Pet Death Record", doc.name, {"source_doctype": None, "source_name": None}, update_modified=False)
			doc.reload()
		_link_source(doc)
		log_event("pet_death.reported", reference_doctype="Pet Death Record", reference_name=doc.name, after=doc.as_dict())
		return ok({"death_record": _death_record_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def confirm_pet_death(name=None, death_record=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		doc = _get_death_record(name or death_record or payload.get("death_record"))
		if not _can_confirm():
			return fail(_("Not permitted"), code="PERMISSION_ERROR")
		if doc.status in {"Finalized", "Cancelled"}:
			return fail(_("Death record cannot be confirmed from its current status."), code="INVALID_STATUS")
		doc.status = "Confirmed"
		doc.confirmed_by = frappe.session.user
		doc.confirmed_at = now_datetime()
		if payload.get("clinical_summary"):
			doc.clinical_summary = payload.get("clinical_summary")
		if payload.get("guardian_visible_summary"):
			doc.guardian_visible_summary = payload.get("guardian_visible_summary")
		doc.save(ignore_permissions=True)
		log_event("pet_death.confirmed", reference_doctype="Pet Death Record", reference_name=doc.name, after=doc.as_dict())
		return ok({"death_record": _death_record_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def finalize_pet_death(name=None, death_record=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		doc = _get_death_record(name or death_record or payload.get("death_record"))
		if not _can_finalize(doc):
			return fail(_("Not permitted"), code="PERMISSION_ERROR")
		if doc.status == "Cancelled":
			return fail(_("Cancelled death records cannot be finalized."), code="INVALID_STATUS")
		if cint(doc.requires_manager_review) and doc.manager_review_status != "Approved":
			return fail(_("Manager review is required before finalizing this death record."), code="MANAGER_REVIEW_REQUIRED")
		before = doc.as_dict()
		doc.status = "Finalized"
		if not doc.confirmed_by:
			doc.confirmed_by = frappe.session.user
			doc.confirmed_at = now_datetime()
		if payload.get("manager_review_note") and cint(doc.requires_manager_review):
			doc.manager_review_note = payload.get("manager_review_note")
		doc.save(ignore_permissions=True)
		_finalize_pet(doc)
		_cancel_future_appointments(doc)
		_link_source(doc, finalized=True)
		log_event("pet_death.finalized", reference_doctype="Pet Death Record", reference_name=doc.name, before=before, after=doc.as_dict())
		return ok({"death_record": _death_record_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def cancel_pet_death_record(name=None, death_record=None, reason=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		doc = _get_death_record(name or death_record or payload.get("death_record"))
		if not _is_manager():
			return fail(_("Not permitted"), code="PERMISSION_ERROR")
		if doc.status == "Finalized":
			return fail(_("Finalized death records cannot be cancelled."), code="INVALID_STATUS")
		doc.status = "Cancelled"
		doc.cancelled_reason = reason or payload.get("cancelled_reason") or payload.get("reason")
		doc.save(ignore_permissions=True)
		log_event("pet_death.cancelled", reference_doctype="Pet Death Record", reference_name=doc.name, after=doc.as_dict())
		return ok({"death_record": _death_record_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_pet_death_record(name=None, death_record=None, pet=None):
	try:
		doc = None
		if name or death_record:
			doc = _get_death_record(name or death_record)
		elif pet:
			_assert_pet_access(pet)
			record = frappe.db.get_value("Pet Death Record", {"pet": pet, "status": ["!=", "Cancelled"]}, "name")
			if record:
				doc = frappe.get_doc("Pet Death Record", record)
		if not doc:
			return fail(_("Death record was not found."), code="NOT_FOUND")
		_assert_pet_access(doc.pet)
		return ok({"death_record": _death_record_payload(doc, owner_safe=_is_guardian_user())})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def issue_death_certificate(name=None, death_record=None, certificate_file=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		doc = _get_death_record(name or death_record or payload.get("death_record"))
		if not _is_manager() and not user_has_full_access():
			return fail(_("Not permitted"), code="PERMISSION_ERROR")
		if doc.status not in {"Confirmed", "Finalized"}:
			return fail(_("Death record must be confirmed before issuing a certificate."), code="INVALID_STATUS")
		doc.certificate_issued = 1
		doc.certificate_no = doc.certificate_no or payload.get("certificate_no") or f"DC-{doc.name}"
		doc.certificate_issued_at = now_datetime()
		doc.certificate_file = certificate_file or payload.get("certificate_file") or doc.certificate_file
		doc.save(ignore_permissions=True)
		log_event("pet_death.certificate_issued", reference_doctype="Pet Death Record", reference_name=doc.name, after=doc.as_dict())
		return ok({"death_record": _death_record_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def manager_review_pet_death(name=None, death_record=None, status="Approved", note=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		doc = manager_review(
			name or death_record or payload.get("death_record"),
			status=payload.get("status") or status,
			note=payload.get("note") or note,
		)
		log_event("pet_death.manager_reviewed", reference_doctype="Pet Death Record", reference_name=doc.name, after=doc.as_dict())
		return ok({"death_record": _death_record_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


def manager_review(death_record: str, status: str = "Approved", note: str | None = None):
	doc = _get_death_record(death_record)
	if not _is_manager():
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	doc.manager_review_status = status
	doc.manager_reviewed_by = frappe.session.user
	doc.manager_reviewed_at = now_datetime()
	doc.manager_review_note = note
	doc.save(ignore_permissions=True)
	return doc


def _resolve_pet(payload: dict) -> str | None:
	pet = cstr(payload.get("pet")).strip()
	if pet:
		return pet
	return source_pet(payload.get("source_doctype"), payload.get("source_name"))


def _validate_single_active_death_record(pet: str):
	existing = frappe.db.get_value("Pet Death Record", {"pet": pet, "status": ["!=", "Cancelled"]}, "name")
	if existing:
		frappe.throw(_("Pet {0} already has active death record {1}.").format(frappe.bold(pet), frappe.bold(existing)))


def _validate_source(source_doctype: str, source_name: str, pet: str):
	if source_doctype not in SOURCE_DOCTYPES:
		frappe.throw(_("Source DocType {0} is not allowed.").format(frappe.bold(source_doctype)))
	if not source_name or not frappe.db.exists(source_doctype, source_name):
		frappe.throw(_("Source record was not found."))
	source_pet_name = source_pet(source_doctype, source_name)
	if source_pet_name and source_pet_name != pet:
		frappe.throw(_("Source pet does not match the death record pet."))


def _reason_payload(reason_name: str | None) -> dict:
	if not reason_name:
		return {}
	return frappe.db.get_value(
		"Pet Death Reason",
		reason_name,
		[
			"name",
			"category",
			"requires_doctor_confirmation",
			"requires_manager_review",
			"requires_incident_report",
			"guardian_visible_label",
		],
		as_dict=True,
	) or {}


def _requires_manager_review(reason: dict, category: str | None, payload: dict) -> bool:
	return bool(
		cint(payload.get("requires_manager_review"))
		or cint(reason.get("requires_manager_review"))
		or category in MANAGER_REVIEW_CATEGORIES
	)


def _get_death_record(name: str | None):
	name = cstr(name).strip()
	if not name:
		frappe.throw(_("Death Record is required."))
	if not frappe.db.exists("Pet Death Record", name):
		frappe.throw(_("Death Record {0} was not found.").format(frappe.bold(name)))
	return frappe.get_doc("Pet Death Record", name)


def _assert_pet_access(pet: str, write=False):
	if user_has_full_access() or get_user_roles() & (MANAGER_ROLES | DOCTOR_ROLES):
		return
	if write and get_user_roles() & REPORT_ROLES:
		return
	guardian = frappe.db.get_value("Guardian", {"user_id": frappe.session.user}, "name")
	if guardian and frappe.db.exists("PetGuardian", {"pet_id": pet, "guardian_id": guardian}):
		if write:
			frappe.throw(_("Not permitted"), frappe.PermissionError)
		return
	frappe.throw(_("Not permitted"), frappe.PermissionError)


def _is_guardian_user() -> bool:
	roles = get_user_roles()
	return bool(roles & GUARDIAN_ROLES and not roles & (MANAGER_ROLES | DOCTOR_ROLES) and not user_has_full_access())


def _is_manager() -> bool:
	return bool(user_has_full_access() or get_user_roles() & MANAGER_ROLES)


def _can_confirm() -> bool:
	return bool(user_has_full_access() or get_user_roles() & (DOCTOR_ROLES | MANAGER_ROLES))


def _can_finalize(doc) -> bool:
	if _is_manager():
		return True
	return bool(get_user_roles() & DOCTOR_ROLES and not cint(doc.requires_manager_review))


def _finalize_pet(doc):
	updates = {"is_deceased": 1, "death_date": get_datetime(doc.death_datetime).date(), "death_record": doc.name}
	if frappe.get_meta("Pet").has_field("status"):
		updates["status"] = "Deceased"
	if frappe.get_meta("Pet").has_field("pet_status"):
		updates["pet_status"] = "Deceased"
	frappe.db.set_value("Pet", doc.pet, updates, update_modified=True)


def _cancel_future_appointments(doc):
	if not doc.pet or not frappe.db.exists("DocType", "Appointment"):
		return
	fields = ["name", "scheduled_time", "status"]
	rows = frappe.get_all(
		"Appointment",
		filters={"custom_pet": doc.pet, "scheduled_time": [">", now_datetime()], "status": ["not in", ["Cancelled", "Closed"]]},
		fields=fields,
		ignore_permissions=True,
	)
	for row in rows:
		updates = {"status": "Cancelled"}
		if frappe.get_meta("Appointment").has_field("custom_cancelled_due_to_death"):
			updates["custom_cancelled_due_to_death"] = 1
		if frappe.get_meta("Appointment").has_field("custom_death_record"):
			updates["custom_death_record"] = doc.name
		frappe.db.set_value("Appointment", row.name, updates, update_modified=True)


def _link_source(doc, finalized=False):
	if not doc.source_doctype or not doc.source_name or not frappe.db.exists(doc.source_doctype, doc.source_name):
		return
	updates = {}
	if doc.source_doctype == "Vet Visit":
		updates = {"death_record": doc.name, "death_during_visit": 1}
		if finalized:
			updates["outcome"] = "Euthanasia" if doc.death_reason_category == "Euthanasia" else "Death"
	elif doc.source_doctype == "Pet Procedure":
		updates = {"death_record": doc.name, "death_during_procedure": 1}
		if finalized:
			updates["outcome"] = "Death"
	elif doc.source_doctype == "Pet Boarding":
		updates = {"death_record": doc.name, "death_during_boarding": 1}
		if finalized:
			updates["boarding_outcome"] = "Death"
	elif doc.source_doctype == "PetCareService":
		updates = {"death_record": doc.name}
	elif doc.source_doctype == "Appointment" and frappe.get_meta("Appointment").has_field("custom_death_record"):
		updates = {"custom_death_record": doc.name}
	if updates:
		frappe.db.set_value(doc.source_doctype, doc.source_name, updates, update_modified=True)


def _source_title(source_doctype: str, source_name: str) -> str | None:
	if source_doctype and source_name:
		return f"{source_doctype} {source_name}"
	return None


def _death_record_payload(doc, owner_safe=False) -> dict:
	payload = {
		"name": doc.name,
		"pet": doc.pet,
		"guardian": doc.guardian,
		"death_datetime": doc.death_datetime,
		"death_reason": doc.death_reason,
		"death_reason_category": doc.death_reason_category,
		"guardian_visible_summary": doc.guardian_visible_summary,
		"status": doc.status,
		"certificate_issued": cint(doc.certificate_issued),
		"certificate_no": doc.certificate_no,
		"certificate_issued_at": doc.certificate_issued_at,
		"certificate_file": doc.certificate_file,
		"body_handling_option": doc.body_handling_option,
	}
	with_link_aliases(payload, pet_field="pet", guardian_field="guardian", include_doctor=False, include_provider=False)
	if owner_safe:
		return payload
	payload.update(
		{
			"customer": doc.customer,
			"reported_datetime": doc.reported_datetime,
			"reported_by": doc.reported_by,
			"confirmed_by": doc.confirmed_by,
			"confirmed_at": doc.confirmed_at,
			"cause_of_death_text": doc.cause_of_death_text,
			"clinical_summary": doc.clinical_summary,
			"internal_note": doc.internal_note,
			"source_doctype": doc.source_doctype,
			"source_name": doc.source_name,
			"source_title": doc.source_title,
			"death_location": doc.death_location,
			"death_location_detail": doc.death_location_detail,
			"was_under_clinic_care": cint(doc.was_under_clinic_care),
			"was_unexpected": cint(doc.was_unexpected),
			"requires_manager_review": cint(doc.requires_manager_review),
			"manager_review_status": doc.manager_review_status,
			"manager_reviewed_by": doc.manager_reviewed_by,
			"manager_reviewed_at": doc.manager_reviewed_at,
			"manager_review_note": doc.manager_review_note,
			"body_released_to": doc.body_released_to,
			"body_released_at": doc.body_released_at,
			"body_release_note": doc.body_release_note,
			"necropsy_requested": cint(doc.necropsy_requested),
			"necropsy_status": doc.necropsy_status,
			"necropsy_result": doc.necropsy_result,
			"amended": cint(doc.amended),
			"amendment_reason": doc.amendment_reason,
			"cancelled_reason": doc.cancelled_reason,
		}
	)
	return payload


def _payload(data, kwargs) -> dict:
	if isinstance(data, str) and data:
		return json.loads(data)
	if isinstance(data, dict):
		return data
	return kwargs or {}


def _error_response(exc):
	if isinstance(exc, frappe.PermissionError):
		return fail(_("Not permitted"), code="PERMISSION_ERROR")
	return fail(cstr(exc), code=getattr(exc, "exc_type", None) or exc.__class__.__name__)
