from __future__ import annotations

import re

import frappe
from frappe.utils import flt

# Material Issues created before pet_app.patches.stock_entry_sales_order_link ran have no
# custom_sales_order: the field did not exist, so _create_stock_issue's assignment was
# silently dropped. Those rows are not merely untidy - _reverse_stock_issue finds nothing
# for them and returns early, so cancelling such an order reports success while leaving its
# stock issued. Backfilling the link is what closes that silent-loss window.
#
# The only surviving evidence is the remarks string _create_stock_issue writes. remarks is
# free text a human could have typed, so a match on it alone is not enough to justify a
# write. Three independent confirmations must all hold, and anything ambiguous is skipped
# and logged rather than guessed at.

REMARKS_PATTERN = re.compile(r"^Order Preparing - (SAL-ORD-\S+)$")


def _candidates():
    return frappe.db.sql(
        """
        select name, remarks, docstatus, modified
        from `tabStock Entry`
        where stock_entry_type = 'Material Issue'
          and (custom_sales_order is null or custom_sales_order = '')
        order by name
        """,
        as_dict=True,
    )


def _item_rows_match(stock_entry, sales_order):
    """Second, independent confirmation: the movement must describe the same goods."""
    se_rows = sorted(
        (r.item_code, flt(r.qty))
        for r in frappe.get_all(
            "Stock Entry Detail",
            filters={"parent": stock_entry},
            fields=["item_code", "qty"],
        )
    )
    so_rows = sorted(
        (r.item_code, flt(r.qty))
        for r in frappe.get_all(
            "Sales Order Item",
            filters={"parent": sales_order},
            fields=["item_code", "qty"],
        )
    )
    return se_rows == so_rows, se_rows, so_rows


def plan():
    """Analyse every candidate without writing. Shared by the dry run and execute()."""
    rows = []
    for se in _candidates():
        row = {
            "stock_entry": se.name,
            "remarks": se.remarks,
            "docstatus": se.docstatus,
            "modified_before": se.modified,
            "order": None,
            "order_exists": False,
            "claim_count": None,
            "items_match": False,
            "se_items": None,
            "so_items": None,
            "verdict": "SKIP",
            "reason": "",
        }

        match = REMARKS_PATTERN.match(se.remarks or "")
        if not match:
            row["reason"] = "remarks does not match the generated shape"
            rows.append(row)
            continue
        order = match.group(1)
        row["order"] = order

        row["order_exists"] = bool(frappe.db.exists("Sales Order", order))
        if not row["order_exists"]:
            row["reason"] = f"Sales Order {order} does not exist"
            rows.append(row)
            continue

        # Ambiguity check: more than one unlinked Material Issue naming the same order means
        # the remarks string cannot identify a single document, so none of them is written.
        claimants = [
            c.name
            for c in _candidates()
            if REMARKS_PATTERN.match(c.remarks or "")
            and REMARKS_PATTERN.match(c.remarks).group(1) == order
        ]
        row["claim_count"] = len(claimants)
        if len(claimants) != 1:
            row["reason"] = f"{len(claimants)} Material Issues claim {order}; ambiguous"
            rows.append(row)
            continue

        matched, se_items, so_items = _item_rows_match(se.name, order)
        row["items_match"], row["se_items"], row["so_items"] = matched, se_items, so_items
        if not matched:
            row["reason"] = "item rows differ from the Sales Order"
            rows.append(row)
            continue

        row["verdict"] = "WRITE"
        rows.append(row)

    return rows


def execute():
    written = 0
    for row in plan():
        if row["verdict"] != "WRITE":
            frappe.logger().info(
                f"[backfill_stock_entry_sales_order] skipped {row['stock_entry']}: {row['reason']}"
            )
            continue
        # update_modified=False: these are submitted documents and the link is a repair of
        # data that should always have been there, not an edit anyone made.
        frappe.db.set_value(
            "Stock Entry",
            row["stock_entry"],
            "custom_sales_order",
            row["order"],
            update_modified=False,
        )
        written += 1

    frappe.logger().info(f"[backfill_stock_entry_sales_order] linked {written} Material Issues")
