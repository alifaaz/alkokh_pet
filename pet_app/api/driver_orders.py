"""Cashier-owned orders, driver stock custody, customer receipts and handovers.

Public contract: docs/DRIVER_ORDERS_CONTRACT.md. No function in this module commits.
"""
from __future__ import annotations

import frappe
from frappe.utils import cint, cstr, flt, nowdate

from pet_app.api.response import standardize_response
from pet_app.utils.driver_orders import (
    ARRANGEMENTS, EPS, allocate, branch_context, cash_balance, doc_context, driver_context,
    enabled, fail, fee_config, loads, managed_doc, provision_fee_account, number, operation, permit,
    provision_driver, rows, snapshot, stamp, stock_available, submit, validate_cash,
    validate_warehouse, visible_branches, ctx_currency, ctx_price_list, ctx_taxes_template,
    ctx_allows_partial, ctx_mode, mode_account, money_account, notify_driver,
)
from pet_app.utils.invoice_reuse import resolve_company


def _result(*docs, driver=None, branch=None, **extra):
    data = {"documents": [{"doctype": d.doctype, "name": d.name} for d in docs], **extra}
    for doc in docs:
        if doc.doctype == "Sales Invoice":
            data["invoice"] = doc.name
            data["goods_total"] = doc.grand_total
            data["outstanding_amount"] = doc.outstanding_amount
            data["direct_delivery_fee"] = doc.custom_direct_delivery_fee
            data["customer_total"] = flt(doc.grand_total) + flt(doc.custom_direct_delivery_fee)
        elif doc.doctype == "Sales Order":
            data["order"] = doc.name
            data["delivery_state"] = doc.custom_delivery_state
    if driver:
        data["settlement"] = snapshot(driver, branch)
    return data


def _managed(doctype, name, pos_profile=None):
    """A driver document and its context. Orders are never scoped by a till: access is
    the caller's native permission on the document. `pos_profile`, when sent, only names
    the till that money taken on this call goes into (any enabled till)."""
    doc = managed_doc(doctype, name)
    ctx = doc_context(doc)
    ctx.till = cstr(pos_profile).strip() or None
    return doc, ctx


def _state(order, state):
    order.custom_delivery_state = state
    # Keep the old Select vocabulary intact for historical clients.
    order.custom_order_status = "Out for Delivery" if state == "Partially Delivered" else state
    order.flags.ignore_permissions = True
    order.save(ignore_permissions=True)
    if state in ("Completed", "Cancelled") and any(r.delivered_qty+EPS < r.qty for r in order.items):
        order.update_status("Closed")
        order.reload()


def _open(order):
    if order.custom_delivery_state in ("Completed", "Cancelled"):
        fail("This order is closed.", "DRIVER_ORDER_CLOSED")


def _item(item_code, uom=None):
    doc = frappe.get_doc("Item", item_code)
    permit("Item", doc=doc)
    if doc.disabled or not doc.is_stock_item or doc.has_variants:
        fail("Driver loads require enabled stock items, not services or variant templates.")
    uom = uom or doc.stock_uom
    factor = 1 if uom == doc.stock_uom else next((flt(r.conversion_factor) for r in doc.uoms if r.uom == uom), 0)
    if factor <= 0:
        fail(f"No stock-UOM conversion exists for {item_code} / {uom}.")
    return doc, uom, factor


def _serial_fields(row):
    # Core creates/validates the corresponding serial/batch bundle for each voucher.
    return {k: row[k] for k in ("serial_no", "batch_no") if row.get(k)}


def _transfer(ctx, driver, lines, *, order=None, returning=False, warehouse=None, cash=None):
    doc = frappe.new_doc("Stock Entry")
    doc.company = ctx.company
    doc.stock_entry_type = "Material Transfer"
    doc.purpose = "Material Transfer"
    doc.custom_driver_movement = "Return" if returning else "Load"
    doc.custom_sales_order = order.name if order else None
    stamp(doc, driver=driver.name, branch=ctx.branch, warehouse=warehouse or driver.custom_warehouse,
        cash=cash or driver.custom_cash_account, profile=ctx.profile.name if ctx.profile else None)
    for line in lines:
        doc.append("items", line)
    stock_available([{"item_code": r["item_code"], "warehouse": r["s_warehouse"],
        "stock_qty": number(r["qty"]) * number(r.get("conversion_factor", 1))} for r in lines])
    submit(doc)
    return doc


@frappe.whitelist(methods=["POST"])
@operation
def setup_driver(driver, pos_profile=None, idempotency_key=None):
    company = resolve_company()
    permit("Driver", "write", frappe.get_doc("Driver", driver))
    permit("Warehouse", "create")
    permit("Account", "create")
    result = provision_driver(driver, company, repair=True)
    return {**result, "documents": [{"doctype": "Driver", "name": driver}]}


@frappe.whitelist(methods=["POST"])
@operation
def create_pos_order(customer, pos_profile=None, items=None, payment_arrangement="Cash on Delivery", driver=None,
                     delivery_fee=None, delivery_date=None, delivery_latitude=None, delivery_longitude=None,
                     discount_type=None, discount_value=0, branch=None, idempotency_key=None):
    ctx = branch_context(branch, till_hint=pos_profile)
    so = _build_order(ctx, customer, items, payment_arrangement, driver, delivery_fee, delivery_date,
        delivery_latitude, delivery_longitude, discount_type, discount_value)
    submit(so)
    if so.custom_driver:
        _notify_assigned(so)
    fee = flt(so.custom_delivery_fee)
    return _result(so, driver=driver, branch=ctx.branch, goods_total=so.grand_total,
        direct_delivery_fee=fee, customer_total=flt(so.grand_total)+fee,
        items=[{"order_item": r.name, "item_code": r.item_code, "qty": r.qty, "uom": r.uom} for r in so.items])


def _build_order(ctx, customer, items, payment_arrangement="Cash on Delivery", driver=None, delivery_fee=None,
                 delivery_date=None, delivery_latitude=None, delivery_longitude=None, discount_type=None,
                 discount_value=0):
    """A new, unsaved driver Sales Order: every check create_pos_order makes."""
    from pet_app.api.pos import _prepare_sale_lines
    permit("Sales Order", "create")
    permit("Sales Order", "submit")
    permit("Customer", doc=frappe.get_doc("Customer", customer))
    if payment_arrangement not in ARRANGEMENTS:
        fail("Unknown payment arrangement.")
    source = rows(items)
    for r in source:
        number(r.get("qty"))
        number(r.get("rate"), zero=True)
        _item(r.get("item_code"), r.get("uom"))
        if r.get("warehouse") and r["warehouse"] != ctx.warehouse:
            fail("Order warehouse comes from the POS Profile or the branch.")
    lines = _prepare_sale_lines(source, ctx.warehouse)
    so = frappe.new_doc("Sales Order")
    so.update({"company": ctx.company, "customer": customer, "transaction_date": nowdate(),
        "delivery_date": delivery_date or nowdate(), "selling_price_list": ctx_price_list(ctx),
        "currency": ctx_currency(ctx), "set_warehouse": ctx.warehouse, "skip_delivery_note": 1,
        "custom_payment_arrangement": payment_arrangement, "custom_payment_method": "Cash on Delivery",
        "custom_delivery_state": "Draft", "custom_order_status": "Draft", "custom_payment_status": "Pending",
        "custom_delivery_latitude": delivery_latitude, "custom_delivery_longitude": delivery_longitude,
        "ignore_pricing_rule": 1})
    driver_doc = driver_context(driver, ctx.company) if driver else None
    fee = number(delivery_fee if delivery_fee is not None else (driver_doc.get("custom_delivery_fee") if driver_doc else 0) or 0, zero=True)
    so.custom_delivery_fee = fee
    stamp(so, driver=driver, branch=ctx.branch, profile=ctx.profile.name if ctx.profile else None)
    for line in lines:
        so.append("items", {**line, "delivery_date": so.delivery_date})
    if discount_type:
        value = number(discount_value, zero=True)
        if discount_type == "Percentage" and value <= 100:
            so.additional_discount_percentage = value
        elif discount_type == "Amount":
            so.discount_amount = value
        else:
            fail("discount_type must be Percentage (0–100) or Amount.")
        so.apply_discount_on = "Grand Total"
    if ctx_taxes_template(ctx):
        so.taxes_and_charges = ctx_taxes_template(ctx)
    return so


