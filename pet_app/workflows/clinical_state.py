from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr


TRANSITIONS = {
	"Vet Case Sheet": {
		"Draft": {"Waiting Practitioner", "In Consultation", "Converted to Visit", "Closed"},
		"Waiting Practitioner": {"In Consultation", "Converted to Visit", "Closed"},
		"In Consultation": {"Converted to Visit", "Closed"},
		"Converted to Visit": {"Closed"},
		"Closed": set(),
	},
	"Vet Visit": {
		"Draft": {"In Progress", "Completed", "Follow-up Needed", "Cancelled"},
		"In Progress": {"Completed", "Follow-up Needed", "Cancelled"},
		"Follow-up Needed": {"In Progress", "Completed", "Cancelled"},
		"Completed": set(),
		"Cancelled": set(),
	},
	"Visit Order": {
		"Draft": {"Ordered", "In Progress", "Completed", "Cancelled"},
		"Ordered": {"In Progress", "Completed", "Cancelled"},
		"In Progress": {"Completed", "Cancelled"},
		"Completed": set(),
		"Cancelled": set(),
	},
	"Lab": {
		"Pending": {"Ordered", "Sample Collected", "In Progress", "Result Entered", "Released", "Cancelled"},
		"Ordered": {"Sample Collected", "In Progress", "Result Entered", "Released", "Cancelled"},
		"Sample Collected": {"In Progress", "Result Entered", "Released", "Cancelled"},
		"In Progress": {"Result Entered", "Released", "Completed", "Cancelled"},
		"Result Entered": {"Released", "Cancelled"},
		"Released": set(),
		"Completed": set(),
		"Cancelled": set(),
	},
	"Imaging": {
		"Pending": {"Ordered", "Scheduled", "In Progress", "Reported", "Released", "Cancelled"},
		"Ordered": {"Scheduled", "In Progress", "Reported", "Released", "Cancelled"},
		"Scheduled": {"In Progress", "Reported", "Released", "Cancelled"},
		"In Progress": {"Reported", "Released", "Completed", "Cancelled"},
		"Reported": {"Released", "Cancelled"},
		"Released": set(),
		"Completed": set(),
		"Cancelled": set(),
	},
	"PetCareService": {
		"pending": {"pending", "completed", "overdue", "cancelled", "Completed", "Cancelled"},
		"overdue": {"pending", "completed", "cancelled", "Completed", "Cancelled"},
		"completed": set(),
		"cancelled": set(),
		"Completed": set(),
		"Cancelled": set(),
	},
	"Pet Procedure": {
		"Pending": {"In Progress", "Completed", "Closed", "Cancelled"},
		"In Progress": {"Completed", "Closed", "Cancelled"},
		"Completed": {"Closed"},
		"Closed": set(),
		"Cancelled": set(),
	},
}


ACTION_ALLOWED_STATUSES = {
	"Vet Visit": {
		"set_case_choice": {"Draft", "In Progress", "Follow-up Needed"},
		"start_consultation": {"Draft", "In Progress", "Follow-up Needed"},
		"save_clinical_note": {"Draft", "In Progress", "Follow-up Needed"},
		"save_diagnoses": {"Draft", "In Progress", "Follow-up Needed"},
		"create_orders": {"Draft", "In Progress", "Follow-up Needed"},
		"cancel_medication": {"Draft", "In Progress", "Follow-up Needed"},
		"complete_case": {"Draft", "In Progress", "Follow-up Needed"},
		"request_follow_up": {"Draft", "In Progress", "Completed", "Follow-up Needed"},
		"request_consult": {"Draft", "In Progress", "Follow-up Needed"},
		"complete_consult": {"Draft", "In Progress", "Follow-up Needed"},
	},
	"Lab": {
		"start_test": {"Pending", "Ordered", "Sample Collected", "In Progress"},
		"collect_sample": {"Pending", "Ordered", "Sample Collected"},
		"save_result": {"Pending", "Ordered", "Sample Collected", "In Progress", "Result Entered"},
		"release": {"Pending", "Ordered", "Sample Collected", "In Progress", "Result Entered"},
		"release_lab_result": {"Pending", "Ordered", "Sample Collected", "In Progress", "Result Entered"},
		"cancel_test": {"Pending", "Ordered", "Sample Collected", "In Progress", "Result Entered"},
	},
	"Imaging": {
		"start_test": {"Pending", "Ordered", "Scheduled", "In Progress"},
		"save_result": {"Pending", "Ordered", "Scheduled", "In Progress", "Reported"},
		"save_imaging_report": {"Pending", "Ordered", "Scheduled", "In Progress", "Reported"},
		"release": {"Pending", "Ordered", "Scheduled", "In Progress", "Reported"},
		"release_imaging_report": {"Pending", "Ordered", "Scheduled", "In Progress", "Reported"},
		"cancel_test": {"Pending", "Ordered", "Scheduled", "In Progress", "Reported"},
	},
	"PetCareService": {
		"start_service": {"pending", "overdue"},
		"finish_service": {"pending", "overdue", "completed"},
		"close_service": {"pending", "overdue", "completed"},
		"cancel_service": {"pending", "overdue"},
	},
	"Pet Procedure": {
		"attach_file": {"In Progress", "Completed"},
		"start_procedure": {"Pending", "In Progress"},
		"save_procedure_note": {"Pending", "In Progress", "Completed"},
		"complete_procedure": {"Pending", "In Progress", "Completed"},
		"close_procedure": {"Pending", "In Progress", "Completed", "Closed"},
		"cancel_procedure": {"Pending", "In Progress"},
	},
}

