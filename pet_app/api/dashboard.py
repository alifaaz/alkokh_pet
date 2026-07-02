import datetime

import frappe
from frappe import _
from frappe.utils import (
    cint,
    flt,
    get_datetime,
    get_first_day,
    get_last_day,
    getdate,
    nowdate,
    pretty_date,
)

from pet_app.api.response import standardize_response

DASHBOARD_ALLOWED_ROLES = {
    "System Manager",
    "Accounts Manager",
    "Accounts User",
    "Accounting",
    "Healthcare Practitioner",
    "Doctor",
    "Healthcare",
    "Pet",
    "E-commerce",
    "Order",
    "POS",
    "Warehouse",
    "Audit",
    "Users",
    "Guardian",
    "Guardians",
    "Setting",
}

DASHBOARD_ALLOWED_ROLE_PROFILES = {
    "Healthcare Profile",
    "Pet Profile",
    "Ecommerce Profile",
    "Order Profile",
    "POS Profile",
    "Accounting Profile",
    "Warehouse Profile",
    "Audit Profile",
    "Users Profile",
    "Guardians Profile",
    "Settings Profile",
}


def _require_analytics_access():
    if frappe.session.user == "Administrator":
        return

    user_roles = set(frappe.get_roles(frappe.session.user) or [])
    if user_roles.intersection(DASHBOARD_ALLOWED_ROLES):
        return

    role_profiles = set(
        frappe.get_all(
            "User Role Profile",
            filters={
                "parent": frappe.session.user,
                "parenttype": "User",
                "parentfield": "role_profiles",
            },
            pluck="role_profile",
        )
        or []
    )
    if role_profiles.intersection(DASHBOARD_ALLOWED_ROLE_PROFILES):
        return

    frappe.throw(_("Not authorized."), frappe.PermissionError)


# ─────────────────────────────────────────
# 1. Order Status Counts
# ─────────────────────────────────────────

@frappe.whitelist()
@standardize_response
def get_order_status_counts():
    """
    GET /api/method/pet_app.api.dashboard.get_order_status_counts
    """
    _require_analytics_access()
    statuses = [
        "Draft",
        "Preparing",
        "Out for Delivery",
        "Cash Collected",
        "Completed",
        "Cancelled"
    ]

    counts = {}
    for status in statuses:
        key = status.lower().replace(" ", "_")
        counts[key] = frappe.db.count("Sales Order", filters={
            "custom_order_status": status,
            "docstatus": 1
        })

    counts["total"] = sum(counts.values())

    frappe.response["data"] = counts


# ─────────────────────────────────────────
# 2. Statistics (Sales, Customers, Products, Revenue)
# ─────────────────────────────────────────

@frappe.whitelist()
@standardize_response
def get_statistics():
    """
    GET /api/method/pet_app.api.dashboard.get_statistics
    """
    _require_analytics_access()
    revenue = frappe.db.sql(
        "SELECT SUM(grand_total) FROM `tabSales Order` WHERE docstatus=1"
    )[0][0] or 0

    frappe.response["data"] = {
        "sales":     frappe.db.count("Sales Order", filters={"docstatus": 1}),
        "customers": frappe.db.count("Customer"),
        "products":  frappe.db.count("Item", filters={"disabled": 0}),
        "revenue":   revenue
    }


# ─────────────────────────────────────────
# 3. Revenue Report (Earning vs Expense)
# ─────────────────────────────────────────

@frappe.whitelist()
@standardize_response
def get_revenue_report(fiscal_year="2026"):
    """
    GET /api/method/pet_app.api.dashboard.get_revenue_report
    GET /api/method/pet_app.api.dashboard.get_revenue_report?fiscal_year=2026
    """
    _require_analytics_access()
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    # Earning — Sales Invoices per month
    earning_rows = frappe.db.sql("""
        SELECT MONTH(posting_date) as month, SUM(grand_total) as total
        FROM `tabSales Invoice`
        WHERE docstatus = 1 AND YEAR(posting_date) = %s
        GROUP BY MONTH(posting_date)
    """, fiscal_year, as_dict=True)

    # Expense — Purchase Invoices per month
    expense_rows = frappe.db.sql("""
        SELECT MONTH(posting_date) as month, SUM(grand_total) as total
        FROM `tabPurchase Invoice`
        WHERE docstatus = 1 AND YEAR(posting_date) = %s
        GROUP BY MONTH(posting_date)
    """, fiscal_year, as_dict=True)

    # Map to dict
    earning_map = {r.month: r.total for r in earning_rows}
    expense_map = {r.month: r.total for r in expense_rows}

    earning_values = [earning_map.get(i, 0) for i in range(1, 13)]
    expense_values = [expense_map.get(i, 0) for i in range(1, 13)]

    total_earning = sum(earning_values)
    total_expense = sum(expense_values)

    frappe.response["data"] = {
        "labels": months,
        "datasets": [
            {"name": "Earning", "values": earning_values},
            {"name": "Expense", "values": expense_values}
        ],
        "total_earning": total_earning,
        "total_expense": total_expense,
        "net_profit": total_earning - total_expense
    }


# ─────────────────────────────────────────
# 4. Best Seller of the Month
# ─────────────────────────────────────────