@frappe.whitelist(methods=["POST"])
@operation
def update_pos_order(order, customer=None, pos_profile=None, items=None, payment_arrangement="Cash on Delivery",
                     delivery_fee=None, delivery_date=None, delivery_latitude=None, delivery_longitude=None,
                     discount_type=None, discount_value=0, idempotency_key=None):
    """Replace an undispatched order's lines and header (Draft or Preparing only).

    A submitted Sales Order cannot change its customer or be re-lined in place, so the
    order is cancelled and amended: the result's `order` is the NEW name (e.g. …-1) and
    `replaced_order` the old one. The driver stays. Refused once anything was dispatched,
    and while a prepayment sits on the order (refund or move it first)."""
    old, ctx = _managed("Sales Order", order, pos_profile)
    permit("Sales Order", "write", old)
    permit("Sales Order", "cancel", old)
    if old.custom_delivery_state not in ("Draft", "Preparing") or old.custom_fulfillment_warehouse \
            or any(flt(r.delivered_qty) > EPS for r in old.items):
        fail("Only a Draft or Preparing order can be edited; it has already been dispatched.", "DRIVER_ACTION_REQUIRED")
    if flt(old.advance_paid) > EPS:
        fail("This order has a prepayment; refund or move it before editing the order.", "DRIVER_ACTION_REQUIRED")
    new = _build_order(ctx, customer, items, payment_arrangement, old.custom_driver,
        delivery_fee if delivery_fee is not None else old.custom_delivery_fee,
        delivery_date or old.delivery_date,
        delivery_latitude if delivery_latitude is not None else old.get("custom_delivery_latitude"),
        delivery_longitude if delivery_longitude is not None else old.get("custom_delivery_longitude"),
        discount_type, discount_value)
    new.amended_from = old.name
    new.custom_delivery_state = old.custom_delivery_state
    new.custom_order_status = old.custom_order_status
    old.flags.driver_orders_amend = True
    old.flags.ignore_permissions = True
    old.cancel()
    submit(new)
    if new.custom_driver:
        notify_driver(new.custom_driver, f"تم تعديل الطلب: {old.name} ← {new.name} — {new.customer_name}",
            "Sales Order", new.name)
    fee = flt(new.custom_delivery_fee)
    return _result(new, driver=new.custom_driver, branch=ctx.branch, replaced_order=old.name,
        goods_total=new.grand_total, direct_delivery_fee=fee, customer_total=flt(new.grand_total)+fee,
        items=[{"order_item": r.name, "item_code": r.item_code, "qty": r.qty, "uom": r.uom} for r in new.items])


@frappe.whitelist(methods=["POST"])
@operation
def cancel_order(order, pos_profile=None, idempotency_key=None):
    """Cancel a delivery order and return everything, in one transaction.

    Sold goods get credit notes (they go back to the driver's custody, onto the order's
    load rows), then every unsold unit on the order's load rows goes back to the branch
    warehouse. The order ends Cancelled. Customer money is NOT refunded here: a paid order
    keeps its receipts and the cashier refunds through refund_customer. The driver's fee,
    once earned, is not reversed (unchanged rule)."""
    so, ctx = _managed("Sales Order", order, pos_profile)
    permit("Sales Order", "write", so)
    if so.custom_delivery_state == "Cancelled":
        fail("This order is already cancelled.", "DRIVER_ORDER_CLOSED")
    docs = []
    if so.custom_delivery_state != "Completed":
        # Stop further delivery first: nothing more is invoiced from this order.
        _close_remainder(so)
        so.reload()
    invoices = _order_invoices(so)
    docs.extend(_return_all_sold(so, ctx))
    # Everything still in the driver's hands for this order goes back to the branch.
    if so.custom_driver:
        held = [r for r in loads(so.custom_driver, so.branch) if r.order == so.name and r.available_qty > EPS]
        if held:
            docs.extend(_return_unsold(ctx, so.custom_driver,
                [{"load_row": r.name, "stock_qty": r.available_qty} for r in held]))
    so.reload()
    if so.custom_delivery_state != "Cancelled":
        values = {"custom_delivery_state": "Cancelled", "custom_order_status": "Cancelled"}
        frappe.db.set_value("Sales Order", so.name, values)
        so.reload()
    # Money the customer paid is now credit owed back: an unused order advance, or a
    # credit note (negative outstanding). The refund stays a manual cashier step.
    credit = -sum(min(0, flt(v)) for v in frappe.get_all("Sales Invoice", filters={
        "docstatus": 1, "is_return": 1, "return_against": ["in", invoices or [""]]}, pluck="outstanding_amount"))
    unused_advance = max(0, flt(so.advance_paid) - flt(frappe.db.sql("""select coalesce(sum(a.allocated_amount),0)
        from `tabSales Invoice Advance` a join `tabSales Invoice` p on p.name=a.parent
        where p.docstatus=1 and p.name in %(inv)s""", {"inv": invoices or [""]})[0][0]))
    if so.custom_driver:
        _notify_cancelled(so)
    return _result(so, *docs, driver=so.custom_driver, branch=ctx.branch,
        refund_due=credit + unused_advance, refund_required=credit + unused_advance > EPS)


def _notify_assigned(so):
    notify_driver(so.custom_driver, f"طلب جديد مسند إليك: {so.name} — {so.customer_name}", "Sales Order", so.name)


def _notify_cancelled(so):
    notify_driver(so.custom_driver, f"تم إلغاء الطلب: {so.name} — {so.customer_name}", "Sales Order", so.name)


def _order_invoices(so):
    """Original (non-return) driver invoices billed from this order."""
    names = set(frappe.get_all("Sales Invoice Item", filters={"sales_order": so.name, "docstatus": 1}, pluck="parent"))
    return sorted(n for n in names if not frappe.db.get_value("Sales Invoice", n, "is_return"))


def _return_all_sold(so, ctx):
    """Credit every unreturned line of every sale billed from this order. The goods go back
    into the driver's custody on their original load rows."""
    credits = []
    for name in _order_invoices(so):
        inv = frappe.get_doc("Sales Invoice", name)
        if not inv.get("custom_driver_flow"):
            continue
        lines = []
        for row in inv.items:
            returned = -flt(frappe.db.sql("""select coalesce(sum(i.qty),0) from `tabSales Invoice Item` i
                join `tabSales Invoice` p on p.name=i.parent where p.docstatus=1 and p.is_return=1
                and i.sales_invoice_item=%s""", row.name)[0][0])
            if flt(row.qty) - returned > EPS:
                lines.append({"invoice_item": row.name, "qty": flt(row.qty) - returned})
        if lines:
            credits.append(_return_sold(inv, ctx, lines))
    return credits


def _refund_credit_note(ctx, credit, amount, payload):
    """Pay a customer back against a credit note (return invoice)."""
    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
    mode, account, till = _payment_account(ctx, credit, payload)
    if account == credit.get("custom_driver_cash_account") and amount > cash_balance(account)+EPS:
        fail("Driver does not hold enough recorded cash for this refund.")
    pe = get_payment_entry("Sales Invoice", credit.name, bank_account=account, bank_amount=amount, party_amount=-amount)
    pe.set("references", [{"reference_doctype": "Sales Invoice", "reference_name": credit.name, "allocated_amount": -amount}])
    pe.paid_amount = pe.received_amount = amount
    pe.mode_of_payment = mode
    pe.reference_no = payload.get("external_reference") or frappe.local.driver_orders_operation
    pe.reference_date = nowdate()
    stamp(pe, driver=credit.custom_driver, branch=credit.branch, warehouse=credit.custom_fulfillment_warehouse,
        cash=credit.custom_driver_cash_account, profile=till)
    pe.custom_driver_payment_kind = "Refund"
    submit(pe)
    _assert_payment_ledger(pe, account, -amount)
    return pe


def _order_load_rows(so):
    """This order's rows in the driver's custody ledger (what 'take the goods back' uses)."""
    if not so.custom_driver:
        return []
    return [{"load_row": r.name, "load": r.load, "item_code": r.item_code, "stock_uom": r.stock_uom,
        "order_item": r.custom_driver_order_item, "loaded_qty": flt(r.transfer_qty), "sold_qty": r.sold_qty,
        "returned_qty": r.returned_qty, "available_qty": r.available_qty, "warehouse": r.warehouse}
        for r in loads(so.custom_driver, so.branch) if r.order == so.name]


@frappe.whitelist(methods=["POST"])
@operation
def assign_driver_to_order(order, driver, pos_profile=None, delivery_fee=None, idempotency_key=None):
    so, ctx = _managed("Sales Order", order, pos_profile)
    permit("Sales Order", "write", so)
    _open(so)
    d = driver_context(driver, ctx.company)
    if so.custom_driver and so.custom_driver != driver:
        fail("An assigned order cannot be reassigned; close its remainder and create a new order.")
    if so.custom_fulfillment_warehouse:
        fail("The driver and fee are frozen after first dispatch.")
    so.custom_driver = d.name
    so.custom_delivery_fee = number(delivery_fee if delivery_fee is not None else d.get("custom_delivery_fee") or 0, zero=True)
    so.flags.ignore_permissions = True
    so.save(ignore_permissions=True)
    _notify_assigned(so)
    return _result(so, driver=driver, branch=ctx.branch)


@frappe.whitelist(methods=["POST"])
@operation
def prepare_order(order, pos_profile=None, idempotency_key=None):
    so, ctx = _managed("Sales Order", order, pos_profile)
    permit("Sales Order", "write", so)
    if so.custom_delivery_state != "Draft":
        fail("Only a Draft delivery order can be prepared.")
    _state(so, "Preparing")
    return _result(so)


