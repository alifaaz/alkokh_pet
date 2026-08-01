from __future__ import annotations

import datetime
import re
from collections import OrderedDict

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, getdate

from pet_app.api.dashboard import _require_analytics_access
from pet_app.api.permissions import get_user_roles
from pet_app.api.scoreboard import RECORD_DOCTYPES as SCOREBOARD_RECORD_DOCTYPES


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

COMPLETED_SERVICE_STATUSES = {"completed", "closed", "done"}
COMPLETED_VISIT_STATUSES = {"completed", "closed", "done"}
COMPLETED_PROCEDURE_STATUSES = {"completed", "closed", "done"}
COMPLETED_LAB_STATUSES = {"released", "result entered", "resulted"}
COMPLETED_IMAGING_STATUSES = {"released", "reported", "result entered"}

SYSTEM_FIELDS = {
	"name",
	"owner",
	"creation",
	"modified",
	"modified_by",
	"docstatus",
	"parent",
	"parenttype",
	"parentfield",
	"idx",
}

CLINICAL_CONFIGS = (
	{
		"doctype": "Vet Visit",
		"practitioner_fields": ("doctor",),
		"status_field": "status",
		"completed_statuses": COMPLETED_VISIT_STATUSES,
		"date_fields": ("completed_at", "closed_at", "modified"),
	},
	{
		"doctype": "Pet Procedure",
		"practitioner_fields": ("doctor", "provider"),
		"status_field": "status",
		"completed_statuses": COMPLETED_PROCEDURE_STATUSES,
		"date_fields": ("completed_at", "closed_at", "modified"),
	},
	{
		"doctype": "Lab",
		"practitioner_fields": ("doctor",),
		"status_field": "status",
		"completed_statuses": COMPLETED_LAB_STATUSES,
		"date_fields": ("released_at", "result_entered_at", "modified"),
	},
	{
		"doctype": "Imaging",
		"practitioner_fields": ("doctor",),
		"status_field": "status",
		"completed_statuses": COMPLETED_IMAGING_STATUSES,
		"date_fields": ("released_at", "result_entered_at", "modified"),
	},
)

STAFF_GROUPS = OrderedDict(
	(
		("coordinators", {"label": "Coordinator", "roles": {"Coordinator", "Coordinatorr"}}),
		("cashiers", {"label": "Cashier", "roles": {"Cashier", "POS Cashier"}}),
		("receptionists", {"label": "Receptionist", "roles": {"Receptionist", "Reception"}}),
		("other_staff", {"label": "Other", "roles": set()}),
	)
)

STAFF_EXCLUDED_ROLES = {
	"All",
	"Guest",
	"Administrator",
	"System Manager",
}

STAFF_EXCLUDED_USERS = {"Administrator", "Guest"}

STAFF_RECORD_DOCTYPES = tuple(SCOREBOARD_RECORD_DOCTYPES)


@frappe.whitelist()
def get_employee_month_dashboard(date_from=None, date_to=None):
	"""Return Employee of the Month leaderboards for the Report Engine."""
	_require_analytics_access()
	df, dt = _parse_required_range(date_from, date_to)
	prev_from, prev_to = _previous_range(df, dt)

	practitioners = _active_practitioners()
	clinical_groups = {
		"service_providers": _safe_group(
			lambda: _build_service_providers(practitioners, df, dt, prev_from, prev_to)
		),
		"doctors": _safe_group(lambda: _build_doctors(practitioners, df, dt, prev_from, prev_to)),
	}
	clinical_user_ids = _clinical_group_user_ids(practitioners, clinical_groups)
	staff_groups = _safe_staff_groups(clinical_user_ids, df, dt, prev_from, prev_to)
	return {
		**clinical_groups,
		**staff_groups,
	}


