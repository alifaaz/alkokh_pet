import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, get_datetime
from pet_app.api.workspace import (
    MANAGEMENT_ROLES, COORDINATOR_ROLES, SERVICE_PROVIDER_ROLES,
    get_user_roles
)
from pet_app.utils.practitioner import get_practitioner_for_user
from pet_app.utils.rating_entities import (
    RATABLE_DOCTYPES,
    get_title,
    resolve_entity_name,
    resolve_pet_name,
)
from pet_app.api.response import standardize_response

MANAGER_ROLES = MANAGEMENT_ROLES | {"Service Reviewer"} | COORDINATOR_ROLES

ALLOWED_ORDER_BY = {
    "rated_at desc",
    "rated_at asc",
    "overall_rating desc",
    "overall_rating asc",
}


@frappe.whitelist()
@standardize_response
def get_ratings(
    reference_doctype=None,
    reference_name=None,
    overall_rating=None,
    performer_id=None,
    rater_id=None,
    from_date=None,
    to_date=None,
    order_by="rated_at desc",
    limit_start=0,
    limit_page_length=50,
):
    filters = []
    if reference_doctype:
        filters.append(["reference_doctype", "=", reference_doctype])
    if reference_name:
        filters.append(["reference_name", "=", reference_name])
    if overall_rating not in (None, ""):
        filters.append(["overall_rating", "=", cint(overall_rating)])
    if performer_id:
        filters.append(["performer_id", "=", performer_id])
    if rater_id:
        filters.append(["rated_by", "=", rater_id])
    _apply_date_range(filters, from_date, to_date)

    filters, is_manager = _apply_scope(filters, reference_name=reference_name)
    if filters is None:
        frappe.response["data"] = {"ratings": [], "stats": _empty_stats()}
        return

    if order_by not in ALLOWED_ORDER_BY:
        order_by = "rated_at desc"

    ratings = frappe.get_all(
        "Rating",
        filters=filters,
        fields=["name", "reference_doctype", "reference_name",
                "rated_by", "overall_rating", "notes",
                "questionnaire", "rated_at",
                "performer_id", "performer_name",
                "sentiment", "sentiment_score"],
        order_by=order_by,
        limit_start=cint(limit_start),
        limit_page_length=cint(limit_page_length),
        ignore_permissions=True,
    )

    tags_by_rating = _fetch_tags([r["name"] for r in ratings])
    user_cache = {}
    title_cache = {}
    entity_cache = {}
    pet_cache = {}

    for r in ratings:
        if is_manager:
            u = r["rated_by"]
            if u not in user_cache:
                user_cache[u] = frappe.db.get_value("User", u, "full_name") or u
            r["rated_by_name"] = user_cache[u]
        else:
            r["rated_by_name"] = None
            r["rated_by"] = None

        key = (r["reference_doctype"], r["reference_name"])
        if key not in title_cache:
            title_cache[key] = get_title(r["reference_doctype"], r["reference_name"])
            entity_cache[key] = resolve_entity_name(r["reference_doctype"], r["reference_name"])
            pet_cache[key] = resolve_pet_name(r["reference_doctype"], r["reference_name"])
        r["reference_title"] = title_cache[key]
        r["entity_name"] = entity_cache[key]
        r["pet_name"] = pet_cache[key]
        r["tags"] = tags_by_rating.get(r["name"], [])
        # Frappe coerces a null Float column to 0.0, so a stored score is
        # indistinguishable from "no notes". The score is only meaningful when a
        # sentiment was computed; null it out otherwise (spec: both null when no
        # notes).
        r["sentiment_score"] = flt(r["sentiment_score"]) if r.get("sentiment") else None

    all_ratings = frappe.get_all(
        "Rating",
        filters=filters,
        fields=["overall_rating"],
        ignore_permissions=True,
    )
    frappe.response["data"] = {
        "ratings": ratings,
        "stats": _calc_stats(all_ratings),
    }


@frappe.whitelist()
@standardize_response
def get_ratings_analytics(from_date=None, to_date=None, reference_doctype=None,
                          performer_id=None, rater_id=None):
    filters = []
    if reference_doctype:
        filters.append(["reference_doctype", "=", reference_doctype])
    if performer_id:
        filters.append(["performer_id", "=", performer_id])
    if rater_id:
        filters.append(["rated_by", "=", rater_id])
    _apply_date_range(filters, from_date, to_date)

    filters, _is_manager = _apply_scope(filters)
    if filters is None:
        frappe.response["data"] = _empty_analytics()
        return

    rows = frappe.get_all(
        "Rating",
        filters=filters,
        fields=["name", "reference_doctype", "reference_name", "overall_rating",
                "performer_id", "performer_name", "sentiment"],
        ignore_permissions=True,
    )

    frappe.response["data"] = {
        "stats": _calc_stats(rows),
        "by_type": _build_by_type(rows),
        "top_entities": _build_top_entities(rows),
        "providers": _build_providers(rows),
        "top_tags": _build_top_tags(rows),
        "sentiment": _build_sentiment(rows),
    }