@frappe.whitelist(methods=["POST"])
@operation
def dispatch_order(order, pos_profile=None, items=None, idempotency_key=None):
    so, ctx = _managed("Sales Order", order, pos_profile)
    permit("Sales Order", "write", so)
    _open(so)
    if so.custom_delivery_state == "Draft":
        fail("Prepare the order before dispatch.")
    d = driver_context(so.custom_driver, ctx.company)
    if so.custom_fulfillment_warehouse and (so.custom_fulfillment_warehouse != d.custom_warehouse or so.custom_driver_cash_account != d.custom_cash_account):
        fail("This order belongs to an earlier driver warehouse/account; close it before creating a new order.")
    current = loads(d.name, ctx.branch)
    selected = rows(items) if items is not None else [{"order_item": r.name,
        "qty": max(0, flt(r.qty)-flt(r.delivered_qty)-sum(x.available_qty for x in current if x.custom_driver_order_item == r.name)/r.conversion_factor)}
        for r in so.items]
    by_name = {r.name: r for r in so.items}
    lines, seen = [], set()
    for r in selected:
        name = r.get("order_item")
        if name not in by_name or name in seen:
            fail("Dispatch must identify unique item rows on this order.")
        seen.add(name)
        src = by_name[name]
        qty = number(r.get("qty"), zero=items is None)
        if qty <= EPS:
            continue
        held = sum(x.available_qty for x in current if x.custom_driver_order_item == name)
        if qty*src.conversion_factor + held > (src.qty-src.delivered_qty)*src.conversion_factor + EPS:
            fail("Requested dispatch exceeds the unfulfilled order quantity.")
        lines.append({"item_code": src.item_code, "qty": qty, "uom": src.uom, "stock_uom": src.stock_uom,
            "conversion_factor": src.conversion_factor, "s_warehouse": src.warehouse, "t_warehouse": d.custom_warehouse,
            "custom_driver_order_item": name, "use_serial_batch_fields": 1, **_serial_fields(r)})
    if not lines:
        fail("No additional stock needs loading; record a delivery result against the stock already with the driver.")
    so.custom_fulfillment_warehouse = d.custom_warehouse
    so.custom_driver_cash_account = d.custom_cash_account
    first_dispatch = so.custom_delivery_state != "Out for Delivery"
    _state(so, "Out for Delivery")
    transfer = _transfer(ctx, d, lines, order=so)
    if first_dispatch:
        notify_driver(d.name, f"الطلب جاهز للتوصيل: {so.name} — {so.customer_name}", "Sales Order", so.name)
    return _result(so, transfer, driver=d.name, branch=ctx.branch)


@frappe.whitelist(methods=["POST"])
@operation
def load_van(driver, pos_profile=None, items=None, branch=None, idempotency_key=None):
    ctx = branch_context(branch, till_hint=pos_profile)
    d = driver_context(driver, ctx.company)
    lines = []
    for r in rows(items):
        item, uom, factor = _item(r.get("item_code"), r.get("uom"))
        lines.append({"item_code": item.name, "qty": number(r.get("qty")), "uom": uom,
            "stock_uom": item.stock_uom, "conversion_factor": factor, "s_warehouse": ctx.warehouse,
            "t_warehouse": d.custom_warehouse, "use_serial_batch_fields": 1, **_serial_fields(r)})
    doc = _transfer(ctx, d, lines)
    return _result(doc, driver=driver, branch=ctx.branch)


def _invoice(ctx, driver, lines, *, order=None, fee=0, return_against=None):
    from pet_app.utils.invoice_reuse import get_or_create_open_invoice
    permit("Sales Invoice", "create")
    permit("Sales Invoice", "submit")
    warehouses = {r.pop("_warehouse") for r in lines}
    accounts = {r.pop("_cash_account") for r in lines}
    if len(warehouses) != 1 or len(accounts) != 1:
        fail("A sale must use loads from one driver warehouse and cash-account snapshot.")
    warehouse, cash = warehouses.pop(), accounts.pop()
    if not return_against:
        stock_available([{"item_code": r["item_code"], "warehouse": warehouse,
            "stock_qty": r["qty"]*r.get("conversion_factor", 1)} for r in lines])
    source = return_against or order
    extra = {"custom_driver_flow": 1, "custom_driver": driver,
        "custom_driver_operation": frappe.local.driver_orders_operation,
        "custom_fulfillment_warehouse": warehouse, "custom_driver_cash_account": cash,
        "set_warehouse": warehouse,
        "custom_cashier_user": frappe.session.user, "custom_direct_delivery_fee": fee,
        "currency": ctx_currency(ctx), "update_stock": 1, "is_pos": 0}
    if not source and ctx_taxes_template(ctx):
        from erpnext.controllers.accounts_controller import get_taxes_and_charges
        extra["taxes_and_charges"] = ctx_taxes_template(ctx)
        extra["taxes"] = get_taxes_and_charges("Sales Taxes and Charges Template", ctx_taxes_template(ctx))
    if not source:
        extra["selling_price_list"] = ctx_price_list(ctx)
    if source:
        for field in ("currency", "conversion_rate", "selling_price_list", "price_list_currency", "plc_conversion_rate",
                      "tax_category", "taxes_and_charges", "apply_discount_on", "additional_discount_percentage",
                      "payment_terms_template", "cost_center", "disable_rounded_total"):
            extra[field] = source.get(field)
        ratio = sum(flt(r["qty"])*flt(r["rate"]) for r in lines) / flt(source.total) if source.total else 0
        if not flt(source.additional_discount_percentage):
            extra["discount_amount"] = flt(source.discount_amount)*ratio
        extra["_goods_discount"] = flt(source.discount_amount)*ratio
        taxes = []
        for tax in source.taxes:
            row = {k: tax.get(k) for k in ("charge_type", "account_head", "description", "included_in_print_rate",
                "included_in_paid_amount", "cost_center", "rate", "row_id", "tax_amount")}
            if tax.charge_type == "Actual":
                row["tax_amount"] = flt(tax.tax_amount)*ratio
            taxes.append(row)
        extra["taxes"] = taxes
    if return_against:
        extra.update(is_return=1, return_against=return_against.name)
        fees = fee_config()
        if fees:
            # The driver keeps his fee on a return; never credit the booked fee back.
            extra["taxes"] = [t for t in extra.get("taxes") or []
                if not (t.get("charge_type") == "Actual" and t.get("account_head") == fees.income)]
    booked = _fee_row(ctx, extra, fee) if fee and not return_against else 0
    extra.pop("_goods_discount", None)
    result = get_or_create_open_invoice(customer=ctx.customer, items=lines,
        source_doctype=source.doctype if source else ("POS Profile" if ctx.profile else "Branch"),
        source_name=source.name if source else (ctx.profile.name if ctx.profile else ctx.branch),
        company=ctx.company, branch=ctx.branch, requires_stock=True, is_pos=0, force_new=True,
        ignore_pricing_rule=1, branch_authorised=True, extra_fields=extra)
    invoice = result.invoice
    if order:
        invoice.allocate_advances_automatically = 1
        invoice.set_advances()
        invoice.save(ignore_permissions=True)
    submit(invoice)
    if booked:
        _book_fee(invoice, driver, booked, order)
    actual = frappe.db.sql("select distinct warehouse from `tabStock Ledger Entry` where voucher_type='Sales Invoice' and voucher_no=%s and is_cancelled=0", invoice.name)
    if {r[0] for r in actual} != {warehouse}:
        fail("Invoice stock ledger did not land in the snapshotted driver warehouse.", "DRIVER_STOCK_LEDGER_MISMATCH")
    return invoice


def _fee_row(ctx, extra, fee):
    """Put the driver's fee on the customer's invoice as delivery income.

    An Actual charge row, not an item line: every driver invoice line must trace to a
    load row. Returns the booked amount (0 when fees are not configured, which keeps the
    old off-books rule)."""
    fees = fee_config()
    if not fees:
        return 0
    fee = flt(fee)
    if fee <= EPS:
        return 0
    # A percentage discount on Grand Total would also discount the fee. Freeze the goods
    # discount as an amount first so only the goods are discounted.
    if flt(extra.get("additional_discount_percentage")) and extra.get("apply_discount_on") == "Grand Total":
        extra["discount_amount"] = flt(extra.get("_goods_discount"))
        extra["additional_discount_percentage"] = 0
    extra.pop("_goods_discount", None)
    extra.setdefault("taxes", [])
    extra["taxes"] = list(extra["taxes"]) + [{"charge_type": "Actual", "account_head": fees.income,
        "description": "Delivery Fee", "tax_amount": fee,
        "cost_center": extra.get("cost_center") or frappe.get_cached_value("Company", ctx.company, "cost_center")}]
    extra["custom_direct_delivery_fee"] = 0
    extra["custom_driver_fee_booked"] = fee
    return fee


