from __future__ import annotations

import base64
import hashlib
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, getdate, now_datetime, nowdate

from pet_app.api.healthcare.boarding import checked_in_boarding_for_visit
from pet_app.api.link_aliases import enrich_link_aliases, with_link_aliases
from pet_app.api.permissions import get_user_roles, require_restriction_value, user_has_full_access
from pet_app.utils import order_billing
from pet_app.utils.branch import apply_branch_filter
from pet_app.utils.guardian_customer import get_or_create_customer_from_guardian
from pet_app.utils.clinical_options import (
	apply_structured_clinical_payload,
	clinical_options_payload,
	has_internal_clinical_note,
	visit_clinical_payload,
)
from pet_app.utils.medical_profile import (
	get_visit_case_context,
	set_visit_case_choice,
	sync_completed_visit,
	sync_diagnoses_from_visit,
	sync_follow_up_from_visit,
	sync_pending_orders_from_visit,
	sync_treatment_from_visit,
	update_profile_for_visit,
)
from pet_app.utils.practitioner import (
	get_practitioner_for_user,
	practitioner_exists,
	practitioner_payload,
	resolve_practitioner,
)
from pet_app.utils.visit_billing import (
	BILLED_VISIT_LOCK_MESSAGE,
	STRICT_MODE,
	apply_billable_item_amounts,
	assert_boarding_billable_item_can_cancel,
	assert_visit_billable_item_can_cancel,
	cancel_boarding_billable_item_by_link,
	cancel_visit_billable_item_by_link,
	summarise_visit_billing,
	visit_billing_status,
)
from pet_app.pet_app.doctype.vet_visit.vet_visit import (
	_create_sales_invoice_for_visit,
	_mark_visit_invoiced,
	get_billable_invoice_items,
)
from pet_app.workflows import clinical_state
from pet_app.api.response import standardize_response


SOURCE_DOCTYPE_ALIASES = {
	"visit": "Vet Visit",
	"vet visit": "Vet Visit",
	"vet_visit": "Vet Visit",
	"case sheet": "Vet Case Sheet",
	"case_sheet": "Vet Case Sheet",
	"appointment": "Appointment",
	"service": "PetCareService",
	"petcareservice": "PetCareService",
	"pet care service": "PetCareService",
	"procedure": "Pet Procedure",
	"pet procedure": "Pet Procedure",
	"pet_procedure": "Pet Procedure",
	"lab": "Lab",
	"radiology": "Imaging",
	"imaging": "Imaging",
	"invoice": "Sales Invoice",
	"sales invoice": "Sales Invoice",
	"sales_invoice": "Sales Invoice",
	"payment": "Payment Entry",
	"payment entry": "Payment Entry",
}

SOURCE_LABELS = {
	"Vet Visit": "Visit",
	"Vet Case Sheet": "Case Sheet",
	"Appointment": "Appointment",
	"PetCareService": "Service",
	"Pet Procedure": "Procedure",
	"Lab": "Lab",
	"Imaging": "Radiology",
	"Sales Invoice": "Invoice",
	"Payment Entry": "Payment",
}

DIAGNOSTIC_RESULT_FILE_FIELDS = {
	"Lab": ("result_file",),
	"Imaging": ("image", "report_file"),
}
# Free-text diagnostic findings, per doctype. Released-only on the visit page and
# history surfaces — see _linked_records_for_visit. The record detail page
# (_diagnostic_detail) is deliberately NOT gated: staff work on a record there.
DIAGNOSTIC_TEXT_FIELDS = {
	"Lab": ("result",),
	"Imaging": ("report",),
}
# Exact Select value. "Reported" / "Result Entered" / "Completed" are NOT released.
DIAGNOSTIC_RELEASED_STATUS = "Released"
DIAGNOSTIC_CANCEL_ACTIONS = {"cancel_test", "cancel_lab", "cancel_imaging", "cancel_lab_order", "cancel_imaging_order"}
IMAGE_FILE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}

# Workspace authorization must stay on DocPerm-backed operational roles.
# Page/sidebar-only roles belong in page access settings, not here.
DOCTOR_WRITE_ROLES = {"Doctor", "Visit Admin"}
DOCTOR_ROLES = DOCTOR_WRITE_ROLES | {"Visit Read"}
COORDINATOR_WRITE_ROLES = {"Coordinatorr", "Coordinator", "Reception"}
COORDINATOR_ROLES = COORDINATOR_WRITE_ROLES
DIAGNOSTIC_WRITE_ROLES = {"Lab Admin", "Radiology Admin"}
DIAGNOSTIC_ROLES = DIAGNOSTIC_WRITE_ROLES | {"Laboratory User", "Lab Read", "Radiology Read"}
ACCOUNTING_WRITE_ROLES = {"Accounts User", "Accounts Manager", "Sales User", "POS Cashier", "POS Admin"}
ACCOUNTING_ROLES = ACCOUNTING_WRITE_ROLES
MANAGEMENT_ROLES = {"System Manager", "Pet App Admin"}
SERVICE_PROVIDER_ROLES = {"Service Provider Manager", "Service Provider", "Groomer", "Nursing User"}

GUARDIAN_ROLES = set()

COORDINATOR_PAGE_ROLES = {"Healthcare Coordinator Page"}
DOCTOR_PAGE_ROLES = {"Visits Page", "Case Sheets Page"}
DIAGNOSTIC_PAGE_ROLES = {"Labs Page", "Radiology Page"}
ACCOUNTING_PAGE_ROLES = {"POS Page", "Sales Invoices Page", "Payment Entries Page"}
SERVICE_PAGE_ROLES = {"Service Provider Page"}

WORKSPACE_ROLES = DOCTOR_ROLES | COORDINATOR_ROLES | DIAGNOSTIC_ROLES | ACCOUNTING_ROLES | MANAGEMENT_ROLES | SERVICE_PROVIDER_ROLES
WORKSPACE_WRITE_ROLES = (
	DOCTOR_WRITE_ROLES
	| COORDINATOR_WRITE_ROLES
	| DIAGNOSTIC_WRITE_ROLES
	| ACCOUNTING_WRITE_ROLES
	| MANAGEMENT_ROLES
	| SERVICE_PROVIDER_ROLES
)
WORKSPACE_MODES = {"doctor", "service", "coordinator", "diagnostics", "accounting", "management", "all"}
# A checked-in stay no longer freezes its visit. It used to block every clinical action,
# because charges only reached an invoice when the visit closed, so an order raised while
# the animal was in a kennel would have missed the bill. Per-order billing removed that:
# order_billing raises each charge at the order's own completion and deliberately never
# writes Vet Visit.sales_invoice, so a late order is charged and the visit stays editable.
# Documentation (notes, diagnoses, attachments, consults, follow-ups) was never a billing
# question at all, and each action still has its own status gate in clinical_state.
#
# complete_case is NOT released, and not for billing reasons:
#   - "Completed" is terminal in clinical_state.TRANSITIONS - there is no reopen.
#   - completing writes Vet Visit.sales_invoice, which turns on _validate_sales_invoice_lock
#     and makes set_values_from_visit throw BILLED_VISIT_LOCK_MESSAGE on every visit-linked
#     Lab/Imaging afterwards. Closing the visit mid-stay would strand the very orders this
#     change exists to allow: they could no longer be released.
#   - _validate_visit_completion_requirements (and STRICT_MODE's pending-records check)
#     demand every diagnostic be finished, which mid-stay they routinely are not.
# The supported order is the other one: close the visit, then board - see
# boarding.VISIT_BOARDING_BLOCKED_VISIT_STATUSES.
VISIT_BOARDING_BLOCKED_ACTIONS = {"complete_case"}

OPEN_VISIT_STATUSES = {"Draft", "In Progress", "Follow-up Needed"}
OPEN_CASE_STATUSES = {"Draft", "Waiting Practitioner", "In Consultation"}
OPEN_SERVICE_STATUSES = {"pending", "overdue", "Pending", "Overdue"}
OPEN_PROCEDURE_STATUSES = {"Pending", "In Progress"}
OPEN_DIAGNOSTIC_STATUSES = {
	"Pending",
	"Ordered",
	"Sample Collected",
	"Scheduled",
	"In Progress",
	"Result Entered",
	"Reported",
}
OPEN_CONSULT_STATUSES = {"Requested", "Accepted"}


@frappe.whitelist()
@standardize_response
def get_my_workspace(
	mode=None,
	user=None,
	search=None,
	priority=None,
	status=None,
	limit=50,
	cursor=0,
	date_from=None,
	date_to=None,
	doctor=None,
	practitioner=None,
	guardian=None,
	pet=None,
	species=None,
	visit_type=None,
	source_type=None,
	billing_status=None,
	branch=None,
	room=None,
	assigned_to=None,
):
	workspace_user = _resolve_workspace_user(user)
	doctor = practitioner or doctor
	roles = get_user_roles(workspace_user)
	workspace_mode = _resolve_mode(mode, roles, workspace_user)

	items = []
	if workspace_mode in {"doctor", "management", "all"}:
		items.extend(_visit_items(workspace_user, roles, search=search))
		items.extend(_procedure_items(workspace_user, roles, search=search, scope="doctor"))
	if workspace_mode in {"service", "management", "all"}:
		items.extend(_service_items(workspace_user, roles, search=search))
		items.extend(_procedure_items(workspace_user, roles, search=search, scope="provider"))
	if workspace_mode in {"coordinator", "management", "all"}:
		items.extend(_appointment_items(workspace_user, roles, search=search))
		items.extend(_case_sheet_items(workspace_user, roles, search=search))
		items.extend(_unassigned_service_items(workspace_user, roles, search=search))
		items.extend(_procedure_items(workspace_user, roles, search=search, scope="coordinator"))
	if workspace_mode in {"diagnostics", "management", "all"}:
		items.extend(_diagnostic_items("Lab", workspace_user, roles, search=search))
		items.extend(_diagnostic_items("Imaging", workspace_user, roles, search=search))
	if workspace_mode in {"accounting", "management", "all"}:
		items.extend(_invoice_items(workspace_user, roles, search=search))

	items = _dedupe_workspace_items(items)
	items = _apply_item_filters(
		items,
		search=search,
		priority=priority,
		status=status,
		date_from=date_from,
		date_to=date_to,
		doctor=doctor,
		guardian=guardian,
		pet=pet,
		species=species,
		visit_type=visit_type,
		source_type=source_type,
		billing_status=billing_status,
		branch=branch,
		room=room,
		assigned_to=assigned_to,
	)
	items.sort(key=_item_sort_key)

	offset = max(cint(cursor), 0)
	page_length = max(min(cint(limit or 50), 200), 1)
	page = items[offset : offset + page_length]
	next_cursor = offset + page_length if offset + page_length < len(items) else None

	return {
		"mode": workspace_mode,
		"user": workspace_user,
		"metrics": _workspace_metrics(items),
		"items": page,
		"next_cursor": next_cursor,
		"cursor": next_cursor,
		"total": len(items),
	}


@frappe.whitelist()
@standardize_response
def get_record(source_type, name):
	doctype = _normalize_source_type(source_type)
	name = cstr(name).strip()
	if not name:
		frappe.throw(_("Record name is required."))

	_assert_record_access(doctype, name)

	if doctype == "Vet Visit":
		return _visit_aggregate(name)

	if doctype == "Vet Case Sheet":
		visit_name = frappe.db.get_value("Vet Case Sheet", name, "vet_visit")
		if visit_name:
			aggregate = _visit_aggregate(visit_name)
			aggregate["focus_source"] = _focus_source(doctype, name)
			return aggregate
		return _case_sheet_aggregate(name)

	linked_visit = _linked_visit_for_source(doctype, name)
	if linked_visit:
		try:
			aggregate = _visit_aggregate(linked_visit)
			aggregate["focus_source"] = _focus_source(doctype, name)
			return aggregate
		except frappe.PermissionError:
			pass

	if doctype == "Appointment":
		follow_up_origin = _follow_up_origin_for_appointment(name)
		if follow_up_origin:
			aggregate = _visit_aggregate(follow_up_origin)
			aggregate["focus_source"] = _focus_source(doctype, name)
			aggregate["pending_follow_up_appointment"] = name
			return aggregate

	return _source_aggregate(doctype, name)


@frappe.whitelist(methods=["POST"])
@standardize_response
def perform_action(source_type, name, action, payload=None):
	doctype = _normalize_source_type(source_type)
	name = cstr(name).strip()
	action = cstr(action).strip()
	payload = _coerce_dict(payload)
	if not name or not action:
		frappe.throw(_("source_type, name, and action are required."))

	_assert_record_access(doctype, name, write=True, action=action)

	if action == "convert_to_visit":
		visit_name = _convert_to_visit(doctype, name, payload)
		return _visit_aggregate(visit_name)
	if action == "convert_to_service":
		service_name = _convert_to_service(doctype, name, payload)
		return get_record("service", service_name)
	if action == "convert_follow_up_to_visit":
		visit_name = _convert_follow_up_to_visit(doctype, name, payload)
		return _visit_aggregate(visit_name)

	visit_name = name if doctype == "Vet Visit" else _linked_visit_for_source(doctype, name)
	if action in {
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
	}:
		if not visit_name:
			frappe.throw(_("This action requires a linked Vet Visit."))
		if action == "set_case_choice":
			_set_case_choice(visit_name, payload)
		elif action == "start_consultation":
			_start_consultation(visit_name)
		elif action == "save_clinical_note":
			_save_clinical_note(visit_name, payload)
		elif action == "save_diagnoses":
			_save_diagnoses(visit_name, payload)
		elif action == "create_orders":
			_create_orders(visit_name, payload)
		elif action == "cancel_medication":
			_cancel_medication(visit_name, payload)
		elif action == "complete_case":
			_complete_case(visit_name, payload)
		elif action == "request_follow_up":
			_request_follow_up(visit_name, payload)
		elif action == "request_consult":
			_request_consult(visit_name, payload)
		elif action == "complete_consult":
			_complete_consult(visit_name, payload)
		return _visit_aggregate(visit_name)

	if action in {
		"start_procedure",
		"save_procedure_note",
		"complete_procedure",
		"close_procedure",
		"cancel_procedure",
	}:
		procedure_name = name if doctype == "Pet Procedure" else cstr(payload.get("procedure") or payload.get("procedure_name")).strip()
		if not procedure_name:
			frappe.throw(_("This action requires a procedure record."))
		_assert_record_access("Pet Procedure", procedure_name, write=True, action=action)
		if action == "save_procedure_note":
			_save_procedure_note(procedure_name, payload)
		else:
			_update_procedure_status(procedure_name, action, payload)
		return get_record("procedure", procedure_name)

	if action in {"start_service", "finish_service", "close_service", "cancel_service"}:
		service_name = name if doctype == "PetCareService" else _linked_service_for_source(doctype, name)
		if not service_name:
			frappe.throw(_("This action requires a service record."))
		_update_service_status(service_name, action, payload)
		return get_record("service", service_name)

	if action in {"start_test", "save_result", "release"} | DIAGNOSTIC_CANCEL_ACTIONS:
		if doctype not in {"Lab", "Imaging"}:
			frappe.throw(_("This action requires a Lab or Radiology record."))
		_update_diagnostic_status(doctype, name, action, payload)
		return get_record(SOURCE_LABELS[doctype], name)

	if action in {"assign", "reassign"}:
		_assign_record(doctype, name, payload)
		return get_record(source_type, name)

	if action == "submit_invoice":
		_submit_invoice(doctype, name)
		return get_record(source_type, name)
	if action == "mark_follow_up":
		_mark_follow_up(doctype, name, payload)
		return get_record(source_type, name)

	frappe.throw(_("Unsupported workspace action: {0}").format(action))


@frappe.whitelist(methods=["POST"])
@standardize_response
def add_note(source_type, name, note=None, content=None, payload=None):
	doctype = _normalize_source_type(source_type)
	name = cstr(name).strip()
	payload = _coerce_dict(payload)
	note_text = cstr(note or content or payload.get("note") or payload.get("content")).strip()
	if not note_text:
		frappe.throw(_("Note is required."))

	_assert_record_access(doctype, name, write=True, action="add_note")
	doc = frappe.get_doc(doctype, name)
	doc.add_comment("Comment", note_text)
	return get_record(source_type, name)


@frappe.whitelist(methods=["POST"])
@standardize_response
def attach_file(source_type, name, payload=None, file_url=None, file_name=None, filedata=None, is_private=1):
	doctype = _normalize_source_type(source_type)
	name = cstr(name).strip()
	payload = _coerce_dict(payload)
	file_url = file_url or payload.get("file_url")
	file_name = file_name or payload.get("file_name") or payload.get("filename")
	filedata = filedata or payload.get("filedata") or payload.get("content")
	is_private = cint(payload.get("is_private", is_private))

	_assert_record_access(doctype, name, write=True, action="attach_file")
	if doctype == "Pet Procedure":
		doc = frappe.get_doc(doctype, name)
		clinical_state.assert_action_allowed(doc, "attach_file")
	if not file_url and not filedata:
		frappe.throw(_("file_url or filedata is required."))

	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": file_name or _file_name_from_url(file_url) or _("Workspace Attachment"),
			"file_url": file_url,
			"content": _decode_filedata(filedata) if filedata else None,
			"attached_to_doctype": doctype,
			"attached_to_name": name,
			"is_private": is_private,
		}
	)
	file_doc.insert(ignore_permissions=True)
	return get_record(source_type, name)


def _resolve_workspace_user(user=None):
	session_user = frappe.session.user
	if not session_user or session_user == "Guest":
		frappe.throw(_("Authentication required."), frappe.PermissionError)

	user = cstr(user or session_user).strip()
	if user != session_user and not user_has_full_access(session_user):
		frappe.throw(_("Not permitted to view another user's workspace."), frappe.PermissionError)
	return user


def _resolve_mode(mode, roles: set[str], user: str) -> str:
	mode = cstr(mode).strip().lower()
	allowed_modes = _allowed_workspace_modes(roles, user)
	if mode in WORKSPACE_MODES:
		if mode in allowed_modes:
			return mode
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	for candidate in ("management", "coordinator", "doctor", "diagnostics", "accounting", "service"):
		if candidate in allowed_modes:
			return candidate
	return "service"


