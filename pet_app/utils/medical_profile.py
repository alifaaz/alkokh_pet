from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr, getdate, now_datetime, nowdate

from pet_app.pet_app.doctype.pet_care_episode.pet_care_episode import ACTIVE_EPISODE_STATUSES
from pet_app.pet_app.doctype.pet_medical_profile.pet_medical_profile import ensure_pet_medical_profile
from pet_app.utils.case_assignment import ensure_episode_practitioner, visit_practitioner
from pet_app.utils.clinical_options import clinical_selection_text


CASE_STATUS_FROM_EPISODE = {
	"Open": "Open",
	"Under Diagnosis": "Under Diagnosis",
	"Pending Diagnostics": "Pending Diagnostics",
	"Under Treatment": "Under Treatment",
	"Monitoring": "Monitoring",
	"Follow-up Scheduled": "Follow-up Scheduled",
	"Follow-up Due": "Follow-up Due",
	"Referred": "Referred",
	"Resolved": "No Active Case",
	"Closed": "No Active Case",
	"Deceased": "Deceased",
	"Cancelled": "No Active Case",
}

DOCTOR_CASE_CHOICES = {"wellness", "continue_case", "new_case"}


def update_profile_for_case_sheet(case_sheet):
	if not case_sheet.get("animal_patient"):
		return None
	profile = _profile_doc(case_sheet.animal_patient, case_sheet.get("guardian"), case_sheet.get("customer"))
	_updates(
		profile,
		{
			"current_clinical_status": "Waiting Intake",
			"current_case_sheet": case_sheet.name,
			"clinical_priority": _priority(case_sheet.get("priority")),
			"active_problem_summary": _first_text(case_sheet.get("chief_complaint"), case_sheet.get("intake_notes")),
			"last_synced_from_doctype": "Vet Case Sheet",
			"last_synced_from_name": case_sheet.name,
			"last_synced_at": now_datetime(),
		},
	)
	return profile.name


def create_or_update_care_episode_from_visit(visit):
	if isinstance(visit, str):
		visit = frappe.get_doc("Vet Visit", visit)
	if not visit.get("animal_patient"):
		return None
	existing = visit.get("care_episode")
	if existing and frappe.db.exists("Pet Care Episode", existing):
		episode = frappe.get_doc("Pet Care Episode", existing)
	else:
		name = _active_episode_for_pet(visit.animal_patient)
		episode = frappe.get_doc("Pet Care Episode", name) if name else _new_episode_from_visit(visit)
		if episode.is_new():
			episode.insert(ignore_permissions=True)
		if visit.meta.has_field("care_episode") and visit.get("care_episode") != episode.name:
			frappe.db.set_value("Vet Visit", visit.name, "care_episode", episode.name, update_modified=False)
			visit.set("care_episode", episode.name)

	changed = False
	for fieldname, value in {
		"current_visit": visit.name,
		"last_visit": visit.name,
		"chief_complaint": episode.get("chief_complaint") or _case_sheet_complaint(visit.get("case_sheet")),
	}.items():
		if value and episode.get(fieldname) != value:
			episode.set(fieldname, value)
			changed = True
	practitioner = visit_practitioner(visit)
	if practitioner and ensure_episode_practitioner(episode, practitioner):
		changed = True
	if episode.episode_status == "Open" and visit.get("status") == "In Progress":
		episode.episode_status = "Under Diagnosis"
		changed = True
	if changed:
		episode.save(ignore_permissions=True)
	return episode.name


