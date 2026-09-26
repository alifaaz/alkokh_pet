"""Settle a customer's already-open Draft Sales Invoice at the till.

MEDICATION_BILLING_CONTRACT §6 says stock does not move at visit close, because the
visit's Sales Invoice is inserted as Draft only; ERPNext relieves it when the cashier
SUBMITS that draft. POS had no knowledge of the draft, so it raised a second invoice
beside it and the dispensed stock was never relieved. This closes that loop: the
cashier's counter items are appended to the clinic's own draft and the one document is
submitted, so one stock movement covers both blocks.

Stock relief - the flag is read, never written
----------------------------------------------
`update_stock` is a HEADER field with no per-row equivalent, so one flag governs the
clinic's lines and the counter's together. On this site the draft's own flag is already
the correct answer and this endpoint never changes it:

  * `utils.invoice_reuse.find_open_invoice` keys reuse on `update_stock`, so a draft is
    homogeneous by construction and a customer may legitimately hold two open drafts.
  * A medication line whose goods already left at dispense is emitted with an explicit
    empty `warehouse` by `vet_visit._get_stock_invoice_context`, which drops the header
    flag to 0 for that draft.

So `update_stock = 1` means "nothing on this document has been relieved yet" and
`update_stock = 0` means "do not relieve anything from this document". Appending a
stock line to a `update_stock = 0` draft would silently never move its stock, and
flipping the flag to 1 would retroactively relieve clinic lines that may already have
been issued at the bedside. Neither is acceptable, so a counter stock item destined for
a non-stock draft is refused with STOCK_FLAG_CONFLICT and the caller raises its own
invoice instead.

Linked visits
-------------
Submitting changes nothing about visit completion. A visit is locked by
`VetVisit._validate_sales_invoice_lock` the moment `sales_invoice` is written, which
`_mark_visit_invoiced` does at DRAFT CREATION, not at submit - and no `on_submit` hook
on Sales Invoice touches Vet Visit. Every visit sharing this draft is therefore already
locked before the cashier sees it, and settling adds no new lock. The reverse direction
still works: `on_cancel` runs `visit_billing.on_sales_invoice_cancel`, which releases
EVERY record linked to the invoice, not just one.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, get_datetime, getdate, nowdate

from erpnext.stock.stock_ledger import is_negative_stock_allowed

from pet_app.api.accounting.cashier import (
    _get_cashier_employee,
    _has_accounting_record_permission,
    _is_accounting_user,
)
from pet_app.api.response import fail, standardize_response
from pet_app.utils.api_response import raise_api_error
from pet_app.utils.branch import resolve_branch_for_pos
from pet_app.utils.invoice_reuse import get_or_create_open_invoice, resolve_company
from pet_app.utils.invoice_source import append_marker, parse_markers, strip_markers

# Currency rounding slack. IQD has no minor unit; anything smaller is float noise.
AMOUNT_EPSILON = 0.005

# Source marker doctype for a counter line. The same vocabulary the settlement methods use
# for the lines they append, so a POS line is recognisable wherever it ends up.
POS_SOURCE_DOCTYPE = "POS Profile"

# Marker written into `remarks` so a retried request can find the sale - or the
# settlement - it already made. A till retry is a till retry either way.
POS_SALE_KEY_PREFIX = "[alkokh-pos-sale:"
POS_SALE_KEY_SUFFIX = "]"

# Per-row columns that must survive the append untouched. A blank value being FILLED IN
# by ERPNext is fine (it is completing the row, not losing anything); a value that was
# set and is now different or gone is a silent data loss and aborts the settlement.
PRESERVED_ITEM_FIELDS = (
    "item_code",
    "qty",
    "rate",
    "warehouse",
    "uom",
    "conversion_factor",
    "batch_no",
    "serial_no",
    "income_account",
    "cost_center",
)


def _err(message, code, **details):
    raise_api_error(message, code=code, details=details or None)


def _may_operate_pos(user: str | None = None) -> bool:
    """Whether this user may settle or ring up at a till.

    Deliberately wide: standing at a till IS the authority to take money for what is in
    front of you, and every user on this site holding a till role already satisfies it.
    Split out from `_require_pos_operator` so `probe=1` can answer the same question
    without raising - the client asks it to decide whether to show the affordance at all.
    """
    if _is_accounting_user(user):
        return True
    return _has_accounting_record_permission(
        ("Sales Invoice", "submit"), ("Sales Invoice", "write"), user=user
    )


def _require_pos_operator():
    if _may_operate_pos():
        return
    # A PetAppAPIError, not frappe.PermissionError, purely so the envelope carries
    # meta.code. A bare PermissionError reaches the client as code "PermissionError",
    # indistinguishable from any other failure, so the till can do nothing better than
    # toast it and offer the same refusal again on the next attempt.
    _err(
        _("You are not authorized to settle invoices at the till."),
        "POS_SETTLE_NOT_PERMITTED",
        user=frappe.session.user,
    )


def _coerce_rows(value, field: str = "extra_items", code: str = "INVALID_EXTRA_ITEMS") -> list[dict]:
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            _err(_("{0} is not valid JSON.").format(field), code)
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        _err(_("{0} must be a list.").format(field), code)
    return [row for row in value if isinstance(row, dict)]


def _is_stock_item(item_code: str) -> bool:
    return bool(cint(frappe.get_cached_value("Item", item_code, "is_stock_item")))


def _row_snapshot(doc) -> dict:
    return {
        row.name: {field: row.get(field) for field in PRESERVED_ITEM_FIELDS}
        for row in doc.items or []
        if row.name
    }


def _assert_rows_preserved(doc, before: dict):
    """The clinic's rows must come back byte-identical, or nothing is submitted.

    `buildSalesInvoiceItems` on the client emits only item_code/description/qty/rate and
    the price mirrors, and `UpdateSalesInvoice` is a full-document save - so there is a
    live question about whether per-row warehouse/uom/batch survive a round trip. This
    endpoint does not have to answer it: it takes the answer at runtime and refuses to
    submit a document whose existing rows changed under it.
    """
    after = _row_snapshot(doc)
    for row_name, original in before.items():
        current = after.get(row_name)
        if current is None:
            _err(
                _("Existing invoice line {0} disappeared while appending counter items.").format(row_name),
                "EXISTING_ROWS_ALTERED",
                row=row_name,
            )
        for field, was in original.items():
            now = current.get(field)
            if was in (None, "", 0) and now not in (None, ""):
                # A blank being completed by ERPNext is the row being finished, not lost.
                continue
            if field in ("qty", "rate", "conversion_factor"):
                if abs(flt(was) - flt(now)) > AMOUNT_EPSILON:
                    _err(
                        _("Existing invoice line {0} had {1} changed from {2} to {3}.").format(
                            row_name, field, was, now
                        ),
                        "EXISTING_ROWS_ALTERED",
                        row=row_name, field=field, was=was, now=now,
                    )
                continue
            if cstr(was) != cstr(now):
                _err(
                    _("Existing invoice line {0} had {1} changed from {2} to {3}.").format(
                        row_name, field, was or _("(blank)"), now or _("(blank)")
                    ),
                    "EXISTING_ROWS_ALTERED",
                    row=row_name, field=field, was=cstr(was), now=cstr(now),
                )


def _sale_key_marker(idempotency_key: str) -> str:
    return f"{POS_SALE_KEY_PREFIX}{cstr(idempotency_key).strip()}{POS_SALE_KEY_SUFFIX}"


def _append_note(doc, note: str) -> None:
    note = cstr(note).strip()
    if not note:
        return
    existing = cstr(doc.remarks).rstrip()
    if note in existing:
        return
    doc.remarks = f"{existing}\n{note}" if existing else note


def _counter_rows_from_markers(doc, profile_name: str) -> set[str]:
    """Which rows this till appended, reconstructed from the provenance markers.

    Only needed on the replay path, where the append happened in a request whose response
    the client never saw and the in-memory row set is gone. Every appended line carries an
    [alkokh-source-group:POS Profile:<till>] marker, so the document itself records which
    block is the counter's and the reprinted receipt splits exactly where the first did.
    """
    names = set()
    for row in doc.items or []:
        for doctype, name in parse_markers(row.description):
            if doctype == POS_SOURCE_DOCTYPE and name == profile_name:
                names.add(row.name)
                break
    return names


def _payment_summary_from_doc(doc) -> dict | None:
    """The payment block rebuilt from a document that was settled by an earlier request."""
    rows = doc.get("payments") or []
    if not rows:
        return None
    row = rows[0]
    return {
        "mode_of_payment": row.mode_of_payment,
        "account": row.account,
        "amount": flt(row.amount),
        "tendered": flt(row.amount),
        # Unknowable after the fact, and zero is the honest answer: the drawer balanced
        # when the sale went through, whatever was handed over at the time.
        "change": 0.0,
    }


def _lock_invoice(invoice: str):
    """SELECT ... FOR UPDATE, before anything is read from the document.

    This is the fix for the lost update between a vet saving a visit and the till
    settling it. A client-side re-read cannot substitute for it: `UpdateSalesInvoice`
    re-fetches first, so Frappe's own check_if_latest never fires and one side's lines
    disappear with no error at all.
    """
    rows = frappe.db.sql(
        """
        SELECT name, modified, docstatus, IFNULL(is_return, 0) AS is_return,
               IFNULL(is_pos, 0) AS is_pos, customer, company, remarks,
               IFNULL(update_stock, 0) AS update_stock,
               IFNULL(total_advance, 0) AS total_advance
        FROM `tabSales Invoice`
        WHERE name = %s
        FOR UPDATE
        """,
        invoice,
        as_dict=True,
    )
    if not rows:
        _err(_("Sales Invoice {0} was not found.").format(invoice), "INVOICE_NOT_FOUND", invoice=invoice)
    return rows[0]


def _validate_settle_target(locked, company: str, customer: str | None) -> None:
    """Refuse anything that is not the document we were promised. Never coerce."""
    invoice = locked.name
    if cint(locked.docstatus) != 0:
        _err(
            _("Sales Invoice {0} is not a Draft.").format(invoice),
            "INVOICE_NOT_DRAFT", invoice=invoice, docstatus=cint(locked.docstatus),
        )
    if cint(locked.is_return):
        _err(_("Sales Invoice {0} is a credit note.").format(invoice), "INVOICE_IS_RETURN", invoice=invoice)
    if cint(locked.is_pos):
        _err(
            _("Sales Invoice {0} is already a POS invoice.").format(invoice),
            "INVOICE_ALREADY_POS", invoice=invoice,
        )
    if flt(locked.total_advance) > AMOUNT_EPSILON:
        # ERPNext skips update_against_document_in_jv() outright when is_pos is set
        # (sales_invoice.py: `if cint(self.is_pos) != 1 and not self.is_return`), so a POS
        # invoice NEVER turns its `advances` rows into Payment Entry References -
        # update_outstanding_amt then rebuilds outstanding from GL entries that were never
        # written. Settling here would take the customer's money, leave the invoice
        # outstanding for the amount already prepaid, and leave the prepayment sitting on
        # the customer as credit: charged once, owing twice.
        #
        # Measured on a 200,000 stay with 120,000 of allocated advances: settled at the
        # till for the correct 80,000, submitted with outstanding 120,000, all three
        # deposits still unallocated.
        #
        # Boarding is the only writer of advances in this app and its invoices are now
        # submitted at check-out, so nothing should reach this line. A draft that does is a
        # check-out whose submit failed, and it is paid as an ordinary invoice - not here.
        _err(
            _(
                "Sales Invoice {0} already carries {1} in prepayments. Settling it at the "
                "till would strand them. Record the payment against the invoice instead."
            ).format(invoice, flt(locked.total_advance)),
            "INVOICE_CARRIES_ADVANCES",
            invoice=invoice, total_advance=flt(locked.total_advance),
        )
    if locked.company != company:
        _err(
            _("Sales Invoice {0} belongs to {1}, not {2}.").format(invoice, locked.company, company),
            "INVOICE_COMPANY_MISMATCH", invoice=invoice,
        )
    if cstr(customer).strip() and cstr(customer).strip() != locked.customer:
        _err(
            _("Sales Invoice {0} belongs to {1}, not {2}.").format(invoice, locked.customer, customer),
            "INVOICE_CUSTOMER_MISMATCH", invoice=invoice,
        )


def _prepare_counter_lines(extra_rows, *, profile, default_warehouse: str, update_stock, invoice: str):
    """Validate the counter's cart into Sales Invoice Item payloads for an existing draft.

    Stock is resolved HERE, before a single row is appended. The draft's own
    `update_stock` is the authority and is never rewritten - see the module docstring - so
    a stock item aimed at a non-stock draft is refused rather than sold off a shelf
    nothing will relieve.
    """
    prepared = []
    for index, row in enumerate(extra_rows):
        item_code = cstr(row.get("item_code")).strip()
        if not item_code:
            _err(_("Counter line {0} is missing item_code.").format(index + 1), "EXTRA_ITEM_INVALID")
        if not frappe.db.exists("Item", item_code):
            _err(_("Item {0} was not found.").format(item_code), "ITEM_NOT_FOUND", item_code=item_code)
        qty = flt(row.get("qty"))
        if qty <= 0:
            _err(
                _("Counter line for {0} must have qty greater than zero.").format(item_code),
                "EXTRA_ITEM_INVALID", item_code=item_code,
            )
        rate = flt(row.get("rate"))
        if rate < 0:
            _err(
                _("Counter line for {0} must not have a negative rate.").format(item_code),
                "EXTRA_ITEM_INVALID", item_code=item_code,
            )

        line = {
            "item_code": item_code,
            "qty": qty,
            "rate": rate,
            "description": append_marker(
                cstr(row.get("item_name")).strip() or item_code, POS_SOURCE_DOCTYPE, profile.name
            ),
        }
        if cstr(row.get("item_name")).strip():
            line["item_name"] = cstr(row.get("item_name")).strip()
        if cstr(row.get("uom")).strip():
            line["uom"] = cstr(row.get("uom")).strip()

        if _is_stock_item(item_code):
            if not cint(update_stock):
                _err(
                    _(
                        "{0} is a stock item, but Sales Invoice {1} does not move stock. "
                        "Appending it here would sell the goods without relieving them. "
                        "Raise a separate POS invoice for the counter items."
                    ).format(item_code, invoice),
                    "STOCK_FLAG_CONFLICT",
                    item_code=item_code, invoice=invoice, update_stock=0,
                )
            row_warehouse = cstr(row.get("warehouse")).strip() or default_warehouse
            if not row_warehouse:
                _err(
                    _("A warehouse is required for stock item {0}.").format(item_code),
                    "COUNTER_ITEM_WAREHOUSE_REQUIRED", item_code=item_code,
                )
            line["warehouse"] = row_warehouse
        prepared.append(line)
    return prepared


def _apply_document_discount(doc, discount_type, discount_value) -> None:
    kind = cstr(discount_type).strip().lower()
    if not kind:
        return
    doc.apply_discount_on = "Grand Total"
    if kind in ("percentage", "percent", "%"):
        doc.additional_discount_percentage = flt(discount_value)
        doc.discount_amount = 0
    elif kind in ("amount", "fixed", "value"):
        doc.additional_discount_percentage = 0
        doc.discount_amount = flt(discount_value)
    else:
        _err(
            _("discount_type must be 'Percentage' or 'Amount'."),
            "INVALID_DISCOUNT_TYPE", discount_type=discount_type,
        )


def _stamp_pos_fields(doc, profile, account: str) -> None:
    """Mark the document as the till's, only where the field really exists.

    `Sales Invoice.cashier` is a Link to Employee, not to User, and `cashier_cash_account`
    does not exist on this site at all. Each value is written only where the field is
    really there and the value really resolves - a missing Employee record must not cost
    the customer their sale.
    """
    doc.is_pos = 1
    doc.pos_profile = profile.name
    stamps = {
        "custom_pos_profile": profile.name,
        "custom_cashier_user": frappe.session.user,
        "cash_bank_account": account,
        "cashier_cash_account": account,
    }
    cashier_employee = _get_cashier_employee()
    if cashier_employee:
        stamps["cashier"] = cashier_employee
    for fieldname, value in stamps.items():
        if value and doc.meta.has_field(fieldname):
            doc.set(fieldname, value)


def _resolve_partner_context(
    delivery_partner, partner_order_ref, partner_customer_name, partner_commission_rate,
    *, customer: str, is_paid: bool, tendered: float, partner_commission_amount=None,
    pos_profile=None,
):
    """The partner arguments, validated together, or None for an ordinary sale.

    A DELIVERY PARTNER ORDER IS NEVER CASH IN THE TILL. Talabat charges the customer at
    their end and transfers the month's takings minus commission; nobody at this counter
    receives a fils of it. `expected_cash_on_hand` is a raw sum over the cashier's cash
    account, so a partner order that touched it would tell the cashier to hand over money
    a delivery app is holding, and the drawer would come up short by the value of every
    app order that shift - discovered by whoever counts it, hours later, with nothing to
    point at. Hence: `Due`, `is_pos = 0`, no payment row, an ordinary receivable against
    the partner's own Customer - or, for an `is_inside` partner, against the real customer
    the cashier picked. Either way the partner holds the money and settles it later.

    The refusal ORDER is fixed by the contract, because the client renders the first code
    it receives as the on-screen block beside Process Transaction. Inactive first (nothing
    else about the order matters if the partner is switched off), then the payment (the
    money is the dangerous part), then the customer, then the reference.

    Every message here is read by a cashier mid-sale, so each one says what to do rather
    than what is structurally wrong.
    """
    partner = cstr(delivery_partner).strip()
    if not partner:
        # The frontend omits the whole group on an ordinary sale, so a counter sale's body
        # is unchanged from today and never reaches any of this.
        return None

    row = frappe.db.get_value(
        "Delivery Partner",
        partner,
        [
            "name", "partner_name", "is_active", "customer", "is_inside",
            "commission_type", "commission_rate", "commission_amount",
        ],
        as_dict=True,
    )
    if not row:
        _err(
            _("Delivery partner {0} is not set up on this system. Pick another partner.").format(partner),
            "PARTNER_NOT_FOUND",
            delivery_partner=partner,
        )

    if not cint(row.is_active):
        _err(
            _("{0} is switched off and cannot take new orders. Ask an administrator to "
              "re-activate it, or sell this order another way.").format(row.partner_name or partner),
            "PARTNER_INACTIVE",
            delivery_partner=row.name,
        )

    if is_paid or flt(tendered) > AMOUNT_EPSILON:
        # The one rule that cannot bend. Booking this as Paid would put the app's money in
        # this drawer, and the shift would come up short by exactly this amount.
        _err(
            _("{0} collects this money from the customer and pays us later, so do not take "
              "payment here. Set the sale to Due.").format(row.partner_name or partner),
            "PARTNER_SALE_MUST_BE_DUE",
            delivery_partner=row.name,
            paid_amount=flt(tendered),
        )

    is_inside = bool(cint(row.is_inside))
    if is_inside:
        # "Bill the App Customer": the order is billed to the real customer the cashier
        # picked, while the partner still holds the money. The mismatch checks below are the
        # WRONG rule here, so this is their counterpart instead - the debt must land on a
        # person, never on an account that stands for somebody else.
        _refuse_inside_partner_customer(row, customer, pos_profile)
    elif not row.customer:
        _err(
            _("{0} has no billing customer set up yet. Ask an administrator to open the "
              "partner record and save it.").format(row.partner_name or partner),
            "PARTNER_CUSTOMER_MISMATCH",
            delivery_partner=row.name,
        )
    elif cstr(customer).strip() != cstr(row.customer).strip():
        # Billing one app's orders to another app's account is invisible until a statement
        # fails to reconcile a month later, by which time the invoices are submitted.
        _err(
            _("A {0} order must be billed to {0}, not to {1}. Select {0} as the customer.").format(
                row.partner_name or partner, cstr(customer).strip()
            ),
            "PARTNER_CUSTOMER_MISMATCH",
            delivery_partner=row.name,
            expected_customer=row.customer,
            customer=customer,
        )

    order_ref = cstr(partner_order_ref).strip()
    if not order_ref:
        _err(
            _("Enter the {0} order number. It is the only thing their monthly statement "
              "can be matched against.").format(row.partner_name or partner),
            "PARTNER_ORDER_REF_REQUIRED",
            delivery_partner=row.name,
        )

    # The caller may send the figure agreed for THIS order; the partner's current one is
    # only the default. Whatever is used is snapshotted onto the invoice and never
    # re-read, so a later renegotiation cannot restate a month that is already closed.
    #
    # The TYPE is the partner's, never the caller's: a till that could choose it could bill
    # a flat-fee partner a percentage, and the settlement would read whichever figure the
    # sale happened to store. Which figure means something is the partner's decision.
    commission_type = "Amount" if row.commission_type == "Amount" else "Percentage"
    rate = 0.0
    amount = 0.0

    if commission_type == "Amount":
        # `partner_commission_rate` is IGNORED here rather than refused. The till sends the
        # partner's rate on every sale, and a flat-fee partner's rate is 0 - refusing it
        # would make every one of their orders unsellable from a till that is otherwise
        # working correctly.
        amount = (
            flt(partner_commission_amount)
            if cstr(partner_commission_amount).strip()
            else flt(row.commission_amount)
        )
        if amount <= 0:
            _err(
                _("The commission for {0} is {1} per order, which cannot be right. Ask an "
                  "administrator to correct the partner record.").format(
                    row.partner_name or partner,
                    frappe.format_value(amount, {"fieldtype": "Currency"}),
                ),
                "PARTNER_INVALID_COMMISSION_AMOUNT",
                delivery_partner=row.name,
                commission_amount=amount,
            )
    else:
        rate = flt(partner_commission_rate) if cstr(partner_commission_rate).strip() else flt(row.commission_rate)
        if rate <= 0 or rate >= 100:
            _err(
                _("The commission rate for {0} is {1}%, which cannot be right. Ask an "
                  "administrator to correct the partner record.").format(row.partner_name or partner, rate),
                "PARTNER_INVALID_COMMISSION_RATE",
                delivery_partner=row.name,
                commission_rate=rate,
            )

    return frappe._dict(
        partner=row.name,
        label=row.partner_name or row.name,
        customer=row.customer,
        order_ref=order_ref,
        customer_name=cstr(partner_customer_name).strip() or None,
        commission_type=commission_type,
        commission_rate=rate,
        commission_amount=amount,
        is_inside=is_inside,
    )


def _refuse_inside_partner_customer(row, customer, pos_profile):
    """An inside partner's order must be billed to a PERSON, not to an account standing in
    for somebody else.

    The two accounts refused are the ones the till lands on without anyone choosing them:
    a delivery partner's own billing Customer (the cashier picked the partner instead of the
    customer), and the profile's walk-in default (the till reseeds it whenever a sale is
    cleared). The debt sits on this customer's account until the partner settles, so on
    either of those it would be owed by nobody who can be asked about it - and a walk-in
    account also takes everyone's small debts, where one of these would never be found.
    """
    customer = cstr(customer).strip()
    label = row.partner_name or row.name

    other_partner = frappe.db.get_value("Delivery Partner", {"customer": customer}, "name")
    if other_partner:
        _err(
            _("A {0} order is billed to the customer who ordered it, not to {1}. Select the "
              "customer in the customer picker.").format(label, customer),
            "PARTNER_INSIDE_NEEDS_REAL_CUSTOMER",
            delivery_partner=row.name,
            customer=customer,
            partner_of_customer=other_partner,
        )

    walk_in = (
        cstr(frappe.db.get_value("POS Profile", cstr(pos_profile).strip(), "customer")).strip()
        if cstr(pos_profile).strip()
        else ""
    )
    if walk_in and customer == walk_in:
        _err(
            _("A {0} order is billed to the customer who ordered it, so it cannot go on the "
              "walk-in customer {1}. Select or create the customer first.").format(label, customer),
            "PARTNER_INSIDE_NEEDS_REAL_CUSTOMER",
            delivery_partner=row.name,
            customer=customer,
        )


def _get_profile(pos_profile: str, company: str):
    pos_profile = cstr(pos_profile).strip()
    if not pos_profile:
        _err(_("POS Profile is required."), "POS_PROFILE_REQUIRED")
    if not frappe.db.exists("POS Profile", pos_profile):
        _err(_("POS Profile {0} was not found.").format(pos_profile), "POS_PROFILE_NOT_FOUND")
    profile = frappe.get_cached_doc("POS Profile", pos_profile)
    if cint(profile.disabled):
        _err(_("POS Profile {0} is disabled.").format(pos_profile), "POS_PROFILE_DISABLED")
    if profile.company != company:
        _err(
            _("POS Profile {0} belongs to {1}, not {2}.").format(pos_profile, profile.company, company),
            "POS_PROFILE_COMPANY_MISMATCH",
        )
    return profile


def _resolve_mode_of_payment(payment_mode, profile) -> str:
    mode = cstr(payment_mode).strip()
    if mode:
        if not frappe.db.exists("Mode of Payment", mode):
            _err(_("Mode of Payment {0} was not found.").format(mode), "PAYMENT_MODE_NOT_FOUND")
        return mode
    for row in profile.get("payments") or []:
        if cint(row.get("default")):
            return row.mode_of_payment
    rows = profile.get("payments") or []
    if rows:
        return rows[0].mode_of_payment
    mode = frappe.db.get_single_value("Pet App Accounting Settings", "default_cash_mode_of_payment")
    if mode:
        return mode
    _err(_("No Mode of Payment is configured for this POS Profile."), "PAYMENT_MODE_REQUIRED")


def _resolve_payment_account(cash_account, mode_of_payment: str, company: str) -> str:
    account = cstr(cash_account).strip()
    if not account:
        account = frappe.db.get_value(
            "Mode of Payment Account",
            {"parent": mode_of_payment, "company": company},
            "default_account",
        )
    if not account:
        account = frappe.db.get_single_value("Pet App Accounting Settings", "treasury_cash_account")
    if not account:
        _err(
            _("No cash or bank account is configured for {0}.").format(mode_of_payment),
            "CASH_ACCOUNT_REQUIRED",
        )
    if frappe.db.get_value("Account", account, "company") != company:
        _err(
            _("Account {0} does not belong to {1}.").format(account, company),
            "CASH_ACCOUNT_COMPANY_MISMATCH",
        )
    return account


def _amount_due(doc) -> float:
    """What the cashier must actually collect: the total less advances already paid.

    `grand_total` is what the stay COST; it is not what is owed at the till. A boarding
    deposit is written to `Sales Invoice.advances` at check-out
    (healthcare/boarding.py::_allocate_boarding_deposit), which raises the invoice in full
    deliberately - so money the customer has already handed over shows up ONLY in
    `total_advance`, and never reduces `grand_total`.

    Reading grand_total here asked one customer for 175,000 on a 175,000 stay he had
    already paid 70,000 against. Because the payment then covered the invoice in full,
    ERPNext had nothing left to reconcile the advance row against: no Payment Entry
    Reference was written, the deposit stayed `unallocated`, and he ended with a 140,000
    credit balance. Eleven customers were in that state, 860,000 between them.

    MUST be read AFTER doc.save(): `total_advance` is recomputed by
    calculate_taxes_and_totals from the advances table, and is stale before it.
    """
    total = flt(doc.rounded_total) or flt(doc.grand_total)
    return max(total - flt(doc.total_advance), 0.0)


def _resolve_applied_payment(
    tendered, invoice_total: float, profile_name: str, *, merged: bool, **details
) -> float:
    """How much of what the cashier tendered is actually booked against this invoice.

    Never more than the invoice is worth: anything tendered above the total is change in
    the drawer, not revenue, and booking it would need a change account this profile has
    no reason to configure.

    `paid_amount` is authoritative and is NEVER read as "pay in full". `_settle` used to
    fall back to the whole total whenever `tendered` was falsy, so a request carrying
    `paid_amount: 0` against a positive total submitted an invoice marked fully paid for
    money nobody had collected - silently, and reconciling to a plausible balance. That
    fallback became reachable the moment a zero grand total became a legal sale, because
    a comp or a giveaway sends exactly that value.

    A zero total with nothing tendered stays legal and is the one case that passes here
    with no payment: it is the comp, and it submits like any other sale.
    """
    tendered = flt(tendered)
    invoice_total = flt(invoice_total)

    if invoice_total > AMOUNT_EPSILON and tendered <= AMOUNT_EPSILON:
        # "Paid" and "nothing was handed over" are two different intentions in one
        # request, the mirror image of DUE_SALE_CANNOT_CARRY_PAYMENT. Booking it as paid
        # would clear a receivable no one settled.
        _err(
            _(
                "payment_status is 'Paid' but nothing was tendered against a total of {0}. "
                "Settle as 'Due' instead, or send the amount collected."
            ).format(invoice_total),
            "PAID_SALE_REQUIRES_PAYMENT",
            invoice_total=invoice_total, paid_amount=tendered, **details,
        )

    # ERPNext's own validate_full_payment only fires for is_created_using_pos, which
    # neither flow sets (it demands a POS Opening Entry), so the profile's setting is
    # enforced here or nowhere.
    allow_partial = cint(frappe.db.get_value("POS Profile", profile_name, "allow_partial_payment"))
    if not allow_partial and tendered + AMOUNT_EPSILON < invoice_total:
        # The cashier believed they were collecting for the counter items alone; the
        # merged total is larger. Submitting would leave an outstanding balance nobody
        # agreed to, so this stops instead - loudly, with both numbers.
        message = (
            _("Partial payment is not allowed on POS Profile {0}. The merged invoice totals {1} but only {2} was tendered.")
            if merged
            else _("Partial payment is not allowed on POS Profile {0}. The sale totals {1} but only {2} was tendered.")
        )
        _err(
            message.format(profile_name, invoice_total, tendered),
            "POS_PARTIAL_PAYMENT_NOT_ALLOWED",
            invoice_total=invoice_total, paid_amount=tendered, **details,
        )

    return min(max(tendered, 0.0), invoice_total)


def _pin_payment_account(doc, account: str) -> None:
    """Re-assert the till's own account AFTER the last draft save, BEFORE submit.

    `SalesInvoice.before_save` runs `set_account_for_mode_of_payment()`, which
    unconditionally rewrites every payments row to
    `get_bank_cash_account(mode_of_payment, company)` - the Mode of Payment's COMPANY
    default, not the till's. On this site both POS Profiles default to the same mode of
    payment (نقداً), whose company default is the STORE's drawer, so the Hotel till's
    takings were being credited to the Store's cash account and no error was raised.

    The fix is a matter of ordering, not of a different value. Frappe's
    `Document.run_before_save_methods` runs `before_save` only for `_action == "save"`;
    the submit transition runs `before_validate` / `validate` / `before_submit` instead.
    So an account written here, between the final save and `submit()`, is the one that
    reaches `make_pos_gl_entries` - which debits `payment_mode.account` directly.

    Set on `cash_bank_account` too, where the field exists, so the header agrees with the
    row rather than reporting the stomped default back to the cashier.
    """
    if not account:
        return
    for row in doc.get("payments") or []:
        row.account = account
    if doc.meta.has_field("cash_bank_account"):
        doc.cash_bank_account = account


def _assert_payment_account_landed(doc, account: str) -> None:
    """Refuse to report success if the till account did not survive the submit.

    `_pin_payment_account` depends on a Frappe lifecycle detail and an ERPNext one, and a
    version bump could move either. Money credited to the wrong drawer is silent - it
    reconciles to a plausible-looking balance in the wrong branch - so this converts that
    silence into a failed sale that rolls back, rather than a receipt the cashier trusts.
    """
    if not account:
        return
    for row in doc.get("payments") or []:
        if cstr(row.account) != cstr(account):
            _err(
                _(
                    "The payment was about to be credited to {0} instead of the till's own "
                    "account {1}. The sale was cancelled rather than post to the wrong drawer."
                ).format(row.account or _("(blank)"), account),
                "PAYMENT_ACCOUNT_NOT_APPLIED",
                expected=account, landed=cstr(row.account),
            )


def _receipt_line(row) -> dict:
    return {
        "item_code": row.item_code,
        "item_name": row.item_name or row.item_code,
        "description": strip_markers(row.description),
        "qty": flt(row.qty),
        "uom": row.uom,
        "rate": flt(row.rate),
        "amount": flt(row.amount),
        "warehouse": row.warehouse or None,
        "batch_no": row.batch_no or None,
    }


def _build_receipt(doc, profile, appended_row_names: set[str], payment: dict | None) -> dict:
    """Clinic block first, then the counter block, so the customer can read the bill.

    Kept as two labelled blocks rather than one flat list precisely because the two
    halves were priced by different people at different times.
    """
    clinic, counter = [], []
    visits: list[str] = []
    for row in doc.items or []:
        if row.name in appended_row_names:
            counter.append(_receipt_line(row))
            continue
        clinic.append(_receipt_line(row))
        for doctype, name in parse_markers(row.description):
            if doctype == "Vet Visit" and name not in visits:
                visits.append(name)

    blocks = []
    if clinic:
        blocks.append({"key": "clinic", "label": _("Clinic"), "items": clinic})
    if counter:
        blocks.append({"key": "counter", "label": _("Counter"), "items": counter})

    # Read off the DOCUMENT, not off the request, so a replayed sale and a settled
    # partner invoice both print correctly - `_sale_result` returns an existing doc on a
    # retry and never sees the original arguments. `doc.get` is None-safe before the
    # schema patch has run.
    partner = cstr(doc.get("custom_delivery_partner")).strip()
    partner_label = None
    if partner:
        partner_label = (
            frappe.db.get_value("Delivery Partner", partner, "partner_name") or partner
        )

    return {
        "invoice": doc.name,
        "posting_date": cstr(doc.posting_date),
        "posting_time": cstr(doc.posting_time),
        "company": doc.company,
        "customer": doc.customer,
        "customer_name": doc.customer_name,
        "currency": doc.currency,
        "pos_profile": profile.name,
        "cashier": frappe.session.user,
        "branch": doc.get("branch"),
        "blocks": blocks,
        "items": clinic + counter,
        "linked_visits": visits,
        "net_total": flt(doc.net_total),
        "total_taxes_and_charges": flt(doc.total_taxes_and_charges),
        "discount_amount": flt(doc.discount_amount),
        "grand_total": flt(doc.grand_total),
        "rounded_total": flt(doc.rounded_total),
        # What the till must actually take. `grand_total` is the cost of the stay; a
        # boarding deposit sits in `total_advance` and is already in the drawer. Showing
        # grand_total as the amount to collect is what charged one customer twice.
        "total_advance": flt(doc.total_advance),
        "amount_due": _amount_due(doc),
        "paid_amount": flt(doc.paid_amount),
        "outstanding_amount": flt(doc.outstanding_amount),
        "payment": payment,
        # WITHOUT THESE A DUE PARTNER SALE PRINTS AS A PLAIN UNPAID INVOICE, and whoever
        # collects it may ask the customer for money the delivery app already took. Both
        # are None on an ordinary sale, so the client can branch on either.
        "partner_label": partner_label,
        "partner_order_ref": cstr(doc.get("custom_partner_order_ref")).strip() or None,
        "remarks": doc.remarks,
    }


def _probe_result() -> dict:
    """What `probe=1` answers, for both settlement methods.

    `available` is deployment AND permission in one flag, because the client has exactly
    one decision to make with it: show the affordance, or do not.
    """
    permitted = _may_operate_pos()
    result = {"available": permitted}
    if not permitted:
        result["code"] = "POS_SETTLE_NOT_PERMITTED"
        result["reason"] = _("You are not authorized to settle invoices at the till.")
    return result


@frappe.whitelist()
@standardize_response
def settle_open_invoice(
    invoice=None,
    pos_profile=None,
    warehouse=None,
    payment_status=None,
    paid_amount=None,
    expected_modified=None,
    payment_mode=None,
    cash_account=None,
    company=None,
    branch=None,
    discount_type=None,
    discount_value=None,
    remarks=None,
    extra_items=None,
    probe=0,
    customer=None,
    idempotency_key=None,
):
    """Append the counter's items to an open clinic draft and settle the whole thing.

    `probe=1` answers before anything else is looked at, so the client can decide
    whether to offer the "Link" affordance without staging a sale to find out.

    `idempotency_key` is optional but strongly advised. Without one `expected_modified`
    is the only replay guard, and it refuses a retry with "this invoice changed" - which
    reads to a cashier as a different problem than "you already paid".

    `branch` is accepted and ignored. The invoice keeps the branch of the clinic that
    raised it: `stamp_branch_on_insert` is a before_insert hook, so an update cannot
    move it anyway, and revenue and stock belong with the clinic that did the work and
    dispensed the goods, not with whichever till took the money. The cashier's branch is
    recorded on the receipt instead.
    """
    if cint(probe):
        # Answered before a single argument is validated and without touching a document,
        # so the client learns the feature is unavailable BEFORE the cashier stages a
        # link - never mid-sale with a customer waiting. A user who may not settle is
        # told so here too, for the same reason.
        return _probe_result()

    try:
        return _settle(
            invoice=invoice,
            pos_profile=pos_profile,
            warehouse=warehouse,
            payment_status=payment_status,
            paid_amount=paid_amount,
            expected_modified=expected_modified,
            payment_mode=payment_mode,
            cash_account=cash_account,
            company=company,
            discount_type=discount_type,
            discount_value=discount_value,
            remarks=remarks,
            extra_items=extra_items,
            customer=customer,
            idempotency_key=idempotency_key,
        )
    except Exception:
        # standardize_response SWALLOWS the exception and returns an envelope, so Frappe's
        # request handler would see a clean return and COMMIT the half-applied append.
        # The rollback has to happen here, while the exception is still in flight.
        frappe.db.rollback()
        raise


def _settle(
    *, invoice, pos_profile, warehouse, payment_status, paid_amount, expected_modified,
    payment_mode, cash_account, company, discount_type, discount_value, remarks,
    extra_items, customer, idempotency_key,
):
    _require_pos_operator()

    invoice = cstr(invoice).strip()
    if not invoice:
        _err(_("Invoice is required."), "INVOICE_REQUIRED")

    payment_status = cstr(payment_status).strip().title()
    if payment_status not in ("Paid", "Due"):
        _err(_("payment_status must be 'Paid' or 'Due'."), "INVALID_PAYMENT_STATUS")

    if not cstr(expected_modified).strip():
        _err(
            _("expected_modified is required so a concurrent edit cannot be overwritten."),
            "EXPECTED_MODIFIED_REQUIRED",
        )

    company = resolve_company(company)
    profile = _get_profile(pos_profile, company)
    extra_rows = _coerce_rows(extra_items)
    idempotency_key = cstr(idempotency_key).strip()

    # 1. Lock the row for the rest of the transaction, before reading anything from it.
    locked = _lock_invoice(invoice)

    # 2. A retry of a settlement that already committed. Checked BEFORE expected_modified,
    #    because a successful settlement is precisely what moves `modified` - so without
    #    this the retry is refused with "this invoice changed", which reads to a cashier
    #    as a different problem than "you already paid", and their next move is to take
    #    the money again.
    if idempotency_key and _sale_key_marker(idempotency_key) in cstr(locked.remarks):
        settled = frappe.get_doc("Sales Invoice", invoice)
        split_docs = [
            frappe.get_doc("Sales Invoice", name)
            for name in _find_settlement_by_key(idempotency_key, company)
            if name != settled.name
        ]
        split_counter = next(
            (candidate for candidate in split_docs if _counter_rows_from_markers(candidate, profile.name)),
            None,
        )
        if split_counter:
            counter_rows = _counter_rows_from_markers(split_counter, profile.name)
            paid_docs = [doc for doc in (settled, split_counter) if doc.get("payments")]
            return _auto_split_result(
                settled,
                split_counter,
                profile,
                _payment_summary_from_doc(paid_docs[0]) if paid_docs else None,
                counter_rows=counter_rows,
                replayed=True,
            )
        counter_rows = _counter_rows_from_markers(settled, profile.name)
        return _settle_result(
            settled,
            profile,
            _payment_summary_from_doc(settled),
            appended_row_names=counter_rows,
            appended_item_count=len(counter_rows),
            replayed=True,
        )

    # 3. Optimistic concurrency. Returned rather than raised: nothing has been written,
    #    so there is nothing to roll back, and the client needs the current value back to
    #    re-read and retry.
    if get_datetime(expected_modified) != get_datetime(locked.modified):
        return fail(
            _("Sales Invoice {0} changed since it was read. Reload it before settling.").format(invoice),
            code="INVOICE_CHANGED",
            data={"invoice": invoice, "modified": cstr(locked.modified)},
        )

    # 4. Refuse anything that is not the document we were promised. Never coerce.
    _validate_settle_target(locked, company, customer)

    doc = frappe.get_doc("Sales Invoice", invoice)
    preserved = _row_snapshot(doc)

    # 5. Resolve stock BEFORE appending. The draft's own flag is the authority and is
    #    never rewritten - see the module docstring.
    default_warehouse = cstr(warehouse).strip() or cstr(profile.warehouse).strip()
    needs_split = bool(extra_rows) and not cint(locked.update_stock) and any(
        _is_stock_item(cstr(row.get("item_code")).strip())
        for row in extra_rows
        if cstr(row.get("item_code")).strip() and frappe.db.exists("Item", cstr(row.get("item_code")).strip())
    )
    if needs_split:
        return _settle_with_counter_split(
            doc=doc,
            profile=profile,
            extra_rows=extra_rows,
            company=company,
            branch=None,
            warehouse=default_warehouse,
            payment_status=payment_status,
            paid_amount=paid_amount,
            payment_mode=payment_mode,
            cash_account=cash_account,
            discount_type=discount_type,
            discount_value=discount_value,
            remarks=remarks,
            idempotency_key=idempotency_key,
        )
    prepared = _prepare_counter_lines(
        extra_rows,
        profile=profile,
        default_warehouse=default_warehouse,
        update_stock=locked.update_stock,
        invoice=invoice,
    )

    for line in prepared:
        doc.append("items", line)

    _apply_document_discount(doc, discount_type, discount_value)
    _append_note(doc, remarks)
    if idempotency_key:
        # Written before the first save, so the marker and the settlement commit or roll
        # back together. A key that survived without the settlement would refuse a
        # legitimate retry; a settlement without the key would accept a duplicate.
        _append_note(doc, _sale_key_marker(idempotency_key))

    # The append is validated and totalled FIRST. Only a document that already saves
    # cleanly gets POS fields and a payment row put on it.
    doc.flags.from_custom_flow = True
    doc.flags.ignore_permissions = True
    doc.save(ignore_permissions=True)
    _assert_rows_preserved(doc, preserved)

    appended_row_names = {row.name for row in doc.items or []} - set(preserved)
    # Net of advances - a boarding deposit already in the drawer is not collected twice.
    # Read after the save above, which is what recomputes total_advance.
    invoice_total = _amount_due(doc)
    tendered = flt(paid_amount)
    payment_summary = None

    if payment_status == "Paid":
        applied = _resolve_applied_payment(
            tendered, invoice_total, profile.name, merged=True, invoice=invoice
        )

        mode_of_payment = _resolve_mode_of_payment(payment_mode, profile)
        account = _resolve_payment_account(cash_account, mode_of_payment, company)

        _stamp_pos_fields(doc, profile, account)
        doc.set("payments", [])
        doc.append(
            "payments",
            {"mode_of_payment": mode_of_payment, "amount": applied, "account": account, "default": 1},
        )
        doc.paid_amount = applied
        payment_summary = {
            "mode_of_payment": mode_of_payment,
            "account": account,
            "amount": flt(applied),
            "tendered": flt(tendered),
            "change": flt(max(tendered - invoice_total, 0)),
        }

        doc.flags.from_custom_flow = True
        doc.flags.ignore_permissions = True
        doc.save(ignore_permissions=True)
        _assert_rows_preserved(doc, preserved)
        # That save just ran before_save -> set_account_for_mode_of_payment, which
        # overwrote the account resolved above with the Mode of Payment's COMPANY
        # default. Put the till's own account back before submit reads it.
        _pin_payment_account(doc, account)
    elif tendered > AMOUNT_EPSILON:
        # A "Due" sale that carries money is two different intentions in one request.
        # Taking the money and still booking it as due, or dropping it silently, are both
        # worse than saying so.
        _err(
            _("payment_status is 'Due' but {0} was tendered. Settle as 'Paid' instead.").format(tendered),
            "DUE_SALE_CANNOT_CARRY_PAYMENT",
            paid_amount=tendered,
        )

    doc.flags.ignore_permissions = True
    doc.submit()
    doc.reload()
    if payment_summary:
        _assert_payment_account_landed(doc, payment_summary["account"])

    doc.add_comment(
        "Comment",
        _("Settled at the till on POS Profile {0} by {1}: {2} counter line(s) appended, {3}.").format(
            profile.name, frappe.session.user, len(prepared), _(payment_status)
        ),
    )

    return _settle_result(
        doc,
        profile,
        payment_summary,
        appended_row_names=appended_row_names,
        appended_item_count=len(prepared),
        replayed=False,
    )


def _settle_result(
    doc, profile, payment_summary, *, appended_row_names, appended_item_count, replayed: bool
) -> dict:
    """The envelope CreatePosSale produces, so one client handler covers every POS write."""
    return {
        "invoice": doc.as_dict(),
        "payment_entry_id": None,
        "receipt": _build_receipt(doc, profile, appended_row_names, payment_summary),
        "settled_existing_invoice": True,
        "settled_invoice_ids": [doc.name],
        "appended_item_count": appended_item_count,
        "created_counter_invoice": None,
        "auto_split_counter_items": False,
        # True when a retry found the settlement a previous attempt had already made, so
        # the client can print the receipt again without charging the customer twice.
        "replayed": replayed,
    }


def _auto_split_result(
    clinic_doc, counter_doc, profile, payment_summary, *, counter_rows: set[str], replayed: bool
) -> dict:
    docs = [clinic_doc, counter_doc]
    receipt = _build_merged_receipt(
        docs, profile, {clinic_doc.name: set(), counter_doc.name: counter_rows}, payment_summary
    )
    if receipt.get("blocks"):
        receipt["blocks"][0]["key"] = "clinic"
        receipt["blocks"][0]["label"] = _("Clinic")
    return {
        "invoice": clinic_doc.as_dict(),
        "payment_entry_id": None,
        "receipt": receipt,
        "settled_existing_invoice": True,
        "settled_invoice_ids": [doc.name for doc in docs],
        "appended_item_count": len(counter_rows),
        "extra_items_invoice": counter_doc.name,
        "created_counter_invoice": counter_doc.name,
        "auto_split_counter_items": True,
        "replayed": replayed,
    }


def _settle_with_counter_split(
    *,
    doc,
    profile,
    extra_rows: list[dict],
    company,
    branch,
    warehouse,
    payment_status,
    paid_amount,
    payment_mode,
    cash_account,
    discount_type,
    discount_value,
    remarks,
    idempotency_key,
):
    if cstr(discount_type).strip():
        kind = cstr(discount_type).strip().lower()
        if kind not in ("percentage", "percent", "%", "amount", "fixed", "value"):
            _err(
                _("discount_type must be 'Percentage' or 'Amount'."),
                "INVALID_DISCOUNT_TYPE", discount_type=discount_type,
            )

    counter_doc, _lines = _make_counter_sale_draft(
        customer=doc.customer,
        profile=profile,
        payment_status=payment_status,
        rows=extra_rows,
        company=company,
        branch=branch or doc.get("branch"),
        warehouse=warehouse,
        posting_date=doc.posting_date,
        due_date=doc.due_date,
        selling_price_list=doc.selling_price_list,
        discount_type=discount_type if cstr(discount_type).strip().lower() in ("percentage", "percent", "%") else None,
        discount_value=discount_value,
        remarks=remarks,
        idempotency_key=idempotency_key,
    )

    if cstr(discount_type).strip().lower() in ("amount", "fixed", "value"):
        _apply_document_discount(doc, discount_type, discount_value)
    _append_note(doc, remarks)
    if idempotency_key:
        _append_note(doc, _sale_key_marker(idempotency_key))

    doc.flags.from_custom_flow = True
    doc.flags.ignore_permissions = True
    doc.save(ignore_permissions=True)

    clinic_total = _amount_due(doc)
    counter_total = _amount_due(counter_doc)
    combined_total = flt(clinic_total + counter_total)
    tendered = flt(paid_amount)
    payment_summary = None

    if payment_status == "Paid":
        applied_total = _resolve_applied_payment(
            tendered, combined_total, profile.name, merged=True,
            invoices=[doc.name, counter_doc.name],
        )
        mode_of_payment = _resolve_mode_of_payment(payment_mode, profile)
        account = _resolve_payment_account(cash_account, mode_of_payment, company)

        remaining = applied_total
        allocations: dict[str, float] = {}
        for invoice_doc, total in ((doc, clinic_total), (counter_doc, counter_total)):
            share = min(remaining, total)
            if share > AMOUNT_EPSILON:
                allocations[invoice_doc.name] = share
                remaining -= share

        for invoice_doc in (doc, counter_doc):
            share = allocations.get(invoice_doc.name)
            if not share:
                invoice_doc.is_pos = 0
                invoice_doc.pos_profile = None
                invoice_doc.flags.from_custom_flow = True
                invoice_doc.flags.ignore_permissions = True
                invoice_doc.save(ignore_permissions=True)
                continue
            _stamp_pos_fields(invoice_doc, profile, account)
            invoice_doc.set("payments", [])
            invoice_doc.append(
                "payments",
                {"mode_of_payment": mode_of_payment, "amount": share, "account": account, "default": 1},
            )
            invoice_doc.paid_amount = share
            invoice_doc.flags.from_custom_flow = True
            invoice_doc.flags.ignore_permissions = True
            invoice_doc.save(ignore_permissions=True)
            _pin_payment_account(invoice_doc, account)

        payment_summary = {
            "mode_of_payment": mode_of_payment,
            "account": account,
            "amount": flt(applied_total),
            "tendered": flt(tendered),
            "change": flt(max(tendered - combined_total, 0)),
            "allocations": [
                {"invoice": name, "amount": flt(amount)} for name, amount in allocations.items()
            ],
        }
    elif tendered > AMOUNT_EPSILON:
        _err(
            _("payment_status is 'Due' but {0} was tendered. Settle as 'Paid' instead.").format(tendered),
            "DUE_SALE_CANNOT_CARRY_PAYMENT",
            paid_amount=tendered,
        )

    for invoice_doc in (doc, counter_doc):
        invoice_doc.flags.ignore_permissions = True
        invoice_doc.submit()
        invoice_doc.reload()
        if payment_summary and invoice_doc.get("payments"):
            _assert_payment_account_landed(invoice_doc, payment_summary["account"])

    doc.add_comment(
        "Comment",
        _("Settled at the till on POS Profile {0} by {1}; counter stock items split to {2}.").format(
            profile.name, frappe.session.user, counter_doc.name
        ),
    )
    counter_doc.add_comment(
        "Comment",
        _("Auto-split counter sale from clinic invoice {0} on POS Profile {1} by {2}.").format(
            doc.name, profile.name, frappe.session.user
        ),
    )

    return _auto_split_result(
        doc,
        counter_doc,
        profile,
        payment_summary,
        counter_rows={row.name for row in counter_doc.items or []},
        replayed=False,
    )


# ---------------------------------------------------------------------------
# Counter sale: one new invoice, created and submitted in a single call.
# ---------------------------------------------------------------------------

def _prepare_sale_lines(rows: list[dict], default_warehouse: str) -> list[dict]:
    """Validate the counter's lines into Sales Invoice Item payloads.

    The client also sends `amount` per row. It is deliberately dropped: a line is worth
    qty * rate as ERPNext computes it, and accepting a client-computed total would let a
    rounding difference - or a tampered payload - decide what the customer is charged.

    `description` is left as the plain item name here; `invoice_reuse._mark_items` appends
    the [alkokh-source-group:POS Profile:<till>] marker to it on the way in, so counter
    lines carry the same provenance as every other billing path.
    """
    prepared: list[dict] = []
    for index, row in enumerate(rows):
        item_code = cstr(row.get("item_code")).strip()
        if not item_code:
            _err(_("Sale line {0} is missing item_code.").format(index + 1), "SALE_ITEM_INVALID")
        if not frappe.db.exists("Item", item_code):
            _err(_("Item {0} was not found.").format(item_code), "ITEM_NOT_FOUND", item_code=item_code)

        qty = flt(row.get("qty"))
        if qty <= 0:
            _err(
                _("Sale line for {0} must have qty greater than zero.").format(item_code),
                "SALE_ITEM_INVALID", item_code=item_code,
            )
        rate = flt(row.get("rate"))
        if rate < 0:
            _err(
                _("Sale line for {0} must not have a negative rate.").format(item_code),
                "SALE_ITEM_INVALID", item_code=item_code,
            )

        item_name = cstr(row.get("item_name")).strip()
        line = {
            "item_code": item_code,
            "qty": qty,
            "rate": rate,
            "description": item_name or item_code,
        }
        if item_name:
            line["item_name"] = item_name
        if cstr(row.get("uom")).strip():
            line["uom"] = cstr(row.get("uom")).strip()

        if _is_stock_item(item_code):
            row_warehouse = cstr(row.get("warehouse")).strip() or default_warehouse
            if not row_warehouse:
                _err(
                    _("A warehouse is required for stock item {0}.").format(item_code),
                    "SALE_ITEM_WAREHOUSE_REQUIRED", item_code=item_code,
                )
            line["warehouse"] = row_warehouse
        prepared.append(line)
    return prepared


def _assert_stock_available(lines: list[dict]) -> None:
    """A refusal the cashier can act on, instead of a negative-stock traceback.

    A counter sale for goods the system does not believe are on the shelf throws inside
    submit. That throw is correct and stays the authority - this check does not replace
    it and takes no lock. What it does is name the item, the warehouse and the shortfall
    BEFORE anything is written, so the cashier is told "only 2 left" rather than shown a
    stack trace after the customer has been asked to pay.

    Which items may go short is decided PER ITEM, by the same helper core uses: medication
    items carry ERPNext's per-Item Allow Negative Stock so a drug is never refused at the
    till over a ledger that is behind the shelf, while food and accessories still stop
    here. Reading only the global Stock Settings flag, as this once did, would have kept
    refusing the very medicines that flag was set to let through.
    """
    wanted: dict[tuple[str, str], float] = {}
    for line in lines:
        warehouse = line.get("warehouse")
        if not warehouse:
            continue
        key = (line["item_code"], warehouse)
        wanted[key] = wanted.get(key, 0.0) + flt(line["qty"])

    for (item_code, warehouse), qty in wanted.items():
        if is_negative_stock_allowed(item_code=item_code):
            continue
        available = flt(
            frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty")
        )
        if available + AMOUNT_EPSILON < qty:
            _err(
                _("{0} has {1} left in {2}, but the sale asks for {3}.").format(
                    item_code, available, warehouse, qty
                ),
                "INSUFFICIENT_STOCK",
                item_code=item_code, warehouse=warehouse,
                available=available, requested=qty,
            )


def _find_sale_by_key(idempotency_key: str, customer: str) -> str | None:
    """The invoice a previous attempt with this key already produced, if any.

    The client retries a request it never saw a response to. Without this, a retry after
    a timeout charges the customer twice and moves the stock twice, and the till has no
    way to tell that it happened. Scoped to the customer as well as the key so a client
    that reuses a key carelessly cannot return somebody else's sale.
    """
    if not idempotency_key:
        return None
    marker = f"{POS_SALE_KEY_PREFIX}{idempotency_key}{POS_SALE_KEY_SUFFIX}"
    rows = frappe.get_all(
        "Sales Invoice",
        filters={
            "customer": customer,
            "docstatus": ["<", 2],
            "remarks": ["like", f"%{marker}%"],
        },
        fields=["name"],
        order_by="creation desc",
        limit_page_length=1,
        ignore_permissions=True,
    )
    return rows[0].name if rows else None


def _make_counter_sale_draft(
    *,
    customer,
    profile,
    payment_status,
    rows: list[dict],
    company,
    branch,
    warehouse,
    posting_date,
    due_date,
    selling_price_list,
    discount_type,
    discount_value,
    remarks,
    idempotency_key,
    partner_ctx=None,
):
    is_paid = cstr(payment_status).strip().title() == "Paid"
    default_warehouse = cstr(warehouse).strip() or cstr(profile.warehouse).strip()
    lines = _prepare_sale_lines(rows, default_warehouse)
    _assert_stock_available(lines)

    sale_branch, branch_authorised = resolve_branch_for_pos(profile.name, branch)

    posting_date = getdate(posting_date) if posting_date else getdate(nowdate())
    due_date = getdate(due_date) if due_date else posting_date
    if due_date < posting_date:
        due_date = posting_date

    sale_remarks = cstr(remarks).strip()
    if idempotency_key:
        marker = f"{POS_SALE_KEY_PREFIX}{idempotency_key}{POS_SALE_KEY_SUFFIX}"
        sale_remarks = f"{sale_remarks}\n{marker}".strip() if sale_remarks else marker

    extra_fields = {
        "set_warehouse": default_warehouse,
        "currency": cstr(profile.currency).strip(),
        "custom_pos_profile": profile.name,
        "custom_cashier_user": frappe.session.user,
    }
    if partner_ctx:
        extra_fields.update({
            "custom_delivery_partner": partner_ctx.partner,
            "custom_partner_order_ref": partner_ctx.order_ref,
            "custom_partner_customer_name": partner_ctx.customer_name,
            # Both figures are written, and the type says which one was charged: a flat-fee
            # order stores rate 0, a percentage order stores amount 0 until the total is
            # known below. A settlement reads the type first and never has to guess.
            "custom_partner_commission_type": partner_ctx.commission_type,
            "custom_partner_commission_rate": partner_ctx.commission_rate,
            # Snapshotted like the commission: the till's double-collection guard reads it
            # off the invoice, so flipping the partner's flag later moves no existing order.
            "custom_partner_is_inside": 1 if partner_ctx.is_inside else 0,
        })

    cashier_employee = _get_cashier_employee()
    if cashier_employee:
        extra_fields["cashier"] = cashier_employee

    if cstr(discount_type).strip():
        kind = cstr(discount_type).strip().lower()
        extra_fields["apply_discount_on"] = "Grand Total"
        if kind in ("percentage", "percent", "%"):
            extra_fields["additional_discount_percentage"] = flt(discount_value)
        elif kind in ("amount", "fixed", "value"):
            extra_fields["discount_amount"] = flt(discount_value)
        else:
            _err(
                _("discount_type must be 'Percentage' or 'Amount'."),
                "INVALID_DISCOUNT_TYPE", discount_type=discount_type,
            )

    price_list = cstr(selling_price_list).strip() or cstr(profile.selling_price_list).strip() or None

    result = get_or_create_open_invoice(
        customer=customer,
        items=lines,
        source_doctype=POS_SOURCE_DOCTYPE,
        source_name=profile.name,
        company=company,
        branch=sale_branch,
        posting_date=posting_date,
        due_date=due_date,
        selling_price_list=price_list,
        remarks=sale_remarks or None,
        requires_stock=True,
        is_pos=1 if is_paid else 0,
        pos_profile=profile.name if is_paid else None,
        force_new=True,
        branch_authorised=branch_authorised,
        extra_fields=extra_fields,
    )
    return result.invoice, lines


@frappe.whitelist()
@standardize_response
def create_pos_sale(
    customer=None,
    pos_profile=None,
    payment_status=None,
    items=None,
    company=None,
    branch=None,
    warehouse=None,
    paid_amount=None,
    payment_mode=None,
    cash_account=None,
    posting_date=None,
    due_date=None,
    selling_price_list=None,
    discount_type=None,
    discount_value=None,
    remarks=None,
    idempotency_key=None,
    delivery_partner=None,
    partner_order_ref=None,
    partner_customer_name=None,
    partner_commission_rate=None,
    partner_commission_amount=None,
):
    """Raise ONE new Sales Invoice for a counter sale and submit it, atomically.

    This is the door POS comes through. `sales_invoice_guard.before_insert` refuses a
    direct `frappe.client.insert`, and correctly so - the guard exists because every
    billable event used to open its own invoice, one customer collecting ten in
    sixty-eight minutes. A counter sale is the case that genuinely needs its own
    document, so it gets a named endpoint rather than a hole in the guard.

    Why it must never reuse an open draft, in EITHER payment mode
    ------------------------------------------------------------
    A counter sale is a closed, stock-moving document; it is at docstatus 0 only for the
    instant between insert and submit. Appending it into a customer's open clinic draft
    would corrupt that draft and race its submit. That is already recorded for the Paid
    case, where `is_pos = 1` keeps `invoice_reuse` out of the way on its own.

    The Due case is the one that was not covered. A Due counter sale is deliberately
    `is_pos = 0` - no payment row, an ordinary receivable - so it would have fallen
    straight into the reuse path and appended the counter's goods to a clinic draft that
    nobody submits at the till. The goods would have been sold and never left the shelf.
    Hence `force_new=True` and an explicit `payment_status` argument: the payment method
    is not what decides whether a document is mergeable.

    Why create and submit are one call
    ----------------------------------
    The client used to insert, then submit, as two requests. A failure between them left
    a draft POS invoice stranded - unsubmitted, unpaid, holding no stock, and invisible
    to the till that made it. One call, one transaction: on any refusal below, the whole
    thing rolls back and there is nothing to clean up.
    """
    try:
        return _create_pos_sale(
            customer=customer,
            pos_profile=pos_profile,
            payment_status=payment_status,
            items=items,
            company=company,
            branch=branch,
            warehouse=warehouse,
            paid_amount=paid_amount,
            payment_mode=payment_mode,
            cash_account=cash_account,
            posting_date=posting_date,
            due_date=due_date,
            selling_price_list=selling_price_list,
            discount_type=discount_type,
            discount_value=discount_value,
            remarks=remarks,
            idempotency_key=idempotency_key,
            delivery_partner=delivery_partner,
            partner_order_ref=partner_order_ref,
            partner_customer_name=partner_customer_name,
            partner_commission_rate=partner_commission_rate,
            partner_commission_amount=partner_commission_amount,
        )
    except Exception:
        # standardize_response SWALLOWS the exception and returns an envelope, so Frappe's
        # request handler would see a clean return and COMMIT a half-created sale. The
        # rollback has to happen here, while the exception is still in flight.
        frappe.db.rollback()
        raise


def _create_pos_sale(
    *, customer, pos_profile, payment_status, items, company, branch, warehouse,
    paid_amount, payment_mode, cash_account, posting_date, due_date,
    selling_price_list, discount_type, discount_value, remarks, idempotency_key,
    delivery_partner, partner_order_ref, partner_customer_name, partner_commission_rate,
    partner_commission_amount,
):
    _require_pos_operator()

    customer = cstr(customer).strip()
    if not customer:
        _err(_("Customer is required."), "CUSTOMER_REQUIRED")
    if not frappe.db.exists("Customer", customer):
        _err(_("Customer {0} was not found.").format(customer), "CUSTOMER_NOT_FOUND", customer=customer)

    payment_status = cstr(payment_status).strip().title()
    if payment_status not in ("Paid", "Due"):
        _err(_("payment_status must be 'Paid' or 'Due'."), "INVALID_PAYMENT_STATUS")
    is_paid = payment_status == "Paid"

    # Before ANY document exists. Every refusal below is something the cashier fixes on
    # the screen in front of them, and the client renders the first code it receives as a
    # persistent block beside Process Transaction rather than a toast that vanishes.
    partner_ctx = _resolve_partner_context(
        delivery_partner, partner_order_ref, partner_customer_name, partner_commission_rate,
        customer=customer, is_paid=is_paid, tendered=flt(paid_amount),
        partner_commission_amount=partner_commission_amount,
        pos_profile=pos_profile,
    )

    company = resolve_company(company)
    # Required for BOTH payment modes. The client omits the profile on a Due sale because
    # ERPNext's own POS fields do not apply to one, but the till is still the till: it is
    # where the branch, the warehouse, the price list and the cashier attribution come
    # from, and a Due sale that cannot say which counter made it is not reportable.
    profile = _get_profile(pos_profile, company)

    idempotency_key = cstr(idempotency_key).strip()
    existing = _find_sale_by_key(idempotency_key, customer)
    if existing:
        doc = frappe.get_doc("Sales Invoice", existing)
        return _sale_result(doc, profile, None, replayed=True, item_count=len(doc.items or []))

    rows = _coerce_rows(items, field="items", code="INVALID_ITEMS")
    if not rows:
        _err(_("A sale needs at least one item."), "NO_ITEMS")

    # One creation site for Sales Invoice in this app, and this goes through it.
    doc, lines = _make_counter_sale_draft(
        customer=customer,
        profile=profile,
        payment_status=payment_status,
        rows=rows,
        company=company,
        branch=branch,
        warehouse=warehouse,
        posting_date=posting_date,
        due_date=due_date,
        selling_price_list=selling_price_list,
        remarks=remarks,
        discount_type=discount_type,
        discount_value=discount_value,
        idempotency_key=idempotency_key,
        partner_ctx=partner_ctx,
    )

    # Only knowable now: ERPNext computed it from the lines and the document discount.
    invoice_total = flt(doc.rounded_total) or flt(doc.grand_total)
    tendered = flt(paid_amount)
    payment_summary = None

    if is_paid:
        applied = _resolve_applied_payment(tendered, invoice_total, profile.name, merged=False)

        mode_of_payment = _resolve_mode_of_payment(payment_mode, profile)
        # The till's own drawer before the Mode of Payment's company default: both
        # profiles share one mode of payment, so the default alone cannot tell them apart.
        account = _resolve_payment_account(
            cstr(cash_account).strip() or cstr(profile.get("custom_cash_account")).strip(),
            mode_of_payment,
            company,
        )

        doc.set("payments", [])
        doc.append(
            "payments",
            {"mode_of_payment": mode_of_payment, "amount": applied, "account": account, "default": 1},
        )
        doc.paid_amount = applied
        if doc.meta.has_field("cash_bank_account"):
            doc.cash_bank_account = account

        doc.flags.from_custom_flow = True
        doc.flags.ignore_permissions = True
        doc.save(ignore_permissions=True)
        # That save just ran before_save -> set_account_for_mode_of_payment, which
        # overwrote `account` with the Mode of Payment's company default.
        _pin_payment_account(doc, account)

        payment_summary = {
            "mode_of_payment": mode_of_payment,
            "account": account,
            "amount": flt(applied),
            "tendered": flt(tendered),
            "change": flt(max(tendered - invoice_total, 0)),
        }
    elif tendered > AMOUNT_EPSILON:
        # A "Due" sale that carries money is two different intentions in one request.
        # Taking the money and still booking it as due, or dropping it silently, are both
        # worse than saying so.
        _err(
            _("payment_status is 'Due' but {0} was tendered. Sell as 'Paid' instead.").format(tendered),
            "DUE_SALE_CANNOT_CARRY_PAYMENT",
            paid_amount=tendered,
        )

    if partner_ctx:
        # Only knowable now, and stamped before the submit that persists it. Taken off the
        # same figure the partner's statement will show against this order.
        if partner_ctx.commission_type == "Amount":
            fee = flt(partner_ctx.commission_amount)
            if fee >= invoice_total - AMOUNT_EPSILON:
                # The flat-fee counterpart of refusing a rate of 100 or more: the invoice
                # would settle to nothing or to a negative receipt, and the tie-out in
                # `create_settlement` has no meaning for it. Refused at the till, where
                # somebody can still decide what to do about the order.
                _err(
                    _("{0} keeps {1} on every order, which is not less than this order's {2}. "
                      "Sell it another way, or ask an administrator about the partner's fee.").format(
                        partner_ctx.label,
                        frappe.format_value(fee, {"fieldtype": "Currency"}),
                        frappe.format_value(flt(invoice_total), {"fieldtype": "Currency"}),
                    ),
                    "PARTNER_COMMISSION_EXCEEDS_TOTAL",
                    delivery_partner=partner_ctx.partner,
                    commission_amount=fee,
                    invoice_total=flt(invoice_total),
                )
            doc.custom_partner_commission_amount = fee
        else:
            doc.custom_partner_commission_amount = flt(
                invoice_total * flt(partner_ctx.commission_rate) / 100.0
            )

    doc.flags.ignore_permissions = True
    doc.submit()
    doc.reload()
    if payment_summary:
        _assert_payment_account_landed(doc, payment_summary["account"])

    doc.add_comment(
        "Comment",
        _("Counter sale on POS Profile {0} by {1}: {2} line(s), {3}.").format(
            profile.name, frappe.session.user, len(lines), _(payment_status)
        ),
    )
    if partner_ctx:
        doc.add_comment(
            "Comment",
            _(
                "Delivery partner order {0} for {1}. Booked as a receivable against the "
                "partner - NO money was taken at the till. Commission {2} ({3})."
            ).format(
                partner_ctx.order_ref,
                partner_ctx.label,
                # What was AGREED, beside what it came to: a flat fee has no percentage to
                # quote, and quoting the effective one would read as a rate that was never
                # negotiated.
                _("flat fee")
                if partner_ctx.commission_type == "Amount"
                else "{0}%".format(flt(partner_ctx.commission_rate)),
                frappe.format_value(
                    flt(doc.get("custom_partner_commission_amount")), {"fieldtype": "Currency"}
                ),
            ),
        )

    return _sale_result(doc, profile, payment_summary, replayed=False, item_count=len(lines))


def _sale_result(doc, profile, payment_summary, *, replayed: bool, item_count: int) -> dict:
    """The same envelope settle_open_invoice returns, so one client handler covers both.

    Every row is a counter row here, so `_build_receipt` renders a single "Counter" block
    and no clinic block - the shape is identical, the content is what differs.
    """
    return {
        "invoice": doc.as_dict(),
        "payment_entry_id": None,
        "receipt": _build_receipt(
            doc, profile, {row.name for row in doc.items or []}, payment_summary
        ),
        "settled_existing_invoice": False,
        "created_invoice": doc.name,
        "item_count": item_count,
        # True when a retry found the sale a previous attempt had already completed, so
        # the client can print the receipt again without charging the customer twice.
        "replayed": replayed,
    }


# ---------------------------------------------------------------------------
# Merged settlement: several open clinic drafts, one payment, one transaction.
# ---------------------------------------------------------------------------


def _coerce_invoice_refs(value) -> list[dict]:
    """`[{invoice, expected_modified}, ...]`, validated and deduplicated.

    `expected_modified` is required PER INVOICE and a null one is refused rather than
    defaulted. The locks are independent - a vet may be saving one of these visits right
    now - and `modified` is only ever populated by the client's detail mapper, so a caller
    that linked from a list row would send null and lose the optimistic lock that stops
    the till overwriting that vet's save.
    """
    rows = _coerce_rows(value, field="invoices", code="INVALID_INVOICES")
    if not rows:
        _err(_("At least one invoice is required."), "INVOICES_REQUIRED")

    refs: dict[str, str] = {}
    for index, row in enumerate(rows):
        name = cstr(row.get("invoice")).strip()
        if not name:
            _err(_("Invoice entry {0} is missing an invoice id.").format(index + 1), "INVOICES_REQUIRED")
        expected = cstr(row.get("expected_modified")).strip()
        if not expected:
            _err(
                _("expected_modified is required for invoice {0} so a concurrent edit cannot be overwritten.").format(name),
                "EXPECTED_MODIFIED_REQUIRED", invoice=name,
            )
        if name in refs and refs[name] != expected:
            _err(
                _("Invoice {0} was listed twice with different expected_modified values.").format(name),
                "INVOICES_DUPLICATED", invoice=name,
            )
        refs[name] = expected

    # Ascending by name, so two tills settling overlapping sets take their locks in the
    # same order and cannot deadlock against each other.
    return [{"invoice": name, "expected_modified": refs[name]} for name in sorted(refs)]


def _find_settlement_by_key(idempotency_key: str, company: str) -> list[str]:
    """Every invoice a previous attempt with this key already settled."""
    if not idempotency_key:
        return []
    marker = _sale_key_marker(idempotency_key)
    rows = frappe.get_all(
        "Sales Invoice",
        filters={"company": company, "docstatus": ["<", 2], "remarks": ["like", f"%{marker}%"]},
        fields=["name"],
        order_by="name asc",
        ignore_permissions=True,
    )
    return [row.name for row in rows]


def _pick_extra_items_invoice(locked_rows: list, needs_stock: bool) -> str:
    """Which invoice of the set the counter's cart is appended to.

    Deterministic and documented, which is all the client asks: the MOST RECENTLY
    MODIFIED eligible invoice, ties broken by name descending. That is the draft the
    customer was most recently at a desk about, so the counter goods land beside the
    freshest clinic lines rather than on a week-old tab.

    Eligibility is the stock flag, never a coercion of it. If the cart holds a stock item
    then only an `update_stock = 1` draft can carry it - appending to a non-stock draft
    would sell goods that nothing ever relieves. The counter block is never split across
    documents, so one invoice takes all of it or the call is refused.
    """
    candidates = [row for row in locked_rows if cint(row.update_stock)] if needs_stock else list(locked_rows)
    if not candidates:
        _err(
            _(
                "The counter cart contains stock items, but none of the linked invoices "
                "moves stock. Raise a separate POS invoice for the counter items."
            ),
            "STOCK_FLAG_CONFLICT",
            invoices=[row.name for row in locked_rows], update_stock=0,
        )
    candidates.sort(key=lambda row: (get_datetime(row.modified), row.name), reverse=True)
    return candidates[0].name


def _build_merged_receipt(docs, profile, counter_rows: dict, payment: dict | None) -> dict:
    """One labelled block per invoice, in order, then the counter block.

    A run of unlabelled lines covering three documents gives the customer no way to
    reconcile the slip against charges they already know about, so every clinic block
    carries its own invoice id and its own totals.
    """
    blocks: list[dict] = []
    visits: list[str] = []
    clinic_items: list[dict] = []
    counter_items: list[dict] = []

    for doc in docs:
        own_counter = counter_rows.get(doc.name) or set()
        lines = []
        for row in doc.items or []:
            line = _receipt_line(row)
            if row.name in own_counter:
                counter_items.append(line)
                continue
            lines.append(line)
            for doctype, name in parse_markers(row.description):
                if doctype == "Vet Visit" and name not in visits:
                    visits.append(name)
        clinic_items.extend(lines)
        if lines:
            blocks.append({
                "key": f"invoice:{doc.name}",
                "label": doc.name,
                "invoice": doc.name,
                "items": lines,
                "grand_total": flt(doc.rounded_total) or flt(doc.grand_total),
                "total_advance": flt(doc.total_advance),
                "amount_due": _amount_due(doc),
                "paid_amount": flt(doc.paid_amount),
                "outstanding_amount": flt(doc.outstanding_amount),
            })

    if counter_items:
        blocks.append({"key": "counter", "label": _("Counter"), "items": counter_items})

    head = docs[0]
    return {
        "invoice": head.name,
        "invoices": [doc.name for doc in docs],
        "posting_date": cstr(head.posting_date),
        "posting_time": cstr(head.posting_time),
        "company": head.company,
        "customer": head.customer,
        "customer_name": head.customer_name,
        "currency": head.currency,
        "pos_profile": profile.name,
        "cashier": frappe.session.user,
        "branch": head.get("branch"),
        "blocks": blocks,
        "items": clinic_items + counter_items,
        "linked_visits": visits,
        "net_total": flt(sum(flt(doc.net_total) for doc in docs)),
        "total_taxes_and_charges": flt(sum(flt(doc.total_taxes_and_charges) for doc in docs)),
        "discount_amount": flt(sum(flt(doc.discount_amount) for doc in docs)),
        "grand_total": flt(sum(flt(doc.rounded_total) or flt(doc.grand_total) for doc in docs)),
        "rounded_total": flt(sum(flt(doc.rounded_total) or flt(doc.grand_total) for doc in docs)),
        # The figure the cashier collects across the set, net of every deposit on it.
        "total_advance": flt(sum(flt(doc.total_advance) for doc in docs)),
        "amount_due": flt(sum(_amount_due(doc) for doc in docs)),
        "paid_amount": flt(sum(flt(doc.paid_amount) for doc in docs)),
        "outstanding_amount": flt(sum(flt(doc.outstanding_amount) for doc in docs)),
        "payment": payment,
        "remarks": head.remarks,
    }


def _merged_result(
    docs, profile, payment, counter_rows: dict, target: str, *, appended_item_count,
    replayed, created_counter_invoice=None, auto_split_counter_items=False,
) -> dict:
    """The same envelope as the other two POS methods, plus the ids actually settled."""
    head = next((doc for doc in docs if doc.name == target), docs[0])
    return {
        "invoice": head.as_dict(),
        "payment_entry_id": None,
        "receipt": _build_merged_receipt(docs, profile, counter_rows, payment),
        "settled_existing_invoice": True,
        "settled_invoice_ids": [doc.name for doc in docs],
        "appended_item_count": appended_item_count,
        "extra_items_invoice": target,
        "created_counter_invoice": created_counter_invoice,
        "auto_split_counter_items": bool(auto_split_counter_items),
        "replayed": replayed,
    }


@frappe.whitelist()
@standardize_response
def settle_open_invoices(
    invoices=None,
    pos_profile=None,
    warehouse=None,
    payment_status=None,
    paid_amount=None,
    idempotency_key=None,
    payment_mode=None,
    cash_account=None,
    company=None,
    branch=None,
    discount_type=None,
    discount_value=None,
    remarks=None,
    extra_items=None,
    probe=0,
    customer=None,
):
    """Settle SEVERAL of a customer's open clinic drafts with one payment, atomically.

    A customer with several open tabs pays once. Either every invoice in the set is
    submitted or none is - which is the entire reason this exists rather than the client
    looping `settle_open_invoice`: the second call can fail after the first has submitted
    and taken the money, leaving the customer paid up on part of a tab that is still open,
    and no client-side compensation can undo a submitted, paid, stock-moving invoice.

    Part payment SUBMITS. The merged amount is applied to the invoices in name order and
    whatever is left unpaid stays on the customer's account as an ordinary receivable.
    Both POS Profiles on this site set `allow_partial_payment`, so the singular method's
    rule 7 does not bite; if a profile turns it off, POS_PARTIAL_PAYMENT_NOT_ALLOWED is
    raised for the merged total rather than for any one document.

    A zero merged total is a real transaction - a comp, a giveaway, a replacement - and
    submits with no payment row at all.

    `branch` is accepted and ignored, exactly as in the singular method: each invoice
    keeps the branch of the clinic that raised it.
    """
    if cint(probe):
        return _probe_result()

    try:
        return _settle_many(
            invoices=invoices,
            pos_profile=pos_profile,
            warehouse=warehouse,
            payment_status=payment_status,
            paid_amount=paid_amount,
            idempotency_key=idempotency_key,
            payment_mode=payment_mode,
            cash_account=cash_account,
            company=company,
            discount_type=discount_type,
            discount_value=discount_value,
            remarks=remarks,
            extra_items=extra_items,
            customer=customer,
        )
    except Exception:
        # standardize_response SWALLOWS the exception and returns an envelope, so Frappe's
        # request handler would see a clean return and COMMIT a partially settled set.
        # The rollback has to happen here, while the exception is still in flight.
        frappe.db.rollback()
        raise


def _settle_many(
    *, invoices, pos_profile, warehouse, payment_status, paid_amount, idempotency_key,
    payment_mode, cash_account, company, discount_type, discount_value, remarks,
    extra_items, customer,
):
    _require_pos_operator()

    payment_status = cstr(payment_status).strip().title()
    if payment_status not in ("Paid", "Due"):
        _err(_("payment_status must be 'Paid' or 'Due'."), "INVALID_PAYMENT_STATUS")

    idempotency_key = cstr(idempotency_key).strip()
    if not idempotency_key:
        # Required here even though the singular method's is optional: a merged settlement
        # moves more money across more documents, so a retry after a timeout has to be
        # recognisable as the SAME settlement rather than a second one.
        _err(
            _("idempotency_key is required for a merged settlement."),
            "IDEMPOTENCY_KEY_REQUIRED",
        )

    refs = _coerce_invoice_refs(invoices)
    company = resolve_company(company)
    profile = _get_profile(pos_profile, company)
    extra_rows = _coerce_rows(extra_items)

    # 1. A retry of a settlement that already committed, checked before any lock is taken
    #    and before expected_modified - which a successful settlement is precisely what
    #    moves. Without this the retry reads as "this invoice changed" rather than
    #    "you already paid", and the cashier's next move is to take the money again.
    already = _find_settlement_by_key(idempotency_key, company)
    if already:
        docs = [frappe.get_doc("Sales Invoice", name) for name in already]
        counter_rows = {doc.name: _counter_rows_from_markers(doc, profile.name) for doc in docs}
        paid_docs = [doc for doc in docs if doc.get("payments")]
        requested = {ref["invoice"] for ref in refs}
        split_counter = next(
            (doc.name for doc in docs if doc.name not in requested and counter_rows.get(doc.name)),
            None,
        )
        return _merged_result(
            docs,
            profile,
            _payment_summary_from_doc(paid_docs[0]) if paid_docs else None,
            counter_rows,
            next((name for name in already if counter_rows.get(name)), already[0]),
            appended_item_count=sum(len(rows) for rows in counter_rows.values()),
            replayed=True,
            created_counter_invoice=split_counter,
            auto_split_counter_items=bool(split_counter),
        )

    # 2. Lock EVERY row, in name order, before reading anything from any of them.
    locked_rows = [_lock_invoice(ref["invoice"]) for ref in refs]
    locked_by_name = {row.name: row for row in locked_rows}

    # 3. Optimistic concurrency, per invoice, NAMING the one that moved. With several in
    #    flight, "this invoice changed, reload" is unactionable if the cashier cannot tell
    #    which of five it refers to.
    for ref in refs:
        locked = locked_by_name[ref["invoice"]]
        if get_datetime(ref["expected_modified"]) != get_datetime(locked.modified):
            return fail(
                _("Sales Invoice {0} changed since it was read. Reload it before settling.").format(locked.name),
                code="INVOICE_CHANGED",
                data={"invoice": locked.name, "modified": cstr(locked.modified)},
                details={"invoice": locked.name, "modified": cstr(locked.modified)},
            )

    # 4. Validate EVERY invoice before touching any of them. One bad member refuses the
    #    whole call, naming it - never settle "the ones that were fine".
    expected_customer = cstr(customer).strip() or locked_rows[0].customer
    for locked in locked_rows:
        _validate_settle_target(locked, company, expected_customer)

    needs_stock = any(
        _is_stock_item(cstr(row.get("item_code")).strip())
        for row in extra_rows
        if cstr(row.get("item_code")).strip() and frappe.db.exists("Item", cstr(row.get("item_code")).strip())
    )
    auto_split_counter = bool(needs_stock and not any(cint(row.update_stock) for row in locked_rows))
    target = None if auto_split_counter else _pick_extra_items_invoice(locked_rows, needs_stock)

    docs = [frappe.get_doc("Sales Invoice", locked.name) for locked in locked_rows]
    preserved = {doc.name: _row_snapshot(doc) for doc in docs}

    # 5. Append the counter cart to the ONE chosen invoice, or split it to its own
    #    stock-moving invoice when every clinic draft is explicitly non-stock. Mark every
    #    member of the set with the idempotency key so the whole settlement replays as a
    #    unit.
    default_warehouse = cstr(warehouse).strip() or cstr(profile.warehouse).strip()
    counter_doc = None
    if auto_split_counter:
        counter_doc, prepared = _make_counter_sale_draft(
            customer=expected_customer,
            profile=profile,
            payment_status=payment_status,
            rows=extra_rows,
            company=company,
            branch=None,
            warehouse=default_warehouse,
            posting_date=docs[0].posting_date,
            due_date=docs[0].due_date,
            selling_price_list=docs[0].selling_price_list,
            discount_type=discount_type,
            discount_value=discount_value,
            remarks=remarks,
            idempotency_key=idempotency_key,
        )
        target = counter_doc.name
        docs.append(counter_doc)
        preserved[counter_doc.name] = _row_snapshot(counter_doc)
    else:
        prepared = _prepare_counter_lines(
            extra_rows,
            profile=profile,
            default_warehouse=default_warehouse,
            update_stock=locked_by_name[target].update_stock,
            invoice=target,
        )

    discount_kind = cstr(discount_type).strip().lower()
    for doc in docs:
        if doc.name == target and not auto_split_counter:
            for line in prepared:
                doc.append("items", line)
            _append_note(doc, remarks)
        # A PERCENTAGE means the same thing on every document and is applied to all of
        # them. A fixed AMOUNT is a lump sum off the merged bill; splitting it across
        # documents would be an arbitrary choice, so it lands on the invoice that also
        # took the counter cart.
        if (
            discount_kind
            and not (auto_split_counter and doc.name == target)
            and (doc.name == target or discount_kind in ("percentage", "percent", "%"))
        ):
            _apply_document_discount(doc, discount_type, discount_value)
        _append_note(doc, _sale_key_marker(idempotency_key))

        doc.flags.from_custom_flow = True
        doc.flags.ignore_permissions = True
        doc.save(ignore_permissions=True)
        _assert_rows_preserved(doc, preserved[doc.name])

    counter_rows = {
        doc.name: (
            {row.name for row in doc.items or []}
            if auto_split_counter and doc.name == target
            else ({row.name for row in doc.items or []} - set(preserved[doc.name])) if doc.name == target else set()
        )
        for doc in docs
    }

    # Net of advances, per invoice: this caps each document's share in the allocation loop
    # below as well as the merged total, so a deposit on one member of the set is not
    # collected again through the combined payment.
    totals = {doc.name: _amount_due(doc) for doc in docs}
    merged_total = flt(sum(totals.values()))
    tendered = flt(paid_amount)
    payment_summary = None

    if payment_status == "Paid":
        applied_total = _resolve_applied_payment(
            tendered, merged_total, profile.name, merged=True,
            invoices=[doc.name for doc in docs],
        )
        mode_of_payment = _resolve_mode_of_payment(payment_mode, profile)
        account = _resolve_payment_account(cash_account, mode_of_payment, company)

        # 6. One payment across the set, allocated in name order. Written as a
        #    Sales Invoice Payment row per invoice rather than one multi-reference
        #    Payment Entry: this app has never written a multi-reference Payment Entry -
        #    the read model supports it but every writer is single-reference - and a row
        #    per document keeps each invoice internally consistent, its own paid_amount
        #    never exceeding its own total.
        remaining = applied_total
        allocations: dict[str, float] = {}
        for doc in docs:
            share = min(remaining, totals[doc.name])
            if share > AMOUNT_EPSILON:
                allocations[doc.name] = share
                remaining -= share

        for doc in docs:
            share = allocations.get(doc.name)
            if not share:
                # Nothing was allocated to this one: it submits as an ordinary receivable
                # and the balance sits on the customer's account. Deliberately NOT stamped
                # is_pos - ERPNext's validate_pos_paid_amount refuses a POS invoice with a
                # positive total and no payment row, and this is not a till sale, it is
                # the remainder of one.
                if auto_split_counter and doc.name == target:
                    doc.is_pos = 0
                    doc.pos_profile = None
                    doc.flags.from_custom_flow = True
                    doc.flags.ignore_permissions = True
                    doc.save(ignore_permissions=True)
                continue
            _stamp_pos_fields(doc, profile, account)
            doc.set("payments", [])
            doc.append(
                "payments",
                {"mode_of_payment": mode_of_payment, "amount": share, "account": account, "default": 1},
            )
            doc.paid_amount = share

            doc.flags.from_custom_flow = True
            doc.flags.ignore_permissions = True
            doc.save(ignore_permissions=True)
            _assert_rows_preserved(doc, preserved[doc.name])
            # That save just ran before_save -> set_account_for_mode_of_payment, which
            # overwrote the account resolved above with the Mode of Payment's COMPANY
            # default. Put the till's own account back before submit reads it.
            _pin_payment_account(doc, account)

        payment_summary = {
            "mode_of_payment": mode_of_payment,
            "account": account,
            "amount": flt(applied_total),
            "tendered": flt(tendered),
            "change": flt(max(tendered - merged_total, 0)),
            "allocations": [
                {"invoice": name, "amount": flt(amount)} for name, amount in allocations.items()
            ],
        }
    elif tendered > AMOUNT_EPSILON:
        _err(
            _("payment_status is 'Due' but {0} was tendered. Settle as 'Paid' instead.").format(tendered),
            "DUE_SALE_CANNOT_CARRY_PAYMENT",
            paid_amount=tendered,
        )

    # 7. Submit only once every document above has validated and saved cleanly, so a
    #    refusal on the last member cannot leave an earlier one submitted and paid.
    for doc in docs:
        doc.flags.ignore_permissions = True
        doc.submit()
        doc.reload()
        if payment_summary and doc.get("payments"):
            _assert_payment_account_landed(doc, payment_summary["account"])

    for doc in docs:
        doc.add_comment(
            "Comment",
            _("Settled at the till on POS Profile {0} by {1} with {2} invoice(s) in one payment: {3}.").format(
                profile.name, frappe.session.user, len(docs), _(payment_status)
            ),
        )

    return _merged_result(
        docs, profile, payment_summary, counter_rows, target,
        appended_item_count=len(prepared), replayed=False,
        created_counter_invoice=counter_doc.name if counter_doc else None,
        auto_split_counter_items=auto_split_counter,
    )