def _book_fee(invoice, driver, fee, order=None):
    """Delivery earned the fee: Dr delivery expense / Cr the driver's fee account."""
    fees = fee_config()
    account = provision_fee_account(driver, invoice.company)
    cost_center = invoice.get("cost_center") or frappe.get_cached_value("Company", invoice.company, "cost_center")
    rows = [{"account": fees.expense, "debit_in_account_currency": fee, "cost_center": cost_center},
        {"account": account, "credit_in_account_currency": fee, "cost_center": cost_center}]
    for row in rows:
        row["branch"] = invoice.branch
    je = frappe.get_doc({"doctype": "Journal Entry", "voucher_type": "Journal Entry", "company": invoice.company,
        "posting_date": invoice.posting_date, "accounts": rows,
        "user_remark": f"Delivery fee earned by driver {driver} on {invoice.name}",
        "custom_driver_flow": 1, "custom_driver_operation": frappe.local.driver_orders_operation,
        "custom_driver": driver, "custom_driver_order": order.name if order else None,
        "custom_driver_invoice": invoice.name, "custom_driver_journal_kind": "Fee"})
    # A consequence of the invoice the caller was permitted to submit, not a separate act:
    # cashiers are not given Journal Entry rights for it.
    je.flags.ignore_permissions = True
    je.insert(ignore_permissions=True)
    je.submit()
    return je


def _line(src, qty, load, *, so=None, request=None):
    data = {k: src.get(k) for k in ("item_code", "item_name", "description", "uom", "stock_uom", "conversion_factor",
        "rate", "price_list_rate", "discount_percentage", "discount_amount", "income_account", "expense_account",
        "cost_center", "item_tax_template") if src.get(k) is not None}
    data.update(qty=qty, warehouse=load.warehouse, custom_driver_load_row=load.name,
        _warehouse=load.warehouse, _cash_account=load.cash_account, use_serial_batch_fields=1)
    if so:
        data.update(sales_order=so.name, so_detail=src.name)
    data.update(_serial_fields(request or {}))
    return data


@frappe.whitelist(methods=["POST"])
@operation
def record_delivery_result(order, pos_profile=None, accepted_items=None, remainder="keep_open", payment=None, idempotency_key=None):
    so, ctx = _managed("Sales Order", order, pos_profile)
    return _deliver(so, ctx, accepted_items, remainder, payment)


FULL_BALANCE = "__full_balance__"


def _deliver(so, ctx, accepted_items, remainder="keep_open", payment=None):
    """Invoice what the customer accepted, optionally take payment, move the state.

    `payment` may carry amount FULL_BALANCE: the server charges the invoice's own
    outstanding (goods + booked fee - advances), so a client never sends the figure."""
    permit("Sales Order", "write", so)
    _open(so)
    if not so.custom_fulfillment_warehouse:
        fail("Dispatch the order before recording delivery.")
    if remainder not in ("keep_open", "close"):
        fail("remainder must be keep_open or close.")
    permit("Driver", doc=frappe.get_doc("Driver", so.custom_driver))
    available = loads(so.custom_driver, ctx.branch)
    selected = rows(accepted_items, empty=True)
    by_name = {r.name: r for r in so.items}
    lines, seen = [], set()
    for r in selected:
        name = r.get("order_item")
        if name not in by_name or name in seen:
            fail("Accepted quantities must identify unique rows on this order.")
        seen.add(name)
        src = by_name[name]
        qty = number(r.get("qty"))
        if qty > src.qty-src.delivered_qty+EPS:
            fail("Accepted quantity exceeds this order's unfulfilled quantity.")
        allocation = allocate(available, src.item_code, qty*src.conversion_factor, order_item=name, load_row=r.get("load_row"))
        if len(allocation) > 1 and _serial_fields(r):
            fail("For serial/batch items specify a single load_row per result.")
        lines.extend(_line(src, amount/src.conversion_factor, load, so=so, request=r) for load, amount in allocation)
    docs = []
    if lines:
        ctx.customer = so.customer
        fee = 0 if so.custom_driver_fee_earned else flt(so.custom_delivery_fee)
        invoice = _invoice(ctx, so.custom_driver, lines, order=so, fee=fee)
        docs.append(invoice)
        if isinstance(payment, dict) and payment.get("amount") == FULL_BALANCE:
            payment = {**payment, "amount": flt(invoice.outstanding_amount)} if flt(invoice.outstanding_amount) > EPS else None
        if payment:
            docs.append(_receive(invoice, ctx, payment))
            invoice.reload()
        so.reload()
        so.custom_driver_fee_earned = 1
    elif payment:
        fail("A refused delivery cannot carry an invoice payment.")
    all_delivered = all(r.delivered_qty+EPS >= r.qty for r in so.items)
    any_delivered = any(r.delivered_qty > EPS for r in so.items)
    state = "Completed" if all_delivered or (remainder == "close" and any_delivered) else (
        "Cancelled" if remainder == "close" else "Partially Delivered" if any_delivered else "Returned")
    _state(so, state)
    return _result(so, *docs, driver=so.custom_driver, branch=ctx.branch)


@frappe.whitelist(methods=["POST"])
@operation
def create_van_sale(driver, customer, pos_profile=None, items=None, delivery_fee=None, payment=None, branch=None,
                    idempotency_key=None):
    ctx = branch_context(branch, till_hint=pos_profile)
    ctx.till = cstr(pos_profile).strip() or None
    d = driver_context(driver, ctx.company)
    permit("Customer", doc=frappe.get_doc("Customer", customer))
    available = loads(driver, ctx.branch)
    lines = []
    for r in rows(items):
        item, uom, factor = _item(r.get("item_code"), r.get("uom"))
        qty, rate = number(r.get("qty")), number(r.get("rate"), zero=True)
        src = frappe._dict(item_code=item.name, item_name=item.item_name, description=item.description,
            uom=uom, stock_uom=item.stock_uom, conversion_factor=factor, rate=rate)
        allocations = allocate(available, item.name, qty*factor, load_row=r.get("load_row"))
        if len(allocations) > 1 and _serial_fields(r):
            fail("For serial/batch items specify a single load_row per sale row.")
        lines.extend(_line(src, amount/factor, load, request=r) for load, amount in allocations)
    ctx.customer = customer
    invoice = _invoice(ctx, driver, lines, fee=number(delivery_fee if delivery_fee is not None else d.custom_delivery_fee or 0, zero=True))
    docs = [invoice]
    if payment:
        docs.append(_receive(invoice, ctx, payment))
        invoice.reload()
    return _result(*docs, driver=driver, branch=ctx.branch)


@frappe.whitelist(methods=["POST"])
@operation
def return_unsold_stock(driver, pos_profile=None, items=None, branch=None, idempotency_key=None):
    ctx = branch_context(branch, till_hint=pos_profile)
    docs = _return_unsold(ctx, driver, items)
    return _result(*docs, driver=driver, branch=ctx.branch)


def _return_unsold(ctx, driver, items):
    d = frappe.get_doc("Driver", driver)
    permit("Driver", doc=d)
    available = {r.name: r for r in loads(driver, ctx.branch)}
    groups, seen = {}, set()
    for r in rows(items):
        name = r.get("load_row")
        if name not in available or name in seen:
            fail("Identify unique load rows belonging to this driver and branch.")
        seen.add(name)
        src = available[name]
        qty = number(r.get("stock_qty"))
        if qty > src.available_qty+EPS:
            fail("Return exceeds unsold stock remaining on that load.")
        target = r.get("warehouse") or src.s_warehouse
        # Return to origin only; damaged stock can subsequently be moved through ERPNext.
        if target != src.s_warehouse:
            fail("Unsold goods must return to their original branch warehouse.")
        line = {"item_code": src.item_code, "qty": qty, "uom": src.stock_uom, "stock_uom": src.stock_uom,
            "conversion_factor": 1, "s_warehouse": src.warehouse, "t_warehouse": target,
            "custom_driver_load_row": src.name, "custom_driver_order_item": src.custom_driver_order_item,
            "use_serial_batch_fields": 1, **_serial_fields(r)}
        groups.setdefault((src.warehouse, src.cash_account), []).append(line)
    return [_transfer(ctx, d, lines, returning=True, warehouse=warehouse, cash=cash) for (warehouse, cash), lines in groups.items()]


@frappe.whitelist(methods=["POST"])
@operation
def close_order_remainder(order, pos_profile=None, idempotency_key=None):
    so, ctx = _managed("Sales Order", order, pos_profile)
    _close_remainder(so)
    if so.custom_driver and so.custom_delivery_state == "Cancelled":
        _notify_cancelled(so)
    return _result(so, driver=so.custom_driver, branch=ctx.branch)


def _close_remainder(so):
    permit("Sales Order", "write", so)
    _open(so)
    _state(so, "Completed" if any(r.delivered_qty > EPS for r in so.items) else "Cancelled")


def _payment_payload(value):
    if isinstance(value, str):
        value = frappe.parse_json(value)
    if not isinstance(value, dict):
        fail("payment must be an object.")
    return value