def set_visit_case_choice(visit, doctor_case_choice: str, *, episode: str | None = None, note: str | None = None):
	if isinstance(visit, str):
		visit = frappe.get_doc("Vet Visit", visit)
	if not visit.get("animal_patient"):
		frappe.throw(_("Visit must have a pet before choosing a case type."))

	choice = _normalize_case_choice(doctor_case_choice)
	active_episode_name = _active_episode_for_pet(visit.animal_patient)
	current_episode_name = visit.get("care_episode") if visit.meta.has_field("care_episode") else None

	if choice == "wellness":
		return _set_visit_case_choice_wellness(visit, current_episode_name, choice, note)
	elif choice == "continue_case":
		target_episode_name = _resolve_continue_episode(visit, episode, active_episode_name)
		_set_visit_episode(visit, target_episode_name)
		_touch_episode_from_visit(frappe.get_doc("Pet Care Episode", target_episode_name), visit)
	elif choice == "new_case":
		if active_episode_name and active_episode_name != current_episode_name:
			frappe.throw(
				_("This pet already has an open case ({0}). Close it before opening a new one.").format(frappe.bold(active_episode_name))
			)
		if current_episode_name and frappe.db.exists("Pet Care Episode", current_episode_name):
			target_episode = frappe.get_doc("Pet Care Episode", current_episode_name)
			if target_episode.episode_status not in ACTIVE_EPISODE_STATUSES:
				frappe.throw(_("Visit is linked to a closed care episode. Choose another active case or clear the link first."))
		else:
			target_episode = _cancelled_episode_opened_by_visit(visit)
			if target_episode:
				_reactivate_episode_from_visit(target_episode, visit)
			else:
				target_episode = _new_episode_from_visit(visit)
				target_episode.insert(ignore_permissions=True)
		_set_visit_episode(visit, target_episode.name)
		_touch_episode_from_visit(target_episode, visit)

	_stamp_case_choice(visit, choice, note)
	visit.save(ignore_permissions=True)
	update_profile_for_visit(visit)
	return get_visit_case_context(visit)


def _set_visit_case_choice_wellness(visit, current_episode_name: str | None, choice: str, note: str | None):
	savepoint = f"visit_wellness_choice_{frappe.generate_hash(length=10)}"
	frappe.db.savepoint(savepoint)
	try:
		_cancel_episode_opened_by_visit_choice(current_episode_name, visit)
		_delete_visit_care_plan_items(visit)
		_set_visit_episode(visit, None)
		_stamp_case_choice(visit, choice, note)
		visit.save(ignore_permissions=True)
		update_profile_for_visit(visit)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise
	else:
		frappe.db.release_savepoint(savepoint)
		return get_visit_case_context(visit)


def get_visit_case_context(visit) -> dict:
	if isinstance(visit, str):
		visit = frappe.get_doc("Vet Visit", visit)

	choice = visit.get("doctor_case_choice") if visit.meta.has_field("doctor_case_choice") else None
	visit_episode_name = _visit_episode_name(visit)
	active_episode = _active_episode_doc_for_pet(visit.get("animal_patient"))
	active_episode_name = active_episode.name if active_episode else None
	profile_active_episode = _profile_active_episode_name(visit.get("animal_patient")) or active_episode_name
	visit_episode = frappe.get_doc("Pet Care Episode", visit_episode_name) if visit_episode_name else None

	return {
		"doctor_case_choice": choice or None,
		"case_choice_required": not bool(choice or visit_episode_name),
		"visit_care_episode": visit_episode_name,
		"profile_active_episode": profile_active_episode,
		"active_episode": active_episode.as_dict(no_nulls=False) if active_episode else None,
		"episode_status": (visit_episode.get("episode_status") if visit_episode else None)
		or (active_episode.get("episode_status") if active_episode else None),
		"can_continue_case": bool(active_episode_name),
		"can_open_new_case": not bool(active_episode_name)
		or (choice == "new_case" and bool(visit_episode_name) and visit_episode_name == active_episode_name),
	}