def _build_service_providers(practitioners, df, dt, prev_from, prev_to):
	candidate_ids = _service_provider_candidates(practitioners)
	current = _service_completed_stats(candidate_ids, df, dt)
	previous = _service_completed_stats(candidate_ids, prev_from, prev_to)
	ratings = _rating_stats(candidate_ids, df, dt)

	rows = []
	for practitioner_id in sorted(candidate_ids):
		completed = cint(current.get(practitioner_id, {}).get("completed_count"))
		if completed <= 0:
			continue
		practitioner = practitioners.get(practitioner_id)
		if not practitioner:
			continue
		rating = ratings.get(practitioner_id, {})
		rows.append(
			{
				**_base_row(practitioner, "Completed services"),
				"completed_count": completed,
				"rating_average": rating.get("rating_average"),
				"rating_count": cint(rating.get("rating_count")),
				"on_time_rate": current.get(practitioner_id, {}).get("on_time_rate"),
				"score": 0,
				"delta": _delta_text(completed, previous.get(practitioner_id, {}).get("completed_count", 0)),
				"badges": [],
			}
		)

	return _rank_group(rows, doctor_group=False)


def _build_doctors(practitioners, df, dt, prev_from, prev_to):
	candidate_ids = {
		practitioner_id
		for practitioner_id, practitioner in practitioners.items()
		if cstr(practitioner.get("practitioner_type")).strip().lower() == "doctor"
	}
	current = _clinical_completed_counts(candidate_ids, df, dt)
	previous = _clinical_completed_counts(candidate_ids, prev_from, prev_to)
	ratings = _rating_stats(candidate_ids, df, dt)

	rows = []
	for practitioner_id in sorted(candidate_ids):
		completed = cint(current.get(practitioner_id))
		if completed <= 0:
			continue
		practitioner = practitioners.get(practitioner_id)
		if not practitioner:
			continue
		rating = ratings.get(practitioner_id, {})
		rows.append(
			{
				**_base_row(practitioner, "Completed clinical records"),
				"completed_count": completed,
				"rating_average": rating.get("rating_average"),
				"rating_count": cint(rating.get("rating_count")),
				"on_time_rate": None,
				"score": 0,
				"delta": _delta_text(completed, previous.get(practitioner_id, 0)),
				"badges": [],
			}
		)

	return _rank_group(rows, doctor_group=True)


def _active_practitioners():
	if not _doctype_exists("Healthcare Practitioner"):
		return {}

	fields = _existing_fields(
		"Healthcare Practitioner",
		["name", "practitioner_name", "practitioner_type", "specialization", "photo", "user_id", "disabled"],
	)
	filters = [["disabled", "=", 0]] if _has_field("Healthcare Practitioner", "disabled") else []
	try:
		rows = frappe.get_all(
			"Healthcare Practitioner",
			filters=filters,
			fields=fields,
			ignore_permissions=True,
		)
	except Exception:
		return {}

	practitioners = OrderedDict()
	for row in rows:
		practitioner_id = row.get("name")
		if not practitioner_id:
			continue
		practitioners[practitioner_id] = {
			"practitioner_id": practitioner_id,
			"name": row.get("practitioner_name") or practitioner_id,
			"image": _public_image_url(row.get("photo") or _user_image(row.get("user_id"))),
			"practitioner_type": row.get("practitioner_type"),
			"specialization": row.get("specialization"),
			"user_id": row.get("user_id"),
		}
	return practitioners


def _user_image(user_id):
	if not user_id or not _doctype_exists("User"):
		return None
	try:
		return frappe.db.get_value("User", user_id, "user_image")
	except Exception:
		return None


def _public_image_url(url):
	text = cstr(url).strip()
	if not text:
		return None
	if "/private/files/" in text or text.startswith("private/files/"):
		return None
	return text


def _service_provider_candidates(practitioners):
	candidates = {
		practitioner_id
		for practitioner_id, practitioner in practitioners.items()
		if cstr(practitioner.get("practitioner_type")).strip().lower() != "doctor"
	}
	if not _doctype_exists("PetCareService") or not _has_field("PetCareService", "provider"):
		return candidates

	try:
		rows = frappe.get_all(
			"PetCareService",
			filters=_base_filters("PetCareService"),
			fields=_existing_fields("PetCareService", ["provider"]),
			ignore_permissions=True,
		)
	except Exception:
		rows = []

	for row in rows:
		provider = row.get("provider")
		if provider in practitioners:
			candidates.add(provider)
	return candidates