def _payment_account(ctx, source, payload):
    """(mode, account, till): till is set only when the money landed in a till."""
    method = payload.get("received_by")
    till = None
    mode = ctx_mode(ctx, payload.get("mode_of_payment"))
    mode_doc = frappe.get_doc("Mode of Payment", mode)
    if not mode_doc.enabled:
        fail("Mode of Payment is disabled.")
    if method == "driver":
        if mode_doc.type != "Cash" or not source.get("custom_driver_cash_account"):
            fail("Driver receipts require Cash and a dispatched driver/account snapshot.")
        account = source.custom_driver_cash_account
        validate_cash(account, ctx.company, source.custom_driver)
    elif method == "shop":
        # Cash at the counter, or any non-cash mode the shop takes (card, wallet, transfer).
        account, till = money_account(ctx.company, mode, ctx.get("till"))
        if mode_doc.type != "Cash" and not payload.get("external_reference"):
            fail("Card, bank and wallet payments require external_reference.")
    elif method == "bank" and mode_doc.type == "Bank":
        account = mode_account(mode, ctx.company)
        if not payload.get("external_reference"):
            fail("Card, bank and wallet payments require external_reference.")
    else:
        fail("received_by must be driver (Cash), shop, or bank (Bank).")
    acc = frappe.db.get_value("Account", account, ["company", "root_type", "account_currency", "is_group", "disabled"], as_dict=True)
    currency = frappe.db.get_value("Company", ctx.company, "default_currency")
    if not acc or acc.company != ctx.company or acc.root_type != "Asset" or acc.is_group or acc.disabled or acc.account_currency != currency:
        fail("Payment destination must be an enabled company-currency Asset ledger.")
    return mode, account, till


def _receive(source, ctx, payload):
    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
    payload = _payment_payload(payload)
    amount = number(payload.get("amount"))
    if source.doctype == "Sales Order":
        _open(source)
        if flt(source.per_billed) > EPS:
            fail("Record further payments against the delivered Sales Invoice.")
        outstanding = flt(source.grand_total)-flt(source.advance_paid)
    else:
        if source.is_return:
            fail("Use refund_customer for a return invoice.")
        # A "Bill the App Customer" partner order: the partner holds this money.
        from pet_app.api.delivery_partners import refuse_partner_collected_invoices
        refuse_partner_collected_invoices([source.name])
        outstanding = flt(source.outstanding_amount)
    if amount > outstanding+EPS:
        fail("Payment exceeds the balance due (a booked delivery fee is part of the invoice; an off-books fee is not).", "GOODS_PAYMENT_EXCEEDS_BALANCE")
    if amount < outstanding-EPS and not ctx_allows_partial(ctx):
        fail("This POS Profile does not allow partial payments.")
    mode, account, till = _payment_account(ctx, source, payload)
    pe = get_payment_entry(source.doctype, source.name, bank_account=account, bank_amount=amount, party_amount=amount)
    pe.paid_amount = pe.received_amount = amount
    pe.mode_of_payment = mode
    pe.reference_no = payload.get("external_reference") or frappe.local.driver_orders_operation
    pe.reference_date = nowdate()
    # Explicit allocation avoids payment terms distributing the full original balance.
    pe.set("references", [{"reference_doctype": source.doctype, "reference_name": source.name, "allocated_amount": amount}])
    stamp(pe, driver=source.custom_driver, branch=source.branch, warehouse=source.custom_fulfillment_warehouse,
        cash=source.custom_driver_cash_account, profile=till)
    pe.custom_driver_payment_kind = "Receipt"
    pe.custom_driver_order = source.name if source.doctype == "Sales Order" else None
    submit(pe)
    _assert_payment_ledger(pe, account, amount)
    return pe


def _assert_payment_ledger(pe, account, expected):
    landed = flt(frappe.db.sql("select coalesce(sum(debit-credit),0) from `tabGL Entry` where voucher_type='Payment Entry' and voucher_no=%s and account=%s and is_cancelled=0", (pe.name, account))[0][0])
    if abs(landed-expected) > 0.01:
        fail("Payment did not land in the expected account.", "DRIVER_PAYMENT_LEDGER_MISMATCH")


@frappe.whitelist(methods=["POST"])
@operation
def record_customer_payment(reference_doctype, reference_name, pos_profile=None, payment=None, idempotency_key=None):
    if reference_doctype not in ("Sales Order", "Sales Invoice"):
        fail("Payment reference must be Sales Order or Sales Invoice.")
    source, ctx = _managed(reference_doctype, reference_name, pos_profile)
    pe = _receive(source, ctx, payment)
    source.reload()
    return _result(source, pe, driver=source.custom_driver, branch=ctx.branch)


@frappe.whitelist(methods=["POST"])
@operation
def receive_driver_cash(driver, pos_profile=None, amount=None, cash_account=None, mode_of_payment=None,
                        branch=None, idempotency_key=None):
    """The driver hands over cash, into the till picked (any enabled till; Cash mode lands
    in its cash account), otherwise into the chosen Mode of Payment's account."""
    ctx = _money_context(branch, driver)
    d = frappe.get_doc("Driver", driver)
    permit("Driver", doc=d)
    account = cash_account or d.custom_cash_account
    allowed = {r["account"] for r in snapshot(driver)["cash_accounts"]}
    if account not in allowed:
        fail("This cash account does not belong to the driver's custody history.")
    validate_cash(account, ctx.company, driver)
    amount = number(amount)
    if amount > cash_balance(account)+EPS:
        fail("Handover exceeds the cash recorded with this driver.")
    mode = ctx_mode(ctx, mode_of_payment)
    destination, till = money_account(ctx.company, mode, pos_profile)
    _validate_destination(destination, ctx.company, account)
    pe = frappe.get_doc({"doctype": "Payment Entry", "company": ctx.company, "payment_type": "Internal Transfer",
        "posting_date": nowdate(), "paid_from": account, "paid_to": destination, "mode_of_payment": mode,
        "paid_amount": amount, "received_amount": amount, "source_exchange_rate": 1, "target_exchange_rate": 1})
    stamp(pe, driver=driver, branch=ctx.branch, cash=account, profile=till)
    pe.custom_driver_payment_kind = "Handover"
    submit(pe)
    _assert_payment_ledger(pe, account, -amount)
    _assert_payment_ledger(pe, destination, amount)
    return _result(pe, driver=driver, branch=ctx.branch)


def _money_context(branch=None, driver=None):
    """Till-less context for a driver money movement: the branch sent, else the user's,
    else the branch of the driver's latest order. Branch is a stamp here, not a stock source."""
    from pet_app.utils.branch import assert_can_write_to_branch, get_current_branch
    branch = cstr(branch).strip() or get_current_branch() or (frappe.db.get_value("Sales Order",
        {"custom_driver": driver, "custom_driver_flow": 1, "docstatus": 1}, "branch", order_by="modified desc") if driver else None)
    if branch:
        assert_can_write_to_branch(branch)
    return frappe._dict(profile=None, till=None, company=resolve_company(), branch=branch, warehouse=None)


def _validate_destination(account, company, source=None):
    row = frappe.db.get_value("Account", account, ["company", "root_type", "is_group", "disabled", "account_currency"], as_dict=True)
    if not row or row.company != company or row.root_type != "Asset" or row.is_group or row.disabled \
            or row.account_currency != frappe.db.get_value("Company", company, "default_currency"):
        fail(f"{account} must be an enabled company-currency Asset ledger.", "PAYMENT_MODE_ACCOUNT_MISSING")
    # Active drivers only: a Left driver (HR-DRI-2026-00022) still points at the shop's
    # own "Cash - K", which must stay a valid destination.
    if account == source or frappe.db.exists("Driver", {"custom_cash_account": account, "status": "Active"}):
        fail("Cash cannot be handed over into a driver's own cash account.")


@frappe.whitelist(methods=["POST"])
@operation
def return_sold_goods(invoice, pos_profile=None, items=None, idempotency_key=None):
    original, ctx = _managed("Sales Invoice", invoice, pos_profile)
    credit = _return_sold(original, ctx, items)
    return _result(credit, driver=original.custom_driver, branch=ctx.branch,
        notice="Goods are back in driver custody. Record physical branch receipt separately; the direct driver fee is not refunded by the shop.")


def _return_sold(original, ctx, items):
    """Credit note for sold driver goods; they return to the driver's custody and show up
    again as available quantity on their original load rows (which carry the order)."""
    if original.is_return:
        fail("Select the original sale, not a return invoice.")
    by_name = {r.name: r for r in original.items}
    available = {r.name: r for r in loads(original.custom_driver, ctx.branch)}
    lines, seen = [], set()
    for r in rows(items):
        name = r.get("invoice_item")
        if name not in by_name or name in seen:
            fail("Return must identify unique original invoice item rows.")
        seen.add(name)
        src = by_name[name]
        qty = number(r.get("qty"))
        returned = -flt(frappe.db.sql("""select coalesce(sum(i.qty),0) from `tabSales Invoice Item` i
            join `tabSales Invoice` p on p.name=i.parent where p.docstatus=1 and p.is_return=1 and i.sales_invoice_item=%s for update""", name)[0][0])
        if qty+returned > src.qty+EPS:
            fail("Return quantity exceeds the original unreturned quantity.")
        load = available.get(src.custom_driver_load_row)
        if not load:
            fail("Original load provenance is missing.")
        line = _line(src, -qty, load, request=r)
        line.update(sales_invoice_item=src.name, sales_order=src.sales_order, so_detail=src.so_detail)
        lines.append(line)
    ctx.customer = original.customer
    return _invoice(ctx, original.custom_driver, lines, return_against=original)