@frappe.whitelist()
@standardize_response
def get_best_seller():
    """
    GET /api/method/pet_app.api.dashboard.get_best_seller
    """
    _require_analytics_access()
    import datetime
    today = datetime.date.today()
    first_day = today.replace(day=1)

    result = frappe.db.sql("""
        SELECT customer, customer_name, SUM(grand_total) as total
        FROM `tabSales Order`
        WHERE docstatus = 1
        AND transaction_date >= %s
        GROUP BY customer
        ORDER BY total DESC
        LIMIT 1
    """, first_day, as_dict=True)

    frappe.response["data"] = result[0] if result else {}


# ─────────────────────────────────────────
# #5 Profit & Expenses Current Month
# ─────────────────────────────────────────

import frappe

@frappe.whitelist()
@standardize_response
def get_profit_and_expenses():
    """
    GET /api/method/pet_app.api.dashboard.get_profit_and_expenses
    """
    _require_analytics_access()

    import datetime

    # ────────────────
    # Dates
    # ────────────────
    today = datetime.date.today()
    first_day_this_month = today.replace(day=1)

    last_month_end = first_day_this_month - datetime.timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)

    # ────────────────
    # Current Month
    # ────────────────

    earning = frappe.db.sql("""
        SELECT SUM(grand_total) as total
        FROM `tabSales Invoice`
        WHERE docstatus = 1
        AND posting_date >= %s
    """, (first_day_this_month,), as_dict=True)[0].total or 0

    expense = frappe.db.sql("""
        SELECT SUM(grand_total) as total
        FROM `tabPurchase Invoice`
        WHERE docstatus = 1
        AND posting_date >= %s
    """, (first_day_this_month,), as_dict=True)[0].total or 0

    # ────────────────
    # Last Month (for growth)
    # ────────────────

    last_month_earning = frappe.db.sql("""
        SELECT SUM(grand_total) as total
        FROM `tabSales Invoice`
        WHERE docstatus = 1
        AND posting_date BETWEEN %s AND %s
    """, (last_month_start, last_month_end), as_dict=True)[0].total or 0

    # ────────────────
    # Growth %
    # ────────────────

    growth = 0
    if last_month_earning > 0:
        growth = round(((earning - last_month_earning) / last_month_earning) * 100, 2)

    # ────────────────
    # Response
    # ────────────────

    frappe.response["data"] = {
        "profit": earning - expense,
        "expenses": expense,
        "growth_percent": growth,
        "current_month": str(first_day_this_month.strftime("%B %Y"))
    }
# ─────────────────────────────────────────
# 6. Orders by Item Group (Donut Chart)
# ─────────────────────────────────────────

@frappe.whitelist()
@standardize_response
def get_orders_by_item_group():
    """
    GET /api/method/pet_app.api.dashboard.get_orders_by_item_group
    """
    _require_analytics_access()
    result = frappe.db.sql("""
        SELECT i.item_group, COUNT(soi.name) as count, SUM(soi.amount) as total
        FROM `tabSales Order Item` soi
        JOIN `tabItem` i ON i.name = soi.item_code
        JOIN `tabSales Order` so ON so.name = soi.parent
        WHERE so.docstatus = 1
        GROUP BY i.item_group
        ORDER BY total DESC
    """, as_dict=True)

    total = sum(r.count for r in result)

    frappe.response["data"] = {
        "total": total,
        "groups": [
            {
                "item_group": r.item_group,
                "count": r.count,
                "value": r.total,
                "percent": round((r.count / total) * 100, 1) if total > 0 else 0
            }
            for r in result
        ]
    }


# ════════════════════════════════════════════════════════════════════════
# Report Engine Dashboards
#   - get_medical_dashboard       (Medical tab)
#   - get_commercial_dashboard    (Commercial tab)
#   - get_drivers_dashboard       (Drivers tab)
#   - get_summary_dashboard       (Executive Summary tab)
#
# Each returns the dashboard object directly; Frappe wraps it as
# { "message": { ...dashboard... } }. Every section is field-existence
# guarded and wrapped so missing columns/tables degrade to the documented
# empty/zero state instead of raising — arrays are [], counts are 0.
# ════════════════════════════════════════════════════════════════════════

MONTH_LABELS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ─────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────

def _table_exists(doctype):
    try:
        return bool(frappe.db.table_exists(doctype))
    except Exception:
        return False


def _has(doctype, field):
    try:
        return bool(frappe.db.has_column(doctype, field))
    except Exception:
        return False


def _safe(fn, default):
    """Run a section builder, returning ``default`` (a valid empty shape) on error."""
    try:
        return fn()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "report-dashboard")
        return default


def _resolve_range(date_from, date_to):
    """Parse the [date_from, date_to] window, defaulting to the current month."""
    today = getdate(nowdate())
    try:
        df = getdate(date_from) if date_from else getdate(get_first_day(today))
    except Exception:
        df = getdate(get_first_day(today))
    try:
        dt = getdate(date_to) if date_to else getdate(get_last_day(today))
    except Exception:
        dt = getdate(get_last_day(today))
    if dt < df:
        df, dt = dt, df
    return df, dt


def _prev_range(df, dt):
    """The equal-length window immediately preceding [df, dt] (for deltas)."""
    length = (dt - df).days
    prev_to = df - datetime.timedelta(days=1)
    prev_from = prev_to - datetime.timedelta(days=length)
    return prev_from, prev_to