def _allowed_workspace_modes(roles: set[str], user: str) -> set[str]:
	if user_has_full_access(user) or roles & MANAGEMENT_ROLES:
		return set(WORKSPACE_MODES)

	allowed_modes = {"service"}
	if roles & (COORDINATOR_ROLES | COORDINATOR_PAGE_ROLES):
		allowed_modes.add("coordinator")
	if _doctor_for_user(user) or roles & (DOCTOR_ROLES | DOCTOR_PAGE_ROLES):
		allowed_modes.add("doctor")
	if roles & (DIAGNOSTIC_ROLES | DIAGNOSTIC_PAGE_ROLES):
		allowed_modes.add("diagnostics")
	if roles & (ACCOUNTING_ROLES | ACCOUNTING_PAGE_ROLES):
		allowed_modes.add("accounting")
	return allowed_modes


def _normalize_source_type(source_type: str) -> str:
	key = cstr(source_type).strip().lower()
	doctype = SOURCE_DOCTYPE_ALIASES.get(key) or cstr(source_type).strip()
	if not doctype or not frappe.db.exists("DocType", doctype):
		frappe.throw(_("Unsupported source type: {0}").format(source_type))
	return doctype


def _has_workspace_doctype_permission(doctype: str, write: bool = False, user: str | None = None) -> bool:
	try:
		return bool(frappe.has_permission(doctype, ptype="write" if write else "read", user=user or frappe.session.user))
	except Exception:
		return False


def _has_legacy_or_doctype_permission(
	roles: set[str],
	legacy_roles: set[str],
	doctype: str,
	write: bool,
	user: str,
) -> bool:
	return bool(roles & legacy_roles) or _has_workspace_doctype_permission(doctype, write, user)


def _assert_record_access(doctype: str, name: str, write: bool = False, action: str | None = None):
	if write and doctype == "Vet Visit" and action in VISIT_BOARDING_BLOCKED_ACTIONS:
		_assert_visit_not_checked_in_boarding(name, action=action)

	user = frappe.session.user
	if user_has_full_access(user):
		return
	if not frappe.db.exists(doctype, name):
		frappe.throw(_("{0} {1} was not found.").format(doctype, name))

	roles = get_user_roles(user)
	_assert_source_restrictions(doctype, name, user)
	if roles & GUARDIAN_ROLES and not write and _guardian_can_read_source(user, doctype, name):
		return

	doctor_roles = DOCTOR_WRITE_ROLES if write else DOCTOR_ROLES
	coordinator_roles = COORDINATOR_WRITE_ROLES if write else COORDINATOR_ROLES
	diagnostic_roles = DIAGNOSTIC_WRITE_ROLES if write else DIAGNOSTIC_ROLES
	accounting_roles = ACCOUNTING_WRITE_ROLES if write else ACCOUNTING_ROLES
	management_roles = MANAGEMENT_ROLES
	workspace_roles = WORKSPACE_WRITE_ROLES if write else WORKSPACE_ROLES
	has_source_access = _has_workspace_doctype_permission(doctype, write, user)
	if not (roles & workspace_roles or has_source_access):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	doctor = _doctor_for_user(user)
	doctor_can_access = not write or bool(roles & doctor_roles) or has_source_access
	if doctype == "Vet Visit":
		visit_doctor = frappe.db.get_value("Vet Visit", name, "doctor")
		if visit_doctor and doctor and visit_doctor == doctor and doctor_can_access:
			return
		if doctor and doctor_can_access and _visit_has_consult_for_doctor(name, doctor):
			return
		if _has_legacy_or_doctype_permission(
			roles,
			coordinator_roles | diagnostic_roles | accounting_roles | management_roles,
			doctype,
			write,
			user,
		):
			return
	elif doctype == "Vet Case Sheet":
		if _has_legacy_or_doctype_permission(roles, doctor_roles | coordinator_roles | management_roles, doctype, write, user):
			return
	elif doctype in {"Lab", "Imaging"}:
		record_doctor = _get_optional_value(doctype, name, "doctor")
		if record_doctor and doctor and record_doctor == doctor and doctor_can_access:
			return
		if _has_legacy_or_doctype_permission(
			roles,
			diagnostic_roles | coordinator_roles | management_roles,
			doctype,
			write,
			user,
		):
			return
	elif doctype == "PetCareService":
		row = frappe.db.get_value(
			"PetCareService",
			name,
			_fields("PetCareService", ["provider", "user", "doctor"]),
			as_dict=True,
		)
		practitioner = _doctor_for_user(user)
		if row and (row.get("provider") in {user, doctor, practitioner} or row.get("user") == user) and (
			roles & workspace_roles or has_source_access
		):
			return
		if row and row.get("doctor") and doctor and row.get("doctor") == doctor and doctor_can_access:
			return
		if _has_legacy_or_doctype_permission(
			roles,
			coordinator_roles | management_roles,
			doctype,
			write,
			user,
		):
			return
		if _has_legacy_or_doctype_permission(
			roles,
			{"Service Provider Manager"},
			doctype,
			write,
			user,
		):
			return
	elif doctype == "Pet Procedure":
		row = frappe.db.get_value(
			"Pet Procedure",
			name,
			_fields("Pet Procedure", ["provider", "doctor"]),
			as_dict=True,
		)
		if row and row.get("provider") == user and (roles & workspace_roles or has_source_access):
			return
		if row and row.get("doctor") and doctor and row.get("doctor") == doctor and doctor_can_access:
			return
		if _has_legacy_or_doctype_permission(
			roles,
			coordinator_roles | management_roles,
			doctype,
			write,
			user,
		):
			return
	elif doctype == "Appointment":
		if _has_legacy_or_doctype_permission(roles, coordinator_roles | doctor_roles | management_roles, doctype, write, user):
			return
	elif doctype in {"Sales Invoice", "Payment Entry"}:
		if _has_legacy_or_doctype_permission(roles, accounting_roles | management_roles, doctype, write, user):
			return

	frappe.throw(_("Not permitted"), frappe.PermissionError)


def _visit_items(user: str, roles: set[str], search=None) -> list[dict]:
	filters = {"docstatus": ["<", 2]}
	doctor = _doctor_for_user(user)
	if not user_has_full_access(user) and doctor and not roles & (COORDINATOR_ROLES | MANAGEMENT_ROLES):
		filters["doctor"] = doctor
	elif not user_has_full_access(user) and roles & DOCTOR_ROLES and doctor:
		filters["doctor"] = doctor

	# Clinic separation applies to the worklist only. Pet history keeps returning visits
	# from every clinic -- see _latest_visits in api/medical_profile.py and the visit
	# workbench, neither of which is branch filtered.
	apply_branch_filter(filters, "Vet Visit", user)

	visit_fields = _fields(
		"Vet Visit",
		[
			"name",
			"status",
			"priority",
			"visit_datetime",
			"case_sheet",
			"animal_patient",
			"guardian",
			"doctor",
			"visit_type",
			"follow_up_preferred_date",
			"follow_up_date",
			"follow_up_status",
			"follow_up_visit_id",
			"sales_invoice",
			"billing_status",
			"total_billable_amount",
			"modified",
			"creation",
		],
	)
	rows = frappe.get_all(
		"Vet Visit",
		filters=filters,
		fields=visit_fields,
		order_by="modified desc",
		limit_page_length=200,
		ignore_permissions=True,
	)
	if doctor and _has_field("Vet Visit", "consult_requests"):
		seen = {row.name for row in rows}
		consult_visit_names = [name for name in _consult_visit_names_for_doctor(doctor) if name not in seen]
		if consult_visit_names:
			rows.extend(
				frappe.get_all(
					"Vet Visit",
					filters={"name": ["in", consult_visit_names], "docstatus": ["<", 2]},
					fields=visit_fields,
					order_by="modified desc",
					limit_page_length=200,
					ignore_permissions=True,
				)
			)
		for row in rows:
			if row.get("doctor") != doctor and _visit_has_consult_for_doctor(row.name, doctor):
				row["_consult_requested_for_user"] = 1
	return [_visit_item(row) for row in rows]


def _case_sheet_items(user: str, roles: set[str], search=None) -> list[dict]:
	case_sheet_filters = {"status": ["in", list(OPEN_CASE_STATUSES)], "docstatus": ["<", 2]}
	apply_branch_filter(case_sheet_filters, "Vet Case Sheet", user)
	rows = frappe.get_all(
		"Vet Case Sheet",
		filters=case_sheet_filters,
		fields=_fields(
			"Vet Case Sheet",
			[
				"name",
				"status",
				"priority",
				"case_sheet_date",
				"guardian",
				"animal_patient",
				"chief_complaint",
				"vet_visit",
				"modified",
				"creation",
			],
		),
		order_by="modified desc",
		limit_page_length=200,
		ignore_permissions=True,
	)
	return [_case_sheet_item(row) for row in rows if not row.get("vet_visit")]


def _appointment_items(user: str, roles: set[str], search=None) -> list[dict]:
	fields = _fields(
		"Appointment",
		[
			"name",
			"status",
			"scheduled_time",
			"customer_name",
			"customer_phone_number",
			"custom_appointment_type",
			"custom_pet",
				"custom_guardian",
				"custom_customer",
				"custom_follow_up_of_visit_id",
				"custom_linked_visit_id",
				"custom_linked_service_id",
				"modified",
			"creation",
		],
	)
	rows = frappe.get_all(
		"Appointment",
		filters={"docstatus": ["<", 2]},
		fields=fields,
		order_by="modified desc",
		limit_page_length=200,
		ignore_permissions=True,
	)
	items = []
	for row in rows:
		if row.get("custom_linked_visit_id") or row.get("custom_linked_service_id"):
			continue
		items.append(_appointment_item(row))
	return items


def _service_items(user: str, roles: set[str], search=None) -> list[dict]:
	filters = {"docstatus": ["<", 2]}
	if not user_has_full_access(user) and not roles & (COORDINATOR_ROLES | MANAGEMENT_ROLES | SERVICE_PROVIDER_ROLES):
		filters["user"] = user
	apply_branch_filter(filters, "PetCareService", user)

	rows = frappe.get_list(
		"PetCareService",
		filters=filters,
		fields=_fields(
			"PetCareService",
			[
				"name",
				"pet_service_name",
				"pet_id",
				"guardian_id",
				"care_service_id",
				"item_code",
				"price",
				"status",
				"doctor",
				"user",
				"visit",
				"order_id",
				"provider",
				"start_date",
				"end_date",
				"due_date",
				"modified",
				"creation",
			],
		),
		order_by="modified desc",
		limit_page_length=200,
	)
	return [_service_item(row) for row in rows if cstr(row.get("status")) in OPEN_SERVICE_STATUSES]


def _unassigned_service_items(user: str, roles: set[str], search=None) -> list[dict]:
	rows = frappe.get_list(
		"PetCareService",
		filters={"docstatus": ["<", 2], "status": ["in", list(OPEN_SERVICE_STATUSES)]},
		fields=_fields(
			"PetCareService",
			[
				"name",
				"pet_service_name",
				"pet_id",
				"guardian_id",
				"care_service_id",
				"item_code",
				"price",
				"status",
				"doctor",
				"user",
				"visit",
				"order_id",
				"provider",
				"start_date",
				"end_date",
				"due_date",
				"modified",
				"creation",
			],
		),
		order_by="modified desc",
		limit_page_length=200,
	)
	return [_service_item(row) for row in rows if not row.get("provider") and not row.get("user")]


def _procedure_items(user: str, roles: set[str], search=None, scope: str | None = None) -> list[dict]:
	if not frappe.db.exists("DocType", "Pet Procedure"):
		return []

	filters = {"docstatus": ["<", 2], "status": ["in", list(OPEN_PROCEDURE_STATUSES)]}
	apply_branch_filter(filters, "Pet Procedure", user)
	doctor = _doctor_for_user(user)
	if scope == "doctor" and not user_has_full_access(user) and not roles & (COORDINATOR_ROLES | MANAGEMENT_ROLES):
		if doctor:
			filters["doctor"] = doctor
		else:
			return []
	elif scope == "provider" and not user_has_full_access(user) and not roles & (COORDINATOR_ROLES | MANAGEMENT_ROLES):
		filters["provider"] = user

	rows = frappe.get_all(
		"Pet Procedure",
		filters=filters,
		fields=_fields(
			"Pet Procedure",
			[
				"name",
				"visit",
				"order_id",
				"pet",
				"guardian",
				"doctor",
				"provider",
				"procedure_template",
				"care_service",
				"status",
				"scheduled_at",
				"scheduled_datetime",
				"started_at",
				"modified",
				"creation",
			],
		),
		order_by="modified desc",
		limit_page_length=200,
		ignore_permissions=True,
	)
	return [_procedure_item(row) for row in rows]


def _diagnostic_items(doctype: str, user: str, roles: set[str], search=None) -> list[dict]:
	rows = frappe.get_all(
		doctype,
		filters={"docstatus": ["<", 2]},
		fields=_fields(
			doctype,
			[
				"name",
				"visit",
				"order_id",
				"pet",
				"doctor",
				"care_service",
				"item_code",
				"status",
				"modified",
				"creation",
			],
		),
		order_by="modified desc",
		limit_page_length=200,
		ignore_permissions=True,
	)
	return [_diagnostic_item(doctype, row) for row in rows if cstr(row.get("status")) in OPEN_DIAGNOSTIC_STATUSES]


def _invoice_items(user: str, roles: set[str], search=None) -> list[dict]:
	rows = frappe.get_all(
		"Sales Invoice",
		filters={"docstatus": ["<", 2]},
		fields=[
			"name",
			"customer",
			"customer_name",
			"posting_date",
			"due_date",
			"status",
			"grand_total",
			"outstanding_amount",
			"docstatus",
			"modified",
			"creation",
		],
		order_by="modified desc",
		limit_page_length=200,
		ignore_permissions=True,
	)
	return [_invoice_item(row) for row in rows if row.get("docstatus") == 0 or flt(row.get("outstanding_amount")) > 0]


def _visit_item(row) -> dict:
	pet = _pet_payload(row.get("animal_patient"))
	guardian = _guardian_payload(row.get("guardian"))
	doctor = _doctor_payload(row.get("doctor"))
	priority = row.get("priority") or _case_sheet_priority(row.get("case_sheet")) or "Normal"
	return _workspace_item(
		"Vet Visit",
		row.name,
		title=_join_title(pet.get("name_label"), row.name),
		subtitle=guardian.get("name_label"),
		status=row.get("status"),
		priority=priority,
		pet=pet,
		guardian=guardian,
			assignee=doctor,
			scheduled_at=row.get("visit_datetime"),
			due_at=row.get("follow_up_preferred_date") or row.get("follow_up_date"),
			next_task="Complete consult" if row.get("_consult_requested_for_user") else _visit_next_task(row),
			modified=row.get("modified"),
			creation=row.get("creation"),
			extra={
				"case_sheet_id": row.get("case_sheet"),
				"sales_invoice": row.get("sales_invoice"),
				"visit_type": row.get("visit_type"),
				"billing_status": row.get("billing_status"),
				"follow_up_status": row.get("follow_up_status"),
				"follow_up_visit_id": row.get("follow_up_visit_id"),
				"consult_requested_for_user": cint(row.get("_consult_requested_for_user")),
			},
		)


def _case_sheet_item(row) -> dict:
	pet = _pet_payload(row.get("animal_patient"))
	guardian = _guardian_payload(row.get("guardian"))
	return _workspace_item(
		"Vet Case Sheet",
		row.name,
		title=_join_title(pet.get("name_label"), row.get("chief_complaint"), row.name),
		subtitle=guardian.get("name_label"),
		status=row.get("status"),
		priority=row.get("priority") or "Normal",
		pet=pet,
		guardian=guardian,
		scheduled_at=row.get("case_sheet_date"),
		next_task="Convert to visit",
		modified=row.get("modified"),
		creation=row.get("creation"),
	)


def _appointment_item(row) -> dict:
	pet = _pet_payload(row.get("custom_pet"))
	guardian = _guardian_payload(row.get("custom_guardian"))
	is_follow_up = bool(row.get("custom_follow_up_of_visit_id"))
	return _workspace_item(
		"Appointment",
		row.name,
		title=_join_title(row.get("customer_name"), row.get("custom_appointment_type"), row.name),
		subtitle=guardian.get("name_label") or row.get("customer_phone_number"),
		status=row.get("status") or "Open",
		priority="Normal",
		pet=pet,
		guardian=guardian,
		scheduled_at=row.get("scheduled_time"),
		due_at=row.get("scheduled_time"),
		next_task="Convert follow-up to visit" if is_follow_up else "Route appointment",
		modified=row.get("modified"),
		creation=row.get("creation"),
		extra={"follow_up_of_visit_id": row.get("custom_follow_up_of_visit_id")} if is_follow_up else None,
	)


def _service_item(row) -> dict:
	pet = _pet_payload(row.get("pet_id"))
	guardian = _guardian_payload(row.get("guardian_id"))
	doctor = _doctor_payload(row.get("doctor"))
	provider = _doctor_payload(row.get("provider"))
	item = _workspace_item(
		"PetCareService",
		row.name,
		title=_join_title(row.get("pet_service_name"), pet.get("name_label"), row.name),
		subtitle=provider.get("name_label") or doctor.get("name_label"),
		status=row.get("status") or "pending",
		priority="Normal",
		pet=pet,
		guardian=guardian,
		assignee={"id": row.get("provider") or row.get("user") or row.get("doctor"), "name_label": provider.get("name_label") or row.get("user") or doctor.get("name_label")},
		# Was `start_date`, which is the moment the work STARTED - stamped by the server,
		# microseconds and all, on 96.9% of rows. Reporting it as "scheduled" made this
		# column mean something different for a service than for a procedure in the same
		# list. Null until someone actually schedules one, which is the honest answer.
		scheduled_at=row.get("scheduled_datetime"),
		due_at=row.get("due_date"),
		next_task="Finish service" if row.get("start_date") else "Start service",
		modified=row.get("modified"),
		creation=row.get("creation"),
		extra={
			"visit_id": row.get("visit"),
			"order_id": row.get("order_id"),
			"care_service_id": row.get("care_service_id"),
			"item_code": row.get("item_code"),
			"price": row.get("price"),
		},
	)
	item["provider_name"] = provider.get("name_label")
	item["doctor"] = row.get("doctor")
	item["doctor_name"] = doctor.get("name_label")
	item["practitioner_name"] = provider.get("name_label") or doctor.get("name_label")
	item["guardian_id"] = row.get("guardian_id")
	item["guardian_id.full_name"] = guardian.get("full_name")
	return item