def _service_completed_stats(candidate_ids, df, dt):
	stats = {
		practitioner_id: {"completed_count": 0, "on_time_count": 0, "with_due_count": 0}
		for practitioner_id in candidate_ids
	}
	if not candidate_ids or not _doctype_exists("PetCareService") or not _has_field("PetCareService", "provider"):
		return _finalize_service_stats(stats)

	date_fields = tuple(field for field in ("end_date", "performed_at", "modified") if _has_field("PetCareService", field))
	fields = _existing_fields(
		"PetCareService",
		["provider", "status", "due_date", *date_fields],
	)
	try:
		rows = frappe.get_all(
			"PetCareService",
			filters=_base_filters("PetCareService") + [["provider", "in", list(candidate_ids)]],
			fields=fields,
			ignore_permissions=True,
		)
	except Exception:
		return _finalize_service_stats(stats)

	for row in rows:
		provider = row.get("provider")
		if provider not in stats:
			continue
		if not _status_in(row.get("status"), COMPLETED_SERVICE_STATUSES):
			continue
		completed_at = _first_value(row, date_fields)
		if not _in_range(completed_at, df, dt):
			continue

		stats[provider]["completed_count"] += 1
		if row.get("due_date"):
			stats[provider]["with_due_count"] += 1
			if getdate(completed_at) <= getdate(row.get("due_date")):
				stats[provider]["on_time_count"] += 1

	return _finalize_service_stats(stats)


def _finalize_service_stats(stats):
	for data in stats.values():
		with_due = cint(data.pop("with_due_count", 0))
		on_time = cint(data.pop("on_time_count", 0))
		data["on_time_rate"] = int(round(on_time / with_due * 100)) if with_due else None
	return stats


def _clinical_completed_counts(candidate_ids, df, dt):
	counts = {practitioner_id: 0 for practitioner_id in candidate_ids}
	seen_by_practitioner = {practitioner_id: set() for practitioner_id in candidate_ids}
	if not candidate_ids:
		return counts

	for config in CLINICAL_CONFIGS:
		doctype = config["doctype"]
		if not _doctype_exists(doctype):
			continue
		status_field = config["status_field"]
		if not _has_field(doctype, status_field):
			continue

		date_fields = tuple(field for field in config["date_fields"] if _has_field(doctype, field))
		practitioner_fields = tuple(
			field for field in config["practitioner_fields"] if _has_field(doctype, field)
		)
		if not practitioner_fields or not date_fields:
			continue

		fields = _existing_fields(doctype, [status_field, *date_fields, *practitioner_fields])
		for practitioner_field in practitioner_fields:
			try:
				rows = frappe.get_all(
					doctype,
					filters=_base_filters(doctype) + [[practitioner_field, "in", list(candidate_ids)]],
					fields=fields,
					ignore_permissions=True,
				)
			except Exception:
				continue
			for row in rows:
				if not _status_in(row.get(status_field), config["completed_statuses"]):
					continue
				completed_at = _first_value(row, date_fields)
				if not _in_range(completed_at, df, dt):
					continue
				practitioner_id = row.get(practitioner_field)
				if practitioner_id not in seen_by_practitioner:
					continue
				key = (doctype, row.get("name"))
				if key in seen_by_practitioner[practitioner_id]:
					continue
				seen_by_practitioner[practitioner_id].add(key)
				counts[practitioner_id] += 1

	return counts


def _rating_stats(practitioner_ids, df, dt):
	if not practitioner_ids or not _doctype_exists("Rating"):
		return {}
	required = ("performer_id", "overall_rating", "rated_at")
	if any(not _has_field("Rating", field) for field in required):
		return {}

	try:
		rows = frappe.get_all(
			"Rating",
			filters=_base_filters("Rating")
			+ [
				["performer_id", "in", list(practitioner_ids)],
				["rated_at", ">=", f"{df} 00:00:00"],
				["rated_at", "<=", f"{dt} 23:59:59"],
			],
			fields=_existing_fields("Rating", required),
			ignore_permissions=True,
		)
	except Exception:
		return {}

	stats = {}
	for row in rows:
		performer_id = row.get("performer_id")
		if performer_id not in practitioner_ids or row.get("overall_rating") in (None, ""):
			continue
		bucket = stats.setdefault(performer_id, {"sum": 0.0, "count": 0})
		bucket["sum"] += flt(row.get("overall_rating"))
		bucket["count"] += 1

	return {
		practitioner_id: {
			"rating_average": round(bucket["sum"] / bucket["count"], 1) if bucket["count"] else None,
			"rating_count": bucket["count"],
		}
		for practitioner_id, bucket in stats.items()
	}