def _buckets(df, dt):
    """Time-series buckets aligned to the range.

    week-or-shorter  -> day buckets   (Mon, Tue, ...)
    single month     -> day-of-month  (1, 2, ...)
    multi-month      -> month buckets (Jan, Feb, ...)
    """
    days = (dt - df).days + 1
    out = []
    if days <= 8:
        cur = df
        while cur <= dt:
            out.append({"label": _(WEEKDAY_LABELS[cur.weekday()]), "start": cur, "end": cur})
            cur += datetime.timedelta(days=1)
    elif df.year == dt.year and df.month == dt.month:
        cur = df
        while cur <= dt:
            out.append({"label": str(cur.day), "start": cur, "end": cur})
            cur += datetime.timedelta(days=1)
    else:
        cur = getdate(get_first_day(df))
        while cur <= dt:
            m_end = getdate(get_last_day(cur))
            out.append({
                "label": _(MONTH_LABELS[cur.month - 1]),
                "start": max(cur, df),
                "end": min(m_end, dt),
            })
            cur = m_end + datetime.timedelta(days=1)
    return out


def _date_value_map(doctype, date_col, df, dt, agg="COUNT(*)", where="", params=None, is_datetime=True):
    """One grouped query -> {date: value}, later folded into buckets."""
    if not _table_exists(doctype):
        return {}
    col = f"DATE(`{date_col}`)" if is_datetime else f"`{date_col}`"
    try:
        rows = frappe.db.sql(
            f"SELECT {col} AS d, {agg} AS v FROM `tab{doctype}` "
            f"WHERE {col} BETWEEN %s AND %s {where} GROUP BY {col}",
            [df, dt] + list(params or []), as_dict=True,
        )
    except Exception:
        return {}
    out = {}
    for r in rows:
        if r.d is None:
            continue
        out[getdate(r.d)] = flt(r.v)
    return out


def _fill_buckets(date_map, buckets):
    data = []
    for b in buckets:
        total = 0
        for d, v in date_map.items():
            if b["start"] <= d <= b["end"]:
                total += v
        data.append(int(round(total)))
    return data


def _count(doctype, date_col, df, dt, where="", params=None, is_datetime=True):
    if not _table_exists(doctype):
        return 0
    col = f"DATE(`{date_col}`)" if is_datetime else f"`{date_col}`"
    try:
        return cint(frappe.db.sql(
            f"SELECT COUNT(*) FROM `tab{doctype}` WHERE {col} BETWEEN %s AND %s {where}",
            [df, dt] + list(params or []),
        )[0][0])
    except Exception:
        return 0


def _sum(doctype, date_col, sum_col, df, dt, where="", params=None, is_datetime=True):
    if not _table_exists(doctype):
        return 0
    col = f"DATE(`{date_col}`)" if is_datetime else f"`{date_col}`"
    try:
        return flt(frappe.db.sql(
            f"SELECT COALESCE(SUM(`{sum_col}`), 0) FROM `tab{doctype}` "
            f"WHERE {col} BETWEEN %s AND %s {where}",
            [df, dt] + list(params or []),
        )[0][0])
    except Exception:
        return 0


def _delta(curr, prev, as_pct=True):
    """Return (text, trend) — e.g. ("▲ 12%", "up") or ("▼ 3", "down")."""
    curr, prev = flt(curr), flt(prev)
    diff = curr - prev
    trend = "up" if diff >= 0 else "down"
    arrow = "▲" if diff >= 0 else "▼"
    if as_pct and prev:
        text = f"{arrow} {abs(round(diff / prev * 100))}%"
    else:
        text = f"{arrow} {abs(round(diff))}"
    return text, trend


def _pts_delta(curr, prev):
    """Delta for percentage metrics, expressed in points: ("▲ 1%", "up")."""
    diff = round(flt(curr) - flt(prev))
    return f"{'▲' if diff >= 0 else '▼'} {abs(diff)}%", ("up" if diff >= 0 else "down")


def _currency():
    """The site/company default currency (e.g. IQD), resolved at runtime."""
    try:
        cur = frappe.get_cached_value("Global Defaults", "Global Defaults", "default_currency")
        if cur:
            return cur
    except Exception:
        pass
    try:
        return frappe.db.get_default("currency") or ""
    except Exception:
        return ""


def _money(v, abbrev=False):
    v = flt(v)
    cur = _currency()
    suffix = f" {cur}" if cur else ""
    if abbrev and abs(v) >= 1000:
        if abs(v) >= 1_000_000:
            return f"{v / 1_000_000:.1f}M{suffix}"
        return f"{v / 1000:.0f}k{suffix}"
    return f"{int(round(v)):,}{suffix}"


def _commas(v):
    if isinstance(v, str):
        return v
    try:
        return f"{int(round(flt(v))):,}"
    except Exception:
        return str(v)


# ─────────────────────────────────────────
# 1. Medical dashboard
# ─────────────────────────────────────────

def _empty_medical():
    return {
        "kpis": [
            {"label": _("Visits"), "value": 0, "icon": "tabler-stethoscope", "color": "primary", "delta": ""},
            {"label": _("Vaccinations due"), "value": 0, "icon": "tabler-vaccine", "color": "warning", "delta": ""},
            {"label": _("Follow-ups"), "value": 0, "icon": "tabler-calendar-check", "color": "info", "delta": ""},
            {"label": _("Cancellations"), "value": 0, "icon": "tabler-calendar-x", "color": "error", "delta": ""},
        ],
        "visitCategories": [],
        "visitSeries": [
            {"name": _("Consultations"), "data": []},
            {"name": _("Vaccinations"), "data": []},
        ],
        "appointmentStatus": {"labels": [_("Completed"), _("Scheduled"), _("Cancelled")], "series": [0, 0, 0]},
        "topProcedures": [],
    }


