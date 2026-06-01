import frappe
from frappe import _
from frappe.utils import flt, cint
from pet_app.api.workspace import (
    MANAGEMENT_ROLES, COORDINATOR_ROLES, SERVICE_PROVIDER_ROLES,
    get_user_roles
)
from pet_app.utils.practitioner import get_practitioner_for_user
from pet_app.api.response import standardize_response

MANAGER_ROLES = MANAGEMENT_ROLES | {"Service Reviewer"} | COORDINATOR_ROLES


@frappe.whitelist()
@standardize_response
def get_ratings(
    reference_doctype=None,
    reference_name=None,
    limit_start=0,
    limit_page_length=50,
):
    user = frappe.session.user
    roles = get_user_roles(user)
    is_manager = bool(roles & MANAGER_ROLES)
    is_provider = bool(roles & SERVICE_PROVIDER_ROLES)

    filters = {}
    if reference_doctype:
        filters["reference_doctype"] = reference_doctype
    if reference_name:
        filters["reference_name"] = reference_name

    if not is_manager and is_provider:
        practitioner = get_practitioner_for_user(user)
        if not practitioner:
            frappe.response["data"] = {"ratings": [], "stats": _empty_stats()}
            return
        my_services = frappe.get_all(
            "PetCareService",
            filters=[["provider", "=", practitioner], ["docstatus", "<", 2]],
            pluck="name",
            ignore_permissions=True,
        )
        if not my_services:
            frappe.response["data"] = {"ratings": [], "stats": _empty_stats()}
            return
        if reference_name and reference_name not in my_services:
            frappe.throw(_("Not permitted"), frappe.PermissionError)
        if not reference_name:
            filters["reference_name"] = ["in", my_services]

    elif not is_manager:
        filters["rated_by"] = user

    ratings = frappe.get_all(
        "Rating",
        filters=filters,
        fields=["name", "reference_doctype", "reference_name",
                "rated_by", "overall_rating", "notes",
                "questionnaire", "rated_at"],
        order_by="rated_at desc",
        limit_start=cint(limit_start),
        limit_page_length=cint(limit_page_length),
        ignore_permissions=True,
    )

    user_cache = {}
    for r in ratings:
        if is_manager:
            u = r["rated_by"]
            if u not in user_cache:
                user_cache[u] = frappe.db.get_value("User", u, "full_name") or u
            r["rated_by_name"] = user_cache[u]
        else:
            r["rated_by_name"] = None
            r["rated_by"] = None
        r["reference_title"] = _get_reference_title(r["reference_doctype"], r["reference_name"])

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


def _get_reference_title(doctype, name):
    if not doctype or not name:
        return name
    try:
        meta = frappe.get_meta(doctype)
        title_field = meta.title_field or "name"
        return frappe.db.get_value(doctype, name, title_field) or name
    except Exception:
        return name
