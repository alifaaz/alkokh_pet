"""The driver's own screen (/drivers/me): his orders, his balance, scan-to-deliver.

Identity is `Driver.user == frappe.session.user`. No endpoint takes a `driver` or a
`pos_profile`: a driver can never read or act for another driver. The driver login holds
no document permissions; each method checks the order is his, then acts for him under
`frappe.local.driver_self_scope` (see utils.driver_orders.permit).

A driver can deliver in full, report a full refusal, or log a failed attempt. Partial
deliveries need per-item quantities and stay with the cashier (record_delivery_result).
The money at the door may be split: cash (into his cash account), card (into the card
mode's account, never his hand), and the rest left Due on the invoice.
"""
from __future__ import annotations

import re
import unicodedata

import frappe
from frappe.utils import cint, cstr, flt

from pet_app.api.response import standardize_response
from pet_app.utils.driver_orders import (
    EPS, SETTINGS, cash_balance, doc_context, enabled, fail, fee_balance, operation,
)

OPEN_STATES = ("Preparing", "Out for Delivery", "Partially Delivered")


def _me():
    user = frappe.session.user
    name = frappe.db.get_value("Driver", {"user": user}, "name") if user and user != "Guest" else None
    if not name:
        fail("This login is not linked to a driver.", "DRIVER_NOT_LINKED")
    driver = frappe.get_doc("Driver", name)
    if driver.status != "Active":
        fail("This driver is not active.", "DRIVER_NOT_LINKED")
    if not enabled():
        fail("Driver orders are disabled.", "DRIVER_ORDERS_DISABLED")
    return driver


def _one_line(html):
    text = re.sub(r"<br\s*/?>|\n", ", ", html or "", flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return ", ".join(p.strip() for p in text.split(",") if p.strip()) or None


def _branch(driver):
    from pet_app.utils.branch import get_current_branch
    return get_current_branch(driver.user) or frappe.db.get_value("Sales Order",
        {"custom_driver": driver.name, "custom_driver_flow": 1, "docstatus": 1}, "branch", order_by="modified desc")


@frappe.whitelist()
@standardize_response
def get_my_profile():
    d = _me()
    return {"driver": d.name, "full_name": d.full_name, "branch": _branch(d), "card_modes": _card_modes()}


def _card_modes():
    """Non-cash modes the driver may take at the door: enabled, with a company account."""
    from pet_app.utils.invoice_reuse import resolve_company
    return frappe.db.sql_list("""select m.name from `tabMode of Payment` m
        join `tabMode of Payment Account` a on a.parent=m.name and a.company=%s and ifnull(a.default_account, '')!=''
        where m.enabled=1 and m.type='Bank' order by m.name""", resolve_company())


GROUPS = {
    "on_road": ("Out for Delivery", "Partially Delivered"),
    "returned": ("Returned",),
    "delivered": ("Completed", "Partially Delivered"),
}
ROW_FIELDS = ["name", "customer", "customer_name", "branch", "custom_driver", "custom_delivery_state",
    "grand_total", "advance_paid", "custom_delivery_fee", "custom_driver_fee_earned", "modified",
    "custom_payment_arrangement", "shipping_address", "address_display", "contact_mobile", "contact_phone"]
OPTIONAL_FIELDS = ["custom_driver_note", "custom_refusal_reason", "custom_refusal_note",
    "custom_return_reason", "custom_return_note", "custom_delivery_latitude", "custom_delivery_longitude"]
RETURN_REASONS = ("damaged", "wrong_order", "changed_mind", "other")
REFUSAL_REASONS = ("refused", "changed_mind", "damaged", "wrong_order")
ATTEMPT_REASONS = ("no_answer", "not_home", "wrong_address")


def _fields():
    meta = frappe.get_meta("Sales Order")
    return ROW_FIELDS + [f for f in OPTIONAL_FIELDS if meta.has_field(f)]


def _show_phone():
    # No stored row means the switch was never turned off: default on.
    stored = frappe.db.sql("select value from `tabSingles` where doctype=%s and field='custom_driver_sees_customer_phone'", SETTINGS)
    return not stored or bool(cint(stored[0][0]))


def _attempts(names):
    if not names or not frappe.db.exists("DocType", "Driver Delivery Attempt"):
        return {}
    result = {}
    for r in frappe.get_all("Driver Delivery Attempt", filters={"parenttype": "Sales Order", "parent": ["in", names]},
            fields=["parent", "reason", "note", "at"], order_by="at asc, idx asc"):
        result.setdefault(r.parent, []).append({"reason": r.reason, "note": r.note, "at": r.at})
    return result


def _mobiles(rows):
    return dict(frappe.get_all("Customer", filters={"name": ["in", list({r.customer for r in rows}) or [""]]},
        fields=["name", "mobile_no"], as_list=True))


def _phone(row, mobiles):
    return row.contact_mobile or row.contact_phone or mobiles.get(row.customer)


def _rows(rows):
    from pet_app.api.driver_orders import order_money_status
    show_phone = _show_phone()
    attempts = _attempts([r.name for r in rows])
    money = order_money_status([r.name for r in rows])
    mobiles = _mobiles(rows) if show_phone else {}
    out = []
    for r in rows:
        fee = flt(r.custom_delivery_fee)
        row = {k: r.get(k) for k in ("name", "customer", "customer_name", "branch", "custom_driver",
            "custom_delivery_state", "grand_total", "advance_paid", "custom_delivery_fee", "custom_driver_fee_earned", "modified")}
        row.update({
            "address": _one_line(r.shipping_address or r.address_display),
            # Call / WhatsApp / Telegram from the driver's menu, on every row.
            "phone": _phone(r, mobiles) if show_phone else None,
            "delivery_latitude": r.get("custom_delivery_latitude"),
            "delivery_longitude": r.get("custom_delivery_longitude"),
            "payment_arrangement": r.custom_payment_arrangement,
            "goods_total": flt(r.grand_total), "delivery_fee": fee, "customer_total": flt(r.grand_total) + fee,
            "delivery_attempts": attempts.get(r.name, []),
            "refusal_reason": r.get("custom_refusal_reason") or None,
            "refusal_note": r.get("custom_refusal_note") or None,
            "driver_note": r.get("custom_driver_note") or None,
            "return_reason": r.get("custom_return_reason") or None,
            "return_note": r.get("custom_return_note") or None})
        row.update(money.get(r.name) or {})
        out.append(row)
    return out


def _mine(driver):
    return {"custom_driver_flow": 1, "custom_driver": driver.name, "docstatus": 1}


# Drivers type the same name many ways: hamza-carrying alefs, ى/ي, ة/ه, Arabic-Indic digits.
FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ی": "ي", "ة": "ه", "ک": "ك",
    **{chr(0x0660 + i): str(i) for i in range(10)}, **{chr(0x06F0 + i): str(i) for i in range(10)}})