def _medical_top_procedures(df, dt):
    pp = "Pet Procedure"
    if not _table_exists(pp):
        return []
    date_expr = "DATE(COALESCE(pp.scheduled_at, pp.completed_at, pp.creation))"
    try:
        if _has(pp, "procedure_template") and _table_exists("Procedure Template"):
            rows = frappe.db.sql(
                f"""
                SELECT COALESCE(pt.procedure_name, pp.procedure_template, 'Procedure') AS label,
                       COUNT(*) AS value
                FROM `tabPet Procedure` pp
                LEFT JOIN `tabProcedure Template` pt ON pt.name = pp.procedure_template
                WHERE {date_expr} BETWEEN %s AND %s
                GROUP BY label ORDER BY value DESC LIMIT 6
                """,
                [df, dt], as_dict=True,
            )
        else:
            rows = frappe.db.sql(
                f"SELECT 'Procedure' AS label, COUNT(*) AS value FROM `tabPet Procedure` pp "
                f"WHERE {date_expr} BETWEEN %s AND %s",
                [df, dt], as_dict=True,
            )
    except Exception:
        return []
    return [{"label": r.label or "Procedure", "value": cint(r.value)} for r in rows if cint(r.value) > 0]


def _build_medical(df, dt):
    pdf, pdt = _prev_range(df, dt)
    buckets = _buckets(df, dt)
    cats = [b["label"] for b in buckets]
    vv, pvr = "Vet Visit", "Pet Vaccination Record"

    has_follow = _has(vv, "follow_up_required")
    has_type = _has(vv, "visit_type")
    has_due = _table_exists(pvr) and _has(pvr, "next_due_date")

    visits = _count(vv, "visit_datetime", df, dt)
    visits_p = _count(vv, "visit_datetime", pdf, pdt)
    vdt = _delta(visits, visits_p)[0]

    vacc_due = _count(pvr, "next_due_date", df, dt, is_datetime=False) if has_due else 0
    vacc_due_p = _count(pvr, "next_due_date", pdf, pdt, is_datetime=False) if has_due else 0
    vacc_dt = _delta(vacc_due, vacc_due_p, as_pct=False)[0]

    follow = _count(vv, "visit_datetime", df, dt, where="AND follow_up_required=1") if has_follow else 0
    follow_p = _count(vv, "visit_datetime", pdf, pdt, where="AND follow_up_required=1") if has_follow else 0
    follow_dt = _delta(follow, follow_p)[0]

    canc = _count(vv, "visit_datetime", df, dt, where="AND status='Cancelled'")
    canc_p = _count(vv, "visit_datetime", pdf, pdt, where="AND status='Cancelled'")
    canc_dt = _delta(canc, canc_p, as_pct=False)[0]

    kpis = [
        {"label": _("Visits"), "value": visits, "icon": "tabler-stethoscope", "color": "primary", "delta": vdt},
        {"label": _("Vaccinations due"), "value": vacc_due, "icon": "tabler-vaccine", "color": "warning", "delta": vacc_dt},
        {"label": _("Follow-ups"), "value": follow, "icon": "tabler-calendar-check", "color": "info", "delta": follow_dt},
        {"label": _("Cancellations"), "value": canc, "icon": "tabler-calendar-x", "color": "error", "delta": canc_dt},
    ]

    if has_type:
        cons_map = _date_value_map(vv, "visit_datetime", df, dt, where="AND visit_type='Consultation'")
        vac_map = _date_value_map(vv, "visit_datetime", df, dt, where="AND visit_type='Vaccination'")
    else:
        cons_map, vac_map = {}, {}
    visit_series = [
        {"name": _("Consultations"), "data": _fill_buckets(cons_map, buckets)},
        {"name": _("Vaccinations"), "data": _fill_buckets(vac_map, buckets)},
    ]

    completed = _count(vv, "visit_datetime", df, dt, where="AND status='Completed'")
    scheduled = _count(vv, "visit_datetime", df, dt, where="AND status IN ('Draft','In Progress','Follow-up Needed')")
    appt_status = {"labels": [_("Completed"), _("Scheduled"), _("Cancelled")], "series": [completed, scheduled, canc]}

    return {
        "kpis": kpis,
        "visitCategories": cats,
        "visitSeries": visit_series,
        "appointmentStatus": appt_status,
        "topProcedures": _medical_top_procedures(df, dt),
    }


@frappe.whitelist()
def get_medical_dashboard(date_from=None, date_to=None):
    """GET /api/method/pet_app.api.dashboard.get_medical_dashboard"""
    _require_analytics_access()
    df, dt = _resolve_range(date_from, date_to)
    return _safe(lambda: _build_medical(df, dt), _empty_medical())


# ─────────────────────────────────────────
# 2. Commercial dashboard
# ─────────────────────────────────────────

