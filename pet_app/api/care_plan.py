from __future__ import annotations

from collections import Counter
import json

import frappe
from frappe import _
from frappe.utils import cint, cstr, get_datetime, getdate, now_datetime, nowdate

from pet_app.api.link_aliases import enrich_link_aliases, with_link_aliases
from pet_app.api.permissions import get_user_roles, require_doctype_permission, user_has_full_access
from pet_app.api.response import fail, ok
from pet_app.api.workspace import _assert_record_access, _has_field, _stamp_appointment_conversion
from pet_app.pet_app.doctype.pet_care_episode.pet_care_episode import ACTIVE_EPISODE_STATUSES
from pet_app.utils.medical_profile import (
	set_visit_case_choice,
	sync_follow_up_from_visit,
	sync_treatment_from_visit,
	update_profile_for_visit,
)
from pet_app.utils.care_plan_links import (
	cancel_linked_plan_appointment,
	close_linked_plan_appointment,
	enrich_plan_appointment_payload,
	plan_appointment as linked_plan_appointment,
	refresh_plan_appointment_status_field,
	scheduled_time_for_plan,
	sync_linked_plan_appointment_schedule,
)
from pet_app.utils.practitioner import get_practitioner_for_user
from pet_app.workflows import clinical_state


ACTIVE_PLAN_STATUSES = {"Planned", "Scheduled", "In Progress", "Overdue"}
PLAN_TERMINAL_STATUSES = {"Done", "Cancelled", "Converted To Visit"}
EPISODE_PLAN_ITEM_TYPE_MAP = {
	"Medication": "Medication",
	"Deworming": "Medication",
	"Lab Recheck": "Lab Test",
	"Imaging Recheck": "Imaging",
	"Procedure": "Procedure",
	"Vaccination": "Procedure",
	"Follow-up Visit": "Recheck",
	"Monitoring": "Instruction",
	"Wound Care": "Instruction",
	"Owner Instruction": "Instruction",
	"Boarding Review": "Instruction",
	"Other": "Instruction",
}
EPISODE_PLAN_STATUS_MAP = {
	"Planned": "Open",
	"In Progress": "Open",
	"Overdue": "Open",
	"Missed": "Open",
	"Changed": "Open",
	"Done": "Completed",
	"Converted To Visit": "Converted to Visit",
}
CASE_TABLE_TERMINAL_ITEM_STATES = {"completed", "cancelled", "converted_to_visit"}
CASE_TABLE_EPISODE_FIELDS = [
	"name",
	"pet",
	"guardian",
	"customer",
	"primary_doctor",
	"episode_title",
	"episode_type",
	"episode_status",
	"priority",
	"severity",
	"opened_visit",
	"current_visit",
	"last_visit",
	"chief_complaint",
	"problem_summary",
	"diagnosis_summary",
	"treatment_summary",
	"primary_diagnosis",
	"started_on",
	"expected_end_date",
	"resolved_on",
	"closed_on",
	"next_follow_up_date",
	"follow_up_status",
	"requires_follow_up",
	"outcome",
	"modified",
	"creation",
]
APPOINTMENT_PLAN_TYPES = {"Follow-up Visit", "Lab Recheck", "Imaging Recheck", "Procedure", "Vaccination"}
MEDICATION_PAYLOAD_KEYS = {"medication", "medication_item", "qty", "dispense_uom", "stock_uom", "conversion_factor", "dosage", "frequency", "duration_days", "instructions", "warehouse"}
CLINICAL_ROLES = {"Doctor", "Physician", "Healthcare", "Healthcare Practitioner", "Healthcare Administrator"}
GUARDIAN_ROLES = {"Guardian", "Guardians", "Pet"}


def _has_plan_item_permission(ptype: str, user: str | None = None) -> bool:
	try:
		return bool(frappe.has_permission("Pet Care Plan Item", ptype=ptype, user=user or frappe.session.user))
	except Exception:
		return False