# Tatweel, harakat and Quranic marks, and the invisible direction marks pasted with phone numbers.
UNMARKED = re.compile("[ـً-ٰٟۖ-ۭ؜​-‏﻿]")


def _fold(text):
    """Spelling-insensitive form for search, applied to the term and to every field."""
    text = UNMARKED.sub("", unicodedata.normalize("NFKC", cstr(text))).translate(FOLD)
    return " ".join(text.casefold().split())


def _search_my_orders(driver, group, state, term, start, length):
    """Search in Python over the driver's own orders: no SQL collation folds Arabic spelling,
    and one driver's orders are few. Matches the fields the row shows, the phone only when
    the driver may see it."""
    rows = frappe.get_all("Sales Order", filters=_mine(driver), order_by="modified desc",
        fields=["name", "customer", "customer_name", "contact_mobile", "contact_phone", "custom_delivery_state"])
    mobiles = _mobiles(rows) if _show_phone() else None
    hits = [r for r in rows if any(term in _fold(v) for v in (r.name, r.customer, r.customer_name,
        _phone(r, mobiles) if mobiles is not None else None) if v)]
    counts = {g: sum(r.custom_delivery_state in states for r in hits) for g, states in GROUPS.items()}
    counts["all"] = len(hits)
    if group != "all":
        hits = [r for r in hits if r.custom_delivery_state in GROUPS[group]]
    if state:  # older clients
        hits = [r for r in hits if r.custom_delivery_state == state]
    page = [r.name for r in hits[start:start + length]]
    found = {r.name: r for r in frappe.get_all("Sales Order", filters={**_mine(driver), "name": ["in", page or [""]]},
        fields=_fields())}
    return {"orders": _rows([found[n] for n in page if n in found]), "total": len(hits), "counts": counts}