def _clinical_group_user_ids(practitioners, clinical_groups):
	user_ids = set()
	for group_key in ("service_providers", "doctors"):
		for row in clinical_groups.get(group_key, {}).get("leaderboard") or []:
			practitioner = practitioners.get(row.get("practitioner_id")) or {}
			user_id = practitioner.get("user_id")
			if user_id:
				user_ids.add(user_id)
	return user_ids


def _safe_staff_groups(clinical_user_ids, df, dt, prev_from, prev_to):
	try:
		return _build_staff_groups(clinical_user_ids, df, dt, prev_from, prev_to)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "employee-month-dashboard-staff")
		return {group_key: _empty_group() for group_key in STAFF_GROUPS}


def _build_staff_groups(clinical_user_ids, df, dt, prev_from, prev_to):
	users = _staff_users(clinical_user_ids)
	current = _staff_activity_counts(list(users), df, dt)
	previous = _staff_activity_counts(list(users), prev_from, prev_to)

	groups = {group_key: [] for group_key in STAFF_GROUPS}
	for user, user_info in users.items():
		group_key = user_info.get("group_key")
		if group_key in groups:
			groups[group_key].append(user)

	return {
		group_key: _build_staff_group(group_key, target_users, users, current, previous)
		for group_key, target_users in groups.items()
	}


def _build_staff_group(group_key, target_users, users, current, previous):
	if not target_users:
		return _empty_group()

	rows = []
	for user in sorted(target_users):
		activity = current.get(user) or {"total": 0, "by_doctype": []}
		completed = cint(activity.get("total"))
		if completed <= 0:
			continue
		user_info = users[user]
		rows.append(
			{
				"rank": 0,
				"practitioner_id": user,
				"name": user_info.get("name") or user,
				"image": _public_image_url(user_info.get("image")),
				"practitioner_type": user_info.get("label") or STAFF_GROUPS[group_key]["label"],
				"specialization": None,
				"completed_count": completed,
				"primary_metric_label": "Documents created",
				"rating_average": None,
				"rating_count": 0,
				"on_time_rate": None,
				"score": 0,
				"delta": _delta_text(completed, (previous.get(user) or {}).get("total", 0)),
				"badges": [],
				"by_doctype": activity.get("by_doctype") or [],
			}
		)

	return _rank_staff_group(rows)


def _staff_users(clinical_user_ids):
	if not _doctype_exists("User"):
		return {}

	try:
		rows = frappe.get_all(
			"User",
			filters=[["enabled", "=", 1], ["user_type", "=", "System User"]],
			fields=["name", "full_name", "user_image", "enabled", "user_type"],
			ignore_permissions=True,
		)
	except Exception:
		return {}

	roles_by_user = _roles_for_users([row.get("name") for row in rows if row.get("name")])
	users = OrderedDict()
	for row in rows:
		user = row.get("name")
		if not user or user in STAFF_EXCLUDED_USERS or user in clinical_user_ids:
			continue
		roles = roles_by_user.get(user, set())
		group_key, label = _staff_group_for_roles(roles)
		if not group_key:
			continue
		users[user] = {
			"user": user,
			"name": row.get("full_name") or user,
			"image": row.get("user_image"),
			"roles": roles,
			"group_key": group_key,
			"label": label,
		}

	return users


def _roles_for_users(users):
	roles_by_user = {}
	for user in users or []:
		if not user:
			continue
		try:
			roles_by_user[user] = set(get_user_roles(user) or [])
		except Exception:
			roles_by_user[user] = set()
	return roles_by_user


def _staff_group_for_roles(roles):
	roles = set(roles or [])
	for group_key, config in STAFF_GROUPS.items():
		if group_key == "other_staff":
			continue
		if roles & config["roles"]:
			return group_key, config["label"]
	if roles and not roles <= STAFF_EXCLUDED_ROLES:
		return "other_staff", STAFF_GROUPS["other_staff"]["label"]
	return None, None