def update_profile_for_visit(visit, *, clinical_status=None, episode_status=None, plan_status=None):
	if isinstance(visit, str):
		visit = frappe.get_doc("Vet Visit", visit)
	if not visit.get("animal_patient"):
		return None
	episode_name = _visit_episode_name(visit)
	episode = frappe.get_doc("Pet Care Episode", episode_name) if episode_name else None
	if episode and episode_status and episode.episode_status != episode_status:
		episode.episode_status = episode_status
		if episode_status in {"Resolved", "Closed"} and not episode.resolved_on:
			episode.resolved_on = getdate(nowdate())
		episode.save(ignore_permissions=True)

	active_episode = episode if episode and episode.episode_status in ACTIVE_EPISODE_STATUSES else _active_episode_doc_for_pet(visit.animal_patient)
	profile = _profile_doc(visit.animal_patient, visit.get("guardian"), visit.get("customer"))
	status = clinical_status or _clinical_status_for_visit(visit)
	updates = {
		"active_care_episode": active_episode.name if active_episode else None,
		"current_visit": visit.name,
		"current_case_sheet": visit.get("case_sheet"),
		"current_doctor": visit.get("doctor"),
		"current_clinical_status": status,
		"current_case_status": CASE_STATUS_FROM_EPISODE.get(active_episode.episode_status, "Open") if active_episode else "No Active Case",
		"last_visit": visit.name,
		"last_visit_date": visit.get("visit_datetime"),
		"last_doctor": visit.get("doctor"),
		"last_synced_from_doctype": "Vet Visit",
		"last_synced_from_name": visit.name,
		"last_synced_at": now_datetime(),
	}
	if visit.get("status") == "Completed":
		updates["last_completed_visit"] = visit.name
	if plan_status:
		updates["treatment_plan_status"] = plan_status
	if episode and visit.get("diagnosis"):
		updates["active_diagnosis_summary"] = _text(visit.get("diagnosis"))
	if episode and visit.get("treatment_plan"):
		updates["active_treatment_summary"] = _text(visit.get("treatment_plan"))
	problem_summary = _first_text(
		visit.get("case_summary"),
		clinical_selection_text(visit, "client_observations"),
		visit.get("doctor_note"),
		visit.get("assessment_note"),
	)
	if episode and problem_summary:
		updates["active_problem_summary"] = problem_summary
	if visit.get("follow_up_date"):
		updates["next_follow_up_date"] = visit.get("follow_up_date")
		updates["follow_up_status"] = visit.get("follow_up_status")
	_updates(profile, updates, allow_none_fields={"active_care_episode"})
	return profile.name


def sync_diagnoses_from_visit(visit):
	if isinstance(visit, str):
		visit = frappe.get_doc("Vet Visit", visit)
	if not visit.get("animal_patient"):
		return None
	diagnosis = _primary_diagnosis(visit) or _text(visit.get("diagnosis"))
	episode_name = _visit_episode_name(visit)
	if episode_name and diagnosis:
		episode = frappe.get_doc("Pet Care Episode", episode_name)
		episode.primary_diagnosis = diagnosis
		episode.diagnosis_summary = diagnosis
		episode.episode_status = "Under Diagnosis"
		episode.save(ignore_permissions=True)
		profile = _profile_doc(visit.animal_patient, visit.get("guardian"), visit.get("customer"))
		_updates(
			profile,
			{
				"active_diagnosis_summary": diagnosis,
				"current_clinical_status": "Under Diagnosis",
				"current_case_status": "Under Diagnosis",
				"last_synced_from_doctype": "Vet Visit",
				"last_synced_from_name": visit.name,
				"last_synced_at": now_datetime(),
			},
		)
	else:
		update_profile_for_visit(visit)


def sync_pending_orders_from_visit(visit):
	if isinstance(visit, str):
		visit = frappe.get_doc("Vet Visit", visit)
	if not visit.get("animal_patient"):
		return None
	pending = [row.title or row.kind for row in visit.get("orders") or [] if row.status not in {"Completed", "Cancelled"}]
	if not pending:
		return
	episode_name = _visit_episode_name(visit)
	if episode_name:
		episode = frappe.get_doc("Pet Care Episode", episode_name)
		episode.episode_status = "Pending Diagnostics"
		episode.save(ignore_permissions=True)
	update_profile_for_visit(visit, clinical_status="Pending Lab / Imaging")
	profile = _profile_doc(visit.animal_patient, visit.get("guardian"), visit.get("customer"))
	updates = {
		"pending_orders_summary": ", ".join(pending[:8]),
		"last_synced_from_doctype": "Vet Visit",
		"last_synced_from_name": visit.name,
		"last_synced_at": now_datetime(),
	}
	if episode_name:
		updates["current_case_status"] = "Pending Diagnostics"
		updates["treatment_plan_status"] = "Waiting Diagnostics"
	_updates(profile, updates)