@frappe.whitelist(methods=["POST"])
def add_plan_item_from_visit(visit=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		visit_name = cstr(visit or payload.get("visit") or payload.get("source_visit")).strip()
		if not visit_name:
			return fail(_("Visit is required."), code="VALIDATION_ERROR")

		require_doctype_permission("Pet Care Plan Item", "create")
		_assert_record_access("Vet Visit", visit_name, write=True, action="add_plan_item")

		visit_doc = frappe.get_doc("Vet Visit", visit_name)
		_validate_visit_can_accept_plan(visit_doc)
		episode_name = visit_doc.get("care_episode")
		if not episode_name:
			return fail(_("Choose New Case or Continue Case before adding care plan items."), code="CASE_CHOICE_REQUIRED")

		plan_type = cstr(payload.get("plan_type") or payload.get("type") or "Other").strip() or "Other"
		requires_appointment = cint(payload.get("requires_appointment"))
		if plan_type == "Follow-up Visit":
			requires_appointment = 1

		plan = frappe.get_doc(
			{
				"doctype": "Pet Care Plan Item",
				"pet": visit_doc.animal_patient,
				"guardian": visit_doc.guardian,
				"customer": visit_doc.customer,
				"care_episode": episode_name,
				"source_visit": visit_doc.name,
				"linked_doctype": payload.get("linked_doctype"),
				"linked_name": payload.get("linked_name"),
				"doctor": visit_doc.doctor,
				"plan_type": plan_type,
				"title": payload.get("title") or plan_type,
				"description": payload.get("description"),
				"instructions": payload.get("instructions"),
				"start_date": payload.get("start_date") or nowdate(),
				"due_date": payload.get("due_date"),
				"due_time": payload.get("due_time"),
				"end_date": payload.get("end_date"),
				"frequency": payload.get("frequency"),
				"duration_days": payload.get("duration_days"),
				"requires_appointment": requires_appointment,
				"requires_reminder": cint(payload.get("requires_reminder")),
				"status": payload.get("status") or "Planned",
				"priority": payload.get("priority") or "Normal",
				"created_from_action": payload.get("created_from_action") or "add_plan_item_from_visit",
				"source_doctype": "Vet Visit",
				"source_name": visit_doc.name,
			}
		)
		plan.insert(ignore_permissions=True)

		if cint(plan.requires_reminder):
			_create_reminder_for_plan(plan)

		_sync_episode_profile_for_plan(plan)
		return _workbench_response(visit_doc.name, plan=plan)
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def update_plan_item(plan_item=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		name = cstr(plan_item or payload.get("plan_item") or payload.get("name")).strip()
		if not name:
			return fail(_("Plan item is required."), code="VALIDATION_ERROR")

		require_doctype_permission("Pet Care Plan Item", "write")
		plan = frappe.get_doc("Pet Care Plan Item", name)
		_assert_plan_access(plan, write=True)
		if plan.status in {"Cancelled", "Converted To Visit"}:
			return fail(_("This plan item can no longer be edited."), code="INVALID_STATE")

		schedule_changed = any(fieldname in payload for fieldname in ("due_date", "due_time"))
		for fieldname in _plan_update_fields():
			if fieldname in payload and plan.meta.has_field(fieldname):
				plan.set(fieldname, payload.get(fieldname))
		if plan.plan_type == "Follow-up Visit":
			plan.requires_appointment = 1
		plan.save(ignore_permissions=True)
		appointment = sync_linked_plan_appointment_schedule(plan) if schedule_changed else linked_plan_appointment(plan)
		if appointment:
			previous_appointment = plan.get("appointment")
			previous_appointment_status = plan.get("appointment_status")
			refresh_plan_appointment_status_field(plan, appointment)
			if plan.get("appointment") != previous_appointment or plan.get("appointment_status") != previous_appointment_status:
				plan.save(ignore_permissions=True)
		_sync_episode_profile_for_plan(plan)
		return ok({"plan_item": _doc_payload(plan), "appointment": _doc_payload(appointment) if appointment else None})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def cancel_plan_item(plan_item=None, reason=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		name = cstr(plan_item or payload.get("plan_item") or payload.get("name")).strip()
		if not name:
			return fail(_("Plan item is required."), code="VALIDATION_ERROR")

		require_doctype_permission("Pet Care Plan Item", "write")
		plan = frappe.get_doc("Pet Care Plan Item", name)
		_assert_plan_access(plan, write=True)
		cancel_reason = reason or payload.get("reason")
		appointment = cancel_linked_plan_appointment(plan, reason=cancel_reason or _("Care plan item was cancelled."))
		if plan.status != "Cancelled":
			plan.status = "Cancelled"
			plan.completion_note = cancel_reason or plan.completion_note
		refresh_plan_appointment_status_field(plan, appointment)
		plan.save(ignore_permissions=True)
		_sync_episode_profile_for_plan(plan)
		return ok({"plan_item": _doc_payload(plan), "appointment": _doc_payload(appointment) if appointment else None})
	except Exception as exc:
		return _error_response(exc)

@frappe.whitelist(methods=["POST"])
def complete_plan_item(plan_item=None, note=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		name = cstr(plan_item or payload.get("plan_item") or payload.get("name")).strip()
		if not name:
			return fail(_("Plan item is required."), code="VALIDATION_ERROR")

		require_doctype_permission("Pet Care Plan Item", "write")
		plan = frappe.get_doc("Pet Care Plan Item", name)
		_assert_plan_access(plan, write=True)
		complete_note = note or payload.get("note")
		appointment = close_linked_plan_appointment(plan, reason=complete_note or _("Care plan item was completed."))
		if plan.status != "Done":
			plan.status = "Done"
			plan.completed_on = plan.completed_on or now_datetime()
			plan.completed_by = plan.completed_by or frappe.session.user
			plan.completion_note = complete_note or plan.completion_note
		refresh_plan_appointment_status_field(plan, appointment)
		plan.save(ignore_permissions=True)
		_sync_episode_profile_for_plan(plan)
		return ok({"plan_item": _doc_payload(plan), "appointment": _doc_payload(appointment) if appointment else None})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def schedule_plan_item_appointment(plan_item=None, appointment_data=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		name = cstr(plan_item or payload.get("plan_item") or payload.get("name")).strip()
		if not name:
			return fail(_("Plan item is required."), code="VALIDATION_ERROR")

		require_doctype_permission("Pet Care Plan Item", "write")
		require_doctype_permission("Appointment", "create")
		plan = frappe.get_doc("Pet Care Plan Item", name)
		_assert_plan_access(plan, write=True)
		if not cint(plan.requires_appointment):
			return fail(_("This plan item does not require an appointment."), code="VALIDATION_ERROR")

		appointment_payload = _payload(appointment_data, {})
		appointment_payload.update(_payload(payload.get("appointment_data"), {}))
		appointment = _get_or_create_plan_appointment(plan, appointment_payload)
		plan.appointment = appointment.name
		refresh_plan_appointment_status_field(plan, appointment)
		if plan.status == "Planned":
			plan.status = "Scheduled"
		plan.save(ignore_permissions=True)
		_sync_episode_profile_for_plan(plan)
		return ok({"plan_item": _doc_payload(plan), "appointment": _doc_payload(appointment)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def convert_plan_item_to_visit(plan_item=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		name = cstr(plan_item or payload.get("plan_item") or payload.get("name")).strip()
		if not name:
			return fail(_("Plan item is required."), code="VALIDATION_ERROR")

		require_doctype_permission("Pet Care Plan Item", "write")
		require_doctype_permission("Vet Case Sheet", "create")
		require_doctype_permission("Vet Visit", "create")
		plan = frappe.get_doc("Pet Care Plan Item", name)
		_assert_plan_access(plan, write=True)

		if plan.converted_visit and frappe.db.exists("Vet Visit", plan.converted_visit):
			return ok({"plan_item": _doc_payload(plan), "visit": plan.converted_visit, "idempotent": True})

		appointment = _plan_appointment(plan)
		linked_visit = appointment.get("custom_linked_visit_id") if appointment and _has_field("Appointment", "custom_linked_visit_id") else None
		if linked_visit and frappe.db.exists("Vet Visit", linked_visit):
			_mark_plan_converted(plan, linked_visit, appointment)
			return ok({"plan_item": _doc_payload(plan), "visit": linked_visit, "idempotent": True})

		visit = _create_visit_from_plan(plan, payload, appointment)
		_mark_plan_converted(plan, visit.name, appointment)
		update_profile_for_visit(visit, clinical_status="In Consultation", episode_status="Under Diagnosis")
		return ok({"plan_item": _doc_payload(plan), "visit": visit.name, "case_sheet": visit.case_sheet})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_care_episode_plan(episode=None, care_episode=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		episode_name = cstr(episode or care_episode or payload.get("episode") or payload.get("care_episode")).strip()
		if not episode_name:
			return fail(_("Care Episode is required."), code="VALIDATION_ERROR")
		if not frappe.db.exists("Pet Care Episode", episode_name):
			return fail(_("Care Episode {0} was not found.").format(episode_name), code="NOT_FOUND")

		require_doctype_permission("Pet Care Plan Item", "read")
		episode_doc = frappe.get_doc("Pet Care Episode", episode_name)
		if episode_doc.get("pet"):
			_assert_pet_access(episode_doc.pet)

		rows = frappe.get_all(
			"Pet Care Plan Item",
			filters={"care_episode": episode_name},
			fields=["*"],
			order_by="creation desc",
			ignore_permissions=True,
		)
		items = [_episode_plan_item_payload(row) for row in rows]
		_enrich_episode_plan_item_display(items)
		items.sort(key=_episode_plan_sort_key)
		return ok(
			{"episode": episode_name, "plan_items": items, "items": items},
			meta={
				"total": len(items),
				"order": "due_date asc, due_time asc, creation desc within the same due slot; undated items last",
				"priority_options": _select_options("Pet Care Plan Item", "priority"),
			},
		)
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_care_episode_detail(episode=None, care_episode=None, data=None, **kwargs):
	"""Return one episode's full case profile, shaped like a single ``get_case_follow_up_table`` case.

	Keyed by episode name alone (no pet filter), so a deep link / bookmark to a case can load
	without any pet context. The payload is exactly one element of that table's ``data.cases[]``
	(``{case, episode, items, visits, follow_up_summary}``), built with the same helpers so the
	frontend can reuse its existing case mapping.
	"""
	try:
		payload = _payload(data, kwargs)
		episode_name = cstr(episode or care_episode or payload.get("episode") or payload.get("care_episode")).strip()
		if not episode_name:
			return fail(_("Care Episode is required."), code="VALIDATION_ERROR")
		if not frappe.db.exists("Pet Care Episode", episode_name):
			return fail(_("Care Episode {0} was not found.").format(episode_name), code="NOT_FOUND")

		require_doctype_permission("Pet Care Episode", "read")
		require_doctype_permission("Pet Care Plan Item", "read")

		rows = frappe.get_all(
			"Pet Care Episode",
			filters={"name": episode_name},
			fields=CASE_TABLE_EPISODE_FIELDS,
			ignore_permissions=True,
		)
		episode_row = dict(rows[0])

		_assert_pet_access(episode_row.get("pet"))
		enrich_link_aliases([episode_row], pet_field="pet", guardian_field="guardian", doctor_field="primary_doctor", include_provider=False)

		items = _case_table_plan_items([episode_name], payload)
		_enrich_case_table_items(items)
		items = _filter_case_table_items_by_state(items, payload)
		visits = _case_table_visits([episode_name])

		case = {
			"case": episode_row,
			"episode": episode_row,
			"items": items,
			"visits": visits,
			"follow_up_summary": _case_table_summary(episode_row, items, visits),
		}
		return ok(case, meta={"episode": episode_name})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def list_due_plan_items(filters=None, data=None, limit_start=0, limit_page_length=50, **kwargs):
	try:
		require_doctype_permission("Pet Care Plan Item", "read")
		payload = _payload(filters or data, kwargs)
		db_filters = {"status": ["in", list(ACTIVE_PLAN_STATUSES)]}
		if payload.get("pet"):
			db_filters["pet"] = payload.get("pet")
		if payload.get("guardian"):
			db_filters["guardian"] = payload.get("guardian")
		if payload.get("doctor"):
			db_filters["doctor"] = payload.get("doctor")
		if payload.get("care_episode"):
			db_filters["care_episode"] = payload.get("care_episode")
		if payload.get("plan_type"):
			db_filters["plan_type"] = payload.get("plan_type")
		if payload.get("date_to") or payload.get("due_date"):
			db_filters["due_date"] = ["<=", getdate(payload.get("date_to") or payload.get("due_date"))]

		limit_start = max(cint(limit_start), 0)
		limit_page_length = max(min(cint(limit_page_length or payload.get("limit") or 50), 200), 1)
		names = frappe.get_all(
			"Pet Care Plan Item",
			filters=db_filters,
			pluck="name",
			order_by="due_date asc, priority desc, modified desc",
			limit_start=limit_start,
			limit_page_length=limit_page_length,
			ignore_permissions=True,
		)
		items = []
		for item_name in names:
			plan = frappe.get_doc("Pet Care Plan Item", item_name)
			_assert_plan_access(plan)
			items.append(_doc_payload(plan))
		_enrich_due_plan_item_display_names(items)
		total = frappe.db.count("Pet Care Plan Item", db_filters)
		return ok({"items": items}, meta={"total": total})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_pet_active_plan(pet=None, pet_id=None):
	try:
		pet_name = cstr(pet or pet_id).strip()
		if not pet_name:
			return fail(_("Pet is required."), code="VALIDATION_ERROR")
		_assert_pet_access(pet_name)
		rows = frappe.get_all(
			"Pet Care Plan Item",
			filters={"pet": pet_name, "status": ["in", list(ACTIVE_PLAN_STATUSES)]},
			fields=["*"],
			order_by="due_date asc, priority desc, modified desc",
			ignore_permissions=True,
		)
		items = [dict(row) for row in rows]
		enrich_link_aliases(items, pet_field="pet", guardian_field="guardian", doctor_field="doctor", include_provider=False)
		return ok({"items": items})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_case_follow_up_table(filters=None, data=None, limit_start=0, limit_page_length=50, **kwargs):
	"""Return table-ready case rows with follow-up/action items grouped under each episode."""
	try:
		require_doctype_permission("Pet Care Episode", "read")
		require_doctype_permission("Pet Care Plan Item", "read")
		payload = _payload(filters or data, kwargs)
		limit_start = max(cint(limit_start or payload.get("limit_start")), 0)
		limit_page_length = max(min(cint(limit_page_length or payload.get("limit") or payload.get("limit_page_length") or 50), 200), 1)

		episodes, total_cases = _case_table_episode_rows(payload, limit_start, limit_page_length)
		if not episodes:
			return ok(
				{"cases": [], "rows": [], "metrics": _case_table_metrics([])},
				meta={"total": total_cases, "limit_start": limit_start, "limit_page_length": limit_page_length},
			)

		for episode in episodes:
			_assert_pet_access(episode.get("pet"))
		enrich_link_aliases(episodes, pet_field="pet", guardian_field="guardian", doctor_field="primary_doctor", include_provider=False)

		episode_names = [row["name"] for row in episodes]
		items = _case_table_plan_items(episode_names, payload)
		_enrich_case_table_items(items)
		items = _filter_case_table_items_by_state(items, payload)
		visits = _case_table_visits(episode_names)

		items_by_episode = _group_by(items, "care_episode")
		visits_by_episode = _group_by(visits, "care_episode")
		require_matching_items = _case_table_has_item_filters(payload)

		cases = []
		for episode in episodes:
			episode_items = items_by_episode.get(episode["name"], [])
			if require_matching_items and not episode_items:
				continue
			episode_visits = visits_by_episode.get(episode["name"], [])
			cases.append(
				{
					"case": episode,
					"episode": episode,
					"items": episode_items,
					"visits": episode_visits,
					"follow_up_summary": _case_table_summary(episode, episode_items, episode_visits),
				}
			)

		return ok(
			{
				"cases": cases,
				"rows": _case_table_flat_rows(cases),
				"metrics": _case_table_metrics(cases),
			},
			meta={
				"total": total_cases,
				"returned": len(cases),
				"limit_start": limit_start,
				"limit_page_length": limit_page_length,
				"default_active_only": True,
				"default_include_closed_items": True,
			},
		)
	except Exception as exc:
		return _error_response(exc)


def _episode_plan_item_payload(row) -> dict:
	from pet_app.api.visit_workbench import _visit_plan_item_payload

	item = _visit_plan_item_payload(row)
	raw_status = item.get("status")
	item["raw_status"] = raw_status
	item["status"] = _episode_plan_status(raw_status)
	item["item_type"] = _episode_plan_item_type(item.get("plan_type"))
	item["owner_instructions"] = item.get("owner_instructions") or item.get("instructions")
	item["assigned_to"] = item.get("doctor")
	item["assigned_to_name"] = None
	item["scheduled_date"] = None
	return item


def _enrich_episode_plan_item_display(items: list[dict]) -> None:
	enrich_link_aliases(items, pet_field="pet", guardian_field="guardian", doctor_field="doctor", include_provider=False)
	appointment_times = _appointment_scheduled_time_map(item.get("appointment") for item in items)
	for item in items:
		item["assigned_to"] = item.get("doctor")
		item["assigned_to_name"] = item.get("doctor_name")
		scheduled_time = appointment_times.get(cstr(item.get("appointment")).strip())
		if scheduled_time:
			item["scheduled_datetime"] = cstr(scheduled_time)
			item["scheduled_date"] = cstr(getdate(scheduled_time))
		elif item.get("status") == "Scheduled" and item.get("due_date"):
			item["scheduled_date"] = item.get("due_date")


def _appointment_scheduled_time_map(appointment_names) -> dict[str, object]:
	names = sorted({cstr(name).strip() for name in appointment_names if cstr(name).strip()})
	if not names:
		return {}
	return {
		row.name: row.get("scheduled_time")
		for row in frappe.get_all(
			"Appointment",
			filters={"name": ["in", names]},
			fields=["name", "scheduled_time"],
			ignore_permissions=True,
		)
	}


def _episode_plan_sort_key(item: dict) -> tuple[int, str, str]:
	return (
		0 if item.get("due_date") else 1,
		cstr(item.get("due_date") or "9999-12-31"),
		cstr(item.get("due_time") or "23:59:59"),
	)


def _episode_plan_item_type(plan_type: str | None) -> str:
	return EPISODE_PLAN_ITEM_TYPE_MAP.get(cstr(plan_type).strip(), "Instruction")


def _episode_plan_status(status: str | None) -> str:
	raw_status = cstr(status).strip()
	return EPISODE_PLAN_STATUS_MAP.get(raw_status, raw_status or "Open")


def _select_options(doctype: str, fieldname: str) -> list[str]:
	field = frappe.get_meta(doctype).get_field(fieldname)
	if not field:
		return []
	return [option for option in cstr(field.options).splitlines() if option]


def _case_table_episode_rows(payload: dict, limit_start: int, limit_page_length: int) -> tuple[list[dict], int]:
	filters = {}
	for incoming, fieldname in (
		("pet", "pet"),
		("pet_id", "pet"),
		("guardian", "guardian"),
		("guardian_id", "guardian"),
		("customer", "customer"),
	):
		if payload.get(incoming):
			filters[fieldname] = payload.get(incoming)

	doctor = cstr(payload.get("doctor") or payload.get("practitioner") or payload.get("primary_doctor")).strip()
	if doctor:
		episode_names = _case_table_episode_names_for_doctor(doctor)
		if not episode_names:
			return [], 0
		filters["name"] = ["in", episode_names]

	status_filter = payload.get("episode_status") or payload.get("case_status")
	if status_filter:
		filters["episode_status"] = ["in", _as_list(status_filter)]
	elif _truthy(payload.get("active_only"), default=True):
		filters["episode_status"] = ["in", list(ACTIVE_EPISODE_STATUSES)]

	rows = frappe.get_all(
		"Pet Care Episode",
		filters=filters,
		fields=CASE_TABLE_EPISODE_FIELDS,
		order_by="modified desc",
		limit_start=limit_start,
		limit_page_length=limit_page_length,
		ignore_permissions=True,
	)
	return [dict(row) for row in rows], _case_table_total(filters, payload)


def _case_table_total(episode_filters: dict, payload: dict) -> int:
	"""Full count of cases matching the filters, ignoring pagination.

	When item-level filters are active, the main endpoint drops episodes with no matching
	items *after* pagination, so a raw episode count would overcount. In that case we count
	the distinct episodes that actually have a matching (and, for state filters, enriched)
	item, so ``meta.total`` stays exact and pagination can page precisely.
	"""
	if not _case_table_has_item_filters(payload):
		return frappe.db.count("Pet Care Episode", episode_filters)

	episode_names = frappe.get_all(
		"Pet Care Episode",
		filters=episode_filters,
		pluck="name",
		ignore_permissions=True,
	)
	if not episode_names:
		return 0

	# Only the enriched follow-up state filter can't be expressed at the DB level; when it is
	# absent, a distinct DB count over the item filters is enough (and cheaper).
	if not (payload.get("follow_up_state") or payload.get("item_state")):
		matching = frappe.get_all(
			"Pet Care Plan Item",
			filters=_case_table_plan_item_filters(episode_names, payload),
			pluck="care_episode",
			ignore_permissions=True,
		)
		return len({name for name in matching if name})

	items = _case_table_plan_items(episode_names, payload)
	_enrich_case_table_items(items)
	items = _filter_case_table_items_by_state(items, payload)
	return len({item.get("care_episode") for item in items if item.get("care_episode")})


def _case_table_episode_names_for_doctor(doctor: str) -> list[str]:
	names = set(
		frappe.get_all(
			"Pet Care Episode",
			filters={"primary_doctor": doctor},
			pluck="name",
			ignore_permissions=True,
		)
	)
	names.update(
		name
		for name in frappe.get_all(
			"Pet Care Plan Item",
			filters={"doctor": doctor, "care_episode": ["is", "set"]},
			pluck="care_episode",
			ignore_permissions=True,
		)
		if name
	)
	return sorted(names)


def _case_table_plan_item_filters(episode_names: list[str], payload: dict) -> dict:
	filters = {"care_episode": ["in", episode_names]}
	if payload.get("doctor") or payload.get("practitioner"):
		filters["doctor"] = payload.get("doctor") or payload.get("practitioner")
	if payload.get("plan_type"):
		filters["plan_type"] = ["in", _as_list(payload.get("plan_type"))]
	if payload.get("item_status") or payload.get("plan_status"):
		filters["status"] = ["in", _as_list(payload.get("item_status") or payload.get("plan_status"))]
	elif not _truthy(payload.get("include_closed_items"), default=True):
		filters["status"] = ["not in", ["Done", "Cancelled", "Converted To Visit"]]
	if payload.get("date_from"):
		filters["due_date"] = [">=", getdate(payload.get("date_from"))]
	if payload.get("date_to") or payload.get("due_date"):
		date_to = getdate(payload.get("date_to") or payload.get("due_date"))
		if "due_date" in filters and isinstance(filters["due_date"], list) and filters["due_date"][0] == ">=":
			filters["due_date"] = ["between", [filters["due_date"][1], date_to]]
		else:
			filters["due_date"] = ["<=", date_to]
	return filters


def _case_table_plan_items(episode_names: list[str], payload: dict) -> list[dict]:
	if not episode_names:
		return []
	filters = _case_table_plan_item_filters(episode_names, payload)
	rows = frappe.get_all(
		"Pet Care Plan Item",
		filters=filters,
		fields=["*"],
		order_by="due_date asc, due_time asc, modified desc",
		ignore_permissions=True,
	)
	return [_episode_plan_item_payload(row) for row in rows]


def _enrich_case_table_items(items: list[dict]) -> None:
	if not items:
		return
	_enrich_episode_plan_item_display(items)
	appointments = _case_table_appointment_map(item.get("appointment") for item in items)
	linked_visit_names = set()
	source_visit_names = set()
	for item in items:
		appointment = appointments.get(cstr(item.get("appointment")).strip()) or {}
		linked_visit = item.get("converted_visit") or appointment.get("custom_linked_visit_id")
		if linked_visit:
			linked_visit_names.add(linked_visit)
		if item.get("source_visit"):
			source_visit_names.add(item.get("source_visit"))
	visits = _case_table_visit_map(sorted(linked_visit_names | source_visit_names))

	for item in items:
		appointment = appointments.get(cstr(item.get("appointment")).strip()) or {}
		linked_visit_name = item.get("converted_visit") or appointment.get("custom_linked_visit_id")
		linked_visit = visits.get(linked_visit_name) if linked_visit_name else None
		source_visit = visits.get(item.get("source_visit")) if item.get("source_visit") else None
		item["appointment_status"] = item.get("appointment_status") or appointment.get("status")
		item["scheduled_datetime"] = item.get("scheduled_datetime") or cstr(appointment.get("scheduled_time") or None)
		item["linked_visit"] = linked_visit_name
		item["linked_visit_status"] = linked_visit.get("status") if linked_visit else None
		item["linked_visit_datetime"] = linked_visit.get("visit_datetime") if linked_visit else None
		item["source_visit_status"] = source_visit.get("status") if source_visit else None
		item["patient_came"] = bool(linked_visit_name)
		item["follow_up_state"] = _case_table_item_state(item, appointment=appointment, linked_visit=linked_visit)
		item["follow_up_label"] = _case_table_item_label(item, linked_visit=linked_visit)


def _case_table_appointment_map(appointment_names) -> dict[str, dict]:
	names = sorted({cstr(name).strip() for name in appointment_names if cstr(name).strip()})
	if not names:
		return {}
	fields = ["name", "status", "scheduled_time"]
	for fieldname in ("custom_linked_visit_id", "custom_converted_target", "custom_converted_at", "custom_care_plan_item"):
		if _has_field("Appointment", fieldname):
			fields.append(fieldname)
	return {
		row.name: dict(row)
		for row in frappe.get_all(
			"Appointment",
			filters={"name": ["in", names]},
			fields=fields,
			ignore_permissions=True,
		)
	}


def _case_table_visits(episode_names: list[str]) -> list[dict]:
	if not episode_names or not _has_field("Vet Visit", "care_episode"):
		return []
	rows = frappe.get_all(
		"Vet Visit",
		filters={"care_episode": ["in", episode_names]},
		fields=[
			"name",
			"care_episode",
			"animal_patient",
			"guardian",
			"doctor",
			"visit_type",
			"status",
			"visit_datetime",
			"diagnosis",
			"follow_up_required",
			"follow_up_status",
			"follow_up_date",
			"follow_up_appointment_id",
			"follow_up_visit_id",
			"follow_up_of_visit_id",
		],
		order_by="visit_datetime desc, creation desc",
		ignore_permissions=True,
	)
	visits = [dict(row) for row in rows]
	enrich_link_aliases(visits, pet_field="animal_patient", guardian_field="guardian", doctor_field="doctor", include_provider=False)
	return visits


def _case_table_visit_map(visit_names: list[str]) -> dict[str, dict]:
	names = sorted({cstr(name).strip() for name in visit_names if cstr(name).strip()})
	if not names:
		return {}
	rows = frappe.get_all(
		"Vet Visit",
		filters={"name": ["in", names]},
		fields=["name", "status", "visit_datetime", "doctor", "visit_type"],
		ignore_permissions=True,
	)
	visits = [dict(row) for row in rows]
	enrich_link_aliases(visits, doctor_field="doctor", include_pet=False, include_guardian=False, include_provider=False)
	return {row["name"]: row for row in visits}


def _case_table_item_state(item: dict, *, appointment=None, linked_visit=None) -> str:
	raw_status = cstr(item.get("raw_status") or item.get("status")).strip()
	if raw_status == "Done":
		return "completed"
	if raw_status == "Cancelled":
		return "cancelled"
	if raw_status == "Converted To Visit" or cint(item.get("converted_to_visit")) or item.get("converted_visit") or linked_visit:
		return "converted_to_visit"
	if raw_status == "Missed":
		return "missed"
	if raw_status == "Overdue":
		return "overdue"
	if appointment and cstr(appointment.get("status")).strip() == "Cancelled":
		return "cancelled"
	if item.get("due_date"):
		due_date = getdate(item.get("due_date"))
		today = getdate(nowdate())
		if due_date < today:
			return "overdue"
		if due_date == today:
			return "due_today"
	if item.get("appointment") or raw_status == "Scheduled":
		return "scheduled"
	if raw_status == "In Progress":
		return "in_progress"
	return "open"


def _case_table_item_label(item: dict, *, linked_visit=None) -> str:
	state = item.get("follow_up_state")
	if state == "converted_to_visit":
		if linked_visit and linked_visit.get("status") == "Completed":
			return "Came / Visit Completed"
		return "Came / Visit Opened"
	labels = {
		"open": "Open",
		"in_progress": "In Progress",
		"scheduled": "Scheduled",
		"due_today": "Due Today",
		"overdue": "Overdue",
		"missed": "Missed",
		"completed": "Done",
		"cancelled": "Cancelled",
	}
	return labels.get(state, "Open")


def _filter_case_table_items_by_state(items: list[dict], payload: dict) -> list[dict]:
	state_filter = payload.get("follow_up_state") or payload.get("item_state")
	if not state_filter:
		return items
	allowed = set(_as_list(state_filter))
	return [item for item in items if item.get("follow_up_state") in allowed]


def _case_table_summary(episode: dict, items: list[dict], visits: list[dict]) -> dict:
	states = Counter(item.get("follow_up_state") or "open" for item in items)
	next_due_date = _next_due_date(items)
	last_visit = visits[0] if visits else None
	return {
		"total_items": len(items),
		"open_items": len([item for item in items if item.get("follow_up_state") not in CASE_TABLE_TERMINAL_ITEM_STATES]),
		"completed_items": states.get("completed", 0),
		"cancelled_items": states.get("cancelled", 0),
		"converted_items": states.get("converted_to_visit", 0),
		"overdue_items": states.get("overdue", 0),
		"due_today_items": states.get("due_today", 0),
		"missed_items": states.get("missed", 0),
		"next_due_date": next_due_date,
		"next_follow_up_date": episode.get("next_follow_up_date") or next_due_date,
		"follow_up_status": episode.get("follow_up_status"),
		"visit_count": len(visits),
		"last_visit": last_visit.get("name") if last_visit else episode.get("last_visit"),
		"last_visit_status": last_visit.get("status") if last_visit else None,
	}


def _case_table_flat_rows(cases: list[dict]) -> list[dict]:
	rows = []
	for case in cases:
		episode = case["episode"]
		if not case["items"]:
			rows.append(_case_table_empty_case_row(episode, case["follow_up_summary"]))
			continue
		for item in case["items"]:
			row = dict(item)
			row.update(
				{
					"row_type": "plan_item",
					"case": episode.get("name"),
					"episode": episode.get("name"),
					"episode_title": episode.get("episode_title"),
					"episode_status": episode.get("episode_status"),
					"primary_doctor": episode.get("primary_doctor"),
					"primary_doctor_name": episode.get("doctor_name"),
					"case_started_on": episode.get("started_on"),
					"case_next_follow_up_date": episode.get("next_follow_up_date"),
				}
			)
			rows.append(row)
	return rows


def _case_table_empty_case_row(episode: dict, summary: dict) -> dict:
	return {
		"row_type": "case",
		"case": episode.get("name"),
		"episode": episode.get("name"),
		"episode_title": episode.get("episode_title"),
		"episode_status": episode.get("episode_status"),
		"pet": episode.get("pet"),
		"pet_name": episode.get("pet_name"),
		"guardian": episode.get("guardian"),
		"guardian_name": episode.get("guardian_name"),
		"primary_doctor": episode.get("primary_doctor"),
		"primary_doctor_name": episode.get("doctor_name"),
		"follow_up_state": "no_items",
		"follow_up_label": "No Plan Items",
		"next_due_date": summary.get("next_due_date"),
	}


def _case_table_metrics(cases: list[dict]) -> dict:
	states = Counter()
	total_items = 0
	for case in cases:
		total_items += len(case.get("items") or [])
		states.update(item.get("follow_up_state") or "open" for item in case.get("items") or [])
	return {
		"total_cases": len(cases),
		"total_items": total_items,
		"open_items": total_items - sum(states.get(state, 0) for state in CASE_TABLE_TERMINAL_ITEM_STATES),
		"overdue_items": states.get("overdue", 0),
		"due_today_items": states.get("due_today", 0),
		"completed_items": states.get("completed", 0),
		"cancelled_items": states.get("cancelled", 0),
		"converted_items": states.get("converted_to_visit", 0),
		"by_state": dict(states),
	}


def _next_due_date(items: list[dict]):
	due_dates = [
		getdate(item.get("due_date"))
		for item in items
		if item.get("due_date") and item.get("follow_up_state") not in CASE_TABLE_TERMINAL_ITEM_STATES
	]
	return min(due_dates) if due_dates else None


def _group_by(rows: list[dict], fieldname: str) -> dict[str, list[dict]]:
	grouped = {}
	for row in rows:
		key = row.get(fieldname)
		if key:
			grouped.setdefault(key, []).append(row)
	return grouped


def _case_table_has_item_filters(payload: dict) -> bool:
	return any(payload.get(key) for key in ("plan_type", "item_status", "plan_status", "follow_up_state", "item_state", "date_from", "date_to", "due_date"))


def _truthy(value, *, default=False) -> bool:
	if value is None:
		return default
	if isinstance(value, str):
		return value.strip().lower() not in {"0", "false", "no", "off"}
	return bool(cint(value))


def _as_list(value) -> list:
	if value is None:
		return []
	if isinstance(value, (list, tuple, set)):
		return [item for item in value if item]
	if isinstance(value, str) and "," in value:
		return [item.strip() for item in value.split(",") if item.strip()]
	return [value]


def _validate_visit_can_accept_plan(visit):
	if cstr(visit.get("status")) == "Cancelled":
		frappe.throw(_("Cancelled visits cannot accept care plan items."))
	clinical_state.assert_visit_not_billed(visit)


def _sync_medication_to_visit(visit, plan, payload: dict):
	if not visit.meta.has_field("prescribed_medications"):
		return
	medication_payload = _coerce_medication_payload(payload)
	if not medication_payload:
		return
	key = (
		cstr(medication_payload.get("medication_item")),
		cstr(medication_payload.get("medication")),
		cstr(medication_payload.get("dosage")),
		cstr(medication_payload.get("frequency")),
		cstr(medication_payload.get("instructions")),
	)
	for row in visit.get("prescribed_medications") or []:
		existing_key = (
			cstr(row.get("medication_item")),
			cstr(row.get("medication")),
			cstr(row.get("dosage")),
			cstr(row.get("frequency")),
			cstr(row.get("instructions")),
		)
		if key == existing_key:
			return
	row_data = {fieldname: medication_payload.get(fieldname) for fieldname in MEDICATION_PAYLOAD_KEYS if medication_payload.get(fieldname) is not None}
	row_data.setdefault("qty", medication_payload.get("qty") or 1)
	row_data.setdefault("instructions", medication_payload.get("instructions") or plan.instructions)
	visit.append("prescribed_medications", row_data)
	visit.save(ignore_permissions=True)
	sync_treatment_from_visit(visit)


def _coerce_medication_payload(payload: dict) -> dict:
	medication = payload.get("medication")
	if isinstance(medication, str):
		return {"medication_item": medication}
	if isinstance(medication, dict):
		result = dict(medication)
	else:
		result = {}
	for key in MEDICATION_PAYLOAD_KEYS:
		if key in payload and key not in result:
			result[key] = payload.get(key)
	if result.get("medication_item") or result.get("medication"):
		return result
	return {}


def _create_reminder_for_plan(plan):
	if not frappe.db.exists("DocType", "Pet Reminder") or not plan.due_date:
		return None
	key = f"care-plan::{plan.name}"
	existing = frappe.db.get_value("Pet Reminder", {"idempotency_key": key}, "name")
	if existing:
		plan.reminder = existing
		plan.reminder_status = frappe.db.get_value("Pet Reminder", existing, "status")
		plan.save(ignore_permissions=True)
		return existing
	reminder = frappe.get_doc(
		{
			"doctype": "Pet Reminder",
			"reminder_type": _reminder_type(plan),
			"pet": plan.pet,
			"guardian": plan.guardian,
			"due_date": plan.due_date,
			"status": "Pending",
			"channel": "In App",
			"reference_doctype": "Pet Care Plan Item",
			"reference_name": plan.name,
			"idempotency_key": key,
			"note": plan.title,
		}
	)
	reminder.insert(ignore_permissions=True)
	plan.reminder = reminder.name
	plan.reminder_status = reminder.status
	plan.save(ignore_permissions=True)
	return reminder.name


def _reminder_type(plan) -> str:
	if plan.plan_type == "Follow-up Visit":
		return "Follow-up Due"
	if plan.plan_type == "Vaccination":
		return "Vaccination Due"
	if plan.plan_type == "Deworming":
		return "Deworming Due"
	return "Follow-up Due"


def _sync_episode_profile_for_plan(plan):
	if plan.source_visit and frappe.db.exists("Vet Visit", plan.source_visit):
		visit = frappe.get_doc("Vet Visit", plan.source_visit)
		if not clinical_state.is_billed_visit(visit):
			if plan.plan_type == "Follow-up Visit":
				if plan.due_date:
					visit.follow_up_required = 1
					visit.follow_up_date = plan.due_date
					if _has_field("Vet Visit", "follow_up_preferred_date"):
						visit.follow_up_preferred_date = plan.due_date
				if plan.appointment and _has_field("Vet Visit", "follow_up_appointment_id"):
					visit.follow_up_appointment_id = plan.appointment
				if _has_field("Vet Visit", "follow_up_status"):
					visit.follow_up_status = "Scheduled" if plan.appointment else "Requested"
				visit.flags.ignore_billing_lock = True
				visit.save(ignore_permissions=True)
				sync_follow_up_from_visit(visit)
			else:
				update_profile_for_visit(visit, plan_status="Active")

	if plan.care_episode and frappe.db.exists("Pet Care Episode", plan.care_episode):
		episode = frappe.get_doc("Pet Care Episode", plan.care_episode)
		if plan.status not in PLAN_TERMINAL_STATUSES:
			episode.episode_status = "Follow-up Scheduled" if plan.plan_type == "Follow-up Visit" else _episode_status_for_plan(plan)
		if plan.due_date and plan.plan_type == "Follow-up Visit":
			episode.next_follow_up_date = plan.due_date
			episode.requires_follow_up = 1
			episode.follow_up_status = "Scheduled" if plan.appointment else "Requested"
		if plan.title and not episode.treatment_summary and plan.plan_type not in {"Follow-up Visit", "Lab Recheck", "Imaging Recheck"}:
			episode.treatment_summary = plan.title
		episode.save(ignore_permissions=True)

	if plan.pet and frappe.db.exists("Pet Medical Profile", {"pet": plan.pet}):
		profile_name = frappe.db.get_value("Pet Medical Profile", {"pet": plan.pet}, "name")
		updates = {
			"active_care_episode": plan.care_episode,
			"treatment_plan_status": _profile_plan_status(plan),
			"current_clinical_status": _profile_clinical_status(plan),
			"current_case_status": _profile_case_status(plan),
			"next_follow_up_date": plan.due_date if plan.plan_type == "Follow-up Visit" else None,
			"follow_up_status": "Scheduled" if plan.appointment else ("Requested" if plan.plan_type == "Follow-up Visit" else None),
			"last_synced_from_doctype": "Pet Care Plan Item",
			"last_synced_from_name": plan.name,
			"last_synced_at": now_datetime(),
		}
		_set_existing_values("Pet Medical Profile", profile_name, updates)


def _episode_status_for_plan(plan) -> str:
	if plan.plan_type in {"Lab Recheck", "Imaging Recheck", "Procedure"}:
		return "Pending Diagnostics"
	if plan.plan_type == "Monitoring":
		return "Monitoring"
	return "Under Treatment"


def _profile_plan_status(plan) -> str:
	if plan.status == "Done":
		return "Completed"
	if plan.status == "Cancelled":
		return "Cancelled"
	if plan.status == "Scheduled" and plan.plan_type == "Follow-up Visit":
		return "Waiting Follow-up"
	return "Active"


def _profile_clinical_status(plan) -> str:
	if plan.status == "Done":
		return "Stable"
	if plan.plan_type == "Follow-up Visit":
		return "Follow-up Scheduled" if plan.appointment else "Monitoring"
	if plan.plan_type in {"Lab Recheck", "Imaging Recheck"}:
		return "Pending Lab / Imaging"
	if plan.plan_type == "Monitoring":
		return "Monitoring"
	return "Under Treatment"


def _profile_case_status(plan) -> str:
	if plan.status == "Done":
		return "No Active Case"
	if plan.plan_type == "Follow-up Visit":
		return "Follow-up Scheduled"
	if plan.plan_type in {"Lab Recheck", "Imaging Recheck"}:
		return "Pending Diagnostics"
	if plan.plan_type == "Monitoring":
		return "Monitoring"
	return "Under Treatment"


def _get_or_create_plan_appointment(plan, payload: dict):
	existing = _plan_appointment(plan)
	if existing:
		_update_plan_appointment(existing, plan, payload)
		return existing

	scheduled_time = payload.get("scheduled_time") or _scheduled_time_for_plan(plan)
	contact = _appointment_contact_for_plan(plan)
	appointment_data = {
		"doctype": "Appointment",
		"status": payload.get("status") or "Open",
		"scheduled_time": scheduled_time,
		"customer_name": payload.get("customer_name") or contact["name"],
		"customer_phone_number": payload.get("customer_phone_number") or contact.get("phone"),
		"customer_email": payload.get("customer_email") or contact["email"],
		"customer_details": payload.get("customer_details") or plan.description or plan.instructions or plan.title,
	}
	for fieldname, value in {
		"custom_appointment_type": "follow_up" if plan.plan_type == "Follow-up Visit" else "visit",
		"custom_pet": plan.pet,
		"custom_guardian": plan.guardian,
		"custom_customer": plan.customer,
		"custom_follow_up_of_visit_id": plan.source_visit if plan.plan_type == "Follow-up Visit" else None,
		"custom_care_plan_item": plan.name,
	}.items():
		if value and _has_field("Appointment", fieldname):
			appointment_data[fieldname] = value
	appointment = frappe.get_doc(appointment_data)
	appointment.insert(ignore_permissions=True)
	return appointment


def _update_plan_appointment(appointment, plan, payload: dict):
	changed = False
	if payload.get("scheduled_time") and not appointment.get("custom_linked_visit_id"):
		appointment.scheduled_time = payload.get("scheduled_time")
		changed = True
	elif not payload.get("scheduled_time") and not appointment.get("custom_linked_visit_id"):
		scheduled_time = scheduled_time_for_plan(plan)
		if cstr(appointment.get("scheduled_time")) != cstr(scheduled_time):
			appointment.scheduled_time = scheduled_time
			changed = True
	if _has_field("Appointment", "custom_care_plan_item") and not appointment.get("custom_care_plan_item"):
		appointment.custom_care_plan_item = plan.name
		changed = True
	if changed:
		appointment.save(ignore_permissions=True)


def _plan_appointment(plan):
	return linked_plan_appointment(plan)


def _scheduled_time_for_plan(plan):
	return scheduled_time_for_plan(plan)


def _appointment_contact_for_plan(plan) -> dict:
	guardian = frappe.db.get_value("Guardian", plan.guardian, ["full_name", "phone", "email_id"], as_dict=True) if plan.guardian else None
	name = (guardian or {}).get("full_name") or plan.customer or plan.guardian or plan.pet or plan.name
	email = (guardian or {}).get("email_id") or f"care-plan-{plan.name.lower()}@pet-app.local"
	return {"name": name, "phone": (guardian or {}).get("phone"), "email": email}


def _create_visit_from_plan(plan, payload: dict, appointment=None):
	doctor = payload.get("doctor") or payload.get("practitioner") or plan.doctor or get_practitioner_for_user()
	case_sheet_data = {
		"doctype": "Vet Case Sheet",
		"status": "Waiting Practitioner",
		"priority": _visit_priority(plan.priority),
		"guardian": plan.guardian,
		"customer": plan.customer,
		"animal_patient": plan.pet,
		"chief_complaint": "Follow-up" if plan.plan_type == "Follow-up Visit" else "Other",
		"intake_notes": plan.description or plan.instructions or plan.title,
	}
	if appointment and _has_field("Vet Case Sheet", "appointment"):
		case_sheet_data["appointment"] = appointment.name
	case_sheet = frappe.get_doc(case_sheet_data)
	case_sheet.insert(ignore_permissions=True)
	visit_data = {
		"doctype": "Vet Visit",
		"case_sheet": case_sheet.name,
		"guardian": plan.guardian,
		"customer": plan.customer,
		"animal_patient": plan.pet,
		"doctor": doctor,
		"priority": _visit_priority(plan.priority),
		"status": "In Progress",
		"visit_type": "Follow-up" if plan.plan_type == "Follow-up Visit" else "Consultation",
		"case_summary": plan.description or plan.instructions,
	}
	if _has_field("Vet Visit", "care_episode"):
		visit_data["care_episode"] = plan.care_episode
	if _has_field("Vet Visit", "appointment") and appointment:
		visit_data["appointment"] = appointment.name
	if _has_field("Vet Visit", "follow_up_of_visit_id") and plan.source_visit:
		visit_data["follow_up_of_visit_id"] = plan.source_visit
	visit = frappe.get_doc(visit_data)
	visit.insert(ignore_permissions=True)
	frappe.db.set_value("Vet Case Sheet", case_sheet.name, {"vet_visit": visit.name, "status": "In Consultation"}, update_modified=False)
	if plan.care_episode:
		set_visit_case_choice(visit, "continue_case", episode=plan.care_episode, note=_("Visit created from care plan item {0}.").format(plan.name))
	else:
		update_profile_for_visit(visit, clinical_status="In Consultation")
	return visit


def _mark_plan_converted(plan, visit_name: str, appointment=None):
	plan.converted_to_visit = 1
	plan.converted_visit = visit_name
	plan.status = "Converted To Visit"
	if appointment:
		plan.appointment = appointment.name
		plan.appointment_status = appointment.get("status")
		_stamp_appointment_conversion(appointment.name, linked_visit_id=visit_name, target="Visit")
	plan.save(ignore_permissions=True)


def _visit_priority(priority: str | None) -> str:
	if priority == "Important":
		return "Urgent"
	return priority if priority in {"Low", "Normal", "Urgent", "Emergency"} else "Normal"


def _assert_plan_access(plan, write=False):
	if plan.source_visit and frappe.db.exists("Vet Visit", plan.source_visit):
		_assert_record_access("Vet Visit", plan.source_visit, write=write, action="care_plan")
		return
	_assert_pet_access(plan.pet, write=write)


def _assert_pet_access(pet: str, write=False):
	if not pet or not frappe.db.exists("Pet", pet):
		frappe.throw(_("Pet {0} was not found.").format(frappe.bold(pet)))
	user = frappe.session.user
	roles = get_user_roles(user)
	plan_ptype = "write" if write else "read"
	if user_has_full_access(user) or roles & CLINICAL_ROLES or _has_plan_item_permission(plan_ptype, user):
		return
	guardian = frappe.db.get_value("Guardian", {"user_id": user}, "name")
	if not write and guardian and frappe.db.exists("PetGuardian", {"guardian_id": guardian, "pet_id": pet}):
		return
	if write:
		require_doctype_permission("Pet Care Plan Item", "write")
	else:
		require_doctype_permission("Pet Care Plan Item", "read")


def _set_existing_values(doctype: str, name: str, values: dict):
	meta = frappe.get_meta(doctype)
	updates = {fieldname: value for fieldname, value in values.items() if value is not None and meta.has_field(fieldname)}
	if updates:
		frappe.db.set_value(doctype, name, updates, update_modified=False)


def _plan_update_fields() -> set[str]:
	return {
		"plan_type",
		"title",
		"description",
		"instructions",
		"start_date",
		"due_date",
		"due_time",
		"end_date",
		"frequency",
		"duration_days",
		"linked_doctype",
		"linked_name",
		"requires_appointment",
		"requires_reminder",
		"priority",
		"status",
	}


def _doc_payload(doc) -> dict | None:
	if not doc:
		return None
	data = with_link_aliases(doc.as_dict(no_nulls=False))
	if doc.doctype == "Pet Care Plan Item":
		enrich_plan_appointment_payload(data)
	return data


def _enrich_due_plan_item_display_names(items: list[dict]) -> None:
	enrich_link_aliases(items, pet_field="pet", guardian_field="guardian", doctor_field="doctor", include_provider=False)


def _workbench_response(visit_name: str, plan=None) -> dict:
	from pet_app.api.visit_workbench import get_visit_workbench

	response = get_visit_workbench(visit_name)
	if response.get("ok") and plan:
		response["data"]["created_plan_item"] = _doc_payload(plan)
	return response


def _payload(data, kwargs) -> dict:
	if isinstance(data, str) and data:
		return json.loads(data)
	if isinstance(data, dict):
		return data
	return kwargs or {}


def _error_response(exc: Exception) -> dict:
	code = "PERMISSION_DENIED" if isinstance(exc, frappe.PermissionError) else getattr(exc, "code", None) or "ERROR"
	return fail(cstr(exc), code=code, details=frappe.get_traceback())