TERMINAL_STATUSES = {
	"Vet Visit": {"Completed", "Cancelled"},
	"Vet Case Sheet": {"Closed"},
	"Visit Order": {"Completed", "Cancelled"},
	"Lab": {"Released", "Completed", "Cancelled"},
	"Imaging": {"Released", "Completed", "Cancelled"},
	"PetCareService": {"completed", "cancelled", "Completed", "Cancelled"},
	"Pet Procedure": {"Closed", "Cancelled"},
}

VISIT_CLINICAL_ACTIONS = {
	"set_case_choice",
	"start_consultation",
	"save_clinical_note",
	"save_diagnoses",
	"create_orders",
	"cancel_medication",
	"complete_case",
	"request_follow_up",
	"request_consult",
	"complete_consult",
}


def transition_status(doc, target_status: str, *, action: str | None = None):
	doctype = doc.doctype
	current = cstr(doc.get("status")).strip()
	target_status = cstr(target_status).strip()
	if not target_status or not doc.meta.has_field("status"):
		return doc
	if not current:
		doc.status = target_status
		return doc
	if current == target_status:
		return doc
	assert_transition(doctype, current, target_status)
	doc.status = target_status
	return doc


def assert_transition_allowed(doc, new_status: str, action: str | None = None):
	if action:
		assert_action_allowed(doc, action)
	if not doc.meta.has_field("status"):
		return
	assert_transition(doc.doctype, cstr(doc.get("status")).strip(), cstr(new_status).strip())


def mark_visit_status(visit, new_status: str, action: str | None = None):
	if isinstance(visit, str):
		visit = frappe.get_doc("Vet Visit", visit)
	if visit.doctype != "Vet Visit":
		frappe.throw(_("mark_visit_status requires a Vet Visit."))
	assert_transition_allowed(visit, new_status, action=action)
	transition_status(visit, new_status, action=action)
	visit.save(ignore_permissions=True)
	return visit


def validate_document_transition(doc):
	if doc.doctype == "Visit Order" and doc.flags.get("allow_status_reconcile"):
		return
	previous = doc.get_doc_before_save()
	if not previous or not doc.meta.has_field("status"):
		return
	current = cstr(previous.get("status")).strip()
	target = cstr(doc.get("status")).strip()
	if current and target and current != target:
		assert_transition(doc.doctype, current, target)


def assert_transition(doctype: str, current: str, target_status: str):
	current = cstr(current).strip()
	target_status = cstr(target_status).strip()
	if not current or not target_status or current == target_status:
		return
	allowed = TRANSITIONS.get(doctype, {}).get(current)
	if allowed is None or target_status not in allowed:
		frappe.throw(
			_("Invalid {0} status transition from {1} to {2}.").format(
				frappe.bold(doctype), frappe.bold(current), frappe.bold(target_status)
			)
		)


def assert_action_allowed(doc, action: str):
	action = cstr(action).strip()
	if not action or not doc.meta.has_field("status"):
		return
	if doc.doctype == "Vet Visit" and action in VISIT_CLINICAL_ACTIONS:
		assert_visit_not_billed(doc)
	allowed = ACTION_ALLOWED_STATUSES.get(doc.doctype, {}).get(action)
	if not allowed:
		return
	status = cstr(doc.get("status")).strip()
	if status not in allowed:
		frappe.throw(
			_("{0} is not allowed for {1} while status is {2}.").format(
				frappe.bold(action), frappe.bold(doc.doctype), frappe.bold(status)
			)
		)


def assert_visit_not_billed(visit):
	if isinstance(visit, str):
		visit = frappe.get_doc("Vet Visit", visit)
	if is_billed_visit(visit):
		frappe.throw(_("This visit is already billed and cannot be modified."))


def assert_action_requirements(action: str, visit, payload=None):
	action = cstr(action).strip()
	if isinstance(visit, str):
		visit = frappe.get_doc("Vet Visit", visit)
	if action in VISIT_CLINICAL_ACTIONS:
		assert_visit_not_billed(visit)
	assert_action_allowed(visit, action)
	if action == "create_orders" and not (payload or {}).get("orders") and not (payload or {}).get("rows"):
		frappe.throw(_("At least one order is required."))
	if action == "request_follow_up":
		payload = payload or {}
		if not (
			payload.get("follow_up_preferred_date")
			or payload.get("preferred_date")
			or payload.get("follow_up_date")
			or visit.get("follow_up_preferred_date")
			or visit.get("follow_up_date")
		):
			frappe.throw(_("Follow-up preferred date is required."))


def is_terminal_status(status: str, doctype: str = "Vet Visit") -> bool:
	return cstr(status).strip() in TERMINAL_STATUSES.get(doctype, set())


def is_billed_visit(visit) -> bool:
	if isinstance(visit, str):
		visit = frappe.db.get_value("Vet Visit", visit, ["billed", "sales_invoice"], as_dict=True)
	if not visit:
		return False
	return bool(getattr(visit, "billed", None) or getattr(visit, "sales_invoice", None) or visit.get("billed") or visit.get("sales_invoice"))