def sync_treatment_from_visit(visit):
	if isinstance(visit, str):
		visit = frappe.get_doc("Vet Visit", visit)
	if not visit.get("animal_patient"):
		return None
	episode_name = _visit_episode_name(visit)
	med_summary = _medication_summary(visit)
	if episode_name:
		episode = frappe.get_doc("Pet Care Episode", episode_name)
		episode.episode_status = "Under Treatment"
		episode.treatment_summary = _text(visit.get("treatment_plan"))
		_sync_episode_medications(episode, visit)
		episode.save(ignore_permissions=True)
		profile = _profile_doc(visit.animal_patient, visit.get("guardian"), visit.get("customer"))
		_updates(
			profile,
			{
				"active_treatment_summary": _text(visit.get("treatment_plan")),
				"active_medication_summary": med_summary,
				"treatment_plan_status": "Active" if (visit.get("treatment_plan") or med_summary) else profile.get("treatment_plan_status"),
				"current_clinical_status": "Under Treatment",
				"current_case_status": "Under Treatment",
				"last_synced_from_doctype": "Vet Visit",
				"last_synced_from_name": visit.name,
				"last_synced_at": now_datetime(),
			},
		)
	else:
		update_profile_for_visit(visit)


def sync_follow_up_from_visit(visit):
	if isinstance(visit, str):
		visit = frappe.get_doc("Vet Visit", visit)
	if not visit.get("animal_patient"):
		return None
	episode_name = _visit_episode_name(visit)
	if episode_name:
		episode = frappe.get_doc("Pet Care Episode", episode_name)
		episode.episode_status = "Follow-up Scheduled"
		episode.requires_follow_up = 1
		episode.next_follow_up_date = visit.get("follow_up_date")
		episode.follow_up_status = visit.get("follow_up_status")
		episode.save(ignore_permissions=True)
	update_profile_for_visit(visit, clinical_status="Follow-up Scheduled")
	profile = _profile_doc(visit.animal_patient, visit.get("guardian"), visit.get("customer"))
	updates = {
		"next_follow_up_date": visit.get("follow_up_date"),
		"follow_up_status": visit.get("follow_up_status"),
		"last_synced_from_doctype": "Vet Visit",
		"last_synced_from_name": visit.name,
		"last_synced_at": now_datetime(),
	}
	if episode_name:
		updates["current_case_status"] = "Follow-up Scheduled"
		updates["treatment_plan_status"] = "Waiting Follow-up"
	_updates(profile, updates)


def sync_completed_visit(visit, outcome=None):
	if isinstance(visit, str):
		visit = frappe.get_doc("Vet Visit", visit)
	outcome = outcome or visit.get("outcome")
	if outcome == "Referred":
		episode_status = "Referred"
		clinical_status = "Referred"
	elif outcome in {"Death", "Euthanasia"}:
		episode_status = "Deceased"
		clinical_status = "Deceased"
	elif visit.get("follow_up_required"):
		episode_status = "Follow-up Scheduled" if visit.get("follow_up_date") else "Monitoring"
		clinical_status = "Follow-up Scheduled" if visit.get("follow_up_date") else "Monitoring"
	else:
		episode_status = None
		clinical_status = "Stable"
	update_profile_for_visit(visit, clinical_status=clinical_status, episode_status=episode_status, plan_status="Completed" if episode_status == "Resolved" else None)


def sync_latest_vitals(visit, vital=None):
	if isinstance(visit, str):
		visit = frappe.get_doc("Vet Visit", visit)
	if not visit.get("animal_patient"):
		return
	profile = _profile_doc(visit.animal_patient, visit.get("guardian"), visit.get("customer"))
	vital = vital or _latest_vital_from_visit(visit)
	updates = {}
	for source, target in (
		("weight", "last_weight"),
		("temperature", "last_temperature"),
		("heart_rate", "last_heart_rate"),
		("respiratory_rate", "last_respiratory_rate"),
	):
		value = vital.get(source) if vital else visit.get(source)
		if value not in (None, ""):
			updates[target] = value
	if vital and vital.get("recorded_at"):
		updates["last_vitals_at"] = vital.get("recorded_at")
	elif updates:
		updates["last_vitals_at"] = now_datetime()
	if updates:
		updates.update({"last_visit": visit.name, "last_synced_from_doctype": "Vet Visit", "last_synced_from_name": visit.name, "last_synced_at": now_datetime()})
		_updates(profile, updates)


