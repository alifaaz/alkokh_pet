"""Admin/read-only user performance profile dashboard.

Powers ``/users/:id/profile`` through the whitelisted method
``pet_app.api.user_performance.get_user_profile_dashboard``.  The endpoint keeps
the payload deliberately plain (no standard response envelope) because the
frontend already unwraps several possible API shapes.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime

import frappe
from frappe import _
from frappe.utils import cint, cstr, date_diff, get_datetime, getdate, now_datetime, nowdate

from pet_app.api.permissions import get_user_role_profiles, get_user_roles
from pet_app.api.workspace import SERVICE_PROVIDER_ROLES
from pet_app.utils.practitioner import get_practitioner_for_user
from pet_app.utils.rating_entities import get_title, resolve_entity_name


USER_ADMIN_ROLES = {"Administrator", "Users", "System Manager", "Pet App Admin"}
DOCTOR_ROLES = {"Doctor", "Physician"}
DOCTOR_PAGE_ROLES = {"Visits Page", "Case Sheets Page"}
SERVICE_PROVIDER_PAGE_ROLES = {"Service Provider Page"}
TERMINAL_STATUSES = {
    "cancelled",
    "canceled",
    "closed",
    "completed",
    "converted to visit",
    "done",
    "paid",
    "released",
    "return",
    "credit note issued",
}
COMPLETED_STATUSES = {"completed", "done", "closed"}
CANCELLED_STATUSES = {"cancelled", "canceled"}
DAY_BUCKET_MAX_SPAN = 92
ACTIVE_WORK_LIMIT = 100

ROUTE_PREFIX_BY_DOCTYPE = {
    "Vet Visit": "/healthcare/visits",
    "Vet Case Sheet": "/healthcare/case-sheets",
    "Appointment": "/healthcare/appointments",
    "PetCareService": "/healthcare/services",
    "Lab": "/healthcare/labs",
    "Imaging": "/healthcare/radiology",
    "Pet Procedure": "/healthcare/procedures",
    "Sales Invoice": "/accounting/sales-invoices",
}

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

ACTIVE_WORK_CONFIGS = [
    {
        "doctype": "Vet Visit",
        "source_type": "Visit",
        "status_field": "status",
        "open_statuses": {"Draft", "In Progress", "Follow-up Needed"},
        "practitioner_fields": ("doctor",),
        "user_fields": (),
        "pet_field": "animal_patient",
        "guardian_field": "guardian",
        "guardian_name_field": "guardian_name",
        "title_field": "visit_type",
        "subtitle_field": "visit_type",
        "priority_field": "priority",
        "scheduled_field": "visit_datetime",
        "route_prefix": "/healthcare/visits",
    },
    {
        "doctype": "Vet Case Sheet",
        "source_type": "Case Sheet",
        "status_field": "status",
        "open_statuses": {"Draft", "Waiting Practitioner", "Waiting Doctor", "In Consultation"},
        "practitioner_fields": ("practitioner",),
        "user_fields": (),
        "pet_field": "animal_patient",
        "guardian_field": "guardian",
        "title_field": "chief_complaint",
        "subtitle_field": "chief_complaint",
        "priority_field": "priority",
        "scheduled_field": "case_sheet_date",
        "route_prefix": "/healthcare/case-sheets",
    },
    {
        "doctype": "Appointment",
        "source_type": "Appointment",
        "status_field": "status",
        "open_statuses": {"Open", "Unverified"},
        "practitioner_fields": ("custom_doctor", "party"),
        "user_fields": ("party",),
        "pet_field": "custom_pet",
        "guardian_field": "custom_guardian",
        "title_field": "custom_appointment_type",
        "subtitle_field": "custom_appointment_type",
        "scheduled_field": "scheduled_time",
        "route_prefix": "/healthcare/appointments",
    },
    {
        "doctype": "PetCareService",
        "source_type": "Service",
        "status_field": "status",
        "open_statuses": {"pending", "overdue", "Pending", "Overdue", "In Progress", "Started"},
        "practitioner_fields": ("provider", "doctor"),
        "user_fields": ("user",),
        "pet_field": "pet_id",
        "guardian_field": "guardian_id",
        "title_field": "pet_service_name",
        "subtitle_field": "category",
        "priority_field": "priority",
        "due_field": "due_date",
        "scheduled_field": "start_date",
        "route_prefix": "/healthcare/services",
    },
    {
        "doctype": "Lab",
        "source_type": "Lab",
        "status_field": "status",
        "open_statuses": {"Pending", "Ordered", "Sample Collected", "In Progress", "Result Entered"},
        "practitioner_fields": ("doctor",),
        "user_fields": ("sample_collected_by", "result_entered_by", "released_by"),
        "pet_field": "pet",
        "title_field": "care_service",
        "subtitle_field": "item_code",
        "priority_field": "priority",
        "scheduled_field": "start_at",
        "route_prefix": "/healthcare/labs",
    },
    {
        "doctype": "Imaging",
        "source_type": "Radiology",
        "status_field": "status",
        "open_statuses": {"Pending", "Ordered", "Scheduled", "In Progress", "Reported"},
        "practitioner_fields": ("doctor",),
        "user_fields": ("sample_collected_by", "result_entered_by", "released_by"),
        "pet_field": "pet",
        "title_field": "care_service",
        "subtitle_field": "item_code",
        "priority_field": "priority",
        "scheduled_field": "start_at",
        "route_prefix": ROUTE_PREFIX_BY_DOCTYPE["Imaging"],
    },
    {
        "doctype": "Pet Procedure",
        "source_type": "Procedure",
        "status_field": "status",
        "open_statuses": {"Pending", "In Progress"},
        "practitioner_fields": ("doctor", "provider"),
        "user_fields": (),
        "pet_field": "pet",
        "guardian_field": "guardian",
        "title_field": "procedure_template",
        "subtitle_field": "care_service",
        "priority_field": "priority",
        "scheduled_field": "scheduled_at",
        "route_prefix": "/healthcare/procedures",
    },
    {
        "doctype": "Sales Invoice",
        "source_type": "Invoice",
        "status_field": "status",
        "open_statuses": {
            "Draft",
            "Unpaid",
            "Partly Paid",
            "Overdue",
            "Unpaid and Discounted",
            "Partly Paid and Discounted",
            "Overdue and Discounted",
        },
        "practitioner_fields": (),
        "user_fields": (),
        "pet_field": None,
        "guardian_field": None,
        "title_field": "customer_name",
        "subtitle_field": "status",
        "due_field": "due_date",
        "scheduled_field": "posting_date",
        "route_prefix": ROUTE_PREFIX_BY_DOCTYPE["Sales Invoice"],
    },
]


@frappe.whitelist()
def get_user_profile_dashboard(user_id: str, from_date=None, to_date=None):
    """Return a complete, read-only dashboard for one user."""
    user = _resolve_target_user(user_id)
    from_date, to_date = _parse_date_range(from_date, to_date)

    identity = _get_identity(user)
    practitioner = _get_practitioner(user, identity)
    active_work = _get_active_work(user, practitioner)
    medical = _get_medical(user, practitioner, identity["roles"], from_date, to_date)
    services = _get_services(user, practitioner, identity["roles"], from_date, to_date)
    ratings = _get_ratings(user, practitioner, from_date, to_date)
    activity = _get_activity(user, practitioner, services, from_date, to_date)
    public_services = _public_services(services)

    return {
        "identity": identity,
        "practitioner": practitioner,
        "summary": _get_summary(active_work, medical, services, ratings),
        "active_work": active_work,
        "medical": medical,
        "services": public_services,
        "ratings": ratings,
        "activity": activity,
        "access": _get_access(identity),
    }


# ─────────────────────────────────────────
# Authorization / identity
# ─────────────────────────────────────────

def _resolve_target_user(user_id):
    requested = cstr(user_id).strip()
    if not requested:
        frappe.throw(_("user_id is required."))

    caller = frappe.session.user
    if requested != caller and not _can_inspect_other_users(caller):
        frappe.throw(
            _("You are not permitted to inspect another user's performance profile."),
            frappe.PermissionError,
        )

    if not frappe.db.exists("User", requested):
        frappe.throw(_("User {0} was not found.").format(requested), frappe.DoesNotExistError)

    return requested


def _can_inspect_other_users(user):
    if user == "Administrator":
        return True
    if get_user_roles(user) & USER_ADMIN_ROLES:
        return True
    try:
        return bool(frappe.has_permission("User", ptype="read", user=user))
    except Exception:
        return False


def _get_identity(user):
    info = frappe.db.get_value(
        "User",
        user,
        ["name", "full_name", "email", "user_image", "enabled", "user_type", "last_login"],
        as_dict=True,
    ) or {}
    roles = sorted(r for r in get_user_roles(user) if r not in {"All", "Guest"})
    role_profiles = get_user_role_profiles(user)
    return {
        "user_id": user,
        "full_name": info.get("full_name") or user,
        "email": info.get("email") or user,
        "user_image": info.get("user_image"),
        "enabled": cint(info.get("enabled")),
        "user_type": info.get("user_type"),
        "roles": roles,
        "role_profiles": role_profiles,
        "last_login": _as_string(info.get("last_login")),
    }


def _get_practitioner(user, identity):
    practitioner_id = get_practitioner_for_user(user, include_disabled=True)
    if not practitioner_id or not _doctype_exists("Healthcare Practitioner"):
        return None

    fields = _existing_fields(
        "Healthcare Practitioner",
        ["name", "practitioner_name", "practitioner_type", "specialization", "photo"],
    )
    row = frappe.db.get_value("Healthcare Practitioner", practitioner_id, fields, as_dict=True) or {}
    return {
        "id": row.get("name") or practitioner_id,
        "name": row.get("practitioner_name") or row.get("name") or practitioner_id,
        "practitioner_type": row.get("practitioner_type"),
        "specialization": row.get("specialization"),
        "image": row.get("photo") or identity.get("user_image"),
    }


# ─────────────────────────────────────────
# Active work
# ─────────────────────────────────────────

def _get_active_work(user, practitioner):
    practitioner_id = practitioner.get("id") if practitioner else None
    assigned = _assigned_records(user)
    rows_by_key = OrderedDict()
    visit_invoice_names = _linked_visit_invoice_names(
        user,
        practitioner_id,
        assigned.get("Vet Visit", set()),
    )

    for config in ACTIVE_WORK_CONFIGS:
        doctype = config["doctype"]
        if not _doctype_exists(doctype):
            continue

        rows = _related_active_rows(config, user, practitioner_id, assigned.get(doctype, set()))
        for row in rows:
            key = (doctype, row.get("name"))
            if not row.get("name") or key in rows_by_key:
                continue
            item = _active_work_item(config, row, user, practitioner)
            if item:
                rows_by_key[key] = item
            if doctype == "Vet Visit" and row.get("sales_invoice"):
                visit_invoice_names.add(row.get("sales_invoice"))

    for invoice in sorted(visit_invoice_names):
        row = _get_active_named_row("Sales Invoice", invoice)
        if not row:
            continue
        key = ("Sales Invoice", invoice)
        if key not in rows_by_key:
            config = _config_for_doctype("Sales Invoice")
            rows_by_key[key] = _active_work_item(config, row, user, practitioner)

    items = [item for item in rows_by_key.values() if item]
    items.sort(key=_active_work_sort_key)
    return [_public_active_work_item(item) for item in items[:ACTIVE_WORK_LIMIT]]


def _linked_visit_invoice_names(user, practitioner_id, assigned_visit_names):
    if not (_doctype_exists("Vet Visit") and _doctype_exists("Sales Invoice")):
        return set()
    if not _has_field("Vet Visit", "sales_invoice"):
        return set()

    relation_sets = [[["owner", "=", user]]]
    if practitioner_id and _has_field("Vet Visit", "doctor"):
        relation_sets.append([["doctor", "=", practitioner_id]])
    if assigned_visit_names:
        relation_sets.append([["name", "in", list(assigned_visit_names)]])

    invoice_names = set()
    fields = _existing_fields("Vet Visit", ["name", "sales_invoice"])
    for relation in relation_sets:
        try:
            rows = frappe.get_all(
                "Vet Visit",
                filters=_base_filters("Vet Visit") + relation + [["sales_invoice", "is", "set"]],
                fields=fields,
                ignore_permissions=True,
            )
        except Exception:
            continue
        invoice_names.update(row.get("sales_invoice") for row in rows if row.get("sales_invoice"))
    return invoice_names


def _related_active_rows(config, user, practitioner_id, assigned_names):
    doctype = config["doctype"]
    relation_sets = [[["owner", "=", user]]]

    if practitioner_id:
        for field in config.get("practitioner_fields") or ():
            if _has_field(doctype, field):
                relation_sets.append([[field, "=", practitioner_id]])

    for field in config.get("user_fields") or ():
        if _has_field(doctype, field):
            relation_sets.append([[field, "=", user]])

    if assigned_names:
        relation_sets.append([["name", "in", list(assigned_names)]])

    rows_by_name = OrderedDict()
    fields = _active_fields(config)
    for relation in relation_sets:
        try:
            rows = frappe.get_all(
                doctype,
                filters=_base_filters(doctype) + relation,
                fields=fields,
                ignore_permissions=True,
                order_by="modified desc",
            )
        except Exception:
            continue
        for row in rows:
            if row.get("name") and _is_active_row(config, row):
                rows_by_name[row.get("name")] = row
    return list(rows_by_name.values())


def _get_active_named_row(doctype, name):
    config = _config_for_doctype(doctype)
    if not config or not _doctype_exists(doctype):
        return None
    try:
        row = frappe.db.get_value(doctype, name, _active_fields(config), as_dict=True)
    except Exception:
        return None
    if row and _is_active_row(config, row):
        return row
    return None


def _active_fields(config):
    fields = {
        "name",
        "owner",
        "creation",
        "modified",
        "status",
        "priority",
        "sales_invoice",
        config.get("status_field"),
        config.get("pet_field"),
        config.get("guardian_field"),
        config.get("guardian_name_field"),
        config.get("title_field"),
        config.get("subtitle_field"),
        config.get("priority_field"),
        config.get("due_field"),
        config.get("scheduled_field"),
        *tuple(config.get("practitioner_fields") or ()),
        *tuple(config.get("user_fields") or ()),
    }
    return _existing_fields(config["doctype"], [f for f in fields if f])


def _is_active_row(config, row):
    status_field = config.get("status_field") or "status"
    status = cstr(row.get(status_field)).strip()
    open_statuses = config.get("open_statuses")
    if open_statuses and status:
        lowered = {cstr(value).lower() for value in open_statuses}
        return status.lower() in lowered
    return status.lower() not in TERMINAL_STATUSES


def _active_work_item(config, row, user, practitioner):
    doctype = config["doctype"]
    name = row.get("name")
    pet_name = _linked_title("Pet", row.get(config.get("pet_field")), "pet_name")
    guardian_name = (
        row.get(config.get("guardian_name_field"))
        or _linked_title("Guardian", row.get(config.get("guardian_field")), "full_name")
    )
    scheduled_at = row.get(config.get("scheduled_field"))
    due_at = row.get(config.get("due_field"))
    title = _active_title(config, row, pet_name)
    assignee_name = _assignee_name(row, config, user, practitioner)

    return {
        "id": name,
        "source_type": config["source_type"],
        "title": title,
        "subtitle": row.get(config.get("subtitle_field")) or config["source_type"],
        "pet_name": pet_name,
        "guardian_name": guardian_name,
        "status": row.get(config.get("status_field") or "status"),
        "priority": row.get(config.get("priority_field")),
        "due_at": _as_string(due_at),
        "scheduled_at": _as_string(scheduled_at),
        "assignee_name": assignee_name,
        "owner": row.get("owner"),
        "route_to": _route_to(doctype, name),
        "_modified": _as_string(row.get("modified")),
    }


def _active_title(config, row, pet_name):
    source = config["source_type"]
    explicit = row.get(config.get("title_field"))
    if source == "Visit":
        return _("Visit for {0}").format(pet_name or row.get("name"))
    if source == "Case Sheet":
        return _("Case sheet for {0}").format(pet_name or row.get("name"))
    if source == "Appointment":
        return _("Appointment for {0}").format(pet_name or row.get("name"))
    if source == "Service":
        return explicit or _("Service for {0}").format(pet_name or row.get("name"))
    if source == "Lab":
        return _("Lab order for {0}").format(pet_name) if pet_name else (explicit or _("Lab order"))
    if source == "Radiology":
        return _("Imaging order for {0}").format(pet_name) if pet_name else (explicit or _("Imaging order"))
    if source == "Procedure":
        return explicit or _("Procedure for {0}").format(pet_name or row.get("name"))
    if source == "Invoice":
        return _("Invoice {0}").format(row.get("name"))
    return explicit or row.get("name")


def _assignee_name(row, config, user, practitioner):
    practitioner_id = practitioner.get("id") if practitioner else None
    practitioner_name = practitioner.get("name") if practitioner else None
    for field in config.get("practitioner_fields") or ():
        if row.get(field) and row.get(field) == practitioner_id:
            return practitioner_name or practitioner_id
    for field in config.get("user_fields") or ():
        if row.get(field) and row.get(field) == user:
            return _user_full_name(user)
    return _user_full_name(row.get("owner"))


def _assigned_records(user):
    if not _doctype_exists("ToDo"):
        return {}
    try:
        rows = frappe.get_all(
            "ToDo",
            filters=[
                ["allocated_to", "=", user],
                ["status", "!=", "Closed"],
                ["reference_type", "in", [c["doctype"] for c in ACTIVE_WORK_CONFIGS]],
            ],
            fields=["reference_type", "reference_name"],
            ignore_permissions=True,
        )
    except Exception:
        return {}

    grouped = {}
    for row in rows:
        if row.get("reference_type") and row.get("reference_name"):
            grouped.setdefault(row["reference_type"], set()).add(row["reference_name"])
    return grouped


def _active_work_sort_key(item):
    status = cstr(item.get("status")).lower()
    overdue = _is_overdue_item(item) or "overdue" in status
    when = item.get("due_at") or item.get("scheduled_at")
    urgent_at = _timestamp(when) if overdue else 0
    newest_at = -_timestamp(item.get("_modified") or item.get("scheduled_at") or item.get("due_at"))
    return (
        0 if overdue else 1,
        _priority_rank(item.get("priority")),
        urgent_at,
        newest_at,
        item.get("source_type") or "",
        item.get("id") or "",
    )


def _public_active_work_item(item):
    return {key: value for key, value in item.items() if not key.startswith("_")}


# ─────────────────────────────────────────
# Medical / service performance
# ─────────────────────────────────────────

def _get_medical(user, practitioner, roles, from_date, to_date):
    if not practitioner or not _is_doctor(practitioner, roles):
        return None

    practitioner_id = practitioner["id"]
    visits = _records_for_practitioner(
        "Vet Visit",
        practitioner_id,
        ("doctor",),
        ["name", "status", "visit_datetime", "creation"],
        ("visit_datetime", "creation"),
        from_date,
        to_date,
    )
    case_sheets = _records_for_practitioner(
        "Vet Case Sheet",
        practitioner_id,
        ("practitioner",),
        ["name", "case_sheet_date", "creation"],
        ("case_sheet_date", "creation"),
        from_date,
        to_date,
    )
    procedures = _records_for_practitioner(
        "Pet Procedure",
        practitioner_id,
        ("doctor", "provider"),
        ["name", "scheduled_at", "completed_at", "creation"],
        ("scheduled_at", "completed_at", "creation"),
        from_date,
        to_date,
    )
    labs = _records_for_practitioner(
        "Lab",
        practitioner_id,
        ("doctor",),
        ["name", "start_at", "released_at", "creation"],
        ("start_at", "released_at", "creation"),
        from_date,
        to_date,
    )
    imaging = _records_for_practitioner(
        "Imaging",
        practitioner_id,
        ("doctor",),
        ["name", "start_at", "released_at", "creation"],
        ("start_at", "released_at", "creation"),
        from_date,
        to_date,
    )
    orders = _visit_orders([row["name"] for row in visits], from_date, to_date)

    open_visits = sum(1 for row in visits if cstr(row.get("status")).lower() not in COMPLETED_STATUSES | CANCELLED_STATUSES)
    completed_visits = sum(1 for row in visits if cstr(row.get("status")).lower() in COMPLETED_STATUSES)

    return {
        "visits": len(visits),
        "open_visits": open_visits,
        "completed_visits": completed_visits,
        "case_sheets": len(case_sheets),
        "orders": len(orders),
        "procedures": len(procedures),
        "diagnostics_requested": len(labs) + len(imaging),
        "metrics": [],
    }


def _get_services(user, practitioner, roles, from_date, to_date):
    if not practitioner or not _is_service_provider(practitioner, roles):
        return None

    practitioner_id = practitioner["id"]
    rows = _records_for_practitioner(
        "PetCareService",
        practitioner_id,
        ("provider", "doctor"),
        ["name", "status", "pet_id", "start_date", "end_date", "cancelled_at", "due_date", "creation", "modified"],
        ("end_date", "cancelled_at", "start_date", "due_date", "creation"),
        from_date,
        to_date,
    )

    assigned = in_progress = completed = cancelled = 0
    completion_seconds = []
    pets_served = set()
    completed_dates = []

    for row in rows:
        status = cstr(row.get("status")).lower()
        is_completed = status in COMPLETED_STATUSES
        is_cancelled = status in CANCELLED_STATUSES
        if is_completed:
            completed += 1
            if row.get("pet_id"):
                pets_served.add(row.get("pet_id"))
            completed_dates.append(row.get("end_date") or row.get("modified") or row.get("creation"))
            seconds = _duration_seconds(row.get("start_date"), row.get("end_date"))
            if seconds is not None:
                completion_seconds.append(seconds)
        elif is_cancelled:
            cancelled += 1
        elif status in {"in progress", "started"} or row.get("start_date"):
            in_progress += 1
        else:
            assigned += 1

    return {
        "assigned": assigned,
        "in_progress": in_progress,
        "completed": completed,
        "cancelled": cancelled,
        "average_completion_time": _format_duration(sum(completion_seconds) / len(completion_seconds)) if completion_seconds else None,
        "pets_served": len(pets_served),
        "metrics": [],
        "_completed_dates": completed_dates,
    }


def _public_services(services):
    if services is None:
        return None
    return {key: value for key, value in services.items() if not key.startswith("_")}


def _records_for_practitioner(doctype, practitioner_id, fields_to_match, fields, date_fields, from_date, to_date):
    if not practitioner_id or not _doctype_exists(doctype):
        return []

    query_fields = _existing_fields(doctype, list({"name", *fields, *fields_to_match}))
    rows_by_name = OrderedDict()
    for field in fields_to_match:
        if not _has_field(doctype, field):
            continue
        try:
            rows = frappe.get_all(
                doctype,
                filters=_base_filters(doctype) + [[field, "=", practitioner_id]],
                fields=query_fields,
                ignore_permissions=True,
            )
        except Exception:
            continue
        for row in rows:
            if row.get("name") and _row_in_date_range(row, date_fields, from_date, to_date):
                rows_by_name[row["name"]] = row
    return list(rows_by_name.values())


def _visit_orders(visit_names, from_date, to_date):
    if not visit_names or not _doctype_exists("Visit Order"):
        return []
    fields = _existing_fields("Visit Order", ["name", "parent", "kind", "status", "creation"])
    try:
        rows = frappe.get_all(
            "Visit Order",
            filters=[
                ["parenttype", "=", "Vet Visit"],
                ["parent", "in", visit_names],
            ],
            fields=fields,
            ignore_permissions=True,
        )
    except Exception:
        return []
    return [row for row in rows if _row_in_date_range(row, ("creation",), from_date, to_date)]


def _is_doctor(practitioner, roles):
    role_set = set(roles)
    return cstr(practitioner.get("practitioner_type")).lower() == "doctor" or bool(
        role_set & (DOCTOR_ROLES | DOCTOR_PAGE_ROLES)
    )


def _is_service_provider(practitioner, roles):
    if cstr(practitioner.get("practitioner_type")).lower() == "service provider":
        return True
    role_set = set(roles)
    if role_set & (SERVICE_PROVIDER_ROLES | SERVICE_PROVIDER_PAGE_ROLES):
        return True
    practitioner_id = practitioner.get("id")
    if practitioner_id and _doctype_exists("Healthcare Practitioner Service Category"):
        return bool(
            frappe.db.exists(
                "Healthcare Practitioner Service Category",
                {"parenttype": "Healthcare Practitioner", "parent": practitioner_id},
            )
        )
    return False


# ─────────────────────────────────────────
# Ratings
# ─────────────────────────────────────────

def _get_ratings(user, practitioner, from_date, to_date):
    performer_ids = [user]
    if practitioner:
        performer_ids.insert(0, practitioner["id"])

    received = _rating_rows("performer_id", performer_ids, from_date, to_date)
    given = _rating_rows("rated_by", [user], from_date, to_date)

    distribution = {str(i): 0 for i in range(1, 6)}
    for row in received:
        key = str(cint(row.get("overall_rating")))
        if key in distribution:
            distribution[key] += 1

    latest_reviews = []
    for row in sorted(received, key=lambda r: r.get("rated_at") or r.get("creation"), reverse=True)[:5]:
        latest_reviews.append(
            {
                "id": row.get("name"),
                "title": _rating_title(row),
                "reference_doctype": row.get("reference_doctype"),
                "reference_name": row.get("reference_name"),
                "route_to": _route_to(row.get("reference_doctype"), row.get("reference_name")),
                "rating": cint(row.get("overall_rating")),
                "note": row.get("notes"),
                "created_at": _as_string(row.get("rated_at") or row.get("creation")),
            }
        )

    return {
        "received_count": len(received),
        "received_average": _average_rating(received),
        "given_count": len(given),
        "given_average": _average_rating(given),
        "distribution": distribution,
        "latest_reviews": latest_reviews,
    }


def _rating_rows(field, values, from_date, to_date):
    if not values or not _doctype_exists("Rating") or not _has_field("Rating", field):
        return []
    rows_by_name = OrderedDict()
    filters = [[field, "in", values]] if len(values) > 1 else [[field, "=", values[0]]]
    filters += _datetime_filters("Rating", "rated_at", from_date, to_date)
    fields = _existing_fields(
        "Rating",
        [
            "name",
            "reference_doctype",
            "reference_name",
            "overall_rating",
            "rated_by",
            "rated_at",
            "creation",
            "notes",
            "performer_id",
            "performer_name",
        ],
    )
    try:
        rows = frappe.get_all(
            "Rating",
            filters=filters,
            fields=fields,
            order_by="rated_at desc",
            ignore_permissions=True,
        )
    except Exception:
        return []
    for row in rows:
        if row.get("name"):
            rows_by_name[row["name"]] = row
    return list(rows_by_name.values())


def _average_rating(rows):
    if not rows:
        return None
    return round(sum(cint(row.get("overall_rating")) for row in rows) / len(rows), 2)


def _rating_title(row):
    reference_doctype = row.get("reference_doctype")
    reference_name = row.get("reference_name")
    if not reference_doctype or not reference_name:
        return row.get("name")
    try:
        return resolve_entity_name(reference_doctype, reference_name) or get_title(reference_doctype, reference_name)
    except Exception:
        return reference_name


# ─────────────────────────────────────────
# Activity / summary / access
# ─────────────────────────────────────────

def _get_activity(user, practitioner, services, from_date, to_date):
    records_created, created_dates = _records_created(user, from_date, to_date)
    modified_dates = _records_modified(user, from_date, to_date)
    completed_dates = list((services or {}).get("_completed_dates") or [])
    given_dates = [row.get("rated_at") or row.get("creation") for row in _rating_rows("rated_by", [user], from_date, to_date)]
    active_days = {getdate(value) for value in created_dates + modified_dates if value}
    total_days, percent = _activity_window(active_days, from_date, to_date)

    return {
        "records_created": records_created,
        "active_days": len(active_days),
        "total_days": total_days,
        "percent": percent,
        "trend": _build_trend(created_dates, completed_dates, given_dates, from_date, to_date),
    }


def _records_created(user, from_date, to_date):
    by_doctype = []
    created_dates = []
    for doctype, label in RECORD_DOCTYPES:
        if not _doctype_exists(doctype):
            continue
        filters = [["owner", "=", user]] + _datetime_filters(doctype, "creation", from_date, to_date)
        try:
            dates = frappe.get_all(doctype, filters=filters, pluck="creation", ignore_permissions=True)
        except Exception:
            continue
        if not dates:
            continue
        created_dates.extend(dates)
        by_doctype.append({"doctype": doctype, "label": _(label), "count": len(dates)})
    by_doctype.sort(key=lambda row: -row["count"])
    return {"total": len(created_dates), "by_doctype": by_doctype}, created_dates


def _records_modified(user, from_date, to_date):
    modified_dates = []
    for doctype, _label in RECORD_DOCTYPES:
        if not _doctype_exists(doctype):
            continue
        filters = [["modified_by", "=", user]] + _datetime_filters(doctype, "modified", from_date, to_date)
        try:
            modified_dates.extend(
                frappe.get_all(doctype, filters=filters, pluck="modified", ignore_permissions=True)
            )
        except Exception:
            continue
    return modified_dates


def _activity_window(active_days, from_date, to_date):
    eff_to = to_date or getdate(nowdate())
    if from_date:
        eff_from = from_date
    elif active_days:
        eff_from = min(active_days)
    else:
        eff_from = eff_to

    total_days = max(date_diff(eff_to, eff_from) + 1, 0)
    percent = max(0, min(100, round(len(active_days) / total_days * 100))) if total_days else 0
    return total_days, percent


def _build_trend(created_dates, completed_dates, given_dates, from_date, to_date):
    all_dates = [value for value in created_dates + completed_dates + given_dates if value]
    if not all_dates:
        return []

    eff_from = from_date or min(getdate(value) for value in all_dates)
    eff_to = to_date or getdate(nowdate())
    granularity = "day" if date_diff(eff_to, eff_from) <= DAY_BUCKET_MAX_SPAN else "month"
    buckets = OrderedDict()

    def add(dates, key):
        for value in dates:
            if not value:
                continue
            period = _bucket_label(getdate(value), granularity)
            bucket = buckets.setdefault(
                period,
                {"period": period, "records_created": 0, "services_completed": 0, "ratings_given": 0},
            )
            bucket[key] += 1

    add(created_dates, "records_created")
    add(completed_dates, "services_completed")
    add(given_dates, "ratings_given")
    return [buckets[period] for period in sorted(buckets)]


def _bucket_label(value, granularity):
    return value.strftime("%Y-%m-%d") if granularity == "day" else value.strftime("%Y-%m")


def _get_summary(active_work, medical, services, ratings):
    overdue = sum(1 for item in active_work if _is_overdue_item(item))
    kpis = [
        {
            "key": "open",
            "label": _("Open work"),
            "value": str(len(active_work)),
            "helper": _("Currently active records"),
            "icon": "tabler-briefcase",
            "color": "warning" if active_work else "success",
        }
    ]
    if medical:
        kpis.append(
            {
                "key": "visits",
                "label": _("Visits"),
                "value": str(medical.get("visits") or 0),
                "helper": _("Visits in range"),
                "icon": "tabler-stethoscope",
                "color": "primary",
            }
        )
    if services:
        kpis.append(
            {
                "key": "services_completed",
                "label": _("Services done"),
                "value": str(services.get("completed") or 0),
                "helper": _("Completed in range"),
                "icon": "tabler-checkup-list",
                "color": "success",
            }
        )
    if ratings.get("received_count"):
        kpis.append(
            {
                "key": "rating",
                "label": _("Rating"),
                "value": str(ratings.get("received_average")),
                "helper": _("{0} received reviews").format(ratings.get("received_count")),
                "icon": "tabler-star",
                "color": "success",
            }
        )

    warnings = []
    if overdue:
        warnings.append(
            {
                "key": "overdue",
                "label": _("Overdue records"),
                "value": overdue,
                "icon": "tabler-alert-triangle",
                "color": "error",
            }
        )

    return {
        "headline": _("User performance state"),
        "caption": _("Open clinical work and recent operational signals."),
        "kpis": kpis,
        "warnings": warnings,
    }


def _get_access(identity):
    roles = identity.get("roles") or []
    role_profiles = identity.get("role_profiles") or []
    notes = []
    role_set = set(roles)
    if role_set & USER_ADMIN_ROLES:
        notes.append(_("Can inspect user administration data."))
    if role_set & DOCTOR_ROLES:
        notes.append(_("Can access clinical records through clinical roles."))
    if role_set & SERVICE_PROVIDER_ROLES:
        notes.append(_("Can access assigned service work."))
    if role_profiles:
        notes.append(_("Role profile grants composed app access."))
    if not notes:
        notes.append(_("No elevated app access detected."))

    return {
        "roles": roles,
        "role_profiles": role_profiles,
        "user_type": identity.get("user_type"),
        "permission_notes": notes,
    }


# ─────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────

def _parse_date_range(from_date, to_date):
    parsed_from = getdate(from_date) if from_date else None
    parsed_to = getdate(to_date) if to_date else None
    if parsed_from and parsed_to and parsed_from > parsed_to:
        frappe.throw(_("from_date cannot be after to_date."))
    return parsed_from, parsed_to


def _datetime_filters(doctype, field, from_date, to_date):
    if not _has_field(doctype, field):
        return []
    filters = []
    if from_date:
        filters.append([field, ">=", f"{from_date} 00:00:00"])
    if to_date:
        filters.append([field, "<=", f"{to_date} 23:59:59"])
    return filters


def _row_in_date_range(row, date_fields, from_date, to_date):
    if not from_date and not to_date:
        return True
    value = _first_value(row, date_fields)
    if not value:
        return False
    day = getdate(value)
    if from_date and day < from_date:
        return False
    if to_date and day > to_date:
        return False
    return True


def _first_value(row, fields):
    for field in fields:
        if row.get(field):
            return row.get(field)
    return None


def _base_filters(doctype):
    return [["docstatus", "<", 2]] if _has_field(doctype, "docstatus") else []


def _existing_fields(doctype, fields):
    existing = []
    for field in fields:
        if field and _has_field(doctype, field) and field not in existing:
            existing.append(field)
    if "name" not in existing:
        existing.insert(0, "name")
    return existing


def _has_field(doctype, fieldname):
    if not fieldname:
        return False
    if fieldname in {
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
    }:
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


def _config_for_doctype(doctype):
    for config in ACTIVE_WORK_CONFIGS:
        if config["doctype"] == doctype:
            return config
    return None


def _route_to(doctype, name):
    prefix = ROUTE_PREFIX_BY_DOCTYPE.get(doctype)
    if not prefix or not name:
        return None
    return f"{prefix}/{name}"


def _priority_rank(priority):
    value = cstr(priority).strip().lower()
    return {
        "emergency": 0,
        "urgent": 1,
        "high": 2,
        "normal": 3,
        "routine": 4,
        "low": 5,
    }.get(value, 6)


def _timestamp(value):
    if not value:
        return 0
    try:
        return get_datetime(value).timestamp()
    except Exception:
        try:
            return get_datetime(getdate(value)).timestamp()
        except Exception:
            return 0


def _linked_title(doctype, name, title_field):
    if not doctype or not name or not _doctype_exists(doctype):
        return None
    try:
        return frappe.db.get_value(doctype, name, title_field) or name
    except Exception:
        return name


def _user_full_name(user):
    if not user:
        return None
    try:
        return frappe.db.get_value("User", user, "full_name") or user
    except Exception:
        return user


def _duration_seconds(start, end):
    if not start or not end:
        return None
    try:
        return max((get_datetime(end) - get_datetime(start)).total_seconds(), 0)
    except Exception:
        return None


def _format_duration(seconds):
    seconds = int(seconds or 0)
    minutes = max(round(seconds / 60), 1)
    if minutes < 60:
        return f"{minutes}m"
    hours, remainder = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {remainder}m" if remainder else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def _is_overdue_item(item):
    status = cstr(item.get("status")).lower()
    if "overdue" in status:
        return True
    due_at = item.get("due_at")
    if not due_at:
        return False
    try:
        return get_datetime(due_at) < now_datetime()
    except Exception:
        try:
            return getdate(due_at) < getdate(nowdate())
        except Exception:
            return False


def _as_string(value):
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return str(value)
    return cstr(value)