@frappe.whitelist()
@standardize_response
def list_my_orders(group="all", limit_start=0, page_length=10, state=None, search=None):
    d = _me()
    group = group or "all"
    if group != "all" and group not in GROUPS:
        fail("group must be on_road, returned, delivered or all.")
    term = _fold(search)
    if term:
        return _search_my_orders(d, group, state, term, max(0, cint(limit_start)), max(1, min(100, cint(page_length))))
    base = _mine(d)
    counts = {g: frappe.db.count("Sales Order", {**base, "custom_delivery_state": ["in", states]}) for g, states in GROUPS.items()}
    counts["all"] = frappe.db.count("Sales Order", base)
    filters = dict(base)
    if group != "all":
        filters["custom_delivery_state"] = ["in", GROUPS[group]]
    if state:  # older clients
        filters["custom_delivery_state"] = state
    rows = frappe.get_all("Sales Order", filters=filters, fields=_fields(),
        start=max(0, cint(limit_start)), page_length=max(1, min(100, cint(page_length))), order_by="modified desc")
    return {"orders": _rows(rows), "total": frappe.db.count("Sales Order", filters), "counts": counts}


@frappe.whitelist()
@standardize_response
def get_my_order(order):
    d = _me()
    rows = frappe.get_all("Sales Order", filters={**_mine(d), "name": order}, fields=_fields())
    if not rows:
        fail("This order is not assigned to you.", "ORDER_NOT_YOURS")
    row = _rows(rows)[0]
    # The order's lines, not the invoice's, so the view is the same before and after delivery.
    # delivered_qty is net of returns: a returned order reads 0.
    row["items"] = frappe.get_all("Sales Order Item", filters={"parenttype": "Sales Order", "parent": rows[0].name},
        fields=["item_code", "item_name", "qty", "uom", "rate", "amount", "delivered_qty"], order_by="idx asc")
    return {"order": row}


@frappe.whitelist()
@standardize_response
def get_my_balance():
    d = _me()
    accounts = {d.custom_cash_account}
    accounts |= set(frappe.get_all("Sales Order", filters={"custom_driver": d.name, "custom_driver_flow": 1, "docstatus": 1},
        pluck="custom_driver_cash_account", distinct=True))
    cash = sum(cash_balance(a) for a in accounts if a)
    fee_account = d.get("custom_fee_account")
    owed = fee_balance(fee_account) if fee_account else 0
    paid = flt(frappe.db.sql("select coalesce(sum(debit),0) from `tabGL Entry` where account=%s and is_cancelled=0",
        fee_account)[0][0]) if fee_account else 0
    return {"fees_owed_to_driver": owed, "fees_paid_to_driver": paid, "cash_held_for_shop": cash, "net": owed - cash}


def _as_driver(fn, **kwargs):
    """Run an operation for the signed-in driver. The permit() bypass is scoped to a
    linked, active driver; every operation re-checks the order is his before acting."""
    user = frappe.session.user
    driver = frappe.db.get_value("Driver", {"user": user, "status": "Active"}, "name") if user != "Guest" else None
    previous = getattr(frappe.local, "driver_self_scope", None)
    frappe.local.driver_self_scope = driver
    try:
        return fn(**kwargs)
    finally:
        frappe.local.driver_self_scope = previous


def _my_open_order(order):
    """Checks 1-2 shared by scan_deliver, report_refusal and report_attempt."""
    d = _me()
    if not order or not frappe.db.exists("Sales Order", order):
        fail("This order is not assigned to you.", "ORDER_NOT_YOURS")
    so = frappe.get_doc("Sales Order", order, for_update=True)
    if not so.get("custom_driver_flow") or so.docstatus != 1 or so.custom_driver != d.name:
        fail("This order is not assigned to you.", "ORDER_NOT_YOURS")
    if so.custom_delivery_state in ("Completed", "Cancelled"):
        fail("This order is closed.", "DRIVER_ORDER_CLOSED")
    if so.custom_delivery_state != "Out for Delivery" or any(flt(r.delivered_qty) > EPS for r in so.items):
        fail("This order is not out for delivery.", "ORDER_NOT_OUT_FOR_DELIVERY")
    return d, so


def _set_order_fields(so, values):
    meta = frappe.get_meta("Sales Order")
    values = {k: v for k, v in values.items() if meta.has_field(k)}
    if values:
        frappe.db.set_value("Sales Order", so.name, values, update_modified=True)


def _reason(value, allowed):
    value = (value or "").strip()
    if value not in allowed:
        fail(f"reason must be one of: {', '.join(allowed)}.", "DRIVER_ORDER_INVALID")
    return value


@frappe.whitelist(methods=["POST"])
def scan_deliver(order, note=None, collection=None, idempotency_key=None):
    """The driver scanned the order's label and pressed Delivered.

    All remaining lines accepted and the remainder closed. `note` is kept as the order's
    driver_note.

    Without `collection`, Cash on Delivery takes the invoice's full balance (goods + booked
    fee) into the driver's cash account. With `collection` = {cash_amount, card_amount,
    card_mode, card_reference?} the customer paid part in cash (driver's hand), part by
    card (the card mode's account; the reference defaults to the order name) and the rest
    stays Due on the invoice. {cash_amount: 0, card_amount: 0} leaves it all Due."""
    return _as_driver(_scan_deliver, order=order, note=note, collection=collection, idempotency_key=idempotency_key)