def protect_care_episode_before_visit_delete(visit):
	if isinstance(visit, str):
		visit = frappe.get_doc("Vet Visit", visit)
	episode = _episode_opened_by_visit_for_delete(visit)
	if not episode or episode.episode_status not in ACTIVE_EPISODE_STATUSES:
		return

	other_visits = frappe.get_all(
		"Vet Visit",
		filters={"care_episode": episode.name, "name": ["!=", visit.name]},
		pluck="name",
		order_by="modified desc",
		ignore_permissions=True,
	)
	if other_visits:
		_detach_visit_from_episode_links(episode, visit, replacement_visit=other_visits[0])
		_clear_profile_visit_links_if_matches(visit, replacement_visit=other_visits[0])
		return

	blockers = _visit_delete_episode_blockers(episode, visit)
	if blockers:
		frappe.throw(
			_(
				"Cannot delete Vet Visit {0} because it opened active Care Episode {1}, which has clinical content: {2}. Close or reassign the care episode before deleting this visit."
			).format(
				frappe.bold(visit.name),
				frappe.bold(episode.name),
				", ".join(blockers),
			)
		)

	_cancel_episode_opened_by_visit_choice(episode.name, visit)
	episode = frappe.get_doc("Pet Care Episode", episode.name)
	_detach_visit_from_episode_links(episode, visit)
	_clear_profile_active_episode_if_matches(episode)
	_clear_profile_visit_links_if_matches(visit)


def _profile_doc(pet, guardian=None, customer=None):
	profile_name = ensure_pet_medical_profile(pet, guardian)
	profile = frappe.get_doc("Pet Medical Profile", profile_name)
	updates = {}
	if guardian and not profile.get("primary_guardian"):
		updates["primary_guardian"] = guardian
	if customer and profile.meta.has_field("customer") and not profile.get("customer"):
		updates["customer"] = customer
	if updates:
		_updates(profile, updates)
	return profile


def _new_episode_from_visit(visit):
	opening_practitioner = visit_practitioner(visit)
	if not opening_practitioner:
		frappe.throw(_("Visit must have a practitioner before opening a new case."))

	return frappe.get_doc(
		{
			"doctype": "Pet Care Episode",
			"pet": visit.animal_patient,
			"guardian": visit.guardian,
			"customer": visit.customer,
			"primary_doctor": opening_practitioner,
			"episode_title": _first_text(_case_sheet_complaint(visit.case_sheet), visit.get("diagnosis"), _("Active Case")),
			"episode_type": _episode_type_from_visit(visit),
			"episode_status": "Under Diagnosis" if visit.status == "In Progress" else "Open",
			"priority": _priority(visit.get("priority")),
			"opened_from_doctype": "Vet Visit",
			"opened_from_name": visit.name,
			"opened_visit": visit.name,
			"current_visit": visit.name,
			"last_visit": visit.name,
			"chief_complaint": _case_sheet_complaint(visit.case_sheet),
			"started_on": getdate(visit.get("visit_datetime") or nowdate()),
		}
	)


def _active_episode_for_pet(pet: str) -> str | None:
	return frappe.db.get_value(
		"Pet Care Episode",
		{"pet": pet, "episode_status": ["in", list(ACTIVE_EPISODE_STATUSES)]},
		"name",
		order_by="modified desc",
	)


def _cancelled_episode_opened_by_visit(visit):
	if not visit.get("animal_patient"):
		return None
	episodes = frappe.get_all(
		"Pet Care Episode",
		filters={"pet": visit.animal_patient, "episode_status": "Cancelled"},
		fields=["name", "opened_visit", "opened_from_name"],
		order_by="creation asc",
		ignore_permissions=True,
	)
	for row in episodes:
		if row.opened_visit != visit.name and row.opened_from_name != visit.name:
			continue
		if frappe.db.count("Vet Visit", {"care_episode": row.name, "name": ["!=", visit.name]}):
			continue
		return frappe.get_doc("Pet Care Episode", row.name)
	return None


def _reactivate_episode_from_visit(episode, visit):
	episode.episode_status = "Under Diagnosis" if visit.get("status") == "In Progress" else "Open"
	for fieldname in ("closed_on", "closed_by", "closure_reason", "resolved_on", "outcome"):
		if episode.meta.has_field(fieldname):
			episode.set(fieldname, None)
	episode.save(ignore_permissions=True)


def _active_episode_doc_for_pet(pet: str | None):
	if not pet:
		return None
	name = _active_episode_for_pet(pet)
	return frappe.get_doc("Pet Care Episode", name) if name else None


def _profile_active_episode_name(pet: str | None) -> str | None:
	if not pet:
		return None
	name = frappe.db.get_value("Pet Medical Profile", {"pet": pet}, "active_care_episode")
	if name and frappe.db.exists("Pet Care Episode", name):
		status = frappe.db.get_value("Pet Care Episode", name, "episode_status")
		if status in ACTIVE_EPISODE_STATUSES:
			return name
	return None