def _procedure_item(row) -> dict:
	pet = _pet_payload(row.get("pet"))
	guardian = _guardian_payload(row.get("guardian"))
	doctor = _doctor_payload(row.get("doctor"))
	provider_id = row.get("provider")
	provider = _doctor_payload(provider_id)
	provider_label = provider.get("name_label") or provider_id
	item = _workspace_item(
		"Pet Procedure",
		row.name,
		title=_join_title(_procedure_template_label(row.get("procedure_template")), pet.get("name_label"), row.name),
		subtitle=provider_label or doctor.get("name_label"),
		status=row.get("status") or "Pending",
		priority="Normal",
		pet=pet,
		guardian=guardian,
		assignee={"id": provider_id or row.get("doctor"), "name_label": provider_label or doctor.get("name_label")},
		scheduled_at=row.get("scheduled_datetime") or row.get("scheduled_at"),
		due_at=row.get("scheduled_datetime") or row.get("scheduled_at"),
		next_task=_procedure_next_task(row),
		modified=row.get("modified"),
		creation=row.get("creation"),
		extra={
			"visit_id": row.get("visit"),
			"order_id": row.get("order_id"),
			"procedure_template": row.get("procedure_template"),
			"care_service_id": row.get("care_service"),
		},
	)
	item["doctor"] = row.get("doctor")
	item["doctor_name"] = doctor.get("name_label")
	item["provider"] = provider_id
	item["provider_name"] = provider_label
	return item


def _diagnostic_item(doctype: str, row) -> dict:
	pet = _pet_payload(row.get("pet"))
	doctor = _doctor_payload(row.get("doctor"))
	return _workspace_item(
		doctype,
		row.name,
		title=_join_title(_care_service_label(row.get("care_service")), pet.get("name_label"), row.name),
		subtitle=doctor.get("name_label"),
		status=row.get("status") or "Pending",
		priority="Normal",
		pet=pet,
		assignee=doctor,
		next_task="Release result" if _diagnostic_has_result(doctype, row.name) else "Save result",
		modified=row.get("modified"),
		creation=row.get("creation"),
		extra={"visit_id": row.get("visit"), "order_id": row.get("order_id"), "care_service_id": row.get("care_service")},
	)


def _invoice_item(row) -> dict:
	return _workspace_item(
		"Sales Invoice",
		row.name,
		title=_join_title(row.get("customer_name") or row.get("customer"), row.name),
		subtitle=_("Outstanding {0}").format(flt(row.get("outstanding_amount"))),
		status=row.get("status") or ("Draft" if row.get("docstatus") == 0 else "Open"),
		priority="Urgent" if flt(row.get("outstanding_amount")) > 0 else "Normal",
		due_at=row.get("due_date"),
		scheduled_at=row.get("posting_date"),
		next_task="Collect payment" if flt(row.get("outstanding_amount")) > 0 else "Submit invoice",
		modified=row.get("modified"),
		creation=row.get("creation"),
		extra={"customer": row.get("customer"), "grand_total": row.get("grand_total"), "outstanding_amount": row.get("outstanding_amount")},
	)


def _workspace_item(
	doctype: str,
	name: str,
	*,
	title=None,
	subtitle=None,
	status=None,
	priority=None,
	pet=None,
	guardian=None,
	assignee=None,
	scheduled_at=None,
	due_at=None,
	next_task=None,
	modified=None,
	creation=None,
	extra=None,
) -> dict:
	source_type = SOURCE_LABELS.get(doctype, doctype)
	pet_payload = pet or {}
	guardian_payload = guardian or {}
	assignee_payload = assignee or {}
	item = {
		"id": f"{source_type}:{name}",
		"name": name,
		"source_type": source_type,
		"source_doctype": doctype,
		"title": title or name,
		"subtitle": subtitle,
		"status": status,
		"priority": priority or "Normal",
		"pet": pet_payload,
		"pet_id": pet_payload.get("id"),
		"pet_name": pet_payload.get("pet_name") or pet_payload.get("name_label"),
		"guardian": guardian_payload,
		"guardian_id": guardian_payload.get("id"),
		"guardian_name": guardian_payload.get("full_name") or guardian_payload.get("name_label"),
		"assignee": assignee_payload,
		"doctor": assignee_payload.get("id"),
		"doctor_name": assignee_payload.get("name_label"),
		"scheduled_at": scheduled_at,
		"due_at": due_at,
		"overdue": _is_overdue(due_at, status),
		"next_task": next_task,
		"modified": modified,
		"creation": creation,
		"badges": [value for value in [source_type, priority, "Overdue" if _is_overdue(due_at, status) else None] if value],
	}
	if extra:
		item.update(extra)
	return item


def _visit_aggregate(visit_name: str) -> dict:
	visit = frappe.get_doc("Vet Visit", visit_name)
	_assert_record_access("Vet Visit", visit.name)
	case_sheet = frappe.get_doc("Vet Case Sheet", visit.case_sheet) if visit.case_sheet and frappe.db.exists("Vet Case Sheet", visit.case_sheet) else None
	pet = _pet_payload(visit.animal_patient)
	guardian = _guardian_payload(visit.guardian)
	doctor = _doctor_payload(visit.doctor)
	billing = _billing_snapshot(visit)
	linked_records = _linked_records_for_visit(visit.name)

	return {
		"summary": {
			"name": visit.name,
			"source_type": "Visit",
			"source_doctype": "Vet Visit",
			"title": _join_title(pet.get("name_label"), visit.name),
			"status": visit.status,
			"priority": visit.get("priority") or _value(case_sheet, "priority") or "Normal",
			"case_sheet_id": visit.case_sheet,
			"visit_datetime": visit.visit_datetime,
			"next_task": _visit_next_task(visit),
			"creation": visit.creation,
			"modified": visit.modified,
		},
		"pet": pet,
		"guardian": guardian,
		"assignee": doctor,
		"case_context": get_visit_case_context(visit),
		"clinical": {
			"chief_complaint": _value(case_sheet, "chief_complaint"),
			**visit_clinical_payload(visit),
			"follow_up_required": visit.follow_up_required,
			"follow_up_reason": visit.get("follow_up_reason"),
			"follow_up_preferred_date": visit.get("follow_up_preferred_date") or visit.follow_up_date,
			"follow_up_date": visit.follow_up_date,
			"follow_up_status": visit.get("follow_up_status") or "Not Needed",
			"vitals": {
				"weight": visit.weight,
				"temperature": visit.temperature,
				"heart_rate": visit.heart_rate,
				"respiratory_rate": visit.respiratory_rate,
			},
			"vital_signs": _visit_vital_signs(visit),
		},
		"follow_up": _visit_follow_up(visit),
		"clinical_options": clinical_options_payload(),
		"consult_requests": _visit_consult_requests(visit),
		"diagnoses": _visit_diagnoses(visit),
		"orders": _visit_orders(visit, linked_records=linked_records),
		"addenda": _visit_addenda(visit.name),
		"linked_records": linked_records,
		"notes": _comments_for("Vet Visit", visit.name),
		"attachments": _attachments_for("Vet Visit", visit.name),
		"billing": billing,
		"timeline": _timeline_for_visit(visit, linked_records=linked_records),
		"raw": {"doctype": visit.doctype, "name": visit.name},
	}


def _case_sheet_aggregate(name: str) -> dict:
	case_sheet = frappe.get_doc("Vet Case Sheet", name)
	pet = _pet_payload(case_sheet.animal_patient)
	guardian = _guardian_payload(case_sheet.guardian)
	return {
		"summary": {
			"name": case_sheet.name,
			"source_type": "Case Sheet",
			"source_doctype": "Vet Case Sheet",
			"title": _join_title(pet.get("name_label"), case_sheet.chief_complaint, case_sheet.name),
			"status": case_sheet.status,
			"priority": case_sheet.priority,
			"next_task": "Convert to visit",
			"creation": case_sheet.creation,
			"modified": case_sheet.modified,
		},
		"pet": pet,
		"guardian": guardian,
		"assignee": {},
			"clinical": {
				"chief_complaint": case_sheet.chief_complaint,
				"intake_summary": case_sheet.intake_notes,
				"overview": None,
			"examination": None,
			"assessment": None,
			"plan": None,
				"instructions": None,
			},
			"follow_up": {},
			"consult_requests": [],
			"diagnoses": [],
			"orders": [],
		"linked_records": [],
		"notes": _comments_for("Vet Case Sheet", case_sheet.name),
		"attachments": _attachments_for("Vet Case Sheet", case_sheet.name),
		"billing": {},
		"timeline": _timeline_for_source("Vet Case Sheet", case_sheet.name),
	}


def _source_aggregate(doctype: str, name: str) -> dict:
	return {
		"summary": _source_brief(doctype, name),
		"pet": {},
		"guardian": {},
		"assignee": {},
			"clinical": {},
			"follow_up": {},
			"consult_requests": [],
			"diagnoses": [],
		"orders": [],
		"linked_records": [],
		"notes": _comments_for(doctype, name),
		"attachments": _attachments_for(doctype, name),
		"billing": {},
		"timeline": _timeline_for_source(doctype, name),
	}


def _visit_diagnoses(visit) -> list[dict]:
	if _has_field("Vet Visit", "diagnoses"):
		rows = []
		for row in visit.get("diagnoses") or []:
			rows.append(
				{
					"name": row.name,
					"disease": row.disease,
					"disease_name": _disease_label(row.disease) or row.disease,
					"diagnosis_text": row.diagnosis_text,
					"is_primary": cint(row.is_primary),
					"severity": row.severity,
					"note": row.note,
				}
			)
		if rows:
			return rows
	if visit.diagnosis:
		return [
			{
				"name": None,
				"disease": None,
				"disease_name": None,
				"diagnosis_text": visit.diagnosis,
				"is_primary": 1,
				"severity": None,
				"note": None,
			}
		]
	return []


def _visit_vital_signs(visit) -> list[dict]:
	if not _has_field("Vet Visit", "vital_signs"):
		return []
	return [
		{
			"name": row.name,
			"recorded_at": row.recorded_at,
			"recorded_by": row.recorded_by,
			"temperature": row.temperature,
			"heart_rate": row.heart_rate,
			"respiratory_rate": row.respiratory_rate,
			"weight": row.weight,
			"body_condition_score": row.body_condition_score,
			"hydration_status": row.hydration_status,
			"mucous_membrane": row.mucous_membrane,
			"capillary_refill_time": row.capillary_refill_time,
			"pain_score": row.pain_score,
			"blood_pressure": row.blood_pressure,
			"spo2": row.spo2,
			"notes": row.notes,
		}
		for row in visit.get("vital_signs") or []
	]


def _visit_addenda(visit_name: str) -> list[dict]:
	if not frappe.db.exists("DocType", "Vet Visit Addendum"):
		return []
	rows = frappe.get_all(
		"Vet Visit Addendum",
		filters={"visit": visit_name},
		fields=["name", "addendum_datetime", "authored_by", "doctor", "addendum_type", "note", "reason"],
		order_by="addendum_datetime desc, creation desc",
		ignore_permissions=True,
	)
	addenda = [dict(row) for row in rows]
	enrich_link_aliases(addenda, doctor_field="doctor", include_pet=False, include_guardian=False, include_provider=False)
	return addenda


def _visit_orders(visit, linked_records=None) -> list[dict]:
	rows = []
	if _has_field("Vet Visit", "orders"):
		for row in visit.get("orders") or []:
			rows.append(_visit_order_row(row))
	linked = _synthesized_orders_from_records(visit.name, {row.get("order_id") for row in rows if row.get("order_id")}, linked_records=linked_records)
	rows.extend(linked)
	return rows


def _visit_follow_up(visit) -> dict:
	return {
		"required": cint(visit.get("follow_up_required")),
		"reason": visit.get("follow_up_reason"),
		"preferred_date": visit.get("follow_up_preferred_date") or visit.get("follow_up_date"),
		"date": visit.get("follow_up_date"),
		"appointment_id": visit.get("follow_up_appointment_id"),
		"visit_id": visit.get("follow_up_visit_id"),
		"of_visit_id": visit.get("follow_up_of_visit_id"),
		"status": visit.get("follow_up_status") or "Not Needed",
	}


def _visit_consult_requests(visit) -> list[dict]:
	if not _has_field("Vet Visit", "consult_requests"):
		return []
	rows = []
	for row in visit.get("consult_requests") or []:
		rows.append(
			{
				"name": row.name,
				"requested_doctor": row.requested_doctor,
				"requested_practitioner": row.requested_doctor,
				"requested_doctor_label": _doctor_payload(row.requested_doctor).get("name_label"),
				"requested_by": row.requested_by,
				"reason": row.reason,
				"status": row.status,
				"consult_note": row.consult_note,
				"requested_at": row.requested_at,
				"completed_at": row.completed_at,
			}
		)
	return rows


def _visit_order_row(row) -> dict:
	return {
		"name": row.name,
		"order_id": row.order_id,
		"kind": row.kind,
		"title": row.title,
		"item_code": row.item_code,
		"template_id": row.template_id,
		"status": row.status,
		"priority": row.priority,
		"qty": row.qty,
		"price": row.price,
		"note": row.note,
		"linked_doctype": row.linked_doctype,
		"linked_name": row.linked_name,
	}


def _linked_records_for_visit(visit_name: str) -> list[dict]:
	records = []
	diagnostic_rows = {}
	for doctype in ("Lab", "Imaging"):
		rows = frappe.get_all(
			doctype,
			filters={"visit": visit_name},
			fields=_fields(
				doctype,
				[
					"name",
					"visit",
					"order_id",
					"pet",
					"doctor",
					"care_service",
					"item_code",
					"body_part",
					"modality",
					"status",
					"modified",
					*DIAGNOSTIC_RESULT_FILE_FIELDS.get(doctype, ()),
					*DIAGNOSTIC_TEXT_FIELDS.get(doctype, ()),
				],
			),
			order_by="modified desc",
			ignore_permissions=True,
		)
		diagnostic_rows[doctype] = rows

	result_files = _diagnostic_result_files(diagnostic_rows)
	for doctype in ("Lab", "Imaging"):
		for row in diagnostic_rows.get(doctype) or []:
			record = {
				"source_type": SOURCE_LABELS[doctype],
				"source_doctype": doctype,
				"name": row.name,
				"status": row.get("status"),
				"order_id": row.get("order_id"),
				"title": _care_service_label(row.get("care_service")) or row.name,
				"body_part": row.get("body_part"),
				"modality": row.get("modality"),
				"modified": row.get("modified"),
				"result_files": result_files.get(doctype, {}).get(row.name, []),
			}
			# Mirrors guardian_portal._released_diagnostic_rows: diagnostic text is
			# withheld until the record is Released. That endpoint can filter in the
			# query because it only ever returns released rows; this payload must still
			# list unreleased records (the visit page renders their status), so the
			# filter is applied per record at emission instead.
			#
			# When not released the key is OMITTED rather than emitted empty, so the
			# frontend can tell "released, no text recorded" (key present, may be null)
			# apart from "not released yet" (key absent).
			if cstr(row.get("status")) == DIAGNOSTIC_RELEASED_STATUS:
				for fieldname in _fields(doctype, list(DIAGNOSTIC_TEXT_FIELDS.get(doctype, ()))):
					record[fieldname] = row.get(fieldname)
			records.append(record)

	rows = frappe.get_all(
		"PetCareService",
		filters={"visit": visit_name},
		fields=_fields("PetCareService", ["name", "pet_service_name", "status", "order_id", "provider", "start_date", "end_date", "modified"]),
		order_by="modified desc",
		ignore_permissions=True,
	)
	for row in rows:
		records.append(
			{
				"source_type": "Service",
				"source_doctype": "PetCareService",
				"name": row.name,
				"status": row.get("status"),
				"order_id": row.get("order_id"),
				"title": row.get("pet_service_name") or row.name,
				"provider": row.get("provider"),
				"provider_name": _doctor_payload(row.get("provider")).get("name_label"),
				"start_date": row.get("start_date"),
				"end_date": row.get("end_date"),
				"modified": row.get("modified"),
			}
		)

	if frappe.db.exists("DocType", "Pet Procedure"):
		rows = frappe.get_all(
			"Pet Procedure",
			filters={"visit": visit_name},
			fields=_fields(
				"Pet Procedure",
				[
					"name",
					"procedure_template",
					"care_service",
					"status",
					"order_id",
					"provider",
					"scheduled_at",
					"started_at",
					"completed_at",
					"closed_at",
					"modified",
				],
			),
			order_by="modified desc",
			ignore_permissions=True,
		)
		for row in rows:
			result_files = []
			seen_files = set()
			for file_row in _attachments_for("Pet Procedure", row.name):
				_append_result_file(result_files, seen_files, file_row)
			records.append(
				{
					"source_type": "Procedure",
					"source_doctype": "Pet Procedure",
					"name": row.name,
					"status": row.get("status"),
					"order_id": row.get("order_id"),
					"title": _procedure_template_label(row.get("procedure_template")) or _care_service_label(row.get("care_service")) or row.name,
					"provider": row.get("provider"),
					"provider_name": _doctor_payload(row.get("provider")).get("name_label") or row.get("provider"),
					"procedure_template": row.get("procedure_template"),
					"care_service_id": row.get("care_service"),
					"scheduled_at": row.get("scheduled_at"),
					"started_at": row.get("started_at"),
					"completed_at": row.get("completed_at"),
					"closed_at": row.get("closed_at"),
					"modified": row.get("modified"),
					"result_files": result_files,
				}
			)
	return records


def _diagnostic_result_files(diagnostic_rows: dict[str, list]) -> dict[str, dict[str, list[dict]]]:
	files_by_doctype = {}
	seen_by_doctype = {}
	for doctype, rows in diagnostic_rows.items():
		names = [row.name for row in rows if row.get("name")]
		files_by_doctype[doctype] = {name: [] for name in names}
		seen_by_doctype[doctype] = {name: set() for name in names}
		if not names:
			continue

		file_rows = frappe.get_all(
			"File",
			filters={"attached_to_doctype": doctype, "attached_to_name": ["in", names]},
			fields=["name", "file_url", "file_name", "attached_to_name"],
			order_by="creation asc",
			ignore_permissions=True,
		)
		for file_row in file_rows:
			attached_to_name = file_row.get("attached_to_name")
			if attached_to_name in files_by_doctype[doctype]:
				_append_result_file(files_by_doctype[doctype][attached_to_name], seen_by_doctype[doctype][attached_to_name], file_row)

		for row in rows:
			for fieldname in _fields(doctype, list(DIAGNOSTIC_RESULT_FILE_FIELDS.get(doctype, ()))):
				_append_result_file(files_by_doctype[doctype][row.name], seen_by_doctype[doctype][row.name], {"file_url": row.get(fieldname)})
	return files_by_doctype