@frappe.whitelist(methods=["POST"])
@operation
def refund_customer(pos_profile=None, amount=None, payment=None, return_invoice=None, advance_payment=None, idempotency_key=None):
    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
    ctx = None
    amount = number(amount)
    payload = _payment_payload(payment)
    if bool(return_invoice) == bool(advance_payment):
        fail("Provide exactly one return_invoice or advance_payment.")
    if return_invoice:
        source = managed_doc("Sales Invoice", return_invoice)
        ctx = doc_context(source)
        ctx.till = cstr(pos_profile).strip() or None
        if not source.is_return or source.outstanding_amount >= -EPS or amount > -source.outstanding_amount+EPS:
            fail("Refund exceeds the outstanding credit on the return invoice.")
        mode, account, till = _payment_account(ctx, source, payload)
        pe = get_payment_entry("Sales Invoice", source.name, bank_account=account, bank_amount=amount, party_amount=-amount)
        pe.set("references", [{"reference_doctype": "Sales Invoice", "reference_name": source.name, "allocated_amount": -amount}])
    else:
        receipt = managed_doc("Payment Entry", advance_payment)
        if receipt.custom_driver_payment_kind != "Receipt" or receipt.payment_type != "Receive":
            fail("Choose an original customer advance receipt.")
        if not receipt.get("custom_driver_order"):
            fail("Advance refund requires an original Sales Order receipt.")
        source = managed_doc("Sales Order", receipt.custom_driver_order)
        ctx = doc_context(source)
        ctx.till = cstr(pos_profile).strip() or None
        if source.custom_delivery_state not in ("Completed", "Cancelled"):
            fail("Close the order remainder before refunding its unused advance.")
        # Unlink only the unused order allocation. Existing invoice allocations stay
        # intact. Refund against the resulting receipt credit, not a fully-paid SO.
        from erpnext.accounts.utils import unlink_ref_doc_from_payment_entries
        permit("Payment Entry", "write", receipt)
        unlink_ref_doc_from_payment_entries(source, receipt.name)
        receipt.reload()
        available = -flt(frappe.db.sql("""select coalesce(sum(amount_in_account_currency),0)
            from `tabPayment Ledger Entry` where against_voucher_type='Payment Entry' and against_voucher_no=%s
            and account=%s and delinked=0 for update""", (receipt.name, receipt.paid_from))[0][0])
        if amount > available+EPS:
            fail("Refund exceeds the unused advance on this receipt.")
        mode, account, till = _payment_account(ctx, source, payload)
        pe = frappe.get_doc({"doctype": "Payment Entry", "company": ctx.company, "payment_type": "Pay",
            "party_type": "Customer", "party": source.customer, "paid_from": account, "paid_to": receipt.paid_from,
            "posting_date": nowdate(), "source_exchange_rate": 1, "target_exchange_rate": 1})
        pe.custom_driver_refund_against = receipt.name
        pe.custom_driver_order = source.name
    if account == source.get("custom_driver_cash_account") and amount > cash_balance(account)+EPS:
        fail("Driver does not hold enough recorded cash for this refund.")
    pe.paid_amount = pe.received_amount = amount
    pe.mode_of_payment = mode
    pe.reference_no = payload.get("external_reference") or frappe.local.driver_orders_operation
    pe.reference_date = nowdate()
    stamp(pe, driver=source.custom_driver, branch=source.branch, warehouse=source.custom_fulfillment_warehouse,
        cash=source.custom_driver_cash_account, profile=till)
    pe.custom_driver_payment_kind = "Refund"
    submit(pe)
    if advance_payment:
        reconciliation = frappe.new_doc("Payment Reconciliation")
        reconciliation.update({"company": ctx.company, "party_type": "Customer", "party": source.customer,
            "receivable_payable_account": receipt.paid_from})
        reconciliation.get_unreconciled_entries()
        invoices = [r.as_dict() for r in reconciliation.invoices if r.invoice_number == pe.name]
        payments = [r.as_dict() for r in reconciliation.payments if r.reference_name == receipt.name]
        if len(invoices) != 1 or len(payments) != 1:
            fail("Unable to isolate this advance and refund for reconciliation.")
        reconciliation.allocate_entries(frappe._dict(invoices=invoices, payments=payments))
        reconciliation.reconcile()
        pe.reload()
    _assert_payment_ledger(pe, account, -amount)
    return _result(pe, driver=source.custom_driver, branch=ctx.branch)


def _read_gate(pos_profile=None):
    """Reads use native permissions only; a till sent by the client changes nothing."""
    if not enabled():
        fail("Driver orders are disabled.", "DRIVER_ORDERS_DISABLED")
    permit("Sales Order")
    return None


@frappe.whitelist()
@standardize_response
def get_driver_settlement_snapshot(driver, pos_profile=None):
    ctx = _read_gate(pos_profile)
    permit("Payment Entry")
    permit("Stock Entry")
    permit("Sales Order")
    return snapshot(driver, ctx.branch if ctx else visible_branches())


@frappe.whitelist()
@standardize_response
def get_order_detail(order, pos_profile=None):
    ctx = _read_gate(pos_profile)
    so = managed_doc("Sales Order", order, ctx)
    # Sales Order read is enough to open the order. Invoices and the driver's cash and
    # stock position are added only for users who may read those ledgers.
    can = {d: bool(frappe.has_permission(d)) for d in ("Sales Invoice", "Payment Entry", "Stock Entry")}
    invoices = frappe.get_all("Sales Invoice Item", filters={"sales_order": so.name, "docstatus": 1}, pluck="parent") if can["Sales Invoice"] else []
    settlement = None
    if so.custom_driver and all(can.values()):
        settlement = snapshot(so.custom_driver, ctx.branch if ctx else visible_branches())
    return {"order": so.as_dict(), "invoices": sorted(set(invoices)), "settlement": settlement,
        "settlement_visible": all(can.values()), "money_status": order_money_status([so.name]).get(so.name),
        "load_rows": _order_load_rows(so) if can["Stock Entry"] else []}


@frappe.whitelist()
@standardize_response
def list_pos_orders(pos_profile=None, state=None, driver=None, limit_start=0, page_length=30, settled=None):
    """`settled`: 1 = orders a Driver Settlement covered, 0 = settleable orders not yet settled."""
    ctx = _read_gate(pos_profile)
    # Without a till, get_list applies the user's Branch permissions to Sales Order.
    # get_list applies the user's permissions (incl. Branch User Permissions); no till filter.
    filters = {"custom_driver_flow": 1, "company": resolve_company(), "docstatus": 1}
    if state:
        filters["custom_delivery_state"] = state
    if driver:
        filters["custom_driver"] = driver
    meta = frappe.get_meta("Sales Order")
    if cstr(settled).strip() in ("0", "1") and meta.has_field("custom_driver_settlement"):
        if cint(settled):
            filters["custom_driver_settlement"] = ["is", "set"]
        else:
            filters["custom_driver_settlement"] = ["is", "not set"]
            filters["per_billed"] = [">", 0]
            if not state:
                filters["custom_delivery_state"] = ["in", SETTLEABLE_STATES]
    extra = [f for f in ("custom_driver_note", "custom_refusal_reason", "custom_refusal_note",
        "custom_return_reason", "custom_return_note") if meta.has_field(f)]
    result = frappe.get_list("Sales Order", filters=filters,
        fields=["name", "customer", "customer_name", "branch", "custom_driver", "custom_delivery_state",
            "grand_total", "advance_paid", "custom_delivery_fee", "custom_driver_fee_earned", "modified"] + extra,
        start=max(0, cint(limit_start)), page_length=max(1, min(100, cint(page_length))), order_by="modified desc")
    # What the driver reported from the road, so the cashier knows before the goods arrive.
    from pet_app.api.driver_self import _attempts
    attempts = _attempts([r.name for r in result])
    money = order_money_status([r.name for r in result])
    for r in result:
        r.update(money.get(r.name) or {})
        r["refusal_reason"] = r.pop("custom_refusal_reason", None)
        r["refusal_note"] = r.pop("custom_refusal_note", None)
        r["driver_note"] = r.pop("custom_driver_note", None)
        r["return_reason"] = r.pop("custom_return_reason", None)
        r["return_note"] = r.pop("custom_return_note", None)
        r["delivery_attempts"] = attempts.get(r.name, [])
    return {"orders": result}


# ---------------------------------------------------------------------------------------
# One-step driver settlement.
#
#     collected_amount == handed_over_amount + fee_amount + adjustment_amount
#
# `collected` is the cash the driver took on the covered orders (goods + booked fee),
# recomputed here from the posted receipts and refunds, never trusted from the client.
# ---------------------------------------------------------------------------------------

# Delivered and invoiced orders, including ones returned or cancelled after delivery: the
# cash the driver still holds from them (at least the fee) must be settleable. A refused
# order has no invoice and is never offered.
SETTLEABLE_STATES = ("Completed", "Returned", "Cancelled")


def _settlement_ready():
    if not frappe.db.exists("DocType", "Driver Settlement") or not frappe.get_meta("Sales Order").has_field("custom_driver_settlement"):
        fail("Driver settlement is not installed yet; run the pending migration.", "DRIVER_SETTLEMENT_NOT_INSTALLED")