def _visit_episode_name(visit) -> str | None:
	if not visit or not visit.meta.has_field("care_episode"):
		return None
	episode_name = visit.get("care_episode")
	return episode_name if episode_name and frappe.db.exists("Pet Care Episode", episode_name) else None


def _episode_opened_by_visit_for_delete(visit):
	episode_name = _visit_episode_name(visit)
	if episode_name:
		episode = frappe.get_doc("Pet Care Episode", episode_name)
		if _episode_was_opened_by_visit(episode, visit):
			return episode

	if not visit.get("animal_patient"):
		return None
	episodes = frappe.get_all(
		"Pet Care Episode",
		filters={"pet": visit.animal_patient, "episode_status": ["in", list(ACTIVE_EPISODE_STATUSES)]},
		fields=["name", "opened_visit", "opened_from_name"],
		order_by="modified desc",
		ignore_permissions=True,
	)
	for row in episodes:
		if row.opened_visit == visit.name or row.opened_from_name == visit.name:
			return frappe.get_doc("Pet Care Episode", row.name)
	return None


def _episode_was_opened_by_visit(episode, visit) -> bool:
	return episode.get("opened_visit") == visit.name or episode.get("opened_from_name") == visit.name


def _normalize_case_choice(choice: str) -> str:
	choice = cstr(choice).strip().lower()
	if choice not in DOCTOR_CASE_CHOICES:
		frappe.throw(_("Doctor case choice must be one of: wellness, continue_case, new_case."))
	return choice


def _resolve_continue_episode(visit, episode: str | None, active_episode_name: str | None) -> str:
	target_episode_name = cstr(episode or active_episode_name).strip()
	if not target_episode_name:
		frappe.throw(_("There is no active care episode to continue."))
	if not frappe.db.exists("Pet Care Episode", target_episode_name):
		frappe.throw(_("Care episode {0} was not found.").format(frappe.bold(target_episode_name)))
	target = frappe.get_doc("Pet Care Episode", target_episode_name)
	if target.pet != visit.animal_patient:
		frappe.throw(_("Care episode {0} belongs to a different pet.").format(frappe.bold(target_episode_name)))
	if target.episode_status not in ACTIVE_EPISODE_STATUSES:
		frappe.throw(_("Care episode {0} is not active.").format(frappe.bold(target_episode_name)))
	return target.name


def _set_visit_episode(visit, episode_name: str | None):
	if not visit.meta.has_field("care_episode"):
		return
	visit.set("care_episode", episode_name)


def _delete_visit_care_plan_items(visit):
	if not frappe.db.exists("DocType", "Pet Care Plan Item"):
		return
	from pet_app.utils.care_plan_links import cancel_linked_plan_appointment

	for plan_name in frappe.get_all("Pet Care Plan Item", filters={"source_visit": visit.name}, pluck="name"):
		plan = frappe.get_doc("Pet Care Plan Item", plan_name)
		cancel_linked_plan_appointment(
			plan,
			reason=_("Care plan item removed when visit was switched to Wellness."),
			clear_plan_link=True,
		)
		frappe.delete_doc("Pet Care Plan Item", plan_name, ignore_permissions=True, force=True)


def _stamp_case_choice(visit, choice: str, note: str | None):
	if visit.meta.has_field("doctor_case_choice"):
		visit.set("doctor_case_choice", choice)
	if visit.meta.has_field("case_choice_by"):
		visit.set("case_choice_by", frappe.session.user)
	if visit.meta.has_field("case_choice_at"):
		visit.set("case_choice_at", now_datetime())
	if note is not None and visit.meta.has_field("case_choice_note"):
		visit.set("case_choice_note", note)