def _append_result_file(target: list[dict], seen_urls: set[str], source) -> None:
	file_url = cstr(source.get("file_url")).strip()
	if not file_url or file_url in seen_urls:
		return
	seen_urls.add(file_url)
	file_name = cstr(source.get("file_name")).strip() or _file_name_from_url(file_url)
	target.append(
		{
			"name": source.get("name") or file_url,
			"file_url": file_url,
			"file_name": file_name,
			"is_image": _is_image_file(file_url),
		}
	)


def _file_name_from_url(file_url: str) -> str:
	return cstr(file_url).split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]


def _is_image_file(file_url: str) -> bool:
	path = cstr(file_url).split("?", 1)[0].lower()
	return any(path.endswith(extension) for extension in IMAGE_FILE_EXTENSIONS)


def _synthesized_orders_from_records(visit_name: str, known_order_ids: set[str], linked_records=None) -> list[dict]:
	orders = []
	for record in (linked_records if linked_records is not None else _linked_records_for_visit(visit_name)):
		order_id = record.get("order_id") or f"{record.get('source_doctype')}::{record.get('name')}"
		if order_id in known_order_ids:
			continue
		orders.append(
			{
				"name": None,
				"order_id": order_id,
				"kind": "radiology" if record.get("source_doctype") == "Imaging" else record.get("source_type", "").lower(),
				"title": record.get("title"),
				"status": _normalize_order_status(record.get("status")),
				"linked_doctype": record.get("source_doctype"),
				"linked_name": record.get("name"),
			}
		)
	return orders


def _billing_snapshot(visit) -> dict:
	"""What the front desk sees. Must not report billed work as outstanding.

	This used to read `Vet Visit.sales_invoice` alone: no visit invoice meant total =
	total_billable_amount and balance = total, i.e. everything owed. Orders now bill at
	their own completion onto their own branch's invoice and never write back to that
	field, so on a fully per-order-billed visit the old reading showed paid work as
	entirely unpaid - to the person taking the money.

	Settlement is resolved row by row against the invoice each row actually reached, never
	by summing invoice totals: invoice reuse means one invoice can carry several visits'
	charges, so its grand_total is not this visit's.
	"""
	summary = summarise_visit_billing(visit)

	active_billables = [row for row in visit.billable_items or [] if not _is_cancelled_billable(row)]
	cancelled_billables = [row for row in visit.billable_items or [] if _is_cancelled_billable(row)]
	return {
		"billing_status": visit_billing_status(visit, summary=summary),
		# The visit's own invoice, still a single Link and still only ever the visit's own
		# branch's invoice. `invoices` below is the full picture.
		"sales_invoice": visit.sales_invoice,
		"total": summary.total,
		"paid": summary.settled,
		"balance": summary.outstanding,
		"billed_amount": summary.billed,
		"unbilled_amount": summary.unbilled,
		"currency": summary.currency,
		"billed": cint(visit.billed),
		# Every invoice this visit's charges reached, the visit's own included. Amounts are
		# the invoice's own, not this visit's share - a merged invoice has no such share.
		"invoices": [
			{
				"sales_invoice": inv.get("name"),
				"branch": inv.get("branch"),
				"docstatus": cint(inv.get("docstatus")),
				"status": inv.get("status"),
				"grand_total": flt(inv.get("grand_total")),
				"outstanding_amount": flt(inv.get("outstanding_amount")),
			}
			for inv in summary.invoices
		],
		"billable_items": [_billable_item(row) for row in active_billables],
		"cancelled_billable_items": [_billable_item(row) for row in cancelled_billables],
	}


def _start_consultation(visit_name: str):
	visit = frappe.get_doc("Vet Visit", visit_name)
	clinical_state.assert_action_allowed(visit, "start_consultation")
	if visit.status != "In Progress":
		clinical_state.transition_status(visit, "In Progress", action="start_consultation")
	if _has_field("Vet Visit", "priority") and not visit.priority:
		visit.priority = _case_sheet_priority(visit.case_sheet) or "Normal"
	visit.save(ignore_permissions=True)
	update_profile_for_visit(visit, clinical_status="In Consultation", episode_status="Under Diagnosis")
	_sync_queue_ticket_for_visit(visit, status="In Consultation", timestamp_field="started_at")
	visit.add_comment("Comment", _("Consultation started by {0}.").format(frappe.session.user))


def _set_case_choice(visit_name: str, payload: dict):
	visit = frappe.get_doc("Vet Visit", visit_name)
	clinical_state.assert_action_allowed(visit, "set_case_choice")
	choice = payload.get("doctor_case_choice") or payload.get("case_choice") or payload.get("choice")
	episode = payload.get("care_episode") or payload.get("episode") or payload.get("episode_name")
	note = payload.get("case_choice_note") if "case_choice_note" in payload else payload.get("note")
	# Doctor-authored name for a case being opened. Only consumed on the new_case
	# paths that actually create/reopen an episode - see set_visit_case_choice.
	case_title = payload.get("case_title") or payload.get("episode_title")
	set_visit_case_choice(visit, choice, episode=episode, note=note, case_title=case_title)


def _save_clinical_note(visit_name: str, payload: dict):
	visit = frappe.get_doc("Vet Visit", visit_name)
	clinical_state.assert_action_allowed(visit, "save_clinical_note")
	field_map = {
		"chief_complaint": None,
		"intake_summary": "intake_summary",
		"overview": "overview",
		"examination": "examination_notes",
		"examination_notes": "examination_notes",
		"diagnosis": "diagnosis",
		"plan": "treatment_plan",
		"treatment_plan": "treatment_plan",
		"assessment_note": "assessment_note",
		"doctor_note": "doctor_note",
		"owner_instruction_note": "owner_instruction_note",
		"follow_up_required": "follow_up_required",
		"follow_up_reason": "follow_up_reason",
		"follow_up_preferred_date": "follow_up_preferred_date",
		"follow_up_date": "follow_up_date",
		"follow_up_status": "follow_up_status",
		"weight": "weight",
		"temperature": "temperature",
		"heart_rate": "heart_rate",
		"respiratory_rate": "respiratory_rate",
	}
	apply_structured_clinical_payload(visit, payload)
	for incoming, fieldname in field_map.items():
		if fieldname and incoming in payload and _has_field("Vet Visit", fieldname):
			visit.set(fieldname, payload.get(incoming))
	if payload.get("status"):
		visit.status = payload.get("status")
	visit.save(ignore_permissions=True)
	update_profile_for_visit(visit)
	if visit.get("treatment_plan") or visit.get("prescribed_medications"):
		sync_treatment_from_visit(visit)


def _save_diagnoses(visit_name: str, payload: dict):
	visit = frappe.get_doc("Vet Visit", visit_name)
	clinical_state.assert_action_allowed(visit, "save_diagnoses")
	rows = _coerce_list(payload.get("diagnoses") if "diagnoses" in payload else payload.get("rows"))
	if _has_field("Vet Visit", "diagnoses"):
		visit.set("diagnoses", [])
		# One lookup per save. Disambiguates same-named Disease records once the
		# catalogue holds one per species; harmless while names are still unique.
		patient_species = _patient_species(visit)
		for row in rows:
			row = _coerce_dict(row)
			row_species = cstr(row.get("species")).strip() or patient_species
			disease = cstr(row.get("disease")).strip()
			if disease and not frappe.db.exists("Disease", disease):
				# Not a primary key, so the client sent a human-readable name.
				# Resolve it by field rather than discarding it.
				disease = _resolve_disease_key(disease, row_species)
			disease = disease or _ensure_disease(row, default_species=patient_species)
			visit.append(
				"diagnoses",
				{
					"disease": disease,
					"diagnosis_text": row.get("diagnosis_text") or row.get("text") or row.get("note"),
					"is_primary": cint(row.get("is_primary")),
					"severity": row.get("severity"),
					"note": row.get("note"),
				},
			)
	primary_text = _primary_diagnosis_text(rows)
	if primary_text:
		visit.diagnosis = primary_text
	elif isinstance(payload.get("assessment"), str) and payload.get("assessment"):
		visit.diagnosis = payload.get("assessment")
	visit.save(ignore_permissions=True)
	sync_diagnoses_from_visit(visit)


def _create_orders(visit_name: str, payload: dict):
	savepoint = f"create_orders_{frappe.generate_hash(length=10)}"
	frappe.db.savepoint(savepoint)
	try:
		_create_orders_atomic(visit_name, payload)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise
	else:
		frappe.db.release_savepoint(savepoint)


def _create_orders_atomic(visit_name: str, payload: dict):
	visit = frappe.get_doc("Vet Visit", visit_name)
	clinical_state.assert_action_allowed(visit, "create_orders")
	if not _has_field("Vet Visit", "orders"):
		frappe.throw(_("Vet Visit is missing the Orders child table. Run migrations first."))
	incoming = _coerce_list(payload.get("orders") if "orders" in payload else payload.get("rows"))
	if not incoming:
		frappe.throw(_("At least one order is required."))

	created_rows = []
	for raw in incoming:
		row = _coerce_dict(raw)
		kind = _normalize_order_kind(row.get("kind") or row.get("type"))
		template_id = row.get("template_id") or row.get("care_service") or row.get("care_service_id")
		if kind == "procedure":
			template_id = row.get("care_service") or row.get("care_service_id")
		order_id = cstr(row.get("order_id")).strip() or _deterministic_order_id(visit, row, kind, template_id)
		order_row = _find_order_row(visit, order_id)
		if not order_row:
			order_row = visit.append("orders", {"order_id": order_id, "kind": kind, "status": "Draft"})
		_update_order_row_from_payload(visit, order_row, row, kind, template_id)
		created_rows.append((order_row, row))

	visit.save(ignore_permissions=True)

	linked_updates = []
	for order_row, raw in created_rows:
		linked = _create_linked_record_for_order(visit, order_row, raw)
		target_status = "Ordered" if order_row.status in {"", "Draft"} else order_row.status
		if linked:
			linked_updates.append((order_row.order_id, linked.doctype, linked.name, target_status))
		else:
			linked_updates.append((order_row.order_id, None, None, target_status))

	visit = frappe.get_doc("Vet Visit", visit.name)
	changed = False
	for order_id, linked_doctype, linked_name, status in linked_updates:
		for row in visit.get("orders") or []:
			if row.order_id != order_id:
				continue
			if row.status != status:
				row.status = status
				changed = True
			if row.linked_doctype != linked_doctype:
				row.linked_doctype = linked_doctype
				changed = True
			if row.linked_name != linked_name:
				row.linked_name = linked_name
				changed = True
	if changed:
		visit.save(ignore_permissions=True)
	sync_pending_orders_from_visit(visit)
	visit.add_comment("Comment", _("Orders created by {0}.").format(frappe.session.user))


def _complete_case(visit_name: str, payload: dict):
	savepoint = f"complete_visit_{frappe.generate_hash(length=10)}"
	frappe.db.savepoint(savepoint)
	try:
		_complete_case_atomic(visit_name, payload)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise
	else:
		frappe.db.release_savepoint(savepoint)


def _complete_case_atomic(visit_name: str, payload: dict):
	visit = frappe.get_doc("Vet Visit", visit_name)
	_assert_no_active_visit_boarding(visit)
	if cstr(visit.get("status")).strip() == "Completed":
		return
	if payload:
		clinical_payload_keys = (
			"overview",
			"examination",
			"diagnosis",
			"assessment",
			"assessment_findings",
			"assessment_note",
			"plan",
			"treatment_plan",
			"instructions",
			"owner_instruction_items",
			"owner_instruction_note",
			"doctor_notes",
			"client_observations",
			"doctor_note",
		)
		if any(key in payload for key in clinical_payload_keys):
			_save_clinical_note(visit_name, payload)
		if payload.get("diagnoses"):
			_save_diagnoses(visit_name, payload)
	visit = frappe.get_doc("Vet Visit", visit_name)
	clinical_state.assert_action_allowed(visit, "complete_case")
	_assert_no_active_visit_boarding(visit)
	if not visit.diagnosis:
		diagnosis_text = _primary_diagnosis_text([row.as_dict() for row in visit.get("diagnoses") or []])
		if diagnosis_text:
			visit.diagnosis = diagnosis_text
	_validate_visit_completion_requirements(visit)

	invoice_items, total_billable_amount = get_billable_invoice_items(visit, allow_non_invoiceable=True)
	if invoice_items and flt(total_billable_amount) > 0:
		invoice_result = _create_sales_invoice_for_visit(visit.name)
		sales_invoice = invoice_result.get("sales_invoice")
		if not sales_invoice or not sales_invoice.get("name"):
			frappe.throw(_("Draft Sales Invoice could not be created for this visit."))
		completed_visit = invoice_result["visit"]
		for fieldname in ("diagnosis", "doctor_note"):
			if visit.get(fieldname) and not completed_visit.get(fieldname):
				completed_visit.set(fieldname, visit.get(fieldname))
		clinical_state.transition_status(completed_visit, "Completed", action="complete_case")
		_mark_visit_invoiced(completed_visit, invoice_result["sales_invoice"], invoice_result["total_billable_amount"])
	else:
		completed_visit = visit
		clinical_state.transition_status(completed_visit, "Completed", action="complete_case")
		completed_visit.sales_invoice = None
		completed_visit.billed = 0
		# Nothing was left to invoice, which now usually means every order billed at its
		# own completion rather than that the visit was worth nothing. flt(total_billable_
		# amount) is 0 here by construction; the field must still read the visit's clinical
		# value or a fully per-order-billed visit reports as free work.
		completed_visit.total_billable_amount = apply_billable_item_amounts(completed_visit)
		if completed_visit.meta.has_field("billing_status"):
			# "Unbilled" is only true when nothing was raised at all. A visit whose orders
			# each billed on completion is billed, and possibly already paid.
			completed_visit.billing_status = visit_billing_status(completed_visit)
		completed_visit.save(ignore_permissions=True)
	sync_completed_visit(completed_visit, outcome=payload.get("outcome") if payload else None)
	_sync_queue_ticket_for_visit(completed_visit, status="Completed", timestamp_field="completed_at")
	completed_visit.add_comment("Comment", _("Case completed by {0}.").format(frappe.session.user))


def _assert_visit_not_checked_in_boarding(visit_name: str, *, action: str | None = None):
	# A visit that is ALREADY Completed may legitimately own a checked-in stay, because
	# boarding can now start from a closed visit. This guard exists to stop an open visit
	# being closed mid-stay; a closed one has nothing left to close, and complete_case on
	# it is a no-op. Checked here rather than in _complete_case_atomic so both layers - the
	# perform_action gate and the direct call - agree.
	if cstr(frappe.db.get_value("Vet Visit", visit_name, "status")).strip() == "Completed":
		return
	boarding = checked_in_boarding_for_visit(visit_name)
	if not boarding:
		return
	verb = _("complete") if action == "complete_case" else _("edit")
	frappe.throw(
		_("Cannot {0} visit {1} while Pet Boarding {2} is Checked In.").format(
			verb, frappe.bold(visit_name), frappe.bold(boarding.name)
		)
	)


def _assert_no_active_visit_boarding(visit):
	_assert_visit_not_checked_in_boarding(visit.name, action="complete_case")


def _validate_visit_completion_requirements(visit):
	if not visit.diagnosis:
		frappe.throw(_("Diagnosis is required before completing the visit."))
	if not has_internal_clinical_note(visit):
		frappe.throw(_("Clinical Note is required before completing the visit."))
	if STRICT_MODE:
		visit._validate_no_pending_clinical_records()


def _cancel_medication(visit_name: str, payload: dict):
	visit = frappe.get_doc("Vet Visit", visit_name)
	clinical_state.assert_action_allowed(visit, "cancel_medication")
	row_id = cstr(
		payload.get("row_name")
		or payload.get("medication_row")
		or payload.get("medication_row_name")
		or payload.get("name")
		or payload.get("idx")
	).strip()
	if not row_id:
		frappe.throw(_("Medication row is required."))
	row = _find_medication_row(visit, row_id)
	if not row:
		frappe.throw(_("Medication row was not found."))
	if flt(row.get("dispensed_qty")) > 0:
		frappe.throw(_("Medication must be returned or reversed before cancellation."))

	row.dispense_status = "Cancelled"
	visit.save(ignore_permissions=True)
	reason = cstr(payload.get("reason") or payload.get("note")).strip()
	comment = _("Medication row {0} cancelled by {1}.").format(row.idx, frappe.session.user)
	if reason:
		comment = _("{0} Reason: {1}").format(comment, reason)
	visit.add_comment("Comment", comment)