def _empty_commercial():
    return {
        "kpis": [
            {"label": _("Revenue"), "value": _money(0, abbrev=True), "icon": "tabler-cash", "color": "success", "delta": ""},
            {"label": _("Orders"), "value": 0, "icon": "tabler-shopping-cart", "color": "primary", "delta": ""},
            {"label": _("Avg. order value"), "value": _money(0), "icon": "tabler-receipt-2", "color": "info", "delta": ""},
            {"label": _("New customers"), "value": 0, "icon": "tabler-user-plus", "color": "warning", "delta": ""},
        ],
        "revenueCategories": [],
        "revenueSeries": [{"name": _("Revenue"), "data": []}],
        "ordersCategories": [],
        "ordersSeries": [{"name": _("Orders"), "data": []}],
        "salesByChannel": {"labels": [], "series": []},
        "topProducts": [],
    }


def _commercial_channels(df, dt):
    si = "Sales Invoice"
    if not _table_exists(si):
        return {"labels": [], "series": []}
    if _has(si, "is_pos"):
        try:
            rows = frappe.db.sql(
                "SELECT IF(is_pos=1, 'POS', 'Online') AS ch, COUNT(*) AS c "
                "FROM `tabSales Invoice` WHERE docstatus=1 AND posting_date BETWEEN %s AND %s GROUP BY ch",
                [df, dt], as_dict=True,
            )
        except Exception:
            rows = []
        m = {r.ch: cint(r.c) for r in rows}
        return {"labels": [_("Online"), _("POS")], "series": [m.get("Online", 0), m.get("POS", 0)]}
    total = _count(si, "posting_date", df, dt, where="AND docstatus=1", is_datetime=False)
    return {"labels": [_("Online")], "series": [total]}


def _commercial_top_products(df, dt):
    if not _table_exists("Sales Invoice Item"):
        return []
    try:
        rows = frappe.db.sql(
            """
            SELECT sii.item_code AS code, MAX(sii.item_name) AS label, COALESCE(SUM(sii.qty), 0) AS value
            FROM `tabSales Invoice Item` sii
            JOIN `tabSales Invoice` si ON si.name = sii.parent
            WHERE si.docstatus=1 AND si.posting_date BETWEEN %s AND %s
            GROUP BY sii.item_code ORDER BY value DESC LIMIT 6
            """,
            [df, dt], as_dict=True,
        )
    except Exception:
        return []
    return [{"label": r.label or r.code, "value": int(round(flt(r.value)))} for r in rows if flt(r.value) > 0]


def _build_commercial(df, dt):
    pdf, pdt = _prev_range(df, dt)
    buckets = _buckets(df, dt)
    cats = [b["label"] for b in buckets]
    si, so, cu = "Sales Invoice", "Sales Order", "Customer"

    revenue = _sum(si, "posting_date", "grand_total", df, dt, where="AND docstatus=1", is_datetime=False)
    revenue_p = _sum(si, "posting_date", "grand_total", pdf, pdt, where="AND docstatus=1", is_datetime=False)
    rev_dt = _delta(revenue, revenue_p)[0]

    orders = _count(so, "transaction_date", df, dt, where="AND docstatus=1", is_datetime=False)
    orders_p = _count(so, "transaction_date", pdf, pdt, where="AND docstatus=1", is_datetime=False)
    ord_dt = _delta(orders, orders_p)[0]

    inv = _count(si, "posting_date", df, dt, where="AND docstatus=1", is_datetime=False)
    inv_p = _count(si, "posting_date", pdf, pdt, where="AND docstatus=1", is_datetime=False)
    aov = revenue / inv if inv else 0
    aov_p = revenue_p / inv_p if inv_p else 0
    aov_dt = _delta(aov, aov_p)[0]

    newc = _count(cu, "creation", df, dt)
    newc_p = _count(cu, "creation", pdf, pdt)
    newc_dt = _delta(newc, newc_p, as_pct=False)[0]

    kpis = [
        {"label": _("Revenue"), "value": _money(revenue, abbrev=True), "icon": "tabler-cash", "color": "success", "delta": rev_dt},
        {"label": _("Orders"), "value": orders, "icon": "tabler-shopping-cart", "color": "primary", "delta": ord_dt},
        {"label": _("Avg. order value"), "value": _money(aov), "icon": "tabler-receipt-2", "color": "info", "delta": aov_dt},
        {"label": _("New customers"), "value": newc, "icon": "tabler-user-plus", "color": "warning", "delta": newc_dt},
    ]

    rev_map = _date_value_map(si, "posting_date", df, dt, agg="COALESCE(SUM(grand_total),0)",
                              where="AND docstatus=1", is_datetime=False)
    ord_map = _date_value_map(so, "transaction_date", df, dt, where="AND docstatus=1", is_datetime=False)

    return {
        "kpis": kpis,
        "revenueCategories": cats,
        "revenueSeries": [{"name": _("Revenue"), "data": _fill_buckets(rev_map, buckets)}],
        "ordersCategories": cats,
        "ordersSeries": [{"name": _("Orders"), "data": _fill_buckets(ord_map, buckets)}],
        "salesByChannel": _commercial_channels(df, dt),
        "topProducts": _commercial_top_products(df, dt),
    }


@frappe.whitelist()
def get_commercial_dashboard(date_from=None, date_to=None):
    """GET /api/method/pet_app.api.dashboard.get_commercial_dashboard"""
    _require_analytics_access()
    df, dt = _resolve_range(date_from, date_to)
    return _safe(lambda: _build_commercial(df, dt), _empty_commercial())