def _cancel_episode_opened_by_visit_choice(episode_name: str | None, visit):
	if not episode_name or not frappe.db.exists("Pet Care Episode", episode_name):
		return
	episode = frappe.get_doc("Pet Care Episode", episode_name)
	if episode.episode_status not in ACTIVE_EPISODE_STATUSES:
		return
	if episode.get("opened_visit") != visit.name and episode.get("opened_from_name") != visit.name:
		return
	other_visit_filters = {"care_episode": episode.name, "name": ["!=", visit.name]}
	if frappe.db.count("Vet Visit", other_visit_filters):
		return
	episode.episode_status = "Cancelled"
	if episode.meta.has_field("closure_reason"):
		episode.closure_reason = episode.closure_reason or _("Case choice changed to wellness.")
	if episode.meta.has_field("closed_on"):
		episode.closed_on = episode.closed_on or getdate(nowdate())
	if episode.meta.has_field("closed_by"):
		episode.closed_by = episode.closed_by or frappe.session.user
	episode.save(ignore_permissions=True)


def _visit_delete_episode_blockers(episode, visit) -> list[str]:
	blockers = []
	for fieldname, label in (
		("problems", _("episode problems")),
		("medications", _("episode medications")),
		("monitoring_items", _("episode monitoring items")),
	):
		if episode.get(fieldname):
			blockers.append(label)

	for fieldname, label in (
		("diagnoses", _("visit diagnoses")),
		("prescribed_medications", _("visit medications")),
		("orders", _("visit orders")),
		("care_services", _("visit care services")),
	):
		if visit.meta.has_field(fieldname) and visit.get(fieldname):
			blockers.append(label)

	if _doctype_count("Pet Care Plan Item", {"care_episode": episode.name}):
		blockers.append(_("care plan items"))
	elif _doctype_count("Pet Care Plan Item", {"source_visit": visit.name}):
		blockers.append(_("care plan items"))

	for doctype, label in (
		("Lab", _("lab orders")),
		("Imaging", _("imaging orders")),
		("Pet Procedure", _("procedures")),
		("PetCareService", _("care service records")),
		("Pet Boarding", _("boarding records")),
	):
		if _linked_visit_count(doctype, visit.name):
			blockers.append(label)

	return blockers


def _doctype_count(doctype: str, filters: dict) -> int:
	if not frappe.db.exists("DocType", doctype):
		return 0
	return frappe.db.count(doctype, filters)


def _linked_visit_count(doctype: str, visit_name: str) -> int:
	if not frappe.db.exists("DocType", doctype):
		return 0
	meta = frappe.get_meta(doctype)
	if not meta.has_field("visit"):
		return 0
	return frappe.db.count(doctype, {"visit": visit_name})


def _detach_visit_from_episode_links(episode, visit, replacement_visit: str | None = None):
	changed = False
	for fieldname, value in (
		("opened_visit", None),
		("current_visit", replacement_visit),
		("last_visit", replacement_visit),
	):
		if episode.meta.has_field(fieldname) and episode.get(fieldname) == visit.name:
			episode.set(fieldname, value)
			changed = True
	if (
		episode.meta.has_field("opened_from_name")
		and episode.get("opened_from_doctype") == "Vet Visit"
		and episode.get("opened_from_name") == visit.name
	):
		episode.opened_from_name = None
		changed = True
	if changed:
		episode.save(ignore_permissions=True)


def _clear_profile_visit_links_if_matches(visit, replacement_visit: str | None = None):
	profile_name = frappe.db.get_value("Pet Medical Profile", {"pet": visit.get("animal_patient")}, "name")
	if not profile_name:
		return
	profile = frappe.get_doc("Pet Medical Profile", profile_name)
	updates = {}
	for fieldname in ("current_visit", "last_visit"):
		if profile.meta.has_field(fieldname) and profile.get(fieldname) == visit.name:
			updates[fieldname] = replacement_visit
	if profile.meta.has_field("last_completed_visit") and profile.get("last_completed_visit") == visit.name:
		updates["last_completed_visit"] = None
	if (
		profile.meta.has_field("last_synced_from_name")
		and profile.get("last_synced_from_doctype") == "Vet Visit"
		and profile.get("last_synced_from_name") == visit.name
	):
		updates["last_synced_from_name"] = replacement_visit
		if replacement_visit is None and profile.meta.has_field("last_synced_from_doctype"):
			updates["last_synced_from_doctype"] = None
	for fieldname, value in updates.items():
		frappe.db.set_value("Pet Medical Profile", profile.name, fieldname, value, update_modified=False)


def _clear_profile_active_episode_if_matches(episode):
	profile_name = frappe.db.get_value("Pet Medical Profile", {"pet": episode.pet}, "name")
	if not profile_name:
		return
	if frappe.db.get_value("Pet Medical Profile", profile_name, "active_care_episode") == episode.name:
		frappe.db.set_value("Pet Medical Profile", profile_name, "active_care_episode", None, update_modified=False)