def _request_follow_up(visit_name: str, payload: dict) -> str:
	if not _has_field("Vet Visit", "follow_up_appointment_id"):
		frappe.throw(_("Vet Visit follow-up fields are missing. Run migrations first."))
	if not _has_field("Appointment", "custom_follow_up_of_visit_id"):
		frappe.throw(_("Appointment follow-up link field is missing. Run migrations first."))

	visit = frappe.get_doc("Vet Visit", visit_name)
	clinical_state.assert_action_allowed(visit, "request_follow_up")
	preferred_date = (
		payload.get("follow_up_preferred_date")
		or payload.get("preferred_date")
		or payload.get("follow_up_date")
		or visit.get("follow_up_preferred_date")
		or visit.get("follow_up_date")
	)
	scheduled_time = payload.get("scheduled_time")
	if scheduled_time and not preferred_date:
		preferred_date = getdate(scheduled_time)
	if not preferred_date:
		frappe.throw(_("Follow-up preferred date is required."))
	preferred_date = getdate(preferred_date)
	scheduled_time = scheduled_time or f"{preferred_date} 09:00:00"

	appointment_name = _existing_follow_up_appointment(visit)
	if appointment_name:
		appointment = frappe.get_doc("Appointment", appointment_name)
		if not appointment.get("custom_linked_visit_id"):
			appointment.scheduled_time = scheduled_time
			appointment.customer_details = payload.get("reason") or visit.get("follow_up_reason") or appointment.get("customer_details")
			if _has_field("Appointment", "custom_appointment_type") and not appointment.get("custom_appointment_type"):
				appointment.custom_appointment_type = "follow_up"
			if _has_field("Appointment", "custom_follow_up_of_visit_id") and not appointment.get("custom_follow_up_of_visit_id"):
				appointment.custom_follow_up_of_visit_id = visit.name
			appointment.save(ignore_permissions=True)
	else:
		contact = _appointment_contact_for_visit(visit)
		appointment_data = {
			"doctype": "Appointment",
			"status": "Open",
			"scheduled_time": scheduled_time,
			"customer_name": contact["name"],
			"customer_phone_number": contact.get("phone"),
			"customer_email": contact["email"],
			"customer_details": payload.get("reason") or visit.get("follow_up_reason") or _("Follow-up for Vet Visit {0}").format(visit.name),
		}
		if _has_field("Appointment", "custom_appointment_type"):
			appointment_data["custom_appointment_type"] = "follow_up"
		if _has_field("Appointment", "custom_pet"):
			appointment_data["custom_pet"] = visit.animal_patient
		if _has_field("Appointment", "custom_guardian"):
			appointment_data["custom_guardian"] = visit.guardian
		if _has_field("Appointment", "custom_customer"):
			appointment_data["custom_customer"] = visit.customer
		if _has_field("Appointment", "custom_follow_up_of_visit_id"):
			appointment_data["custom_follow_up_of_visit_id"] = visit.name
		appointment = frappe.get_doc(appointment_data)
		appointment.insert(ignore_permissions=True)
		appointment_name = appointment.name
		appointment.add_comment("Comment", _("Follow-up requested from Vet Visit {0}.").format(visit.name))

	visit.follow_up_required = 1
	visit.follow_up_date = preferred_date
	visit.follow_up_appointment_id = appointment_name
	if _has_field("Vet Visit", "follow_up_reason"):
		visit.follow_up_reason = payload.get("reason") or visit.get("follow_up_reason")
	if _has_field("Vet Visit", "follow_up_preferred_date"):
		visit.follow_up_preferred_date = preferred_date
	if _has_field("Vet Visit", "follow_up_status"):
		linked_visit = appointment.get("custom_linked_visit_id") if appointment_name else None
		visit.follow_up_status = "Seen" if linked_visit else "Scheduled"
	visit.flags.ignore_billing_lock = True
	visit.save(ignore_permissions=True)
	sync_follow_up_from_visit(visit)
	visit.add_comment("Comment", _("Follow-up appointment {0} requested by {1}.").format(appointment_name, frappe.session.user))
	return appointment_name


def _convert_follow_up_to_visit(doctype: str, name: str, payload: dict) -> str:
	if not _has_field("Vet Visit", "follow_up_of_visit_id"):
		frappe.throw(_("Vet Visit follow-up link fields are missing. Run migrations first."))
	if not _has_field("Appointment", "custom_follow_up_of_visit_id"):
		frappe.throw(_("Appointment follow-up link field is missing. Run migrations first."))

	appointment = None
	if doctype == "Appointment":
		appointment = frappe.get_doc("Appointment", name)
		original_visit_name = appointment.get("custom_follow_up_of_visit_id") or payload.get("follow_up_of_visit_id") or payload.get("original_visit")
	elif doctype == "Vet Visit":
		original_visit_name = name
		appointment_name = payload.get("appointment") or payload.get("appointment_id")
		if appointment_name:
			appointment = frappe.get_doc("Appointment", appointment_name)
		else:
			appointment_name = frappe.db.get_value("Vet Visit", original_visit_name, "follow_up_appointment_id")
			appointment = frappe.get_doc("Appointment", appointment_name) if appointment_name and frappe.db.exists("Appointment", appointment_name) else None
			if not appointment:
				appointment_name = _existing_follow_up_appointment(frappe.get_doc("Vet Visit", original_visit_name))
				appointment = frappe.get_doc("Appointment", appointment_name) if appointment_name else None
	else:
		frappe.throw(_("convert_follow_up_to_visit requires an Appointment or Vet Visit."))

	if not original_visit_name or not frappe.db.exists("Vet Visit", original_visit_name):
		frappe.throw(_("Original follow-up visit is required."))
	original_visit = frappe.get_doc("Vet Visit", original_visit_name)

	if appointment and appointment.get("custom_linked_visit_id"):
		_update_original_follow_up_seen(original_visit, appointment.get("custom_linked_visit_id"), appointment.name)
		return appointment.get("custom_linked_visit_id")
	if original_visit.get("follow_up_visit_id") and frappe.db.exists("Vet Visit", original_visit.follow_up_visit_id):
		if appointment:
			_stamp_appointment_conversion(appointment.name, linked_visit_id=original_visit.follow_up_visit_id, target="Visit")
		return original_visit.follow_up_visit_id

	existing_visit = frappe.db.get_value("Vet Visit", {"follow_up_of_visit_id": original_visit.name}, "name")
	if existing_visit:
		if appointment:
			_stamp_appointment_conversion(appointment.name, linked_visit_id=existing_visit, target="Visit")
		_update_original_follow_up_seen(original_visit, existing_visit, appointment.name if appointment else None)
		return existing_visit

	if not appointment:
		frappe.throw(_("A follow-up Appointment is required before conversion."))

	doctor = payload.get("practitioner") or payload.get("doctor") or original_visit.doctor or _doctor_for_user(frappe.session.user)
	if not doctor:
		frappe.throw(_("Healthcare Practitioner is required to convert follow-up to visit."))

	case_sheet_data = {
		"doctype": "Vet Case Sheet",
		"status": "Waiting Practitioner",
		"priority": payload.get("priority") or original_visit.get("priority") or "Normal",
		"guardian": original_visit.guardian,
		"customer": original_visit.customer,
		"animal_patient": original_visit.animal_patient,
		"chief_complaint": payload.get("chief_complaint") or _("Follow-up"),
		"intake_notes": payload.get("intake_notes") or _follow_up_intake_note(original_visit, appointment),
		"weight": original_visit.weight,
	}
	if appointment and _has_field("Vet Case Sheet", "appointment"):
		case_sheet_data["appointment"] = appointment.name
	case_sheet = frappe.get_doc(case_sheet_data)
	case_sheet.insert(ignore_permissions=True)

	visit_data = {
		"doctype": "Vet Visit",
		"case_sheet": case_sheet.name,
		"customer": original_visit.customer or get_or_create_customer_from_guardian(original_visit.guardian),
		"guardian": original_visit.guardian,
		"animal_patient": original_visit.animal_patient,
		"doctor": doctor,
		"priority": payload.get("priority") or original_visit.get("priority") or "Normal",
		"status": "In Progress",
		"visit_type": "Follow-up",
		"case_summary": case_sheet.get("intake_notes"),
		"weight": original_visit.weight,
		"follow_up_of_visit_id": original_visit.name,
	}
	if appointment and _has_field("Vet Visit", "appointment"):
		visit_data["appointment"] = appointment.name
	if original_visit.get("care_episode") and _has_field("Vet Visit", "care_episode"):
		visit_data["care_episode"] = original_visit.get("care_episode")
	follow_up_visit = frappe.get_doc(visit_data)
	follow_up_visit.insert(ignore_permissions=True)
	if original_visit.get("care_episode"):
		set_visit_case_choice(
			follow_up_visit,
			"continue_case",
			episode=original_visit.get("care_episode"),
			note=payload.get("case_choice_note") or _("Follow-up converted from visit {0}.").format(original_visit.name),
		)
		update_profile_for_visit(follow_up_visit, clinical_status="In Consultation", episode_status="Under Diagnosis")
	else:
		update_profile_for_visit(follow_up_visit, clinical_status="In Consultation")
	frappe.db.set_value("Vet Case Sheet", case_sheet.name, {"vet_visit": follow_up_visit.name, "status": "In Consultation"}, update_modified=False)
	_sync_queue_ticket_for_visit(follow_up_visit, status="Waiting")

	_stamp_appointment_conversion(appointment.name, linked_visit_id=follow_up_visit.name, target="Visit")
	_update_original_follow_up_seen(original_visit, follow_up_visit.name, appointment.name)
	follow_up_visit.add_comment("Comment", _("Follow-up visit converted from Appointment {0}.").format(appointment.name))
	return follow_up_visit.name


def _request_consult(visit_name: str, payload: dict):
	if not _has_field("Vet Visit", "consult_requests"):
		frappe.throw(_("Vet Visit consult request table is missing. Run migrations first."))
	requested_doctor = cstr(
		payload.get("requested_practitioner")
		or payload.get("practitioner")
		or payload.get("requested_doctor")
		or payload.get("doctor")
	).strip()
	if not requested_doctor or not practitioner_exists(requested_doctor):
		frappe.throw(_("Requested healthcare practitioner is required."))
	visit = frappe.get_doc("Vet Visit", visit_name)
	clinical_state.assert_action_allowed(visit, "request_consult")
	for row in visit.get("consult_requests") or []:
		if row.requested_doctor == requested_doctor and row.status in OPEN_CONSULT_STATUSES:
			frappe.throw(_("An open consult request already exists for practitioner {0}.").format(requested_doctor))
	visit.append(
		"consult_requests",
		{
			"requested_doctor": requested_doctor,
			"requested_by": frappe.session.user,
			"reason": payload.get("reason"),
			"status": "Requested",
			"requested_at": now_datetime(),
		},
	)
	visit.flags.ignore_billing_lock = True
	visit.save(ignore_permissions=True)
	visit.add_comment("Comment", _("Consult requested from {0} by {1}.").format(requested_doctor, frappe.session.user))


def _complete_consult(visit_name: str, payload: dict):
	if not _has_field("Vet Visit", "consult_requests"):
		frappe.throw(_("Vet Visit consult request table is missing. Run migrations first."))
	consult_note = cstr(payload.get("consult_note") or payload.get("note")).strip()
	if not consult_note:
		frappe.throw(_("Consult note is required."))
	visit = frappe.get_doc("Vet Visit", visit_name)
	clinical_state.assert_action_allowed(visit, "complete_consult")
	row = _find_consult_request_row(visit, payload)
	if not row:
		frappe.throw(_("Open consult request was not found."))
	if row.status not in OPEN_CONSULT_STATUSES:
		frappe.throw(_("Only Requested or Accepted consults can be completed."))
	if not _can_complete_consult(row):
		frappe.throw(_("Not permitted to complete this consult."), frappe.PermissionError)
	row.status = "Completed"
	row.consult_note = consult_note
	row.completed_at = now_datetime()
	visit.flags.ignore_billing_lock = True
	visit.save(ignore_permissions=True)
	visit.add_comment("Comment", _("Consult completed by {0}.").format(frappe.session.user))


def _order_scheduled_datetime(raw: dict):
	"""When a visit order is due to happen, or None. One name across both order routes.

	`scheduled_datetime` is the name; `scheduled_at` is accepted as a deprecated alias so
	the client that sends it today keeps working through the transition. Procedures had a
	Datetime and services a Date, for no reason anyone recorded - the split was accidental,
	and it is how a procedure ended up able to carry a time and a lab not.

	Not to be confused with `due_date`, which stays. That is the DAY a service is owed: it
	is required, it defaults to today on every one of the four writers, and 98.9% of live
	rows hold exactly their creation date. It cannot answer "when is this booked" because
	it is populated whether or not anyone decided anything.

	Delegates the parse to the boarding module so both routes agree on what counts as a
	schedule, and in particular so neither ever falls back to now().
	"""
	from pet_app.api.healthcare.boarding import _coerce_scheduled_datetime

	value = raw.get("scheduled_datetime")
	if value is None or not cstr(value).strip():
		value = raw.get("scheduled_at")
	return _coerce_scheduled_datetime(value)


def _create_linked_record_for_order(visit, order_row, raw: dict):
	kind = _normalize_order_kind(order_row.kind)
	if kind not in {"lab", "radiology", "service", "procedure"}:
		return None

	existing_linked = _existing_linked_record_for_order(kind, visit.name, order_row.order_id, order_row.linked_doctype, order_row.linked_name)
	if existing_linked:
		return existing_linked

	template_id = order_row.template_id or raw.get("template_id") or raw.get("care_service") or raw.get("care_service_id")
	if kind != "procedure" and not template_id:
		frappe.throw(_("Order {0} requires a Care Service template.").format(order_row.title))

	scheduled_datetime = _order_scheduled_datetime(raw)

	if kind == "lab":
		doc = frappe.get_doc(
			{
				"doctype": "Lab",
				"visit": visit.name,
				"order_id": order_row.order_id,
				"pet": visit.animal_patient,
				"doctor": visit.doctor,
				"care_service": template_id,
				"status": "Ordered",
				"scheduled_datetime": scheduled_datetime,
			}
		)
	elif kind == "radiology":
		template = frappe.get_cached_doc("CareService template", template_id)
		body_part = cstr(raw.get("body_part") or raw.get("bodyPart") or order_row.get("body_part") or template.get("body_part")).strip()
		modality = cstr(raw.get("modality") or order_row.get("modality") or template.get("modality")).strip()
		doc = frappe.get_doc(
			{
				"doctype": "Imaging",
				"visit": visit.name,
				"order_id": order_row.order_id,
				"pet": visit.animal_patient,
				"doctor": visit.doctor,
				"care_service": template_id,
				"status": "Ordered",
				"priority": order_row.priority,
				"body_part": body_part or None,
				"modality": modality or None,
				"order_note": order_row.note,
				"scheduled_datetime": scheduled_datetime,
			}
		)
	elif kind == "service":
		doc = frappe.get_doc(
			{
				"doctype": "PetCareService",
				"pet_service_name": order_row.title,
				"pet_id": visit.animal_patient,
				"care_service_id": template_id,
				"status": "pending",
				"doctor": visit.doctor,
				"visit": visit.name,
				"order_id": order_row.order_id,
				"provider": raw.get("provider"),
				# `due_date` keeps its own meaning - the DAY this is owed, required and
				# defaulted - and is deliberately not the schedule. See the note on
				# _order_scheduled_datetime.
				"due_date": raw.get("due_date") or nowdate(),
				"scheduled_datetime": scheduled_datetime,
				"description": order_row.note,
			}
		)
	else:
		procedure_template = cstr(raw.get("procedure_template") or raw.get("procedure") or raw.get("template_id")).strip()
		if not procedure_template or not frappe.db.exists("Procedure Template", procedure_template):
			frappe.throw(_("Order {0} requires a Procedure Template.").format(order_row.title))
		template = frappe.get_cached_doc("Procedure Template", procedure_template)
		care_service = raw.get("care_service") or raw.get("care_service_id")
		if not care_service:
			_validate_procedure_template_billing(template)
		doc = frappe.get_doc(
			{
				"doctype": "Pet Procedure",
				"visit": visit.name,
				"order_id": order_row.order_id,
				"pet": visit.animal_patient,
				"guardian": visit.guardian,
				"doctor": visit.doctor,
				"provider": raw.get("provider"),
				"procedure_template": procedure_template,
				"care_service": care_service,
				"status": "Pending",
				# `scheduled_at` is the old name for this and is still accepted on input
				# (see the coercion above), so a client that has not moved yet keeps
				# working. Both columns are written for now; `scheduled_at` is the one
				# scheduled for removal, not this.
				"scheduled_at": scheduled_datetime,
				"scheduled_datetime": scheduled_datetime,
				"indication": raw.get("indication") or order_row.note,
			}
		)
	doc.insert(ignore_permissions=True)
	doc.add_comment("Comment", _("Created from Vet Visit order {0}.").format(order_row.order_id))
	return doc


def _validate_procedure_template_billing(template):
	if not cstr(template.get("item_code")).strip():
		frappe.throw(
			_("Procedure Template {0} must have an Item Code before it can be billed.").format(
				frappe.bold(template.name)
			)
		)
	if flt(template.get("price")) <= 0:
		frappe.throw(
			_("Procedure Template {0} must have a positive Price before it can be billed.").format(
				frappe.bold(template.name)
			)
		)


def _existing_linked_record_for_order(kind: str, visit_name: str, order_id: str, linked_doctype=None, linked_name=None):
	if linked_doctype and linked_name and frappe.db.exists(linked_doctype, linked_name):
		return frappe.get_doc(linked_doctype, linked_name)

	doctype = {
		"lab": "Lab",
		"radiology": "Imaging",
		"service": "PetCareService",
		"procedure": "Pet Procedure",
	}.get(kind)
	if not doctype or not frappe.db.exists("DocType", doctype):
		return None
	name = frappe.db.get_value(doctype, {"visit": visit_name, "order_id": order_id}, "name")
	if name:
		return frappe.get_doc(doctype, name)
	return None


def _positive_payload_weight(payload: dict) -> float | None:
	if "weight" not in payload:
		return None
	weight = flt(payload.get("weight"))
	return weight if weight > 0 else None


def _sync_pet_weight_from_started_service(service, weight: float | None):
	if not weight or weight <= 0 or not service.pet_id:
		return
	current = flt(frappe.db.get_value("Pet", service.pet_id, "weight") or 0)
	if current == weight:
		return
	frappe.db.set_value("Pet", service.pet_id, "weight", weight, update_modified=False)
	frappe.db.commit()
	frappe.logger().info(f"Updated Pet {service.pet_id} weight to {weight}")


def _stamp_billed_item_on_service(service, billing_plan) -> None:
	"""Record on the service what it was actually billed for.

	`service_option` is what normally stamps `item_code`/`price`, and a service created
	from a Vet Visit order never has one - so those fields stayed empty on the document
	while billing resolved the real values through the CareService template. Anything
	reading the service's own fields therefore saw a service with no price: the workspace
	reported "no billing item" for a charge that had already been raised. Teaching one
	screen a second lookup path would leave every other screen fooled the same way, so the
	document is made to tell the truth instead.

	Written from the billing plan rather than re-derived, so the stamped values are the
	same ones that reach the invoice line by construction - the two cannot drift.

	Only when empty. A `service_option` sets these deliberately (its own item and rate) and
	must never be overwritten; where it did, `plan_order_billing` already preferred the
	document's values, so the stamp would be a no-op anyway.

	Only when it bills. `billing_plan` is None for a service that raises no charge - no
	visit, already billed, no resolvable branch, or the state columns absent - and such a
	service is left exactly as it was.
	"""
	if not billing_plan:
		return
	if not cstr(service.get("item_code")).strip():
		service.item_code = billing_plan.item_code
	if not flt(service.get("price")):
		service.price = billing_plan.rate


