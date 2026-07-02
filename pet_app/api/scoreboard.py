"""Personal activity scoreboard for the user profile page (`/profile`).

Powers the single **Scoreboard** tab: for one user (the session user by
default) it reports the records they created, the services they completed and
ratings they received as a provider, the ratings they gave as a rater, an
activity score and a time-bucketed trend -- all filterable by a date range.

Mirrors the style of ``pet_app.api.dashboard`` and
``pet_app.api.ratings.get_ratings_analytics``: lightweight ``frappe.get_all``
reads aggregated in Python, wrapped by ``@standardize_response`` so the payload
is delivered under the envelope ``data`` key.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, date_diff, flt, getdate, nowdate

from pet_app.api.permissions import get_user_roles, user_has_full_access
from pet_app.api.response import standardize_response
from pet_app.api.workspace import MANAGEMENT_ROLES
from pet_app.utils.practitioner import get_practitioner_for_user

# Doctypes counted under "records created", in stable display order. Each carries
# a human-readable group label (translated on the backend; the frontend can also
# map the stable ``doctype`` key). Non-existent doctypes are skipped gracefully.
RECORD_DOCTYPES = [
    ("PetCareService", "Care services"),
    ("Vet Case Sheet", "Case sheets"),
    ("Vet Visit", "Visits"),
    ("Pet Procedure", "Procedures"),
    ("Lab", "Labs"),
    ("Imaging", "Radiology"),
    ("Appointment", "Appointments"),
    ("Rating", "Ratings"),
]

DOCTYPE_LABELS = dict(RECORD_DOCTYPES)

# PetCareService rows in these statuses count as a completed service. The status
# field mixes lowercase and title-case values, so compare case-insensitively.
COMPLETED_SERVICE_STATUSES = {"completed"}

# A range wider than this (in days) is bucketed by month rather than by day.
DAY_BUCKET_MAX_SPAN = 92


@frappe.whitelist()
@standardize_response
def get_user_scoreboard(user_id=None, from_date=None, to_date=None):
    """Return the full scoreboard for ``user_id`` (default: the session user).

    A non-privileged caller may only report on themselves; a foreign ``user_id``
    from such a caller is ignored and replaced with the session user.
    """
    user = _resolve_target_user(user_id)
    from_date = getdate(from_date) if from_date else None
    to_date = getdate(to_date) if to_date else None

    practitioner = get_practitioner_for_user(user)
    is_provider = bool(practitioner)

    records_created, created_dates = _records_created(user, from_date, to_date)
    completed_dates = (
        _completed_service_dates(practitioner, from_date, to_date) if is_provider else []
    )
    provider = (
        _provider_section(practitioner, len(completed_dates), from_date, to_date)
        if is_provider
        else None
    )
    rater, given_dates = _rater_section(user, from_date, to_date)
    activity = _activity_section(user, created_dates, from_date, to_date)
    trend = _build_trend(created_dates, completed_dates, given_dates, from_date, to_date)

    frappe.response["data"] = {
        "identity": _identity(user, is_provider, rater is not None),
        "records_created": records_created,
        "provider": provider,
        "rater": rater,
        "activity": activity,
        "trend": trend,
    }


# ─────────────────────────────────────────
# Target resolution / identity
# ─────────────────────────────────────────

def _resolve_target_user(user_id):
    """Resolve the user to report on, enforcing cross-user view permission."""
    caller = frappe.session.user
    requested = (user_id or "").strip() if isinstance(user_id, str) else user_id

    if not requested or requested == caller:
        return caller

    if not _can_view_others(caller):
        # Silently fall back to the caller's own scoreboard (acceptance #5).
        return caller

    if not frappe.db.exists("User", requested):
        frappe.throw(_("User {0} was not found.").format(requested), frappe.DoesNotExistError)
    return requested


def _can_view_others(user):
    return user_has_full_access(user) or bool(get_user_roles(user) & MANAGEMENT_ROLES)


def _identity(user, is_provider, is_rater):
    fields = ["full_name", "user_image", "user_type"]
    info = frappe.db.get_value("User", user, fields, as_dict=True) or {}
    roles = sorted(r for r in get_user_roles(user) if r not in {"All", "Guest"})
    return {
        "user_id": user,
        "full_name": info.get("full_name") or user,
        "user_image": info.get("user_image"),
        "user_type": info.get("user_type"),
        "roles": roles,
        "is_service_provider": is_provider,
        "is_rater": is_rater,
    }


# ─────────────────────────────────────────
# Records created
# ─────────────────────────────────────────

def _records_created(user, from_date, to_date):
    """Per-doctype counts of docs owned by ``user``, plus every creation date.

    Returns ``(section, created_dates)`` where ``created_dates`` is the flat list
    of ``creation`` datetimes across all doctypes (one per record), reused for
    the activity and trend sections.
    """
    by_doctype = []
    created_dates = []
    for doctype, label in RECORD_DOCTYPES:
        if not _table_exists(doctype):
            continue
        filters = [["owner", "=", user]] + _date_filters("creation", from_date, to_date)
        rows = frappe.get_all(doctype, filters=filters, pluck="creation", ignore_permissions=True)
        if not rows:
            continue
        created_dates.extend(rows)
        by_doctype.append({"doctype": doctype, "label": _(label), "count": len(rows)})

    by_doctype.sort(key=lambda d: -d["count"])
    return {"total": len(created_dates), "by_doctype": by_doctype}, created_dates


# ─────────────────────────────────────────
# Provider: services completed + ratings received
# ─────────────────────────────────────────

def _completed_service_dates(practitioner, from_date, to_date):
    """Effective completion datetimes of this provider's completed services.

    Date basis is ``end_date`` with a ``modified`` fallback (the spec's
    ``performed_at`` does not exist on PetCareService). The range is applied to
    that effective timestamp in Python so the fallback is honoured.
    """
    rows = frappe.get_all(
        "PetCareService",
        filters=[["provider", "=", practitioner], ["docstatus", "<", 2]],
        fields=["status", "end_date", "modified"],
        ignore_permissions=True,
    )
    dates = []
    for row in rows:
        if (row.get("status") or "").lower() not in COMPLETED_SERVICE_STATUSES:
            continue
        ts = row.get("end_date") or row.get("modified")
        if _in_range(ts, from_date, to_date):
            dates.append(ts)
    return dates


def _provider_section(practitioner, services_completed, from_date, to_date):
    rows = frappe.get_all(
        "Rating",
        filters=[["performer_id", "=", practitioner]] + _date_filters("rated_at", from_date, to_date),
        fields=["overall_rating"],
        ignore_permissions=True,
    )
    reviews = len(rows)
    stars = sum(cint(r["overall_rating"]) for r in rows)
    distribution = {str(i): 0 for i in range(1, 6)}
    for r in rows:
        key = str(cint(r["overall_rating"]))
        if key in distribution:
            distribution[key] += 1

    return {
        "services_completed": services_completed,
        "stars_received": stars,
        "reviews_received": reviews,
        "average_received": round(stars / reviews, 2) if reviews else None,
        "distribution": distribution,
    }


# ─────────────────────────────────────────
# Rater: ratings given
# ─────────────────────────────────────────

def _rater_section(user, from_date, to_date):
    """Ratings the user authored. Returns ``(section_or_None, given_dates)``.

    ``None`` when the user has given no ratings in range -> identity.is_rater
    becomes false.
    """
    rows = frappe.get_all(
        "Rating",
        filters=[["rated_by", "=", user]] + _date_filters("rated_at", from_date, to_date),
        fields=["overall_rating", "reference_doctype", "rated_at"],
        ignore_permissions=True,
    )
    if not rows:
        return None, []

    total = len(rows)
    rating_sum = sum(cint(r["overall_rating"]) for r in rows)

    counts = {}
    for r in rows:
        dt = r["reference_doctype"]
        counts[dt] = counts.get(dt, 0) + 1
    by_type = [
        {"doctype": dt, "label": _(DOCTYPE_LABELS.get(dt, dt)), "count": count}
        for dt, count in sorted(counts.items(), key=lambda kv: -kv[1])
    ]

    section = {
        "ratings_given": total,
        "average_given": round(rating_sum / total, 2) if total else None,
        "by_type": by_type,
    }
    return section, [r["rated_at"] for r in rows]


# ─────────────────────────────────────────
# Activity score (active_days basis)
# ─────────────────────────────────────────

def _activity_section(user, created_dates, from_date, to_date):
    """Distinct days active / days in range -> percent.

    "Active" = created OR modified at least one document that day. Created days
    are reused from the records-created scan; modified days are fetched here.
    """
    active = {getdate(d) for d in created_dates}
    for doctype, _label in RECORD_DOCTYPES:
        if not _table_exists(doctype):
            continue
        filters = [["modified_by", "=", user]] + _date_filters("modified", from_date, to_date)
        for ts in frappe.get_all(doctype, filters=filters, pluck="modified", ignore_permissions=True):
            active.add(getdate(ts))

    eff_to = to_date or getdate(nowdate())
    if from_date:
        eff_from = from_date
    elif active:
        eff_from = min(active)
    else:
        eff_from = eff_to

    total_days = max(date_diff(eff_to, eff_from) + 1, 0)
    active_days = len(active)
    if total_days > 0:
        percent = max(0, min(100, round(active_days / total_days * 100)))
    else:
        percent = 0

    return {
        "percent": percent,
        "active_days": active_days,
        "total_days": total_days,
        "basis": "active_days",
    }


# ─────────────────────────────────────────
# Trend
# ─────────────────────────────────────────

def _build_trend(created_dates, completed_dates, given_dates, from_date, to_date):
    all_dates = created_dates + completed_dates + given_dates
    if not all_dates:
        return []

    eff_from = from_date or min(getdate(d) for d in all_dates)
    eff_to = to_date or getdate(nowdate())
    granularity = "day" if date_diff(eff_to, eff_from) <= DAY_BUCKET_MAX_SPAN else "month"

    buckets = {}

    def add(dates, key):
        for d in dates:
            period = _bucket_label(getdate(d), granularity)
            bucket = buckets.setdefault(
                period,
                {"period": period, "records_created": 0, "services_completed": 0, "ratings_given": 0},
            )
            bucket[key] += 1

    add(created_dates, "records_created")
    add(completed_dates, "services_completed")
    add(given_dates, "ratings_given")

    return [buckets[period] for period in sorted(buckets)]


def _bucket_label(d, granularity):
    return d.strftime("%Y-%m-%d") if granularity == "day" else d.strftime("%Y-%m")


# ─────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────

def _date_filters(field, from_date, to_date):
    filters = []
    if from_date:
        filters.append([field, ">=", f"{from_date} 00:00:00"])
    if to_date:
        filters.append([field, "<=", f"{to_date} 23:59:59"])
    return filters


def _in_range(ts, from_date, to_date):
    if ts is None:
        return False
    day = getdate(ts)
    if from_date and day < from_date:
        return False
    if to_date and day > to_date:
        return False
    return True


def _table_exists(doctype):
    try:
        return bool(frappe.db.table_exists(doctype))
    except Exception:
        return False