# ─────────────────────────────────────────
# 3. Drivers dashboard
# ─────────────────────────────────────────

def _empty_drivers():
    return {
        "kpis": [
            {"label": _("Active drivers"), "value": 0, "icon": "tabler-steering-wheel", "color": "primary", "delta": ""},
            {"label": _("Deliveries"), "value": 0, "icon": "tabler-package", "color": "success", "delta": ""},
            {"label": _("On-time rate"), "value": "0%", "icon": "tabler-clock-check", "color": "info", "delta": ""},
            {"label": _("Utilization"), "value": "0%", "icon": "tabler-gauge", "color": "warning", "delta": ""},
        ],
        "deliveriesCategories": [],
        "deliveriesSeries": [{"name": _("Completed"), "data": []}],
        "leaderboard": [],
    }


def _sla_available():
    da = "Delivery Assignment"
    return (_has(da, "sales_order") and _table_exists("Sales Order")
            and _has("Sales Order", "delivery_date"))


def _drivers_ontime_rate(df, dt):
    """On-time % of completed deliveries. SLA-based when a delivery_date exists,
    else a delivered/(delivered+failed) success rate."""
    da = "Delivery Assignment"
    if not _table_exists(da):
        return 0
    if _sla_available():
        try:
            row = frappe.db.sql(
                """
                SELECT SUM(CASE WHEN DATE(da.delivered_at) <= so.delivery_date THEN 1 ELSE 0 END) AS ontime,
                       COUNT(*) AS total
                FROM `tabDelivery Assignment` da
                JOIN `tabSales Order` so ON so.name = da.sales_order
                WHERE da.status='Delivered' AND DATE(da.delivered_at) BETWEEN %s AND %s
                  AND so.delivery_date IS NOT NULL
                """,
                [df, dt], as_dict=True,
            )
        except Exception:
            row = []
        if row and cint(row[0].total):
            return int(round(cint(row[0].ontime) / cint(row[0].total) * 100))
    try:
        row = frappe.db.sql(
            """
            SELECT SUM(CASE WHEN status='Delivered' THEN 1 ELSE 0 END) AS delivered,
                   SUM(CASE WHEN status IN ('Delivered','Failed') THEN 1 ELSE 0 END) AS attempted
            FROM `tabDelivery Assignment` WHERE DATE(assigned_at) BETWEEN %s AND %s
            """,
            [df, dt], as_dict=True,
        )
    except Exception:
        row = []
    if row and cint(row[0].attempted):
        return int(round(cint(row[0].delivered) / cint(row[0].attempted) * 100))
    return 0


def _drivers_utilization(df, dt, active):
    da = "Delivery Assignment"
    if not _table_exists(da) or not active:
        return 0
    try:
        row = frappe.db.sql(
            "SELECT COUNT(DISTINCT driver) AS c FROM `tabDelivery Assignment` "
            "WHERE status='Delivered' AND DATE(delivered_at) BETWEEN %s AND %s",
            [df, dt], as_dict=True,
        )
    except Exception:
        return 0
    worked = cint(row[0].c) if row else 0
    return min(100, int(round(worked / active * 100)))


def _leaderboard_ontime(df, dt, ids):
    da = "Delivery Assignment"
    m = {}
    if not ids:
        return m
    if _sla_available():
        try:
            rows = frappe.db.sql(
                """
                SELECT da.driver AS driver,
                       SUM(CASE WHEN DATE(da.delivered_at) <= so.delivery_date THEN 1 ELSE 0 END) AS ontime,
                       COUNT(*) AS total
                FROM `tabDelivery Assignment` da
                JOIN `tabSales Order` so ON so.name = da.sales_order
                WHERE da.status='Delivered' AND DATE(da.delivered_at) BETWEEN %(df)s AND %(dt)s
                  AND da.driver IN %(ids)s AND so.delivery_date IS NOT NULL
                GROUP BY da.driver
                """,
                {"df": df, "dt": dt, "ids": tuple(ids)}, as_dict=True,
            )
            for r in rows:
                if cint(r.total):
                    m[r.driver] = int(round(cint(r.ontime) / cint(r.total) * 100))
        except Exception:
            pass
    missing = [i for i in ids if i not in m]
    if missing:
        try:
            rows = frappe.db.sql(
                """
                SELECT driver,
                       SUM(CASE WHEN status='Delivered' THEN 1 ELSE 0 END) AS delivered,
                       SUM(CASE WHEN status IN ('Delivered','Failed') THEN 1 ELSE 0 END) AS attempted
                FROM `tabDelivery Assignment`
                WHERE driver IN %(ids)s AND DATE(assigned_at) BETWEEN %(df)s AND %(dt)s
                GROUP BY driver
                """,
                {"ids": tuple(missing), "df": df, "dt": dt}, as_dict=True,
            )
            for r in rows:
                if cint(r.attempted):
                    m[r.driver] = int(round(cint(r.delivered) / cint(r.attempted) * 100))
        except Exception:
            pass
    # Drivers that completed deliveries but have no failures recorded count as on-time.
    for i in ids:
        m.setdefault(i, 100)
    return m