def _staff_activity_counts(users, df, dt):
	stats = {user: {"total": 0, "by_doctype": []} for user in users}
	if not users:
		return stats

	for doctype, label in STAFF_RECORD_DOCTYPES:
		if not _doctype_exists(doctype):
			continue
		for user in users:
			count = _staff_doctype_activity_count(doctype, user, df, dt)
			if count <= 0:
				continue
			stats[user]["total"] += count
			stats[user]["by_doctype"].append({"doctype": doctype, "label": _(label), "count": count})

	for data in stats.values():
		data["by_doctype"].sort(key=lambda row: (-cint(row.get("count")), cstr(row.get("label")).lower()))
	return stats


def _staff_doctype_activity_count(doctype, user, df, dt):
	seen = set()
	filters = [["owner", "=", user]] + _datetime_filters("creation", df, dt)
	try:
		created = frappe.get_all(doctype, filters=filters, pluck="name", ignore_permissions=True)
	except Exception:
		created = []
	seen.update(name for name in created if name)

	if _is_submittable(doctype):
		try:
			submitted = frappe.get_all(
				doctype,
				filters=[
					["docstatus", "=", 1],
					["modified_by", "=", user],
					* _datetime_filters("modified", df, dt),
				],
				pluck="name",
				ignore_permissions=True,
			)
		except Exception:
			submitted = []
		seen.update(name for name in submitted if name)

	return len(seen)


def _is_submittable(doctype):
	try:
		return bool(cint(getattr(frappe.get_meta(doctype), "is_submittable", 0)))
	except Exception:
		return False


def _rank_staff_group(rows):
	if not rows:
		return _empty_group()

	max_completed = max(cint(row.get("completed_count")) for row in rows) or 0
	for row in rows:
		row["score"] = int(round(cint(row.get("completed_count")) / max_completed * 100)) if max_completed else 0

	rows.sort(key=lambda row: (-cint(row.get("completed_count")), cstr(row.get("name")).lower()))
	for index, row in enumerate(rows, start=1):
		row["rank"] = index

	return {
		"winner": rows[0] if rows else None,
		"summary": {
			"total_people": len(rows),
			"total_completed": sum(cint(row.get("completed_count")) for row in rows),
			"average_rating": None,
			"top_score": max((cint(row.get("score")) for row in rows), default=0),
		},
		"leaderboard": rows,
	}


def _rank_group(rows, doctor_group):
	if not rows:
		return _empty_group()

	max_completed = max(cint(row.get("completed_count")) for row in rows) or 1
	max_rating = max((flt(row["rating_average"]) for row in rows if row.get("rating_average") is not None), default=None)
	max_on_time = max((cint(row["on_time_rate"]) for row in rows if row.get("on_time_rate") is not None), default=None)

	for row in rows:
		row["score"] = _performance_score(row, max_completed)

	rows.sort(key=_ranking_key)

	for index, row in enumerate(rows, start=1):
		row["rank"] = index
		row["badges"] = _badges(row, doctor_group, max_completed, max_rating, max_on_time)

	return {
		"winner": rows[0] if rows else None,
		"summary": _summary(rows),
		"leaderboard": rows,
	}


def _performance_score(row, max_completed):
	completed_component = (cint(row.get("completed_count")) / max_completed * 100) if max_completed else 0
	rating_average = row.get("rating_average")
	rating_component = (flt(rating_average) / 5 * 100) if rating_average is not None else 0
	on_time_rate = row.get("on_time_rate")
	on_time_component = cint(on_time_rate) if on_time_rate is not None else 0
	return max(0, min(100, int(round(completed_component * 0.7 + rating_component * 0.2 + on_time_component * 0.1))))


def _ranking_key(row):
	rating_sort = -flt(row.get("rating_average")) if row.get("rating_average") is not None else 1
	on_time_sort = -cint(row.get("on_time_rate")) if row.get("on_time_rate") is not None else 1
	return (
		-cint(row.get("completed_count")),
		rating_sort,
		on_time_sort,
		cstr(row.get("name")).lower(),
	)