@frappe.whitelist()
@standardize_response
def get_ratings_trend(from_date=None, to_date=None, bucket="month", reference_doctype=None,
                      performer_id=None, rater_id=None):
    bucket = (bucket or "month").lower()
    if bucket not in ("day", "week", "month"):
        bucket = "month"

    filters = []
    if reference_doctype:
        filters.append(["reference_doctype", "=", reference_doctype])
    if performer_id:
        filters.append(["performer_id", "=", performer_id])
    if rater_id:
        filters.append(["rated_by", "=", rater_id])
    _apply_date_range(filters, from_date, to_date)

    filters, _is_manager = _apply_scope(filters)
    if filters is None:
        frappe.response["data"] = {"buckets": []}
        return

    rows = frappe.get_all(
        "Rating",
        filters=filters,
        fields=["overall_rating", "rated_at"],
        ignore_permissions=True,
    )

    grouped = {}
    for r in rows:
        if not r.get("rated_at"):
            continue
        period = _bucket_label(get_datetime(r["rated_at"]), bucket)
        grouped.setdefault(period, []).append(r)

    buckets = []
    for period in sorted(grouped.keys()):
        items = grouped[period]
        stats = _calc_stats(items)
        buckets.append({
            "period": period,
            "count": stats["total"],
            "average": stats["average"],
            "distribution": stats["distribution"],
        })

    frappe.response["data"] = {"buckets": buckets}


# ─────────────────────────────────────────
# Scope / filter helpers
# ─────────────────────────────────────────

def _apply_scope(filters, reference_name=None):
    """Apply role-based row scoping shared by every endpoint.

    Returns ``(filters, is_manager)``. ``filters`` is ``None`` when the caller
    should short-circuit to an empty result set.
    """
    user = frappe.session.user
    roles = get_user_roles(user)
    is_manager = bool(roles & MANAGER_ROLES)
    is_provider = bool(roles & SERVICE_PROVIDER_ROLES)

    if is_manager:
        return filters, True

    if is_provider:
        practitioner = get_practitioner_for_user(user)
        if not practitioner:
            return None, is_manager
        my_services = frappe.get_all(
            "PetCareService",
            filters=[["provider", "=", practitioner], ["docstatus", "<", 2]],
            pluck="name",
            ignore_permissions=True,
        )
        if not my_services:
            return None, is_manager
        if reference_name and reference_name not in my_services:
            frappe.throw(_("Not permitted"), frappe.PermissionError)
        if not reference_name:
            filters.append(["reference_name", "in", my_services])
        return filters, is_manager

    filters.append(["rated_by", "=", user])
    return filters, is_manager


def _apply_date_range(filters, from_date, to_date):
    if from_date:
        filters.append(["rated_at", ">=", f"{getdate(from_date)} 00:00:00"])
    if to_date:
        filters.append(["rated_at", "<=", f"{getdate(to_date)} 23:59:59"])


def _fetch_tags(rating_names):
    if not rating_names:
        return {}
    rows = frappe.get_all(
        "Rating Tag Selection",
        filters={"parenttype": "Rating", "parent": ["in", rating_names]},
        fields=["parent", "tag_key", "tag_label"],
        order_by="idx asc",
        ignore_permissions=True,
    )
    grouped = {}
    for row in rows:
        grouped.setdefault(row["parent"], []).append(
            {"tag_key": row["tag_key"], "tag_label": row["tag_label"]}
        )
    return grouped


# ─────────────────────────────────────────
# Aggregation builders
# ─────────────────────────────────────────

def _avg(total_sum, count):
    return round(total_sum / count, 2) if count else 0.0


def _build_by_type(rows):
    agg = {dt: {"count": 0, "sum": 0} for dt in RATABLE_DOCTYPES}
    for r in rows:
        dt = r["reference_doctype"]
        bucket = agg.setdefault(dt, {"count": 0, "sum": 0})
        bucket["count"] += 1
        bucket["sum"] += cint(r["overall_rating"])

    result = []
    for dt in RATABLE_DOCTYPES:
        bucket = agg[dt]
        result.append({
            "reference_doctype": dt,
            "count": bucket["count"],
            "average": _avg(bucket["sum"], bucket["count"]),
        })
    # Include any non-standard doctypes that still carry ratings.
    for dt, bucket in agg.items():
        if dt not in RATABLE_DOCTYPES and bucket["count"]:
            result.append({
                "reference_doctype": dt,
                "count": bucket["count"],
                "average": _avg(bucket["sum"], bucket["count"]),
            })
    return result