def _leaderboard_zones(df, dt, ids):
    da = "Delivery Assignment"
    m = {}
    if not ids:
        return m
    if _has(da, "sales_order") and _table_exists("Sales Order") and _has("Sales Order", "territory"):
        try:
            rows = frappe.db.sql(
                """
                SELECT da.driver AS driver, so.territory AS territory, COUNT(*) AS c
                FROM `tabDelivery Assignment` da
                JOIN `tabSales Order` so ON so.name = da.sales_order
                WHERE da.status='Delivered' AND DATE(da.delivered_at) BETWEEN %(df)s AND %(dt)s
                  AND da.driver IN %(ids)s AND so.territory IS NOT NULL AND so.territory != ''
                GROUP BY da.driver, so.territory
                """,
                {"df": df, "dt": dt, "ids": tuple(ids)}, as_dict=True,
            )
            best = {}
            for r in rows:
                cur = best.get(r.driver)
                if cur is None or cint(r.c) > cur[1]:
                    best[r.driver] = (r.territory, cint(r.c))
            for k, v in best.items():
                m[k] = v[0]
        except Exception:
            pass
    return m


def _drivers_leaderboard(df, dt):
    da, dr = "Delivery Assignment", "Driver"
    if not _table_exists(da):
        return []
    try:
        rows = frappe.db.sql(
            "SELECT driver, COUNT(*) AS deliveries FROM `tabDelivery Assignment` "
            "WHERE status='Delivered' AND DATE(delivered_at) BETWEEN %s AND %s AND driver IS NOT NULL "
            "GROUP BY driver ORDER BY deliveries DESC LIMIT 10",
            [df, dt], as_dict=True,
        )
    except Exception:
        return []
    if not rows:
        return []
    ids = [r.driver for r in rows]
    names = {}
    if _table_exists(dr):
        for d in frappe.get_all(dr, filters={"name": ["in", ids]}, fields=["name", "full_name"]):
            names[d.name] = d.full_name
    ontime = _leaderboard_ontime(df, dt, ids)
    zones = _leaderboard_zones(df, dt, ids)
    return [
        {
            "driver": names.get(r.driver) or r.driver,
            "deliveries": cint(r.deliveries),
            "onTime": cint(ontime.get(r.driver, 0)),
            "zone": zones.get(r.driver) or "—",
        }
        for r in rows
    ]


def _build_drivers(df, dt):
    pdf, pdt = _prev_range(df, dt)
    buckets = _buckets(df, dt)
    cats = [b["label"] for b in buckets]
    da, dr = "Delivery Assignment", "Driver"

    active = frappe.db.count(dr, {"status": "Active"}) if _table_exists(dr) else 0
    new_drv = _count(dr, "creation", df, dt)
    new_drv_p = _count(dr, "creation", pdf, pdt)
    active_dt = _delta(new_drv, new_drv_p, as_pct=False)[0]

    deliveries = _count(da, "delivered_at", df, dt, where="AND status='Delivered'")
    deliveries_p = _count(da, "delivered_at", pdf, pdt, where="AND status='Delivered'")
    del_dt = _delta(deliveries, deliveries_p)[0]

    ontime = _drivers_ontime_rate(df, dt)
    ontime_p = _drivers_ontime_rate(pdf, pdt)
    ontime_dt = _pts_delta(ontime, ontime_p)[0]

    util = _drivers_utilization(df, dt, active)

    kpis = [
        {"label": _("Active drivers"), "value": active, "icon": "tabler-steering-wheel", "color": "primary", "delta": active_dt},
        {"label": _("Deliveries"), "value": deliveries, "icon": "tabler-package", "color": "success", "delta": del_dt},
        {"label": _("On-time rate"), "value": f"{ontime}%", "icon": "tabler-clock-check", "color": "info", "delta": ontime_dt},
        {"label": _("Utilization"), "value": f"{util}%", "icon": "tabler-gauge", "color": "warning", "delta": ""},
    ]

    del_map = _date_value_map(da, "delivered_at", df, dt, where="AND status='Delivered'")

    return {
        "kpis": kpis,
        "deliveriesCategories": cats,
        "deliveriesSeries": [{"name": _("Completed"), "data": _fill_buckets(del_map, buckets)}],
        "leaderboard": _drivers_leaderboard(df, dt),
    }


@frappe.whitelist()
def get_drivers_dashboard(date_from=None, date_to=None):
    """GET /api/method/pet_app.api.dashboard.get_drivers_dashboard"""
    _require_analytics_access()
    df, dt = _resolve_range(date_from, date_to)
    return _safe(lambda: _build_drivers(df, dt), _empty_drivers())


# ─────────────────────────────────────────
# 4. Executive summary dashboard
# ─────────────────────────────────────────

def _empty_summary():
    return {"headlineKpis": [], "miniPanels": [], "activity": []}