def _collection(value):
    if isinstance(value, str):
        value = frappe.parse_json(value) if value.strip() else None
    if value is None:
        return None
    if not isinstance(value, dict):
        fail("collection must be an object.", "DRIVER_ORDER_INVALID")
    cash, card = flt(value.get("cash_amount")), flt(value.get("card_amount"))
    if cash < 0 or card < 0:
        fail("Collected amounts cannot be negative.", "DRIVER_ORDER_INVALID")
    mode = (value.get("card_mode") or "").strip() or None
    if card > EPS:
        if not mode or mode not in _card_modes():
            fail("Choose a card mode of payment for the card amount.", "DRIVER_ORDER_INVALID")
    return frappe._dict(cash=cash, card=card, card_mode=mode,
        card_reference=(value.get("card_reference") or "").strip() or None)


@operation
def _scan_deliver(order, note=None, collection=None, idempotency_key=None):
    from pet_app.api.driver_orders import FULL_BALANCE, _deliver, _receive
    d, so = _my_open_order(order)
    ctx = doc_context(so)
    split = _collection(collection)
    items = [{"order_item": r.name, "qty": flt(r.qty) - flt(r.delivered_qty)} for r in so.items if flt(r.qty) - flt(r.delivered_qty) > EPS]
    payment = None
    if split is None:
        if so.custom_payment_arrangement == "Cash on Delivery":
            payment = {"received_by": "driver", "amount": FULL_BALANCE, "mode_of_payment": _cash_mode(ctx.profile)}
    elif split.cash > EPS:
        payment = {"received_by": "driver", "amount": split.cash, "mode_of_payment": _cash_mode(ctx.profile)}
    result = _deliver(so, ctx, items, "close", payment)
    if split is not None:
        invoice = result.get("invoice")
        if split.card > EPS:
            if not invoice:
                fail("Nothing was invoiced, so no card payment can be taken.", "DRIVER_ORDER_INVALID")
            pe = _receive(frappe.get_doc("Sales Invoice", invoice), ctx, {"received_by": "bank", "amount": split.card,
                "mode_of_payment": split.card_mode, "external_reference": split.card_reference or so.name})
            result["documents"].append({"doctype": pe.doctype, "name": pe.name})
        due = flt(frappe.db.get_value("Sales Invoice", invoice, "outstanding_amount")) if invoice else 0
        result["outstanding_amount"] = due
        result["collection"] = {"cash": split.cash, "card": split.card, "due": due}
    if (note or "").strip():
        _set_order_fields(so, {"custom_driver_note": note.strip()})
    return result


@frappe.whitelist(methods=["POST"])
def report_refusal(order, reason, note=None, idempotency_key=None):
    """The customer refused the whole order: no invoice, no payment, no fee; state
    Returned. The goods stay with the driver until the cashier receives unsold stock."""
    return _as_driver(_report_refusal, order=order, reason=reason, note=note, idempotency_key=idempotency_key)


@operation
def _report_refusal(order, reason, note=None, idempotency_key=None):
    from pet_app.api.driver_orders import _deliver
    reason = _reason(reason, REFUSAL_REASONS)
    d, so = _my_open_order(order)
    result = _deliver(so, doc_context(so), [], "keep_open", None)
    _set_order_fields(so, {"custom_refusal_reason": reason, "custom_refusal_note": (note or "").strip() or None})
    return result


@frappe.whitelist(methods=["POST"])
def report_attempt(order, reason, note=None, idempotency_key=None):
    """Could not hand it over this time. Nothing moves: no stock, money or state; the
    attempt is logged on the order for the cashier."""
    return _as_driver(_report_attempt, order=order, reason=reason, note=note, idempotency_key=idempotency_key)


@operation
def _report_attempt(order, reason, note=None, idempotency_key=None):
    from frappe.utils import now_datetime
    reason = _reason(reason, ATTEMPT_REASONS)
    d, so = _my_open_order(order)
    if not frappe.get_meta("Sales Order").has_field("custom_delivery_attempts"):
        fail("Delivery attempts are not installed yet; run the pending migration.", "DRIVER_ORDER_INVALID")
    idx = cint(frappe.db.sql("select coalesce(max(idx),0) from `tabDriver Delivery Attempt` where parent=%s", so.name)[0][0]) + 1
    row = frappe.get_doc({"doctype": "Driver Delivery Attempt", "parent": so.name, "parenttype": "Sales Order",
        "parentfield": "custom_delivery_attempts", "idx": idx, "reason": reason,
        "note": (note or "").strip() or None, "at": now_datetime(), "driver": d.name})
    row.db_insert()
    frappe.db.set_value("Sales Order", so.name, "modified", now_datetime(), update_modified=False)
    return {"documents": [{"doctype": "Sales Order", "name": so.name}], "order": so.name,
        "delivery_state": so.custom_delivery_state, "delivery_attempts": _attempts([so.name]).get(so.name, [])}