def _badges(row, doctor_group, max_completed, max_rating, max_on_time):
	badges = []
	if doctor_group:
		if row.get("rank") == 1:
			badges.append("Clinical leader")
		if row.get("rating_average") is not None and max_rating is not None and flt(row["rating_average"]) == max_rating:
			badges.append("Top rated")
		return badges

	if cint(row.get("completed_count")) == max_completed:
		badges.append("Most services")
	if row.get("rating_average") is not None and max_rating is not None and flt(row["rating_average"]) == max_rating:
		badges.append("Top rated")
	if row.get("on_time_rate") is not None and max_on_time is not None and cint(row["on_time_rate"]) == max_on_time:
		badges.append("On-time leader")
	return badges


def _summary(rows):
	total_completed = sum(cint(row.get("completed_count")) for row in rows)
	rating_weight = sum(cint(row.get("rating_count")) for row in rows)
	if rating_weight:
		average_rating = round(
			sum(flt(row.get("rating_average")) * cint(row.get("rating_count")) for row in rows) / rating_weight,
			1,
		)
	else:
		average_rating = None
	return {
		"total_people": len(rows),
		"total_completed": total_completed,
		"average_rating": average_rating,
		"top_score": max((cint(row.get("score")) for row in rows), default=0),
	}


def _base_row(practitioner, primary_metric_label):
	return {
		"rank": 0,
		"practitioner_id": practitioner.get("practitioner_id"),
		"name": practitioner.get("name"),
		"image": practitioner.get("image"),
		"practitioner_type": practitioner.get("practitioner_type"),
		"specialization": practitioner.get("specialization"),
		"completed_count": 0,
		"primary_metric_label": primary_metric_label,
		"rating_average": None,
		"rating_count": 0,
		"on_time_rate": None,
		"score": 0,
		"delta": None,
		"badges": [],
	}


def _empty_group():
	return {
		"winner": None,
		"summary": {
			"total_people": 0,
			"total_completed": 0,
			"average_rating": None,
			"top_score": 0,
		},
		"leaderboard": [],
	}


def _safe_group(fn):
	try:
		return fn()
	except Exception:
		frappe.log_error(frappe.get_traceback(), "employee-month-dashboard")
		return _empty_group()


def _parse_required_range(date_from, date_to):
	df = _parse_date_arg("date_from", date_from)
	dt = _parse_date_arg("date_to", date_to)
	if df > dt:
		frappe.throw(_("date_from cannot be after date_to."))
	return df, dt


def _parse_date_arg(label, value):
	text = cstr(value).strip()
	if not text:
		frappe.throw(_("{0} is required.").format(label))
	if not DATE_RE.match(text):
		frappe.throw(_("{0} must use YYYY-MM-DD format.").format(label))
	try:
		return datetime.date.fromisoformat(text)
	except ValueError:
		frappe.throw(_("{0} must be a valid date.").format(label))


def _previous_range(df, dt):
	length = (dt - df).days
	prev_to = df - datetime.timedelta(days=1)
	prev_from = prev_to - datetime.timedelta(days=length)
	return prev_from, prev_to


def _delta_text(current, previous):
	diff = cint(current) - cint(previous)
	if diff > 0:
		return f"+{diff} vs previous period"
	if diff < 0:
		return f"{diff} vs previous period"
	return "0 vs previous period"


def _first_value(row, fields):
	for field in fields:
		if row.get(field):
			return row.get(field)
	return None


def _in_range(value, df, dt):
	if not value:
		return False
	try:
		day = getdate(value)
	except Exception:
		return False
	return df <= day <= dt


def _status_in(value, statuses):
	return cstr(value).strip().lower() in statuses


def _base_filters(doctype):
	return [["docstatus", "<", 2]] if _has_field(doctype, "docstatus") else []


def _datetime_filters(field, df, dt):
	return [[field, ">=", f"{df} 00:00:00"], [field, "<=", f"{dt} 23:59:59"]]


def _existing_fields(doctype, fields):
	existing = []
	for field in fields:
		if field and field not in existing and _has_field(doctype, field):
			existing.append(field)
	if "name" not in existing:
		existing.insert(0, "name")
	return existing


def _has_field(doctype, fieldname):
	if not fieldname:
		return False
	if fieldname in SYSTEM_FIELDS:
		return True
	try:
		return bool(frappe.get_meta(doctype).has_field(fieldname))
	except Exception:
		return False


def _doctype_exists(doctype):
	try:
		return bool(frappe.db.table_exists(doctype))
	except Exception:
		return False