def _update_service_status(service_name: str, action: str, payload: dict):
	service = frappe.get_doc("PetCareService", service_name)
	if action == "cancel_service":
		_assert_service_cancellable(service)
		_assert_linked_order_billing_cancellable(service, "Service")
	clinical_state.assert_action_allowed(service, action)
	# Pre-flight before anything is written. finish_service/close_service are aliases and
	# assert_action_allowed above has already refused either on a completed service.
	billing_plan = (
		order_billing.plan_order_billing(service, item_type="Service")
		if action in {"finish_service", "close_service"}
		else None
	)
	started_weight = None
	if action == "start_service":
		started_weight = _positive_payload_weight(payload)
		service.start_date = service.start_date or now_datetime()
		if started_weight:
			service.weight = started_weight
			service.flags.skip_pet_weight_sync = True
		if payload.get("provider"):
			service.provider = payload.get("provider")
	elif action in {"finish_service", "close_service"}:
		service.end_date = service.end_date or now_datetime()
		clinical_state.transition_status(service, "completed", action=action)
		_stamp_billed_item_on_service(service, billing_plan)
	elif action == "cancel_service":
		clinical_state.transition_status(service, "cancelled", action=action)
	if payload.get("description"):
		service.description = payload.get("description")
	service.save(ignore_permissions=True)
	if billing_plan:
		order_billing.commit_order_billing(service, billing_plan)
	if action == "start_service":
		_sync_pet_weight_from_started_service(service, started_weight)
	if action == "cancel_service":
		if not service.get("visit"):
			_cancel_linked_order_billable(service, "Service")
		reason = cstr(payload.get("reason") or payload.get("note")).strip()
		comment = _("Service cancelled by {0}.").format(frappe.session.user)
		if reason:
			comment = _("{0} Reason: {1}").format(comment, reason)
		service.add_comment("Comment", comment)


def _assert_linked_visit_not_billed(visit_name: str | None):
	if not visit_name:
		return
	visit = frappe.db.get_value("Vet Visit", visit_name, ["billed", "sales_invoice"], as_dict=True)
	if visit and (visit.get("billed") or visit.get("sales_invoice")):
		frappe.throw(_(BILLED_VISIT_LOCK_MESSAGE))


def _assert_service_cancellable(service) -> None:
	if cstr(service.get("status")).strip().casefold() == "completed" or service.get("end_date"):
		frappe.throw(_("This service is already completed and can't be cancelled."))


def _assert_procedure_cancellable(procedure) -> None:
	if procedure.get("status") in {"Completed", "Closed"} or procedure.get("completed_at") or procedure.get("closed_at"):
		frappe.throw(_("This procedure is already completed and can't be cancelled."))


def _assert_diagnostic_cancellable(doc) -> None:
	if doc.doctype == "Lab" and _lab_has_results(doc):
		frappe.throw(_("This lab already has results and can't be cancelled."))
	if doc.doctype == "Imaging" and _imaging_has_report_or_image(doc):
		frappe.throw(_("This imaging order already has a report or image and can't be cancelled."))


def _lab_has_results(doc) -> bool:
	return bool(
		doc.get("result")
		or doc.get("result_entered_at")
		or doc.get("result_entered_by")
		or doc.get("released_at")
		or doc.get("released_by")
		or doc.get("status") in {"Result Entered", "Released", "Completed"}
	)


def _imaging_has_report_or_image(doc) -> bool:
	return bool(
		doc.get("report")
		or doc.get("image")
		or doc.get("result_entered_at")
		or doc.get("result_entered_by")
		or doc.get("released_at")
		or doc.get("released_by")
		or doc.get("status") in {"Reported", "Released", "Completed"}
	)


def _assert_linked_order_billing_cancellable(doc, item_type: str) -> None:
	linked_service_id = f"{doc.doctype}::{doc.name}"
	if doc.get("visit"):
		legacy_kwargs = {}
		if doc.doctype == "PetCareService" and item_type == "Service":
			legacy_kwargs = {"allow_legacy_service_fallback": True, "require_match": True}
		assert_visit_billable_item_can_cancel(
			doc.visit,
			linked_service_id=linked_service_id,
			linked_doctype=doc.doctype,
			linked_name=doc.name,
			order_id=doc.get("order_id"),
			item_type=item_type,
			**legacy_kwargs,
		)
	elif doc.get("source_doctype") == "Pet Boarding":
		assert_boarding_billable_item_can_cancel(
			doc.get("source_name"),
			linked_service_id=linked_service_id,
			linked_doctype=doc.doctype,
			linked_name=doc.name,
			order_id=doc.get("order_id"),
			item_type=item_type,
		)


def _cancel_linked_order_billable(doc, item_type: str) -> None:
	linked_service_id = f"{doc.doctype}::{doc.name}"
	if doc.get("visit"):
		legacy_kwargs = {}
		if doc.doctype == "PetCareService" and item_type == "Service":
			legacy_kwargs = {"allow_legacy_service_fallback": True, "require_match": True}
		cancel_visit_billable_item_by_link(
			doc.visit,
			linked_service_id=linked_service_id,
			linked_doctype=doc.doctype,
			linked_name=doc.name,
			order_id=doc.get("order_id"),
			item_type=item_type,
			**legacy_kwargs,
		)
	elif doc.get("source_doctype") == "Pet Boarding":
		cancel_boarding_billable_item_by_link(
			doc.get("source_name"),
			linked_service_id=linked_service_id,
			linked_doctype=doc.doctype,
			linked_name=doc.name,
			order_id=doc.get("order_id"),
			item_type=item_type,
		)


def _find_medication_row(visit, row_id: str):
	for row in visit.get("prescribed_medications") or []:
		if row.name == row_id or cstr(row.idx) == row_id:
			return row
	return None


def _update_diagnostic_status(doctype: str, name: str, action: str, payload: dict):
	doc = frappe.get_doc(doctype, name)
	state_action = "cancel_test" if action in DIAGNOSTIC_CANCEL_ACTIONS else action
	if state_action == "cancel_test":
		_assert_diagnostic_cancellable(doc)
		_assert_linked_order_billing_cancellable(doc, "Lab" if doctype == "Lab" else "Imaging")
		doc.flags.allow_billed_visit_cancellation = True
	clinical_state.assert_action_allowed(doc, state_action)
	if action == "start_test":
		clinical_state.transition_status(doc, "In Progress", action=action)
	if action in {"save_result", "release"}:
		if doctype == "Lab" and _has_field("Lab", "result"):
			doc.result = payload.get("result") or payload.get("report") or doc.result
			if action == "save_result":
				doc.result_entered_by = doc.result_entered_by or frappe.session.user
				doc.result_entered_at = doc.result_entered_at or now_datetime()
				clinical_state.transition_status(doc, "Result Entered", action=action)
		if doctype == "Imaging":
			if _has_field("Imaging", "report"):
				doc.report = payload.get("report") or payload.get("result") or doc.report
			if _has_field("Imaging", "image") and payload.get("image"):
				doc.image = payload.get("image")
			if action == "save_result":
				doc.result_entered_by = doc.result_entered_by or frappe.session.user
				doc.result_entered_at = doc.result_entered_at or now_datetime()
				clinical_state.transition_status(doc, "Reported", action=action)
	if action == "release":
		doc.released_by = doc.released_by or frappe.session.user
		doc.released_at = doc.released_at or now_datetime()
		clinical_state.transition_status(doc, "Released", action=action)
	if state_action == "cancel_test":
		clinical_state.transition_status(doc, "Cancelled", action=state_action)
	doc.save(ignore_permissions=True)
	if state_action == "cancel_test":
		_update_order_status_for_link(doc.visit, doc.get("order_id"), "Cancelled", doctype, doc.name)
		reason = cstr(payload.get("reason") or payload.get("note")).strip()
		comment = _("{0} cancelled by {1}.").format(SOURCE_LABELS[doctype], frappe.session.user)
		if reason:
			comment = _("{0} Reason: {1}").format(comment, reason)
		doc.add_comment("Comment", comment)
		return
	target_status = "Completed" if doc.status in {"Released", "Completed"} else "In Progress"
	_update_order_status_for_link(doc.visit, doc.get("order_id"), target_status, doctype, doc.name)


def _update_procedure_status(procedure_name: str, action: str, payload: dict):
	doc = frappe.get_doc("Pet Procedure", procedure_name)
	if action == "cancel_procedure":
		_assert_procedure_cancellable(doc)
		_assert_linked_order_billing_cancellable(doc, "Procedure")
		doc.flags.allow_billed_visit_cancellation = True
	clinical_state.assert_action_allowed(doc, action)
	billing_plan = None
	if action == "start_procedure":
		clinical_state.transition_status(doc, "In Progress", action=action)
		doc.started_at = doc.started_at or now_datetime()
		if payload.get("provider"):
			doc.provider = payload.get("provider")
	elif action == "complete_procedure":
		_save_procedure_note(doc.name, payload, save=False, doc=doc, action=action)
		clinical_state.transition_status(doc, "Completed", action=action)
		doc.completed_at = doc.completed_at or now_datetime()
	elif action == "close_procedure":
		_save_procedure_note(doc.name, payload, save=False, doc=doc, action=action)
		# Pre-flight before anything is written. close_procedure is the billing moment for
		# a procedure, and assert_action_allowed above has already refused a second close.
		billing_plan = order_billing.plan_order_billing(doc, item_type="Procedure")
		if doc.status not in {"Completed", "Closed"}:
			clinical_state.transition_status(doc, "Completed", action=action)
			doc.completed_at = doc.completed_at or now_datetime()
		clinical_state.transition_status(doc, "Closed", action=action)
		doc.closed_at = doc.closed_at or now_datetime()
	elif action == "cancel_procedure":
		clinical_state.transition_status(doc, "Cancelled", action=action)
	else:
		frappe.throw(_("Unsupported procedure action: {0}").format(action))
	doc.save(ignore_permissions=True)
	if billing_plan:
		order_billing.commit_order_billing(doc, billing_plan)


def _save_procedure_note(procedure_name: str, payload: dict, save: bool = True, doc=None, action: str = "save_procedure_note"):
	doc = doc or frappe.get_doc("Pet Procedure", procedure_name)
	clinical_state.assert_action_allowed(doc, action)
	field_map = {
		"scheduled_datetime": "scheduled_datetime",
		# Deprecated alias, still accepted. Mapped onto the new column as well, below.
		"scheduled_at": "scheduled_at",
		"provider": "provider",
		"indication": "indication",
		"consent_obtained": "consent_obtained",
		"anesthesia_used": "anesthesia_used",
		"procedure_note": "procedure_note",
		"note": "procedure_note",
		"findings": "findings",
		"outcome": "outcome",
		"complications": "complications",
		"aftercare_instructions": "aftercare_instructions",
		"aftercare": "aftercare_instructions",
	}
	for incoming, fieldname in field_map.items():
		if incoming in payload and payload.get(incoming) is not None:
			doc.set(fieldname, payload.get(incoming))
	# Keep the two columns in step while `scheduled_at` is still being retired: whichever
	# name arrived, both hold it, so no reader goes blank mid-transition.
	if "scheduled_datetime" in payload or "scheduled_at" in payload:
		moment = _order_scheduled_datetime(payload)
		doc.set("scheduled_datetime", moment)
		if doc.meta.has_field("scheduled_at"):
			doc.set("scheduled_at", moment)
	_update_procedure_checklist(doc, payload)
	if save:
		doc.save(ignore_permissions=True)


def _update_procedure_checklist(doc, payload: dict):
	rows = _coerce_list(payload.get("checklist") if "checklist" in payload else payload.get("checklist_items"))
	if not rows:
		return
	by_name = {row.name: row for row in doc.get("checklist") or [] if row.name}
	by_title = {cstr(row.step_title).strip().lower(): row for row in doc.get("checklist") or [] if row.step_title}
	for raw in rows:
		item = _coerce_dict(raw)
		row = None
		row_name = cstr(item.get("name") or item.get("checklist_item_name")).strip()
		if row_name:
			row = by_name.get(row_name)
		title = cstr(item.get("step_title") or item.get("title")).strip()
		if not row and title:
			row = by_title.get(title.lower())
		if not row:
			if not title:
				frappe.throw(_("Procedure checklist item needs a step title."))
			row = doc.append("checklist", {"step_title": title})
		if "required" in item:
			row.required = cint(item.get("required"))
		if "done" in item:
			row.done = cint(item.get("done"))
		if "note" in item:
			row.note = item.get("note")


def _assign_record(doctype: str, name: str, payload: dict):
	doc = frappe.get_doc(doctype, name)
	if doctype == "Vet Visit":
		doctor = payload.get("practitioner") or payload.get("doctor") or payload.get("assignee")
		if doctor:
			doc.doctor = doctor
	elif doctype in {"Lab", "Imaging"}:
		doctor = payload.get("practitioner") or payload.get("doctor") or payload.get("assignee")
		if doctor:
			doc.doctor = doctor
	elif doctype == "PetCareService":
		if payload.get("provider") is not None:
			doc.provider = payload.get("provider")
		if payload.get("user") is not None:
			doc.user = payload.get("user")
		if payload.get("practitioner") is not None or payload.get("doctor") is not None:
			doc.doctor = payload.get("practitioner") or payload.get("doctor")
	elif doctype == "Pet Procedure":
		if payload.get("provider") is not None:
			doc.provider = payload.get("provider")
		if payload.get("practitioner") is not None or payload.get("doctor") is not None:
			doc.doctor = payload.get("practitioner") or payload.get("doctor")
	else:
		frappe.throw(_("Assignment is not supported for {0}.").format(doctype))
	doc.save(ignore_permissions=True)
	doc.add_comment("Comment", _("Assigned by {0}.").format(frappe.session.user))


def _convert_to_visit(doctype: str, name: str, payload: dict) -> str:
	if doctype == "Vet Case Sheet":
		case_sheet = frappe.get_doc("Vet Case Sheet", name)
	elif doctype == "Appointment":
		appointment = frappe.get_doc("Appointment", name)
		existing_visit = appointment.get("custom_linked_visit_id")
		if existing_visit:
			_apply_case_choice_payload(existing_visit, payload)
			return existing_visit
		case_sheet = _case_sheet_from_appointment(appointment, payload)
	else:
		frappe.throw(_("Only Appointment or Case Sheet can be converted to a visit."))

	existing = frappe.db.get_value("Vet Visit", {"case_sheet": case_sheet.name}, "name")
	if existing:
		_apply_case_choice_payload(existing, payload)
		return existing

	explicit = payload.get("practitioner") or payload.get("doctor")
	doctor = resolve_practitioner(explicit, case_sheet)
	require_restriction_value("practitioner", doctor)
	visit_data = {
		"doctype": "Vet Visit",
		"case_sheet": case_sheet.name,
		"customer": case_sheet.customer or get_or_create_customer_from_guardian(case_sheet.guardian),
		"guardian": case_sheet.guardian,
		"animal_patient": case_sheet.animal_patient,
		"doctor": doctor,
		"priority": payload.get("priority") or case_sheet.priority or "Normal",
		"status": "In Progress",
		"visit_type": payload.get("visit_type") or "Consultation",
		"case_summary": case_sheet.get("intake_notes"),
		"weight": case_sheet.weight,
	}
	if _has_field("Vet Visit", "appointment"):
		visit_data["appointment"] = case_sheet.get("appointment")
	visit = frappe.get_doc(visit_data)
	visit.insert(ignore_permissions=True)
	if _case_choice_from_payload(payload):
		_apply_case_choice_payload(visit, payload)
	else:
		update_profile_for_visit(visit, clinical_status="Waiting Doctor")
	frappe.db.set_value("Vet Case Sheet", case_sheet.name, {"vet_visit": visit.name, "status": "In Consultation"}, update_modified=False)
	_sync_queue_ticket_for_visit(visit, status="Waiting")
	if doctype == "Appointment":
		_stamp_appointment_conversion(name, linked_visit_id=visit.name, target="Visit")
	return visit.name


def _apply_case_choice_payload(visit, payload: dict):
	choice = _case_choice_from_payload(payload)
	if not choice:
		return
	if isinstance(visit, str):
		visit = frappe.get_doc("Vet Visit", visit)
	set_visit_case_choice(
		visit,
		choice,
		episode=payload.get("care_episode") or payload.get("episode") or payload.get("episode_name"),
		note=payload.get("case_choice_note") or payload.get("note"),
	)


def _case_choice_from_payload(payload: dict):
	return payload.get("doctor_case_choice") or payload.get("case_choice") or payload.get("choice")


def _case_sheet_from_appointment(appointment, payload: dict):
	if _has_field("Vet Case Sheet", "appointment"):
		existing = frappe.db.get_value("Vet Case Sheet", {"appointment": appointment.name}, "name")
		if existing:
			return frappe.get_doc("Vet Case Sheet", existing)

	pet = appointment.get("custom_pet") or payload.get("pet") or payload.get("pet_id")
	guardian = appointment.get("custom_guardian") or payload.get("guardian") or payload.get("guardian_id")
	if not pet or not guardian:
		frappe.throw(_("Appointment needs pet and guardian before conversion."))
	case_sheet_data = {
		"doctype": "Vet Case Sheet",
		"status": "Waiting Practitioner",
		"priority": payload.get("priority") or "Normal",
		"guardian": guardian,
		"animal_patient": pet,
		"chief_complaint": payload.get("chief_complaint") or _complaint_from_appointment(appointment),
		"intake_notes": payload.get("intake_notes") or appointment.get("customer_details"),
	}
	if _has_field("Vet Case Sheet", "appointment"):
		case_sheet_data["appointment"] = appointment.name
	if appointment.get("custom_customer"):
		case_sheet_data["customer"] = appointment.get("custom_customer")
	case_sheet = frappe.get_doc(case_sheet_data)
	case_sheet.insert(ignore_permissions=True)
	return case_sheet


