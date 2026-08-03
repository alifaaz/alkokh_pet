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
MEDICATION_PLAN_TYPES = {"Medication", "Injection"}
EPISODE_PLAN_ITEM_TYPE_MAP = {
	"Medication": "Medication",
	"Injection": "Medication",
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
# A case is "closed" when its episode reached one of these terminal statuses.
# Reported as-is: `outcome` is NOT reconciled against `episode_status`, because the
# two genuinely disagree in live data (Resolved episodes carrying outcome Death).
CASE_TABLE_CLOSED_EPISODE_STATUSES = {"Resolved", "Closed", "Deceased", "Referred", "Cancelled"}
# Bucket for closed cases whose `outcome` was never filled in. Its own bucket, not a guess.
CASE_TABLE_NO_OUTCOME_BUCKET = "Not Set"
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
def get_pet_active_plan(pet=None, pet_id=None, include_missed=0):
	try:
		pet_name = cstr(pet or pet_id).strip()
		if not pet_name:
			return fail(_("Pet is required."), code="VALIDATION_ERROR")
		_assert_pet_access(pet_name)
		# ACTIVE_PLAN_STATUSES is shared with list_due_plan_items, so widen a local
		# copy rather than the constant - otherwise that endpoint silently starts
		# returning missed items too.
		#
		# _truthy, not raw truthiness: include_missed arrives over HTTP as a string,
		# and "0" is truthy in Python. Defaults OFF, so existing callers are
		# byte-identical.
		statuses = set(ACTIVE_PLAN_STATUSES)
		if _truthy(include_missed):
			statuses.add("Missed")
		rows = frappe.get_all(
			"Pet Care Plan Item",
			filters={"pet": pet_name, "status": ["in", sorted(statuses)]},
			fields=["*"],
			order_by="due_date asc, priority desc, modified desc",
			ignore_permissions=True,
		)
		items = [dict(row) for row in rows]
		# Same enrichment the follow-up board runs, so the two surfaces cannot
		# disagree about an item's state. This adds follow_up_state / follow_up_label
		# (via _case_table_item_state) plus the appointment and linked-visit context
		# that rule depends on - without which an item the board calls
		# "converted_to_visit" would show here as merely active.
		#
		# _enrich_case_table_items calls _enrich_episode_plan_item_display first,
		# which applies enrich_link_aliases with the same arguments used here before,
		# so the pet/guardian/doctor display names are still present. All lookups
		# inside are batched per call, never per item.
		#
		# Note these rows come from fields=["*"], so `status` holds the raw DB value
		# and `raw_status` is absent; _case_table_item_state falls back to `status`,
		# which is why the rule reads the same value on both paths.
		_enrich_case_table_items(items)
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

		cases, total_cases = _case_table_paged_cases(payload, limit_start, limit_page_length)
		if not cases:
			# No rows on THIS page (limit_start past the end), but the metrics still
			# describe the whole dataset so the cards stay stable.
			return ok(
				{
					"cases": [],
					"rows": [],
					"metrics": _case_table_metrics(_case_table_metrics_cases()),
				},
				meta={
					"total": total_cases,
					"returned": 0,
					"limit_start": limit_start,
					"limit_page_length": limit_page_length,
					"default_active_only": True,
					"default_include_closed_items": True,
				},
			)

		# Access checks and display-only alias lookups run on the PAGE, after slicing -
		# not on the pre-filter superset. An episode the item filter drops is never
		# returned, so it must not be able to raise a permission error either.
		page_episodes = [case["episode"] for case in cases]
		for episode in page_episodes:
			_assert_pet_access(episode.get("pet"))
		enrich_link_aliases(page_episodes, pet_field="pet", guardian_field="guardian", doctor_field="primary_doctor", include_provider=False)

		return ok(
			{
				"cases": cases,
				"rows": _case_table_flat_rows(cases),
				# Metrics are ABSOLUTE: the whole dataset, independent of the caller's
				# filters and paging. `meta.total` below stays filter-aware for paging.
				"metrics": _case_table_metrics(_case_table_metrics_cases()),
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
	_enrich_medication_plan_item_fields(items)
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


def _enrich_medication_plan_item_fields(items: list[dict]) -> None:
	linked_names = sorted(
		{
			cstr(item.get("linked_name")).strip()
			for item in items
			if _is_medication_plan_item(item)
			and cstr(item.get("linked_doctype")).strip() == "Vet Visit Medication Item"
			and cstr(item.get("linked_name")).strip()
		}
	)
	if not linked_names:
		for item in items:
			if _is_medication_plan_item(item):
				item.setdefault("medication", item.get("title"))
				item.setdefault("dose", item.get("dosage"))
				item.setdefault("duration", _duration_label(item.get("duration_days")))
		return

	rows = frappe.get_all(
		"Vet Visit Medication Item",
		filters={"name": ["in", linked_names]},
		fields=[
			"name",
			"parent",
			"medication",
			"medication_item",
			"qty",
			"dispense_uom",
			"stock_uom",
			"conversion_factor",
			"dosage",
			"frequency",
			"duration_days",
			"instructions",
			"warehouse",
			"dispense_status",
			"dispensed_qty",
			"dispensed_by",
			"dispensed_at",
			"batch_no",
			"expiry_date",
		],
		ignore_permissions=True,
	)
	medication_rows = {row.name: dict(row) for row in rows}
	medication_names = sorted({cstr(row.get("medication")).strip() for row in rows if cstr(row.get("medication")).strip()})
	item_codes = sorted({cstr(row.get("medication_item")).strip() for row in rows if cstr(row.get("medication_item")).strip()})
	medication_labels = _medication_label_map(medication_names)
	item_labels = _item_label_map(item_codes)

	for item in items:
		if not _is_medication_plan_item(item):
			continue
		row = medication_rows.get(cstr(item.get("linked_name")).strip())
		if not row:
			item.setdefault("medication", item.get("title"))
			item.setdefault("dose", item.get("dosage"))
			item.setdefault("duration", _duration_label(item.get("duration_days")))
			continue

		medication = cstr(row.get("medication")).strip()
		medication_item = cstr(row.get("medication_item")).strip()
		dosage = row.get("dosage")
		duration_days = row.get("duration_days") if row.get("duration_days") not in (None, "") else item.get("duration_days")

		item["medication"] = medication or medication_item or item.get("title")
		item["medication_name"] = medication_labels.get(medication) or item_labels.get(medication_item) or item["medication"]
		item["medication_item"] = medication_item or None
		item["medication_row"] = row.get("name")
		item["medication_visit"] = row.get("parent")
		item["dose"] = dosage
		item["dosage"] = dosage
		item["frequency"] = row.get("frequency") or item.get("frequency")
		item["duration_days"] = duration_days
		item["duration"] = _duration_label(duration_days)
		item["qty"] = row.get("qty")
		item["dispense_uom"] = row.get("dispense_uom")
		item["stock_uom"] = row.get("stock_uom")
		item["conversion_factor"] = row.get("conversion_factor")
		item["warehouse"] = row.get("warehouse")
		item["dispense_status"] = row.get("dispense_status")
		item["dispensed_qty"] = row.get("dispensed_qty")
		item["dispensed_by"] = row.get("dispensed_by")
		item["dispensed_at"] = row.get("dispensed_at")
		item["batch_no"] = row.get("batch_no")
		item["expiry_date"] = row.get("expiry_date")
		if row.get("instructions") and not item.get("instructions"):
			item["instructions"] = row.get("instructions")
			item["owner_instructions"] = row.get("instructions")


def _is_medication_plan_item(item: dict) -> bool:
	return cstr(item.get("plan_type")).strip() in MEDICATION_PLAN_TYPES or cstr(item.get("medication")).strip()


def _medication_label_map(medication_names: list[str]) -> dict[str, str]:
	if not medication_names:
		return {}
	return {
		row.name: row.get("medication_name") or row.name
		for row in frappe.get_all(
			"Medication",
			filters={"name": ["in", medication_names]},
			fields=["name", "medication_name"],
			ignore_permissions=True,
		)
	}


def _item_label_map(item_codes: list[str]) -> dict[str, str]:
	if not item_codes:
		return {}
	return {
		row.name: row.get("item_name") or row.name
		for row in frappe.get_all(
			"Item",
			filters={"name": ["in", item_codes]},
			fields=["name", "item_name"],
			ignore_permissions=True,
		)
	}


def _duration_label(duration_days) -> str | None:
	if duration_days in (None, ""):
		return None
	days = cint(duration_days)
	if days <= 0:
		return None
	return _("{0} day").format(days) if days == 1 else _("{0} days").format(days)


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


def _case_table_episode_filters(payload: dict) -> dict | None:
	"""Episode-level filters only - the part that CAN be expressed in SQL.

	Returns None (not an empty dict) when the payload names a doctor with no episodes:
	that means "match nothing", which an empty filter dict would silently invert into
	"match everything".
	"""
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
			return None
		filters["name"] = ["in", episode_names]

	status_filter = payload.get("episode_status") or payload.get("case_status")
	if status_filter:
		filters["episode_status"] = ["in", _as_list(status_filter)]
	elif _truthy(payload.get("active_only"), default=True):
		filters["episode_status"] = ["in", list(ACTIVE_EPISODE_STATUSES)]

	return filters


def _case_table_episodes(episode_filters: dict, limit_start: int = 0, limit_page_length: int = 0) -> list[dict]:
	"""Fetch episode rows in the board's canonical order. limit_page_length=0 means no limit."""
	rows = frappe.get_all(
		"Pet Care Episode",
		filters=episode_filters,
		fields=CASE_TABLE_EPISODE_FIELDS,
		order_by="modified desc",
		limit_start=limit_start,
		limit_page_length=limit_page_length,
		ignore_permissions=True,
	)
	return [dict(row) for row in rows]


def _case_table_paged_cases(payload: dict, limit_start: int, limit_page_length: int) -> tuple[list[dict], int]:
	"""One page of cases, plus the exact total of the SAME population.

	Two paths, deliberately:

	* **No item-level filter.** The episode filters are pure SQL, so LIMIT/OFFSET on
	  `Pet Care Episode` already slices the final population - the page and the count
	  describe the same set. Keep it: this is the common path and must not regress
	  into an unpaged scan.

	* **Item-level filter active.** `follow_up_state` is computed in Python from the
	  linked appointment and linked visit (see `_case_table_build_cases`), and
	  `_case_table_build_cases` additionally DROPS episodes left with no matching item.
	  Neither can be applied before the SQL slice. Paginating first therefore indexed
	  an unfiltered superset while the total counted the filtered set - two different
	  populations behind one offset, so pages came back short and later cases were
	  unreachable. Fix: fetch every matching episode unpaged, build/enrich/filter to
	  the correct case list, and only then slice it in Python.

	`total` is `len()` of the very list the page is sliced from, so rows and total are
	one computation and cannot drift - which also removes the separate count query
	that used to disagree with the rows by one.

	Cost: the unpaged branch reads at most the full episode set allowed by the episode
	filters (80 active / 160 total on this site) and their plan items and visits. The
	endpoint already runs exactly this pass over all 160 episodes for the KPI cards on
	every call, so this replaces the old duplicate count-pass rather than adding one.
	"""
	episode_filters = _case_table_episode_filters(payload)
	if episode_filters is None:
		return [], 0

	if not _case_table_has_item_filters(payload):
		episodes = _case_table_episodes(episode_filters, limit_start, limit_page_length)
		total = frappe.db.count("Pet Care Episode", episode_filters)
		return _case_table_build_cases(episodes, payload), total

	cases = _case_table_build_cases(_case_table_episodes(episode_filters), payload)
	total = len(cases)
	if limit_page_length:
		return cases[limit_start : limit_start + limit_page_length], total
	return cases[limit_start:], total


def _case_table_build_cases(episodes: list[dict], payload: dict) -> list[dict]:
	"""Group plan items and visits under each episode.

	Shared by the paged response and the unpaged metrics pass so both run the
	identical enrichment path. `follow_up_state` is computed in Python from
	raw_status, due_date, converted_to_visit/converted_visit, the linked
	appointment's status and whether a linked visit exists - so a SQL aggregate
	could not reproduce it, and the KPI cards would contradict the row badges
	beneath them. Hence a second pass through this same code rather than a
	GROUP BY.
	"""
	if not episodes:
		return []
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
	return cases


def _case_table_metrics_cases() -> list[dict]:
	"""Every case in the dataset - the population the KPI cards describe.

	Deliberately ignores BOTH the caller's filters and pagination: the cards are a
	dashboard over the whole dataset, not a description of the current view. The
	caller's payload is not consulted at all.

	`active_only` must be passed explicitly as False. `_case_table_episode_filters`
	defaults it to True via ``_truthy(payload.get("active_only"), default=True)``,
	so a merely-empty payload would silently return active episodes only - 80 of
	160 on this site - which would look like a working absolute metric while
	quietly excluding every closed case.

	Display-only work (link aliases) and the per-row `_assert_pet_access` check are
	skipped: this feeds aggregate counts, not returned rows.
	"""
	neutral_payload = {"active_only": False}
	# Deliberately uncapped: a cap would silently under-count, which is the failure
	# mode this whole change fixes.
	episodes = _case_table_episodes(_case_table_episode_filters(neutral_payload))
	return _case_table_build_cases(episodes, neutral_payload)


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
	if _truthy(payload.get("done_today")):
		# Items completed TODAY - the population behind metrics.done_today_items.
		#
		# `status == "Done"` is exactly equivalent to follow_up_state == "completed":
		# that is the first, unconditional branch of _case_table_item_state, so no
		# later rule can override it. This filter therefore selects precisely the
		# items the metric counts, which is the point - a card and the view it opens
		# must show the same number.
		#
		# The date is computed in PYTHON and passed as a literal. The MySQL session
		# runs in UTC (NOW() == UTC_TIMESTAMP()) while completed_on is stored in
		# system-local time (Asia/Baghdad), so CURDATE()/DATE(NOW()) would be three
		# hours out and mis-bucket every completion between 00:00 and 03:00 local.
		#
		# Set last so it wins over item_status/include_closed_items: asking for
		# "completed today" implies Done, whatever an earlier status filter said.
		today = getdate(nowdate())
		filters["status"] = "Done"
		filters["completed_on"] = ["between", [f"{today} 00:00:00", f"{today} 23:59:59.999999"]]
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


def _completed_on_date(item: dict):
	"""The local date an item was completed, or None. Never falls back to `modified`:
	"touched today" is not "completed today"."""
	value = item.get("completed_on")
	if not value:
		return None
	try:
		return getdate(value)
	except Exception:
		return None


def _case_table_metrics(cases: list[dict]) -> dict:
	"""Aggregate the case rows handed in.

	Callers pass the ABSOLUTE set from `_case_table_metrics_cases` - the whole
	dataset, not a page and not the caller's filtered view. `total_cases` is
	therefore simply the length of that set, and `open_cases + closed_cases`
	partitions it exactly.
	"""
	states = Counter()
	outcomes = Counter()
	total_items = 0
	done_today_items = 0
	open_cases = 0
	closed_cases = 0
	today = getdate(nowdate())  # Baghdad-local, same basis as due_today

	for case in cases:
		items = case.get("items") or []
		total_items += len(items)
		for item in items:
			state = item.get("follow_up_state") or "open"
			states[state] += 1
			if state == "completed" and _completed_on_date(item) == today:
				done_today_items += 1

		episode = case.get("episode") or case.get("case") or {}
		# Complementary by construction: every case lands in exactly one bucket, so
		# open_cases + closed_cases == total_cases holds even for an unexpected or
		# blank episode_status (which counts as open rather than vanishing).
		if cstr(episode.get("episode_status")).strip() in CASE_TABLE_CLOSED_EPISODE_STATUSES:
			closed_cases += 1
			outcomes[cstr(episode.get("outcome")).strip() or CASE_TABLE_NO_OUTCOME_BUCKET] += 1
		else:
			open_cases += 1

	return {
		"total_cases": len(cases),
		"open_cases": open_cases,
		"total_items": total_items,
		"open_items": total_items - sum(states.get(state, 0) for state in CASE_TABLE_TERMINAL_ITEM_STATES),
		"overdue_items": states.get("overdue", 0),
		"due_today_items": states.get("due_today", 0),
		"completed_items": states.get("completed", 0),
		"done_today_items": done_today_items,
		"cancelled_items": states.get("cancelled", 0),
		"converted_items": states.get("converted_to_visit", 0),
		"closed_cases": closed_cases,
		"closed_by_outcome": dict(outcomes),
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
	# `done_today` is a boolean flag, so it needs _truthy rather than raw truthiness:
	# the string "0" is truthy in Python and would otherwise enable the post-filter
	# episode drop while _case_table_plan_item_filters correctly ignored the flag.
	if _truthy(payload.get("done_today")):
		return True
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