def _build_top_entities(rows):
    agg = {}
    performer_names = {}
    for r in rows:
        dt = r["reference_doctype"]
        key = (dt, r["reference_name"])
        bucket = agg.setdefault(key, {"count": 0, "sum": 0})
        bucket["count"] += 1
        bucket["sum"] += cint(r["overall_rating"])
        # First non-empty performer_name wins, mirroring the prior linear scan.
        if key not in performer_names and r.get("performer_name"):
            performer_names[key] = r["performer_name"]

    by_doctype = {dt: [] for dt in RATABLE_DOCTYPES}
    for (dt, name), bucket in agg.items():
        by_doctype.setdefault(dt, []).append({
            "reference_name": name,
            "count": bucket["count"],
            "average": _avg(bucket["sum"], bucket["count"]),
        })

    result = {}
    for dt, entities in by_doctype.items():
        entities.sort(key=lambda e: (-e["average"], -e["count"]))
        top = entities[:5]
        for entity in top:
            entity["entity_name"] = resolve_entity_name(dt, entity["reference_name"])
            entity["performer_name"] = performer_names.get((dt, entity["reference_name"]))
            entity["pet_name"] = resolve_pet_name(dt, entity["reference_name"])
        result[dt] = top
    return result


def _build_providers(rows):
    agg = {}
    for r in rows:
        pid = r.get("performer_id")
        if not pid:
            continue
        bucket = agg.setdefault(pid, {
            "performer_name": r.get("performer_name"),
            "reviews": 0,
            "sum": 0,
            "entities": set(),
        })
        bucket["reviews"] += 1
        bucket["sum"] += cint(r["overall_rating"])
        bucket["entities"].add((r["reference_doctype"], r["reference_name"]))
        if not bucket["performer_name"] and r.get("performer_name"):
            bucket["performer_name"] = r["performer_name"]

    providers = []
    for pid, bucket in agg.items():
        providers.append({
            "performer_id": pid,
            "performer_name": bucket["performer_name"],
            "entities": len(bucket["entities"]),
            "reviews": bucket["reviews"],
            "average": _avg(bucket["sum"], bucket["reviews"]),
        })
    providers.sort(key=lambda p: (-p["average"], -p["reviews"]))
    return providers


def _build_top_tags(rows):
    if not rows:
        return []
    overall_by_rating = {r["name"]: cint(r["overall_rating"]) for r in rows}
    tag_rows = frappe.get_all(
        "Rating Tag Selection",
        filters={"parenttype": "Rating", "parent": ["in", list(overall_by_rating.keys())]},
        fields=["parent", "tag_key", "tag_label"],
        ignore_permissions=True,
    )

    agg = {}
    for row in tag_rows:
        key = row["tag_key"]
        bucket = agg.setdefault(key, {"tag_label": row.get("tag_label") or key, "count": 0, "sum": 0})
        bucket["count"] += 1
        bucket["sum"] += overall_by_rating.get(row["parent"], 0)
        if not bucket["tag_label"] and row.get("tag_label"):
            bucket["tag_label"] = row["tag_label"]

    tags = []
    for key, bucket in agg.items():
        tags.append({
            "tag_key": key,
            "tag_label": bucket["tag_label"],
            "count": bucket["count"],
            "avg_rating": _avg(bucket["sum"], bucket["count"]),
        })
    tags.sort(key=lambda t: (-t["count"], -t["avg_rating"]))
    return tags


def _build_sentiment(rows):
    sentiment = {"positive": 0, "neutral": 0, "negative": 0}
    for r in rows:
        value = r.get("sentiment")
        if value in sentiment:
            sentiment[value] += 1
    return sentiment


def _bucket_label(dt, bucket):
    if bucket == "day":
        return dt.strftime("%Y-%m-%d")
    if bucket == "week":
        iso = dt.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    return dt.strftime("%Y-%m")


# ─────────────────────────────────────────
# Stats helpers
# ─────────────────────────────────────────

def _calc_stats(ratings):
    total = len(ratings)
    avg = round(sum(flt(r["overall_rating"]) for r in ratings) / total, 1) if total else 0
    distribution = {str(i): 0 for i in range(1, 6)}
    for r in ratings:
        key = str(cint(r["overall_rating"]))
        if key in distribution:
            distribution[key] += 1
    return {"total": total, "average": avg, "distribution": distribution}


def _empty_stats():
    return {"total": 0, "average": 0, "distribution": {str(i): 0 for i in range(1, 6)}}


def _empty_analytics():
    return {
        "stats": _empty_stats(),
        "by_type": _build_by_type([]),
        "top_entities": {dt: [] for dt in RATABLE_DOCTYPES},
        "providers": [],
        "top_tags": [],
        "sentiment": {"positive": 0, "neutral": 0, "negative": 0},
    }