def _convert_to_service(doctype: str, name: str, payload: dict) -> str:
	if doctype != "Appointment":
		frappe.throw(_("Only Appointment can be converted to a service."))
	appointment = frappe.get_doc("Appointment", name)
	existing_service = appointment.get("custom_linked_service_id")
	if existing_service:
		return existing_service

	template_id = payload.get("template_id") or payload.get("care_service_id") or payload.get("care_service")
	pet = appointment.get("custom_pet") or payload.get("pet") or payload.get("pet_id")
	if not template_id or not pet:
		frappe.throw(_("Care service template and pet are required to convert to a service."))
	service = frappe.get_doc(
		{
			"doctype": "PetCareService",
			"pet_service_name": payload.get("title") or _care_service_label(template_id) or _("Service Appointment"),
			"pet_id": pet,
			"guardian_id": appointment.get("custom_guardian") or payload.get("guardian") or payload.get("guardian_id"),
			"care_service_id": template_id,
			"status": "pending",
			"doctor": payload.get("practitioner") or payload.get("doctor") or _doctor_for_user(frappe.session.user),
			"provider": payload.get("provider"),
			"due_date": payload.get("due_date") or getdate(appointment.get("scheduled_time") or nowdate()),
			"description": payload.get("description") or appointment.get("customer_details"),
		}
	)
	service.insert(ignore_permissions=True)
	_stamp_appointment_conversion(name, linked_service_id=service.name, target="Service")
	return service.name


def _submit_invoice(doctype: str, name: str):
	if doctype != "Sales Invoice":
		frappe.throw(_("submit_invoice requires a Sales Invoice."))
	doc = frappe.get_doc("Sales Invoice", name)
	if doc.docstatus == 0:
		doc.submit()


def _mark_follow_up(doctype: str, name: str, payload: dict):
	doc = frappe.get_doc(doctype, name)
	doc.add_comment("Comment", payload.get("note") or _("Billing follow-up marked by {0}.").format(frappe.session.user))
	visit_name = _linked_visit_for_source(doctype, name)
	if visit_name and _has_field("Vet Visit", "billing_status"):
		frappe.db.set_value("Vet Visit", visit_name, "billing_status", "Follow-up", update_modified=False)


def _update_order_status_for_link(visit_name: str | None, order_id: str | None, status: str, linked_doctype=None, linked_name=None):
	if not visit_name or not order_id or not _has_field("Vet Visit", "orders"):
		return
	visit = frappe.get_doc("Vet Visit", visit_name)
	changed = False
	for row in visit.get("orders") or []:
		if row.order_id == order_id:
			clinical_state.transition_status(row, status)
			row.linked_doctype = linked_doctype or row.linked_doctype
			row.linked_name = linked_name or row.linked_name
			changed = True
	if changed:
		visit.flags.ignore_billing_lock = True
		visit.save(ignore_permissions=True)


def _sync_queue_ticket_for_visit(visit, status: str | None = None, timestamp_field: str | None = None):
	if not frappe.db.exists("DocType", "Pet Queue Ticket"):
		return
	ticket_name = frappe.db.get_value("Pet Queue Ticket", {"visit": visit.name}, "name")
	if not ticket_name and visit.case_sheet:
		ticket_name = frappe.db.get_value("Pet Queue Ticket", {"case_sheet": visit.case_sheet}, "name")
	if not ticket_name and visit.get("appointment"):
		ticket_name = frappe.db.get_value("Pet Queue Ticket", {"appointment": visit.get("appointment")}, "name")
	if not ticket_name:
		return
	ticket = frappe.get_doc("Pet Queue Ticket", ticket_name)
	ticket.visit = visit.name
	ticket.case_sheet = ticket.case_sheet or visit.case_sheet
	ticket.appointment = ticket.appointment or visit.get("appointment")
	ticket.guardian = ticket.guardian or visit.guardian
	ticket.customer = ticket.customer or visit.customer
	ticket.pet = ticket.pet or visit.animal_patient
	ticket.doctor = ticket.doctor or visit.doctor
	if status == "Waiting" and ticket.status == "Called":
		status = None
	if status and ticket.status not in {"Completed", "No Show", "Cancelled"}:
		ticket.status = status
	if timestamp_field and ticket.meta.has_field(timestamp_field) and not ticket.get(timestamp_field):
		ticket.set(timestamp_field, now_datetime())
	ticket.save(ignore_permissions=True)


def _linked_visit_for_source(doctype: str, name: str) -> str | None:
	if doctype == "Vet Visit":
		return name
	if doctype == "Vet Case Sheet":
		return frappe.db.get_value("Vet Case Sheet", name, "vet_visit")
	if doctype in {"Lab", "Imaging"}:
		return _get_optional_value(doctype, name, "visit")
	if doctype == "PetCareService":
		return _get_optional_value(doctype, name, "visit")
	if doctype == "Pet Procedure":
		return _get_optional_value(doctype, name, "visit")
	if doctype == "Appointment" and _has_field("Appointment", "custom_linked_visit_id"):
		return frappe.db.get_value("Appointment", name, "custom_linked_visit_id")
	return None


def _linked_service_for_source(doctype: str, name: str) -> str | None:
	if doctype == "PetCareService":
		return name
	if doctype == "Appointment" and _has_field("Appointment", "custom_linked_service_id"):
		return frappe.db.get_value("Appointment", name, "custom_linked_service_id")
	return None


def _focus_source(doctype: str, name: str) -> dict:
	focus = _source_brief(doctype, name)
	if doctype == "Imaging":
		focus["detail"] = _diagnostic_detail(doctype, name)
	if doctype == "Pet Procedure":
		focus["detail"] = _procedure_detail(name)
	return focus


def _source_brief(doctype: str, name: str) -> dict:
	row = frappe.db.get_value(doctype, name, _fields(doctype, ["name", "status", "modified", "creation"]), as_dict=True) or {}
	title = name
	if doctype == "Pet Procedure":
		title = _procedure_template_label(_get_optional_value(doctype, name, "procedure_template")) or name
	return {
		"name": name,
		"source_type": SOURCE_LABELS.get(doctype, doctype),
		"source_doctype": doctype,
		"title": title,
		"status": row.get("status"),
		"priority": "Normal",
		"creation": row.get("creation"),
		"modified": row.get("modified"),
	}


def _diagnostic_detail(doctype: str, name: str) -> dict:
	doc = frappe.get_doc(doctype, name)
	attachments = _attachments_for(doctype, doc.name)
	result_files = []
	seen_files = set()
	for file_row in attachments:
		_append_result_file(result_files, seen_files, file_row)
	for fieldname in _fields(doctype, list(DIAGNOSTIC_RESULT_FILE_FIELDS.get(doctype, ()))):
		_append_result_file(result_files, seen_files, {"file_url": doc.get(fieldname)})
	payload = {
		"name": doc.name,
		"doctype": doc.doctype,
		"visit": doc.get("visit"),
		"order_id": doc.get("order_id"),
		"pet": doc.get("pet"),
		"doctor": doc.get("doctor"),
		"care_service": doc.get("care_service"),
		"item_code": doc.get("item_code"),
		"status": doc.get("status"),
		"priority": doc.get("priority"),
		"body_part": doc.get("body_part"),
		"modality": doc.get("modality"),
		"order_note": doc.get("order_note"),
		"result": doc.get("result"),
		"report": doc.get("report"),
		"image": doc.get("image"),
		"attachment_required": cint(doc.get("attachment_required")),
		"released_by": doc.get("released_by"),
		"released_at": doc.get("released_at"),
		"doctor_reviewed": cint(doc.get("doctor_reviewed")),
		"doctor_reviewed_at": doc.get("doctor_reviewed_at"),
		"result_visibility": doc.get("result_visibility"),
		"attachments": attachments,
		"result_files": result_files,
	}
	return with_link_aliases(payload, pet_field="pet", doctor_field="doctor", include_guardian=False, include_provider=False)


def _procedure_detail(name: str) -> dict:
	doc = frappe.get_doc("Pet Procedure", name)
	payload = {
		"name": doc.name,
		"visit": doc.visit,
		"order_id": doc.order_id,
		"pet": doc.pet,
		"guardian": doc.guardian,
		"doctor": doc.doctor,
		"practitioner": doc.doctor,
		"doctor_label": _doctor_payload(doc.doctor).get("name_label"),
		"practitioner_label": _doctor_payload(doc.doctor).get("name_label"),
		"provider": doc.provider,
		"procedure_template": doc.procedure_template,
		"procedure_template_label": _procedure_template_label(doc.procedure_template),
		"care_service": doc.care_service,
		"care_service_label": _care_service_label(doc.care_service),
		"item_code": doc.item_code,
		"rate": doc.rate,
		"status": doc.status,
		"scheduled_at": doc.scheduled_at,
		"scheduled_datetime": doc.get("scheduled_datetime"),
		"started_at": doc.started_at,
		"completed_at": doc.completed_at,
		"closed_at": doc.closed_at,
		"indication": doc.indication,
		"consent_obtained": cint(doc.consent_obtained),
		"anesthesia_used": cint(doc.anesthesia_used),
		"procedure_note": doc.procedure_note,
		"findings": doc.findings,
		"outcome": doc.outcome,
		"complications": doc.complications,
		"aftercare_instructions": doc.aftercare_instructions,
		"checklist": [
			{
				"name": row.name,
				"step_title": row.step_title,
				"required": cint(row.required),
				"done": cint(row.done),
				"note": row.note,
			}
			for row in doc.get("checklist") or []
		],
		"attachments": _attachments_for("Pet Procedure", doc.name),
		"notes": _comments_for("Pet Procedure", doc.name),
	}
	return with_link_aliases(payload, pet_field="pet", guardian_field="guardian", doctor_field="doctor", provider_field="provider")


def _pet_payload(pet_name: str | None) -> dict:
	if not pet_name or not frappe.db.exists("Pet", pet_name):
		return {}
	row = frappe.db.get_value(
		"Pet",
		pet_name,
		["name", "pet_name", "animal_species", "animal_type", "breed", "gender", "weight", "pet_image"],
		as_dict=True,
	)
	return {
		"id": row.name,
		"name": row.name,
		"name_label": row.pet_name or row.name,
		"pet_name": row.pet_name,
		"species": row.animal_type or row.animal_species,
		"breed": row.breed,
		"gender": row.gender,
		"weight": row.weight,
		"image": row.pet_image,
	}


def _guardian_payload(guardian_name: str | None) -> dict:
	if not guardian_name or not frappe.db.exists("Guardian", guardian_name):
		return {}
	row = frappe.db.get_value(
		"Guardian",
		guardian_name,
		["name", "full_name", "phone", "email_id", "customer_id", "guardian_image"],
		as_dict=True,
	)
	if not row:
		return {}
	full_name = row.full_name or row.name
	return {
		"id": row.name,
		"name": row.name,
		"name_label": full_name,
		"full_name": full_name,
		"phone": row.phone,
		"email": row.email_id,
		"customer_id": row.customer_id,
		"image": row.guardian_image,
	}


def _doctor_payload(doctor_name: str | None) -> dict:
	return practitioner_payload(doctor_name)


def _comments_for(doctype: str, name: str) -> list[dict]:
	rows = frappe.get_all(
		"Comment",
		filters={"reference_doctype": doctype, "reference_name": name, "comment_type": "Comment"},
		fields=["name", "content", "owner", "creation", "modified"],
		order_by="creation desc",
		limit_page_length=50,
		ignore_permissions=True,
	)
	return [dict(row) for row in rows]


def _attachments_for(doctype: str, name: str) -> list[dict]:
	rows = frappe.get_all(
		"File",
		filters={"attached_to_doctype": doctype, "attached_to_name": name},
		fields=["name", "file_name", "file_url", "is_private", "owner", "creation", "modified"],
		order_by="creation desc",
		ignore_permissions=True,
	)
	return [dict(row) for row in rows]


def _timeline_for_visit(visit, linked_records=None) -> list[dict]:
	events = [
		{"type": "created", "at": visit.creation, "label": _("Visit created"), "source_doctype": "Vet Visit", "name": visit.name},
		{"type": "status", "at": visit.modified, "label": _("Visit status: {0}").format(visit.status), "source_doctype": "Vet Visit", "name": visit.name},
	]
	for record in (linked_records if linked_records is not None else _linked_records_for_visit(visit.name)):
		events.append(
			{
				"type": "linked_record",
				"at": record.get("modified"),
				"label": _("{0}: {1}").format(record.get("source_type"), record.get("status")),
				"source_doctype": record.get("source_doctype"),
				"name": record.get("name"),
			}
		)
	for comment in _comments_for("Vet Visit", visit.name):
		events.append({"type": "note", "at": comment.get("creation"), "label": _("Note added"), "name": comment.get("name")})
	return sorted(events, key=lambda item: cstr(item.get("at")), reverse=True)


def _timeline_for_source(doctype: str, name: str) -> list[dict]:
	row = frappe.db.get_value(doctype, name, _fields(doctype, ["name", "status", "creation", "modified"]), as_dict=True) or {}
	return [
		{"type": "created", "at": row.get("creation"), "label": _("{0} created").format(SOURCE_LABELS.get(doctype, doctype)), "source_doctype": doctype, "name": name},
		{"type": "status", "at": row.get("modified"), "label": _("Status: {0}").format(row.get("status") or ""), "source_doctype": doctype, "name": name},
	]


def _apply_item_filters(
	items: list[dict],
	search=None,
	priority=None,
	status=None,
	date_from=None,
	date_to=None,
	doctor=None,
	guardian=None,
	pet=None,
	species=None,
	visit_type=None,
	source_type=None,
	billing_status=None,
	branch=None,
	room=None,
	assigned_to=None,
) -> list[dict]:
	search_text = cstr(search).strip().lower()
	priority_text = cstr(priority).strip()
	status_text = cstr(status).strip()
	date_from_value = getdate(date_from) if date_from else None
	date_to_value = getdate(date_to) if date_to else None
	doctor_text = cstr(doctor).strip()
	guardian_text = cstr(guardian).strip()
	pet_text = cstr(pet).strip()
	species_text = cstr(species).strip().lower()
	visit_type_text = cstr(visit_type).strip()
	source_type_text = cstr(source_type).strip().lower()
	billing_status_text = cstr(billing_status).strip()
	branch_text = cstr(branch).strip()
	room_text = cstr(room).strip()
	assigned_to_text = cstr(assigned_to).strip()
	filtered = []
	for item in items:
		if priority_text and item.get("priority") != priority_text:
			continue
		if status_text and item.get("status") != status_text:
			continue
		if source_type_text and cstr(item.get("source_type")).strip().lower() != source_type_text and cstr(item.get("source_doctype")).strip().lower() != source_type_text:
			continue
		if pet_text and item.get("pet", {}).get("id") != pet_text and item.get("pet", {}).get("name") != pet_text:
			continue
		if guardian_text and item.get("guardian", {}).get("id") != guardian_text and item.get("guardian", {}).get("name") != guardian_text:
			continue
		if species_text and cstr(item.get("pet", {}).get("species")).strip().lower() != species_text:
			continue
		if doctor_text and item.get("assignee", {}).get("id") != doctor_text:
			continue
		if assigned_to_text and item.get("assignee", {}).get("id") != assigned_to_text:
			continue
		if visit_type_text and item.get("visit_type") != visit_type_text:
			continue
		if billing_status_text and item.get("billing_status") != billing_status_text:
			continue
		if branch_text and item.get("branch") != branch_text:
			continue
		if room_text and item.get("room") != room_text:
			continue
		if date_from_value or date_to_value:
			item_date = _item_filter_date(item)
			if not item_date:
				continue
			if date_from_value and item_date < date_from_value:
				continue
			if date_to_value and item_date > date_to_value:
				continue
		if search_text:
			haystack = " ".join(
				cstr(value)
				for value in [
					item.get("name"),
					item.get("title"),
					item.get("subtitle"),
					item.get("source_type"),
					item.get("status"),
					item.get("pet", {}).get("name_label"),
					item.get("guardian", {}).get("name_label"),
				]
			).lower()
			if search_text not in haystack:
				continue
		filtered.append(item)
	return filtered


def _item_filter_date(item: dict):
	for fieldname in ("scheduled_at", "due_at", "creation", "modified"):
		value = item.get(fieldname)
		if value:
			return getdate(value)
	return None


def _dedupe_workspace_items(items: list[dict]) -> list[dict]:
	seen = set()
	deduped = []
	for item in items:
		key = (item.get("source_doctype"), item.get("name"))
		if key in seen:
			continue
		seen.add(key)
		deduped.append(item)
	return deduped


def _workspace_metrics(items: list[dict]) -> dict:
	return {
		"total": len(items),
		"overdue": sum(1 for item in items if item.get("overdue")),
		"urgent": sum(1 for item in items if item.get("priority") in {"Urgent", "Emergency"}),
		"by_source": _counts_by(items, "source_type"),
		"by_status": _counts_by(items, "status"),
	}


def _counts_by(items: list[dict], fieldname: str) -> dict:
	counts = {}
	for item in items:
		key = item.get(fieldname) or "Unknown"
		counts[key] = counts.get(key, 0) + 1
	return counts


def _item_sort_key(item: dict):
	priority_rank = {"Emergency": 0, "Urgent": 1, "Normal": 2, "Low": 3}
	overdue_rank = 0 if item.get("overdue") else 1
	date_value = item.get("due_at") or item.get("scheduled_at") or item.get("modified") or item.get("creation") or ""
	return (overdue_rank, priority_rank.get(item.get("priority"), 2), cstr(date_value))


def _fields(doctype: str, candidates: list[str]) -> list[str]:
	standard = {"name", "owner", "creation", "modified", "modified_by", "docstatus", "idx"}
	meta = frappe.get_meta(doctype)
	return [fieldname for fieldname in candidates if fieldname in standard or meta.has_field(fieldname)]


def _has_field(doctype: str, fieldname: str) -> bool:
	try:
		return frappe.get_meta(doctype).has_field(fieldname)
	except Exception:
		return False


def _get_optional_value(doctype: str, name: str, fieldname: str):
	if not _has_field(doctype, fieldname):
		return None
	return frappe.db.get_value(doctype, name, fieldname)


def _follow_up_origin_for_appointment(name: str) -> str | None:
	if not _has_field("Appointment", "custom_follow_up_of_visit_id"):
		return None
	return frappe.db.get_value("Appointment", name, "custom_follow_up_of_visit_id")


def _existing_follow_up_appointment(visit) -> str | None:
	appointment_name = visit.get("follow_up_appointment_id")
	if appointment_name and frappe.db.exists("Appointment", appointment_name):
		return appointment_name
	if not _has_field("Appointment", "custom_follow_up_of_visit_id"):
		return None
	filters = {"custom_follow_up_of_visit_id": visit.name}
	if _has_field("Appointment", "custom_linked_visit_id"):
		filters["custom_linked_visit_id"] = ["is", "not set"]
	return frappe.db.get_value(
		"Appointment",
		filters,
		"name",
	) or frappe.db.get_value("Appointment", {"custom_follow_up_of_visit_id": visit.name}, "name")