def _my_order(order):
    d = _me()
    if not order or not frappe.db.exists("Sales Order", order):
        fail("This order is not assigned to you.", "ORDER_NOT_YOURS")
    so = frappe.get_doc("Sales Order", order, for_update=True)
    if not so.get("custom_driver_flow") or so.docstatus != 1 or so.custom_driver != d.name:
        fail("This order is not assigned to you.", "ORDER_NOT_YOURS")
    return d, so


@frappe.whitelist(methods=["POST"])
def add_note(order, note, idempotency_key=None):
    """Any state: the driver's note on his order, shown to the cashier (replaces the last)."""
    return _as_driver(_add_note, order=order, note=note, idempotency_key=idempotency_key)


@operation
def _add_note(order, note, idempotency_key=None):
    d, so = _my_order(order)
    note = (note or "").strip()
    if not note:
        fail("Write a note.", "DRIVER_ORDER_INVALID")
    if not frappe.get_meta("Sales Order").has_field("custom_driver_note"):
        fail("Driver notes are not installed yet; run the pending migration.", "DRIVER_ORDER_INVALID")
    _set_order_fields(so, {"custom_driver_note": note})
    return {"documents": [{"doctype": "Sales Order", "name": so.name}], "order": so.name, "driver_note": note}


@frappe.whitelist(methods=["POST"])
def report_return(order, reason, note=None, refund_cash=0, idempotency_key=None):
    """The customer gave back a delivered order.

    Every sold line is credited (the goods go back into the driver's custody) and the
    order becomes Returned. With refund_cash on a Cash-on-Delivery order the driver pays the
    customer back from his cash account for what the credit notes owe (goods).

    The delivery fee STANDS: the trip was made, a return never credits the fee back (the
    same rule as return_sold_goods), so the refund is the goods only."""
    return _as_driver(_report_return, order=order, reason=reason, note=note, refund_cash=refund_cash,
        idempotency_key=idempotency_key)


@operation
def _report_return(order, reason, note=None, refund_cash=0, idempotency_key=None):
    from pet_app.api.driver_orders import _refund_credit_note, _result, _return_all_sold, _state
    reason = _reason(reason, RETURN_REASONS)
    d, so = _my_order(order)
    if so.custom_delivery_state not in ("Completed", "Partially Delivered"):
        fail("Only a delivered order can be returned.", "ORDER_NOT_OUT_FOR_DELIVERY")
    if cint(refund_cash) and so.custom_payment_arrangement != "Cash on Delivery":
        fail("Only a cash-on-delivery order is refunded by the driver; the shop refunds the rest.", "DRIVER_ORDER_INVALID")
    ctx = doc_context(so)
    credits = _return_all_sold(so, ctx)
    if not credits:
        fail("Nothing on this order is left to return.", "DRIVER_ORDER_INVALID")
    docs = list(credits)
    if cint(refund_cash):
        from pet_app.api.driver_orders import _order_figures
        # He can only give back cash he took: a card or Due part is the shop's to refund.
        held = flt(_order_figures([so])[so.name].collected_amount)
        payload = {"received_by": "driver", "mode_of_payment": _cash_mode(ctx.profile)}
        for credit in credits:
            credit.reload()
            owed = min(-flt(credit.outstanding_amount), held)
            if owed > EPS:
                docs.append(_refund_credit_note(ctx, credit, owed, payload))
                held -= owed
    frappe.db.set_value("Sales Order", so.name, {"custom_delivery_state": "Returned", "custom_order_status": "Returned"})
    _set_order_fields(so, {"custom_return_reason": reason, "custom_return_note": (note or "").strip() or None})
    so.reload()
    return _result(so, *docs, driver=so.custom_driver, branch=so.branch)


def _cash_mode(profile):
    from pet_app.api.accounting.cashier import _default_cash_mode_of_payment
    mode = _default_cash_mode_of_payment(profile)
    if not mode or frappe.db.get_value("Mode of Payment", mode, "type") != "Cash":
        fail("No Cash mode of payment is configured for driver collections.")
    return mode
