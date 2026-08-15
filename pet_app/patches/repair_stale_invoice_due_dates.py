from __future__ import annotations

import frappe
from frappe.utils import getdate, nowdate

# Open drafts opened on an earlier day carried a due_date - and a Payment Schedule row -
# from the day they were created. ERPNext moves posting_date to today on every save
# (TransactionBase.validate_posting_time), so the next append left due_date in the past
# and threw "Due Date cannot be before Posting Date". Invoice reuse is what made a draft
# get saved on a later day for the first time, which is why this only appeared now.
#
# The durable fix is sales_invoice_guard.fix_due_date on before_validate; this patch only
# clears the backlog that predates it. Run after that fix is live, so a draft repaired
# today and appended to next week goes through the corrected path rather than throwing
# again.
#
# Scope is deliberately narrower than "every stale draft":
#
#   * docstatus 0 only - a submitted invoice's dates are booked, not ours to move.
#   * not POS, not a return - neither participates in reuse, so neither can hit this.
#   * set_posting_time = 0 ONLY. The 143 drafts with set_posting_time = 1 keep their
#     posting_date on save, so due == posting already holds and they cannot throw.
#     Moving them to today would hand out a credit period nobody asked for.
#
# Both the parent field and the child rows are moved: set_due_date() rebuilds the parent
# from max(payment_schedule.due_date) inside validate(), so a stale row alone is enough
# to resurrect the old date.

CANDIDATE_SQL = """
    SELECT DISTINCT si.name
    FROM `tabSales Invoice` si
    JOIN `tabPayment Schedule` ps ON ps.parent = si.name
    WHERE si.docstatus = 0
      AND IFNULL(si.is_pos, 0) = 0
      AND IFNULL(si.is_return, 0) = 0
      AND IFNULL(si.set_posting_time, 0) = 0
      AND ps.due_date < %(today)s
"""


def _candidates(today):
    return [row[0] for row in frappe.db.sql(CANDIDATE_SQL, {"today": today})]


def execute():
    today = getdate(nowdate())
    names = _candidates(today)
    if not names:
        return

    repaired = 0
    for name in names:
        # db_set / direct SQL rather than doc.save(): saving would run the full Sales
        # Invoice validation chain on 562 documents, any one of which could fail for an
        # unrelated reason and abort the whole patch. Only two date columns move.
        frappe.db.sql(
            "UPDATE `tabPayment Schedule` SET due_date = %(today)s "
            "WHERE parent = %(name)s AND due_date < %(today)s",
            {"today": today, "name": name},
        )
        frappe.db.sql(
            "UPDATE `tabSales Invoice` SET due_date = %(today)s "
            "WHERE name = %(name)s AND due_date < %(today)s",
            {"today": today, "name": name},
        )
        repaired += 1

    frappe.db.commit()
    frappe.logger().info(
        f"[invoice-due-date-repair] repaired {repaired} draft(s) to due_date {today}"
    )