def _appointment_contact_for_visit(visit) -> dict:
	guardian = frappe.db.get_value(
		"Guardian",
		visit.guardian,
		["full_name", "phone", "email_id", "customer_id"],
		as_dict=True,
	) if visit.guardian else None
	customer_email = _get_optional_value("Customer", visit.customer, "email_id") if visit.customer else None
	name = (guardian or {}).get("full_name") or visit.customer or visit.guardian or visit.name
	email = (guardian or {}).get("email_id") or customer_email or f"followup-{visit.name.lower()}@pet-app.local"
	return {"name": name, "phone": (guardian or {}).get("phone"), "email": email}


def _update_original_follow_up_seen(original_visit, follow_up_visit_name: str, appointment_name: str | None):
	if appointment_name and _has_field("Vet Visit", "follow_up_appointment_id"):
		original_visit.follow_up_appointment_id = appointment_name
	if appointment_name and frappe.db.exists("Appointment", appointment_name):
		scheduled_time = frappe.db.get_value("Appointment", appointment_name, "scheduled_time")
		if scheduled_time:
			if _has_field("Vet Visit", "follow_up_preferred_date") and not original_visit.get("follow_up_preferred_date"):
				original_visit.follow_up_preferred_date = getdate(scheduled_time)
			if not original_visit.get("follow_up_date"):
				original_visit.follow_up_date = getdate(scheduled_time)
	if _has_field("Vet Visit", "follow_up_visit_id"):
		original_visit.follow_up_visit_id = follow_up_visit_name
	if _has_field("Vet Visit", "follow_up_status"):
		original_visit.follow_up_status = "Seen"
	if _has_field("Vet Visit", "follow_up_required"):
		original_visit.follow_up_required = 1
	original_visit.flags.ignore_billing_lock = True
	original_visit.save(ignore_permissions=True)
	original_visit.add_comment("Comment", _("Follow-up visit {0} linked by {1}.").format(follow_up_visit_name, frappe.session.user))


def _follow_up_intake_note(original_visit, appointment) -> str:
	parts = [_("Follow-up of Vet Visit {0}.").format(original_visit.name)]
	if original_visit.get("follow_up_reason"):
		parts.append(_("Reason: {0}").format(original_visit.follow_up_reason))
	if appointment and appointment.get("customer_details"):
		parts.append(cstr(appointment.customer_details))
	return "\n".join(parts)


def _find_consult_request_row(visit, payload: dict):
	consult_request_name = cstr(payload.get("consult_request_name") or payload.get("consult_request") or payload.get("row_name")).strip()
	requested_doctor = (
		cstr(
			payload.get("requested_practitioner")
			or payload.get("practitioner")
			or payload.get("requested_doctor")
			or payload.get("doctor")
		).strip()
		or _doctor_for_user(frappe.session.user)
	)
	for row in visit.get("consult_requests") or []:
		if consult_request_name and row.name == consult_request_name:
			return row
		if not consult_request_name and requested_doctor and row.requested_doctor == requested_doctor and row.status in OPEN_CONSULT_STATUSES:
			return row
	return None


def _can_complete_consult(row) -> bool:
	user = frappe.session.user
	if user_has_full_access(user):
		return True
	roles = get_user_roles(user)
	if roles & (COORDINATOR_ROLES | MANAGEMENT_ROLES):
		return True
	doctor = _doctor_for_user(user)
	return bool(doctor and row.requested_doctor == doctor)


def _visit_has_consult_for_doctor(visit_name: str, doctor: str) -> bool:
	if not doctor or not frappe.db.exists("DocType", "Visit Consult Request"):
		return False
	return bool(
		frappe.db.exists(
			"Visit Consult Request",
			{
				"parent": visit_name,
				"parenttype": "Vet Visit",
				"parentfield": "consult_requests",
				"requested_doctor": doctor,
				"status": ["in", list(OPEN_CONSULT_STATUSES)],
			},
		)
	)


def _consult_visit_names_for_doctor(doctor: str) -> list[str]:
	if not doctor or not frappe.db.exists("DocType", "Visit Consult Request"):
		return []
	rows = frappe.get_all(
		"Visit Consult Request",
		filters={
			"parenttype": "Vet Visit",
			"parentfield": "consult_requests",
			"requested_doctor": doctor,
			"status": ["in", list(OPEN_CONSULT_STATUSES)],
		},
		fields=["parent"],
		ignore_permissions=True,
	)
	seen = set()
	names = []
	for row in rows:
		if row.parent not in seen:
			seen.add(row.parent)
			names.append(row.parent)
	return names


def _value(doc, fieldname: str):
	return doc.get(fieldname) if doc else None


def _doctor_for_user(user: str) -> str | None:
	return get_practitioner_for_user(user)


def _guardian_can_read_source(user: str, doctype: str, name: str) -> bool:
	guardian = frappe.db.get_value("Guardian", {"user_id": user}, "name")
	if not guardian:
		return False

	if doctype == "Vet Visit":
		row = frappe.db.get_value("Vet Visit", name, ["guardian", "animal_patient"], as_dict=True)
	elif doctype == "Vet Case Sheet":
		row = frappe.db.get_value("Vet Case Sheet", name, ["guardian", "animal_patient"], as_dict=True)
	elif doctype == "Appointment":
		fields = _fields("Appointment", ["custom_guardian", "custom_pet"])
		if not fields:
			return False
		row = frappe.db.get_value("Appointment", name, fields, as_dict=True)
		if row:
			row = frappe._dict({"guardian": row.get("custom_guardian"), "animal_patient": row.get("custom_pet")})
	elif doctype in {"Lab", "Imaging"}:
		visit_name = _get_optional_value(doctype, name, "visit")
		return bool(visit_name and _guardian_can_read_source(user, "Vet Visit", visit_name))
	elif doctype in {"PetCareService", "Pet Procedure"}:
		visit_name = _get_optional_value(doctype, name, "visit")
		if visit_name:
			return _guardian_can_read_source(user, "Vet Visit", visit_name)
		pet_field = "pet_id" if doctype == "PetCareService" else "pet"
		pet = _get_optional_value(doctype, name, pet_field)
		return bool(pet and frappe.db.exists("PetGuardian", {"guardian_id": guardian, "pet_id": pet}))
	else:
		return False

	if not row:
		return False
	if row.get("guardian") and row.get("guardian") != guardian:
		return False
	return bool(row.get("animal_patient") and frappe.db.exists("PetGuardian", {"guardian_id": guardian, "pet_id": row.get("animal_patient")}))


def _assert_source_restrictions(doctype: str, name: str, user: str):
	if user_has_full_access(user):
		return
	doctor = None
	if doctype == "Vet Visit":
		doctor = frappe.db.get_value("Vet Visit", name, "doctor")
	elif doctype == "Vet Case Sheet":
		visit_name = frappe.db.get_value("Vet Case Sheet", name, "vet_visit")
		if visit_name:
			doctor = frappe.db.get_value("Vet Visit", visit_name, "doctor")
	elif doctype in {"Lab", "Imaging", "PetCareService", "Pet Procedure"}:
		doctor = _get_optional_value(doctype, name, "doctor")
	elif doctype == "Appointment":
		for fieldname in ("practitioner", "custom_doctor", "doctor"):
			if _has_field("Appointment", fieldname):
				doctor = frappe.db.get_value("Appointment", name, fieldname)
				if doctor:
					break
	if doctor:
		require_restriction_value("practitioner", doctor, user=user)


def _case_sheet_priority(case_sheet: str | None) -> str | None:
	if not case_sheet:
		return None
	return frappe.db.get_value("Vet Case Sheet", case_sheet, "priority")


def _visit_next_task(row) -> str:
	status = row.get("status")
	follow_up_status = row.get("follow_up_status")
	if follow_up_status == "Scheduled" and not row.get("follow_up_visit_id"):
		return "Await follow-up"
	if status == "Draft":
		return "Start consultation"
	if status == "In Progress":
		return "Complete case"
	if status == "Follow-up Needed":
		return "Schedule follow-up"
	if status == "Completed":
		return "Review billing"
	return "Review"


def _procedure_next_task(row) -> str:
	status = row.get("status")
	if status == "Pending":
		return "Start procedure"
	if status == "In Progress":
		return "Complete procedure"
	if status == "Completed":
		return "Close procedure"
	return "Review procedure"


def _diagnostic_has_result(doctype: str, name: str) -> bool:
	if doctype == "Lab":
		return bool(_get_optional_value("Lab", name, "result"))
	if doctype == "Imaging":
		return bool(_get_optional_value("Imaging", name, "report"))
	return False


def _care_service_label(care_service: str | None) -> str | None:
	if not care_service:
		return None
	for doctype in ("CareService template", "CareService"):
		if frappe.db.exists("DocType", doctype) and frappe.db.exists(doctype, care_service):
			return frappe.db.get_value(doctype, care_service, "service_name") or care_service
	return care_service


def _procedure_template_label(procedure_template: str | None) -> str | None:
	if not procedure_template or not frappe.db.exists("DocType", "Procedure Template"):
		return None
	if not frappe.db.exists("Procedure Template", procedure_template):
		return None
	return frappe.db.get_value("Procedure Template", procedure_template, "procedure_name") or procedure_template


def _disease_label(disease: str | None) -> str | None:
	if not disease or not frappe.db.exists("Disease", disease):
		return None
	return frappe.db.get_value("Disease", disease, "disease_name")


def _resolve_disease_key(disease_name: str | None, species: str | None = None) -> str | None:
	"""Resolve a Disease primary key from a human-readable disease name.

	Looks up by FIELD, never by primary key. The two are the same string under the
	current `field:disease_name` naming, so this is behaviour-preserving today; they
	diverge once Disease moves to an autoname series, at which point a primary-key
	lookup on a typed name would miss every time.

	Returns the actual `name` (primary key), or None if nothing matches.
	"""
	disease_name = cstr(disease_name).strip()
	if not disease_name:
		return None
	species = cstr(species).strip()

	# Most specific match first. A species-less record is the generic catalogue
	# entry and is preferred over an arbitrary other-species one.
	filter_sets = []
	if species:
		filter_sets.append({"disease_name": disease_name, "species": species})
		filter_sets.append({"disease_name": disease_name, "species": ""})
	filter_sets.append({"disease_name": disease_name})

	for filters in filter_sets:
		# get_all with an explicit order_by so the result is deterministic once
		# duplicate disease_names become possible.
		match = frappe.db.get_all(
			"Disease", filters=filters, fields=["name"], order_by="creation asc", limit=1
		)
		if match:
			return match[0].name
	return None


def _patient_species(visit) -> str | None:
	"""The patient's animal type, used to disambiguate same-named Disease records.

	Pet carries two species-ish fields and only one of them is right here:
	`animal_species` is the biological class (Mammal, Bird, Reptile) while
	`animal_type` is the animal (Dog, Cat, Rabbit). `Disease.species` holds
	animal_type values, so that is the one to match on - threading animal_species
	would never match and would silently defeat the lookup it is meant to sharpen.

	Resolved once per save by the caller, never per diagnosis row.
	"""
	pet = cstr(getattr(visit, "animal_patient", "")).strip()
	if not pet:
		return None
	return cstr(frappe.db.get_value("Pet", pet, "animal_type")).strip() or None


def _ensure_disease(row: dict, default_species: str | None = None) -> str | None:
	disease_name = cstr(row.get("disease_name") or row.get("disease")).strip()
	if not disease_name:
		return None
	species = cstr(row.get("species")).strip() or cstr(default_species).strip()
	existing = _resolve_disease_key(disease_name, species)
	if existing:
		return existing
	doc = frappe.get_doc(
		{
			"doctype": "Disease",
			"disease_name": disease_name,
			"species": species,
			"category_a": row.get("category_a") or row.get("category"),
			"active": 1,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _primary_diagnosis_text(rows: list[Any]) -> str | None:
	if not rows:
		return None
	coerced = [_coerce_dict(row) for row in rows]
	primary = next((row for row in coerced if cint(row.get("is_primary"))), coerced[0])
	return (
		primary.get("diagnosis_text")
		or primary.get("text")
		or primary.get("note")
		or primary.get("disease_name")
		or primary.get("disease")
	)


def _new_order_id(visit) -> str:
	existing = {row.order_id for row in visit.get("orders") or [] if row.order_id}
	index = len(existing) + 1
	while True:
		order_id = f"{visit.name}-ORD-{index:03d}"
		if order_id not in existing:
			return order_id
		index += 1


def _deterministic_order_id(visit, row: dict, kind: str, template_id: str | None) -> str:
	procedure_template = cstr(row.get("procedure_template") or row.get("procedure")).strip()
	title = cstr(row.get("title") or row.get("name")).strip()
	identity = "|".join(
		[
			visit.name,
			kind,
			cstr(template_id).strip(),
			procedure_template,
			cstr(row.get("item_code")).strip(),
			title,
			cstr(row.get("body_part") or row.get("bodyPart")).strip(),
			cstr(row.get("modality")).strip(),
			cstr(row.get("note")).strip(),
		]
	)
	digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:10].upper()
	return f"{visit.name}-ORD-{digest}"


def _find_order_row(visit, order_id: str):
	for row in visit.get("orders") or []:
		if row.order_id == order_id:
			return row
	return None


def _update_order_row_from_payload(visit, order_row, row: dict, kind: str, template_id: str | None):
	order_row.kind = kind
	order_row.title = (
		row.get("title")
		or row.get("name")
		or _procedure_template_label(row.get("procedure_template") or row.get("procedure") or row.get("template_id"))
		or _care_service_label(template_id)
		or kind.title()
	)
	order_row.item_code = row.get("item_code")
	order_row.template_id = template_id
	order_row.priority = row.get("priority") or order_row.priority or visit.get("priority") or "Normal"
	order_row.qty = flt(row.get("qty") or order_row.qty or 1)
	order_row.price = flt(row.get("price") or row.get("rate") or order_row.price or 0)
	order_row.note = row.get("note") if "note" in row else order_row.note
	if _has_field("Visit Order", "body_part"):
		order_row.body_part = row.get("body_part") or row.get("bodyPart") or order_row.get("body_part")
	if _has_field("Visit Order", "modality"):
		order_row.modality = row.get("modality") or order_row.get("modality")
	if not order_row.status:
		order_row.status = "Draft"


def _normalize_order_kind(kind: str) -> str:
	kind = cstr(kind).strip().lower()
	if kind in {"radiology", "imaging", "image"}:
		return "radiology"
	if kind in {"lab", "laboratory"}:
		return "lab"
	if kind in {"service", "petcareservice", "care"}:
		return "service"
	if kind in {"procedure", "proc"}:
		return "procedure"
	if kind in {"medication", "medicine", "drug"}:
		return "medication"
	return "other"


def _normalize_order_status(status: str | None) -> str:
	status = cstr(status).strip().lower()
	if status in {"completed", "complete", "released", "closed"}:
		return "Completed"
	if status in {"cancelled", "canceled"}:
		return "Cancelled"
	if status in {"pending", "ordered"}:
		return "Ordered"
	return "In Progress" if status else "Draft"


def _billable_item(row) -> dict:
	return {
		"name": row.name,
		"item_name": row.item_name,
		"item_code": row.item_code,
		"item_type": row.item_type,
		"qty": row.qty,
		"rate": row.rate,
		"amount": row.amount,
		"status": row.status,
		"note": row.note,
		"linked_service_id": row.linked_service_id,
		"linked_doctype": row.get("linked_doctype"),
		"linked_name": row.get("linked_name"),
		"order_id": row.get("order_id"),
	}


def _is_cancelled_billable(row) -> bool:
	return cstr(row.get("status")).strip() == "Cancelled"


def _is_overdue(due_at, status=None) -> bool:
	if not due_at or cstr(status).lower() in {"completed", "closed", "cancelled", "canceled", "paid"}:
		return False
	try:
		return getdate(due_at) < getdate()
	except Exception:
		return False


def _join_title(*parts) -> str:
	return " - ".join(cstr(part).strip() for part in parts if cstr(part).strip())


def _complaint_from_appointment(appointment) -> str:
	appointment_type = cstr(appointment.get("custom_appointment_type")).strip().lower()
	if appointment_type == "visit":
		return "Checkup"
	if appointment_type == "follow_up":
		return "Follow-up"
	if appointment_type:
		return "Other"
	return "Checkup"


def _stamp_appointment_conversion(name: str, linked_visit_id=None, linked_service_id=None, target=None):
	updates = {}
	if linked_visit_id and _has_field("Appointment", "custom_linked_visit_id"):
		updates["custom_linked_visit_id"] = linked_visit_id
	if linked_service_id and _has_field("Appointment", "custom_linked_service_id"):
		updates["custom_linked_service_id"] = linked_service_id
	if target and _has_field("Appointment", "custom_converted_target"):
		updates["custom_converted_target"] = target
	if _has_field("Appointment", "custom_converted_at"):
		updates["custom_converted_at"] = now_datetime()
	if updates:
		frappe.db.set_value("Appointment", name, updates, update_modified=True)


def _file_name_from_url(file_url: str | None) -> str | None:
	if not file_url:
		return None
	return cstr(file_url).rstrip("/").split("/")[-1] or None


def _decode_filedata(filedata: str):
	if not filedata:
		return None
	value = cstr(filedata)
	if "," in value and value.split(",", 1)[0].startswith("data:"):
		value = value.split(",", 1)[1]
	return base64.b64decode(value)


def _coerce_dict(value=None) -> dict:
	if value is None:
		return {}
	if isinstance(value, str):
		value = frappe.parse_json(value) if value.strip() else {}
	if isinstance(value, dict):
		return value
	if hasattr(value, "as_dict"):
		return value.as_dict()
	if not value:
		return {}
	return dict(value)


def _coerce_list(value=None) -> list:
	if value is None:
		return []
	if isinstance(value, str):
		value = frappe.parse_json(value) if value.strip() else []
	if isinstance(value, dict):
		for key in ("rows", "orders", "diagnoses", "data"):
			if key in value:
				value = value.get(key)
				break
	if value is None:
		return []
	if not isinstance(value, list):
		frappe.throw(_("Expected a list payload."))
	return value
