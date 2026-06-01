import frappe
from frappe import _
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
