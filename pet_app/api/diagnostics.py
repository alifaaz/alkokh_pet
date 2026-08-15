from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, cstr, now_datetime

from pet_app.api.link_aliases import with_link_aliases
from pet_app.api.response import fail, ok
from pet_app.utils import order_billing
from pet_app.workflows import clinical_state


@frappe.whitelist(methods=["POST"])
def collect_sample(lab=None, name=None):
	try:
		doc = _get_doc("Lab", lab or name)
		doc.check_permission("write")
		clinical_state.assert_action_allowed(doc, "collect_sample")
		doc.sample_collected_by = doc.sample_collected_by or frappe.session.user
		doc.sample_collected_at = doc.sample_collected_at or now_datetime()
		clinical_state.transition_status(doc, "Sample Collected", action="collect_sample")
		doc.save(ignore_permissions=True)
		_sync_order_status(doc, "In Progress")
		return ok({"lab": _diagnostic_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def save_lab_result(lab=None, name=None, result=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		doc = _get_doc("Lab", lab or name or payload.get("lab"))
		doc.check_permission("write")
		clinical_state.assert_action_allowed(doc, "save_result")
		doc.result = result if result is not None else payload.get("result") or doc.result
		if not doc.result:
			return fail(_("Lab result is required."), code="VALIDATION_ERROR")
		_set_review_fields(doc, payload)
		doc.result_entered_by = frappe.session.user
		doc.result_entered_at = now_datetime()
		doc.result_visibility = payload.get("result_visibility") or doc.result_visibility or "Clinical Team"
		clinical_state.transition_status(doc, "Result Entered", action="save_result")
		doc.save(ignore_permissions=True)
		_sync_order_status(doc, "In Progress")
		return ok({"lab": _diagnostic_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def release_lab_result(lab=None, name=None, result_visibility=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		doc = _get_doc("Lab", lab or name or payload.get("lab"))
		doc.check_permission("write")
		clinical_state.assert_action_allowed(doc, "release_lab_result")
		if not doc.result:
			return fail(_("Lab result is required before release."), code="VALIDATION_ERROR")
		if cint(doc.attachment_required) and not _has_attachment(doc):
			return fail(_("An attachment is required before release."), code="VALIDATION_ERROR")
		_set_review_fields(doc, payload)
		doc.result_visibility = result_visibility or payload.get("result_visibility") or doc.result_visibility or "Guardian Visible"
		doc.released_by = frappe.session.user
		doc.released_at = now_datetime()
		# Resolved and validated BEFORE the release is written: this endpoint converts its
		# own exceptions into a fail() response, so a billing problem raised after the save
		# would leave the lab Released and uncharged. None for a lab with no performing
		# branch, which is every lab today - that charge rides the visit as before.
		plan = order_billing.plan_order_billing(doc, item_type="Lab")
		clinical_state.transition_status(doc, "Released", action="release_lab_result")
		doc.save(ignore_permissions=True)
		_sync_order_status(doc, "Completed")
		billing = order_billing.commit_order_billing(doc, plan)
		payload_out = _diagnostic_payload(doc)
		if billing:
			payload_out["billing"] = billing
		return ok({"lab": payload_out})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def save_imaging_report(imaging=None, name=None, report=None, image=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		doc = _get_doc("Imaging", imaging or name or payload.get("imaging"))
		doc.check_permission("write")
		clinical_state.assert_action_allowed(doc, "save_imaging_report")
		doc.report = report if report is not None else payload.get("report") or doc.report
		doc.image = image or payload.get("image") or doc.image
		if not doc.report:
			return fail(_("Imaging report is required."), code="VALIDATION_ERROR")
		_set_review_fields(doc, payload)
		doc.result_entered_by = frappe.session.user
		doc.result_entered_at = now_datetime()
		doc.result_visibility = payload.get("result_visibility") or doc.result_visibility or "Clinical Team"
		clinical_state.transition_status(doc, "Reported", action="save_imaging_report")
		doc.save(ignore_permissions=True)
		_sync_order_status(doc, "In Progress")
		return ok({"imaging": _diagnostic_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def release_imaging_report(imaging=None, name=None, result_visibility=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		doc = _get_doc("Imaging", imaging or name or payload.get("imaging"))
		doc.check_permission("write")
		clinical_state.assert_action_allowed(doc, "release_imaging_report")
		if not doc.report:
			return fail(_("Imaging report is required before release."), code="VALIDATION_ERROR")
		if cint(doc.attachment_required) and not (doc.image or _has_attachment(doc)):
			return fail(_("An image or attachment is required before release."), code="VALIDATION_ERROR")
		_set_review_fields(doc, payload)
		doc.result_visibility = result_visibility or payload.get("result_visibility") or doc.result_visibility or "Guardian Visible"
		doc.released_by = frappe.session.user
		doc.released_at = now_datetime()
		# Pre-flight before the release is written - see release_lab_result. For radiology
		# this is the case the whole change exists for: performing_branch is "hotel", so
		# the charge leaves the visit here and lands on the facility's invoice.
		plan = order_billing.plan_order_billing(doc, item_type="Imaging")
		clinical_state.transition_status(doc, "Released", action="release_imaging_report")
		doc.save(ignore_permissions=True)
		_sync_order_status(doc, "Completed")
		billing = order_billing.commit_order_billing(doc, plan)
		payload_out = _diagnostic_payload(doc)
		if billing:
			payload_out["billing"] = billing
		return ok({"imaging": payload_out})
	except Exception as exc:
		return _error_response(exc)


def _get_doc(doctype, name):
	name = cstr(name).strip()
	if not name:
		frappe.throw(_("{0} is required.").format(doctype))
	return frappe.get_doc(doctype, name)


def _diagnostic_payload(doc) -> dict:
	payload = {
		"name": doc.name,
		"doctype": doc.doctype,
		"visit": doc.visit,
		"order_id": doc.get("order_id"),
		"pet": doc.pet,
		"doctor": doc.doctor,
		"care_service": doc.care_service,
		"item_code": doc.get("item_code"),
		"body_part": doc.get("body_part"),
		"modality": doc.get("modality"),
		"status": doc.status,
		"result": doc.get("result"),
		"report": doc.get("report"),
		"image": doc.get("image"),
		"sample_collected_by": doc.get("sample_collected_by"),
		"sample_collected_at": doc.get("sample_collected_at"),
		"result_entered_by": doc.get("result_entered_by"),
		"result_entered_at": doc.get("result_entered_at"),
		"released_by": doc.get("released_by"),
		"released_at": doc.get("released_at"),
		"doctor_reviewed": cint(doc.get("doctor_reviewed")),
		"doctor_reviewed_at": doc.get("doctor_reviewed_at"),
		"result_visibility": doc.get("result_visibility"),
	}
	return with_link_aliases(payload, pet_field="pet", doctor_field="doctor", include_guardian=False, include_provider=False)


def _set_review_fields(doc, payload):
	if "doctor_reviewed" in payload:
		doc.doctor_reviewed = cint(payload.get("doctor_reviewed"))
		if doc.doctor_reviewed and not doc.doctor_reviewed_at:
			doc.doctor_reviewed_at = now_datetime()
	if payload.get("doctor_reviewed_at"):
		doc.doctor_reviewed_at = payload.get("doctor_reviewed_at")
	if "attachment_required" in payload:
		doc.attachment_required = cint(payload.get("attachment_required"))


def _has_attachment(doc) -> bool:
	return bool(
		frappe.db.exists(
			"File",
			{"attached_to_doctype": doc.doctype, "attached_to_name": doc.name},
		)
	)


def _sync_order_status(doc, status, *, visit=None, save=True, allow_rewind=False):
	order_id = doc.get("order_id")
	if not doc.visit or not order_id:
		return False
	visit = visit or frappe.get_doc("Vet Visit", doc.visit)
	changed = False
	for row in visit.get("orders") or []:
		if row.order_id == order_id:
			try:
				clinical_state.transition_status(row, status)
			except frappe.ValidationError:
				if not allow_rewind:
					raise
				row.status = status
				row.flags.allow_status_reconcile = True
			row.linked_doctype = doc.doctype
			row.linked_name = doc.name
			changed = True
	if changed and save:
		visit.save(ignore_permissions=True)
	return changed


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