def _touch_episode_from_visit(episode, visit):
	changed = False
	for fieldname, value in {
		"current_visit": visit.name,
		"last_visit": visit.name,
		"chief_complaint": episode.get("chief_complaint") or _case_sheet_complaint(visit.get("case_sheet")),
	}.items():
		if value and episode.meta.has_field(fieldname) and episode.get(fieldname) != value:
			episode.set(fieldname, value)
			changed = True
	practitioner = visit_practitioner(visit)
	if practitioner and ensure_episode_practitioner(episode, practitioner):
		changed = True
	if episode.episode_status == "Open" and visit.get("status") == "In Progress":
		episode.episode_status = "Under Diagnosis"
		changed = True
	if changed:
		episode.save(ignore_permissions=True)


def _updates(doc, updates: dict, allow_none_fields: set[str] | None = None):
	allow_none_fields = allow_none_fields or set()
	changed = {}
	for fieldname, value in updates.items():
		if (value is None and fieldname not in allow_none_fields) or not doc.meta.has_field(fieldname):
			continue
		if cstr(doc.get(fieldname)) != cstr(value):
			changed[fieldname] = value
	if changed:
		frappe.db.set_value(doc.doctype, doc.name, changed, update_modified=False)
		for fieldname, value in changed.items():
			doc.set(fieldname, value)


def _clinical_status_for_visit(visit) -> str:
	if visit.get("status") == "Completed":
		return "Stable"
	if visit.get("status") == "Cancelled":
		return "No Active Case"
	if visit.get("status") == "In Progress":
		return "In Consultation"
	return "Waiting Doctor"


def _episode_type_from_visit(visit) -> str:
	if visit.get("visit_type") == "Emergency":
		return "Emergency"
	if visit.get("visit_type") == "Procedure":
		return "Surgery / Procedure"
	if visit.get("visit_type") == "Vaccination":
		return "Vaccination Course"
	if visit.get("visit_type") == "Follow-up":
		return "Follow-up Case"
	return "General Wellness"


def _case_sheet_complaint(case_sheet: str | None) -> str | None:
	if not case_sheet:
		return None
	return frappe.db.get_value("Vet Case Sheet", case_sheet, "chief_complaint")


def _primary_diagnosis(visit) -> str | None:
	for row in visit.get("diagnoses") or []:
		if row.get("is_primary"):
			return row.get("diagnosis_text") or row.get("disease")
	for row in visit.get("diagnoses") or []:
		return row.get("diagnosis_text") or row.get("disease")
	return None


def _medication_summary(visit) -> str:
	parts = []
	for row in visit.get("prescribed_medications") or []:
		parts.append(row.get("medication") or row.get("medication_item"))
	return ", ".join(part for part in parts if part)


def _sync_episode_medications(episode, visit):
	existing = {(row.get("source_visit"), row.get("medication_item"), row.get("medication")) for row in episode.get("medications") or []}
	for row in visit.get("prescribed_medications") or []:
		key = (visit.name, row.get("medication_item"), row.get("medication"))
		if key in existing:
			continue
		episode.append(
			"medications",
			{
				"medication": row.get("medication"),
				"medication_item": row.get("medication_item"),
				"dosage": row.get("dosage"),
				"frequency": row.get("frequency"),
				"duration_days": row.get("duration_days"),
				"instructions": row.get("instructions"),
				"status": "Prescribed",
				"source_visit": visit.name,
				"dispense_status": row.get("dispense_status"),
			},
		)


def _latest_vital_from_visit(visit) -> dict:
	rows = list(visit.get("vital_signs") or [])
	if not rows:
		return {}
	latest = max(rows, key=lambda row: (cstr(row.get("recorded_at")), row.idx or 0))
	return latest.as_dict() if hasattr(latest, "as_dict") else dict(latest)


def _priority(value) -> str:
	value = cstr(value).strip()
	if value in {"Low", "Normal", "Important", "Urgent", "Emergency", "Critical"}:
		return value
	return "Normal"


def _first_text(*values) -> str | None:
	for value in values:
		text = _text(value)
		if text:
			return text
	return None


def _text(value) -> str:
	return cstr(value).strip()