def _settlement_start():
    """Orders delivered before this were handed over through Handover. Unset (Frappe
    returns a year-1 placeholder) fails closed: nothing is offered until it is set."""
    value = frappe.db.get_single_value("Pet App Accounting Settings", "custom_driver_settlement_start")
    if not value or str(value) < "2000-01-01":
        fail("Set the Driver Settlement Start Date in Pet App Accounting Settings.", "DRIVER_SETTLEMENT_NOT_INSTALLED")
    return value


def _order_figures(orders):
    """{order: {invoice, posting_date, collected_amount, fee_amount, cash_account}}.

    collected = receipts paid INTO the order's driver-cash snapshot, minus refunds the
    driver paid OUT of it, across the order and every invoice (and return) billed from it.
    """
    orders = {o.name: o for o in orders}
    if not orders:
        return {}
    names = list(orders)
    invoices = frappe.db.sql("""select distinct i.sales_order, p.name, p.posting_date, p.is_return,
            coalesce(p.custom_driver_fee_booked, 0) as fee
        from `tabSales Invoice Item` i join `tabSales Invoice` p on p.name=i.parent
        where p.docstatus=1 and i.sales_order in %(names)s""", {"names": names}, as_dict=True)
    result = {n: frappe._dict(invoice=None, posting_date=None, collected_amount=0.0, fee_amount=0.0,
        other_collected=0.0, cash_account=orders[n].custom_driver_cash_account, docs={n}) for n in names}
    for row in invoices:
        figure = result[row.sales_order]
        figure.docs.add(row.name)
        if not row.is_return:
            figure.fee_amount += flt(row.fee)
            if not figure.posting_date or row.posting_date > figure.posting_date:
                figure.posting_date, figure.invoice = row.posting_date, row.name
    by_doc = {doc: order for order, figure in result.items() for doc in figure.docs}
    payments = frappe.db.sql("""select distinct pe.name, r.reference_name, pe.custom_driver_order, pe.paid_amount,
            pe.paid_from, pe.paid_to, pe.custom_driver_payment_kind as kind
        from `tabPayment Entry` pe left join `tabPayment Entry Reference` r on r.parent=pe.name
        where pe.docstatus=1 and pe.custom_driver_flow=1 and pe.custom_driver_payment_kind in ('Receipt', 'Refund')
          and (r.reference_name in %(docs)s or pe.custom_driver_order in %(names)s)""",
        {"docs": list(by_doc), "names": names}, as_dict=True)
    seen = set()
    for pe in payments:
        order = by_doc.get(pe.reference_name) or (pe.custom_driver_order if pe.custom_driver_order in result else None)
        if not order or pe.name in seen:
            continue
        seen.add(pe.name)
        cash = result[order].cash_account
        if pe.kind == "Receipt" and pe.paid_to == cash:
            result[order].collected_amount += flt(pe.paid_amount)
        elif pe.kind == "Refund" and pe.paid_from == cash:
            result[order].collected_amount -= flt(pe.paid_amount)
        elif pe.kind == "Receipt":
            # Card, wallet, bank, or paid at the shop: the customer paid, but not into the driver's hand.
            result[order].other_collected += flt(pe.paid_amount)
    return result


def _settlement_start_or_none():
    value = frappe.db.get_single_value("Pet App Accounting Settings", "custom_driver_settlement_start")
    return value if value and str(value) >= "2000-01-01" else None


def order_money_status(names):
    """{order: status} for order rows. Two separate answers:

    - the driver's side: settlement_status "settled" (stamped by a Driver Settlement),
      "pending" (delivered and invoiced on/after the settlement start, not yet settled) or
      "not_applicable" (not delivered yet, refused, or handed over before the start date);
    - the customer's side: payment_status Paid / Partly Paid / Unpaid / Refund Due (None
      before an invoice), from the order's invoices and returns. A Due or card sale can be
      driver-settled while the customer still owes the shop.
    """
    names = [n for n in names if n]
    if not names:
        return {}
    settled_field = frappe.get_meta("Sales Order").has_field("custom_driver_settlement")
    orders = frappe.get_all("Sales Order", filters={"name": ["in", names]}, fields=["name", "custom_delivery_state",
        "custom_driver_cash_account"] + (["custom_driver_settlement"] if settled_field else []))
    figures = _order_figures(orders)
    totals = {}
    for row in frappe.db.sql("""select distinct i.sales_order, p.name, p.grand_total, p.outstanding_amount
            from `tabSales Invoice Item` i join `tabSales Invoice` p on p.name=i.parent
            where p.docstatus=1 and i.sales_order in %(names)s""", {"names": names}, as_dict=True):
        total = totals.setdefault(row.sales_order, [0.0, 0.0])
        total[0] += flt(row.grand_total)
        total[1] += flt(row.outstanding_amount)
    start = _settlement_start_or_none()
    result = {}
    for o in orders:
        f = figures[o.name]
        settlement = o.get("custom_driver_settlement") or None
        if settlement:
            settlement_status = "settled"
        elif (o.custom_delivery_state in SETTLEABLE_STATES and f.invoice and start
                and str(f.posting_date) >= str(start)):
            settlement_status = "pending"
        else:
            settlement_status = "not_applicable"
        invoiced, outstanding = totals.get(o.name, (0.0, 0.0))
        if not f.invoice:
            payment_status = None
        elif outstanding < -EPS:
            payment_status = "Refund Due"
        elif outstanding <= EPS:
            payment_status = "Paid"
        elif outstanding >= invoiced - EPS:
            payment_status = "Unpaid"
        else:
            payment_status = "Partly Paid"
        result[o.name] = {"driver_settlement": settlement, "settlement_status": settlement_status,
            "is_settled": bool(settlement), "payment_status": payment_status,
            "invoiced_amount": flt(invoiced), "outstanding_amount": flt(outstanding),
            "cash_collected": flt(f.collected_amount), "other_collected": flt(f.other_collected)}
    return result


def _settlement_orders(driver, branch=None, *, unsettled_only=False, from_date=None, to_date=None, names=None):
    filters = {"custom_driver_flow": 1, "custom_driver": driver, "docstatus": 1,
        "custom_delivery_state": ["in", SETTLEABLE_STATES], "company": resolve_company()}
    if branch:
        filters["branch"] = branch
    if names is not None:
        filters["name"] = ["in", names or [""]]
    orders = frappe.get_all("Sales Order", filters=filters, fields=["name", "customer", "customer_name",
        "custom_delivery_state", "custom_driver_settlement", "custom_driver_cash_account", "branch"])
    figures = _order_figures(orders)
    start = _settlement_start()
    rows = []
    for o in orders:
        f = figures[o.name]
        if not f.invoice or (start and str(f.posting_date) < str(start)):
            continue
        if unsettled_only and o.custom_driver_settlement:
            continue
        if not unsettled_only and ((from_date and str(f.posting_date) < str(from_date)) or (to_date and str(f.posting_date) > str(to_date))):
            continue
        rows.append(frappe._dict(order=o.name, invoice=f.invoice, posting_date=f.posting_date, customer=o.customer,
            customer_name=o.customer_name, custom_delivery_state=o.custom_delivery_state,
            collected_amount=flt(f.collected_amount), fee_amount=flt(f.fee_amount),
            settlement=o.custom_driver_settlement, cash_account=f.cash_account, branch=o.branch))
    rows.sort(key=lambda r: (str(r.posting_date), r.order), reverse=True)
    return rows


def _settlement_read(driver, pos_profile):
    ctx = _read_gate(pos_profile)
    _settlement_ready()
    permit("Payment Entry")
    if driver:
        permit("Driver", doc=frappe.get_doc("Driver", driver))
    return ctx.branch if ctx else visible_branches()


def _public(row):
    return {k: row[k] for k in ("order", "invoice", "posting_date", "customer", "customer_name",
        "custom_delivery_state", "collected_amount", "fee_amount", "settlement")}


@frappe.whitelist()
@standardize_response
def get_driver_period_summary(driver, pos_profile=None, from_date=None, to_date=None):
    branch = _settlement_read(driver, pos_profile)
    period = _settlement_orders(driver, branch, from_date=from_date, to_date=to_date)
    unsettled = _settlement_orders(driver, branch, unsettled_only=True)
    filters = {"driver": driver}
    if from_date or to_date:
        filters["posting_date"] = ["between", [from_date or "1900-01-01", to_date or "2999-12-31"]]
    handed = frappe.get_all("Driver Settlement", filters=filters, pluck="handed_over_amount")
    return {"collected_amount": sum(r.collected_amount for r in period),
        "fee_amount": sum(r.fee_amount for r in period),
        "handed_over_amount": sum(flt(h) for h in handed),
        "net_due": sum(r.collected_amount - r.fee_amount for r in unsettled),
        "unsettled_count": len(unsettled),
        "settled_count": sum(1 for r in period if r.settlement)}


@frappe.whitelist()
@standardize_response
def list_driver_settlement_orders(driver, pos_profile=None, from_date=None, to_date=None, unsettled_only=0,
                                  limit_start=0, page_length=50):
    branch = _settlement_read(driver, pos_profile)
    rows = _settlement_orders(driver, branch, unsettled_only=bool(cint(unsettled_only)),
        from_date=from_date, to_date=to_date)
    start, length = max(0, cint(limit_start)), max(1, min(100, cint(page_length)))
    return {"orders": [_public(r) for r in rows[start:start+length]], "total": len(rows)}