def _summary_activity(limit=8):
    events = []

    def add(ts, icon, color, text):
        try:
            if ts is None:
                return
            events.append((get_datetime(ts), {"icon": icon, "color": color, "text": text, "time": pretty_date(ts)}))
        except Exception:
            pass

    try:
        if _table_exists("Sales Invoice"):
            for r in frappe.get_all("Sales Invoice", filters={"docstatus": 1},
                                    fields=["name", "customer_name", "creation"],
                                    order_by="creation desc", limit=6):
                add(r.creation, "tabler-receipt", "success",
                    _("Invoice {0} created for {1}").format(r.name, r.customer_name or _("a customer")))
    except Exception:
        pass
    try:
        if _table_exists("Pet Vaccination Record"):
            for r in frappe.get_all("Pet Vaccination Record",
                                    fields=["name", "pet", "creation"],
                                    order_by="creation desc", limit=6):
                add(r.creation, "tabler-vaccine", "primary",
                    _("Vaccination recorded for {0}").format(r.pet or _("a pet")))
    except Exception:
        pass
    try:
        if _table_exists("Delivery Assignment"):
            for r in frappe.get_all("Delivery Assignment", filters={"status": "Delivered"},
                                    fields=["name", "driver", "delivered_at", "modified"],
                                    order_by="modified desc", limit=6):
                dn = None
                if r.driver:
                    try:
                        dn = frappe.db.get_value("Driver", r.driver, "full_name")
                    except Exception:
                        dn = None
                add(r.delivered_at or r.modified, "tabler-truck-delivery", "warning",
                    _("Delivery {0} completed by {1}").format(r.name, dn or r.driver or _("a driver")))
    except Exception:
        pass
    try:
        if _table_exists("Customer"):
            for r in frappe.get_all("Customer", fields=["name", "customer_name", "creation"],
                                    order_by="creation desc", limit=6):
                add(r.creation, "tabler-user-plus", "info",
                    _("New customer {0} registered").format(r.customer_name or r.name))
    except Exception:
        pass
    try:
        if _table_exists("Vet Visit"):
            for r in frappe.get_all("Vet Visit", filters={"status": "Cancelled"},
                                    fields=["name", "modified"],
                                    order_by="modified desc", limit=6):
                add(r.modified, "tabler-calendar-x", "error",
                    _("Visit {0} was cancelled").format(r.name))
    except Exception:
        pass

    events.sort(key=lambda e: e[0], reverse=True)
    return [e[1] for e in events[:limit]]


def _build_summary(df, dt):
    med = _safe(lambda: _build_medical(df, dt), None) or {}
    com = _safe(lambda: _build_commercial(df, dt), None) or {}
    drv = _safe(lambda: _build_drivers(df, dt), None) or {}

    def kpi(d, i):
        try:
            return d.get("kpis", [])[i]
        except Exception:
            return {}

    def trend(k):
        return "up" if "▲" in (k.get("delta") or "") else "down"

    def with_vs(k):
        d = k.get("delta") or ""
        return f"{d} {_('vs prev period')}" if d else ""

    def spark(d, key):
        try:
            return (d.get(key) or [{}])[0].get("data", [])
        except Exception:
            return []

    cur = _currency()
    m_visits, m_vacc = kpi(med, 0), kpi(med, 1)
    c_rev, c_orders = kpi(com, 0), kpi(com, 1)
    d_active, d_del, d_ontime = kpi(drv, 0), kpi(drv, 1), kpi(drv, 2)

    headline = [
        {"label": _("Revenue"), "value": c_rev.get("value", _money(0, abbrev=True)), "delta": with_vs(c_rev),
         "trend": trend(c_rev), "icon": "tabler-cash", "color": "success"},
        {"label": _("Visits"), "value": _commas(m_visits.get("value", 0)), "delta": with_vs(m_visits),
         "trend": trend(m_visits), "icon": "tabler-stethoscope", "color": "primary"},
        {"label": _("Orders"), "value": _commas(c_orders.get("value", 0)), "delta": with_vs(c_orders),
         "trend": trend(c_orders), "icon": "tabler-shopping-cart", "color": "info"},
        {"label": _("Active drivers"), "value": _commas(d_active.get("value", 0)), "delta": with_vs(d_active),
         "trend": trend(d_active), "icon": "tabler-steering-wheel", "color": "warning"},
    ]

    mini = [
        {"title": _("Medical"), "icon": "tabler-heartbeat", "color": "primary",
         "primaryValue": _commas(m_visits.get("value", 0)), "primaryLabel": _("Visits"),
         "secondaryValue": _commas(m_vacc.get("value", 0)), "secondaryLabel": _("Vaccines due"),
         "spark": spark(med, "visitSeries")},
        {"title": _("Commercial"), "icon": "tabler-building-store", "color": "success",
         "primaryValue": str(c_rev.get("value", "")).replace(f" {cur}", "") if cur else str(c_rev.get("value", "")),
         "primaryLabel": _("Revenue ({0})").format(cur) if cur else _("Revenue"),
         "secondaryValue": _commas(c_orders.get("value", 0)), "secondaryLabel": _("Orders"),
         "spark": spark(com, "revenueSeries")},
        {"title": _("Drivers"), "icon": "tabler-truck-delivery", "color": "warning",
         "primaryValue": _commas(d_del.get("value", 0)), "primaryLabel": _("Deliveries"),
         "secondaryValue": d_ontime.get("value", "0%"), "secondaryLabel": _("On-time"),
         "spark": spark(drv, "deliveriesSeries")},
    ]

    return {"headlineKpis": headline, "miniPanels": mini, "activity": _summary_activity()}


@frappe.whitelist()
def get_summary_dashboard(date_from=None, date_to=None):
    """GET /api/method/pet_app.api.dashboard.get_summary_dashboard"""
    _require_analytics_access()
    df, dt = _resolve_range(date_from, date_to)
    return _safe(lambda: _build_summary(df, dt), _empty_summary())