@frappe.whitelist()
@standardize_response
def list_driver_settlements(driver=None, pos_profile=None, limit_start=0, page_length=20):
    branch = _settlement_read(driver, pos_profile)
    filters = {}
    if driver:
        filters["driver"] = driver
    if branch:
        filters["branch"] = branch
    fields = ["name", "driver", "posting_date", "from_date", "to_date", "collected_amount", "fee_amount",
        "adjustment_amount", "adjustment_reason", "handed_over_amount", "mode_of_payment", "payment_entry",
        "journal_entry", "order_count", "remarks"]
    rows = frappe.get_all("Driver Settlement", filters=filters, fields=fields, order_by="posting_date desc, creation desc",
        start=max(0, cint(limit_start)), page_length=max(1, min(100, cint(page_length))))
    return {"settlements": rows, "total": frappe.db.count("Driver Settlement", filters)}


@frappe.whitelist(methods=["POST"])
@operation
def create_driver_settlement(driver, pos_profile=None, from_date=None, to_date=None, posting_date=None, orders=None,
                             collected_amount=0, fee_amount=0, adjustment_amount=0, adjustment_reason=None,
                             handed_over_amount=0, mode_of_payment=None, remarks=None, idempotency_key=None):
    from pet_app.utils.driver_orders import fee_balance
    _settlement_ready()
    # The same gate as a Handover: money changes hands between the shop and the driver.
    permit("Payment Entry", "create")
    permit("Payment Entry", "submit")
    d = frappe.get_doc("Driver", driver)
    permit("Driver", doc=d)
    if not cstr(mode_of_payment).strip():
        fail("Choose the Mode of Payment the money moves through.", "PAYMENT_MODE_REQUIRED")

    names = orders
    if isinstance(names, str):
        names = frappe.parse_json(names)
    if not isinstance(names, list) or not names or len(set(names)) != len(names) or not all(isinstance(n, str) and n for n in names):
        fail("orders must be a non-empty list of unique Sales Order names.")
    if not cstr(pos_profile).strip():
        fail("Choose the till the settlement goes through.", "DRIVER_TILL_REQUIRED")
    # Any enabled till: no assignment check. The orders need only be this driver's and
    # readable by the user; they may come from any branch.
    first = managed_doc("Sales Order", sorted(names)[0])
    ctx = doc_context(first)
    ctx.branch = frappe.db.get_value("POS Profile", pos_profile, "branch") or ctx.branch
    mode = ctx_mode(ctx, mode_of_payment)
    destination, till_name = money_account(ctx.company, mode, pos_profile)
    till = frappe._dict(profile=till_name, cash_account=destination)
    for name in sorted(names):
        so = managed_doc("Sales Order", name)
        if so.custom_driver != driver:
            fail(f"{name} is not assigned to driver {driver}.")
        if so.get("custom_driver_settlement"):
            fail(f"{name} is already settled in {so.custom_driver_settlement}.", "ORDER_ALREADY_SETTLED")
        if so.custom_delivery_state not in SETTLEABLE_STATES:
            fail(f"{name} is not delivered and closed yet; only completed orders can be settled.")
    rows = {r.order: r for r in _settlement_orders(driver, None, names=names)}
    missing = [n for n in names if n not in rows]
    if missing:
        fail(f"{', '.join(missing)} cannot be settled here: no delivery invoice, or delivered before driver settlement started (use Handover).")
    cash_accounts = {rows[n].cash_account for n in names}
    if len(cash_accounts) != 1 or None in cash_accounts:
        fail("Settle orders from one driver cash account at a time.")
    cash = cash_accounts.pop()
    validate_cash(cash, ctx.company, driver)

    collected = flt(sum(rows[n].collected_amount for n in names), 2)
    fee = number(fee_amount, zero=True)
    adjustment = flt(adjustment_amount)
    handed = flt(handed_over_amount)
    if abs(collected - flt(collected_amount)) > 0.005:
        fail(f"Collected on these orders is {collected}, not {flt(collected_amount)}. Reload the orders.", "SETTLEMENT_DOES_NOT_TIE_OUT")
    if abs(collected - (handed + fee + adjustment)) > 0.005:
        fail(f"Collected {collected} must equal handed over {handed} + fees {fee} + adjustment {adjustment}.", "SETTLEMENT_DOES_NOT_TIE_OUT")
    if abs(adjustment) > EPS and not cstr(adjustment_reason).strip():
        fail("Give a reason for the adjustment.", "SETTLEMENT_DOES_NOT_TIE_OUT")
    if collected <= EPS and fee <= EPS:
        fail("Nothing to settle on these orders.")
    if collected > cash_balance(cash) + EPS:
        fail("The driver's cash account holds less than these orders collected; part was already handed over. Adjust through Handover first.")
    fees = fee_config()
    fee_account = d.get("custom_fee_account") if fees else None
    if fee > EPS:
        if not fee_account:
            fail("This driver has no fee account; fees are not booked yet.", "DRIVER_FEE_NOT_CONFIGURED")
        if fee > fee_balance(fee_account) + EPS:
            fail(f"Fees exceed what the shop owes this driver ({fee_balance(fee_account)}).", "SETTLEMENT_DOES_NOT_TIE_OUT")
    if abs(adjustment) > EPS:
        adjust_account = (fees or {}).get("shortage" if adjustment > 0 else "overage")
        if not adjust_account:
            fail("Configure the driver shortage and overage accounts in Pet App Accounting Settings.", "DRIVER_FEE_NOT_CONFIGURED")

    _validate_destination(destination, ctx.company, cash)
    settlement = frappe.get_doc({"doctype": "Driver Settlement", "driver": driver, "company": ctx.company,
        "branch": ctx.branch, "pos_profile": till_name, "posting_date": posting_date or nowdate(),
        "from_date": from_date, "to_date": to_date, "fee_amount": fee, "adjustment_amount": adjustment,
        "adjustment_reason": adjustment_reason, "handed_over_amount": handed,
        "mode_of_payment": mode,
        "cash_account": cash, "fee_account": fee_account, "till_account": till.cash_account, "remarks": remarks,
        "orders": [{"sales_order": n, "sales_invoice": rows[n].invoice, "posting_date": rows[n].posting_date,
            "customer": rows[n].customer, "customer_name": rows[n].customer_name,
            "collected_amount": rows[n].collected_amount, "fee_amount": rows[n].fee_amount} for n in names]})
    settlement.insert(ignore_permissions=True)

    cost_center = frappe.get_cached_value("Company", ctx.company, "cost_center")
    lines = []
    def line(account, amount):
        if abs(amount) > EPS:
            lines.append({"account": account, "cost_center": cost_center, "branch": ctx.branch,
                "debit_in_account_currency": amount if amount > 0 else 0,
                "credit_in_account_currency": -amount if amount < 0 else 0})
    line(cash, -collected)
    line(fee_account, fee)
    if abs(adjustment) > EPS:
        line(adjust_account, adjustment)
    line(till.cash_account, handed)
    je = frappe.get_doc({"doctype": "Journal Entry", "voucher_type": "Journal Entry", "company": ctx.company,
        "posting_date": settlement.posting_date, "accounts": lines, "cheque_no": settlement.name,
        "cheque_date": settlement.posting_date,
        "user_remark": f"Driver settlement {settlement.name}: {len(names)} orders for driver {driver}",
        "custom_driver_flow": 1, "custom_driver_operation": frappe.local.driver_orders_operation,
        "custom_driver": driver, "custom_driver_journal_kind": "Settlement"})
    je.flags.ignore_permissions = True
    je.insert(ignore_permissions=True)
    je.submit()
    settlement.db_set("journal_entry", je.name)
    for n in names:
        frappe.db.set_value("Sales Order", n, "custom_driver_settlement", settlement.name, update_modified=False)
    _assert_journal_ledger(je, cash, -collected)
    _assert_journal_ledger(je, till.cash_account, handed)
    body = f"{len(names)} طلبات — سلّمت {handed:,.0f}، أجورك {fee:,.0f}"
    if abs(adjustment) > EPS:
        body += f"، تسوية {adjustment:,.0f}"
    notify_driver(driver, f"تمت تسوية حسابك: {settlement.name} — {body}", "Driver Settlement", settlement.name, body=body)
    return {"settlement": settlement.name, "journal_entry": je.name, "payment_entry": None,
        "settled_orders": names, "documents": [{"doctype": "Driver Settlement", "name": settlement.name},
            {"doctype": "Journal Entry", "name": je.name}]}


def _assert_journal_ledger(je, account, expected):
    landed = flt(frappe.db.sql("select coalesce(sum(debit-credit),0) from `tabGL Entry` where voucher_type='Journal Entry' and voucher_no=%s and account=%s and is_cancelled=0", (je.name, account))[0][0])
    if abs(landed-expected) > 0.01:
        fail("Settlement did not land in the expected account.", "DRIVER_PAYMENT_LEDGER_MISMATCH")
