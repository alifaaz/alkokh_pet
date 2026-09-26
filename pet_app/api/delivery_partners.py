"""Collecting a delivery partner's month, and the arithmetic that has to hold.

100,000 of sales at 25% arrives as 75,000. The partner charged the customer, kept their
commission, and transferred the rest. The receivable is for the FULL 100,000, so banking
only the 75,000 would leave every invoice a quarter open forever. The commission is
therefore booked as an expense in the same Payment Entry, and the invoices close:

    Dr Bank / Cash          75,000     (received_amount)
    Dr Commission Expense   25,000     (commission_amount, + any adjustment)
        Cr Partner Receivable      100,000     (gross_amount)

ONE TRANSACTION, OR NOTHING
---------------------------
The settlement, the Payment Entry and the stamp on every invoice it covers are written
together. Split into separate saves, a failure between them either credits the partner's
account with no document explaining it, or marks invoices settled against money that never
arrived - and both are found by whoever reconciles the month, long after the evidence has
gone. `create_settlement` rolls the whole thing back on any refusal, for the same reason
and by the same mechanism `create_pos_sale` does.

WHY NOT A MODE OF PAYMENT PER PARTNER
-------------------------------------
Considered and rejected. `set_account_for_mode_of_payment` has already been caught
rewriting one till's takings into another till's drawer - which is why `create_pos_sale`
re-asserts the profile's cash account between the last save and the submit - and a partner
Mode of Payment walks straight into that, putting the app's money back in a drawer nobody
collected it in. It also gives no per-invoice settled state, so reconciliation degrades to
"the account balance looks about right".

See `docs/backend/delivery-partners.md` for the contract.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cstr, flt, getdate, nowdate

from erpnext.accounts.party import get_party_account

from pet_app.api.response import standardize_response
from pet_app.utils.api_response import raise_api_error
from pet_app.utils.invoice_reuse import resolve_company

# Currency rounding slack, matching pet_app.api.pos. IQD has no minor unit.
AMOUNT_EPSILON = 0.005


def _err(message, code, **details):
    raise_api_error(message, code=code, details=details or None)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def _may_settle_partners(user: str | None = None) -> bool:
    """Whether this user may collect a partner's month.

    Split out from `_require_settlement_permission` so `probe=1` can answer the same
    question without raising - the client hides the Record settlement button entirely
    rather than offering an affordance that will refuse.
    """
    return bool(
        frappe.has_permission("Delivery Partner Settlement", "create", user=user)
        and frappe.has_permission("Payment Entry", "submit", user=user)
    )


def _require_settlement_permission():
    if _may_settle_partners():
        return
    _err(
        _("You are not authorized to record delivery partner settlements."),
        "SETTLEMENT_NOT_PERMITTED",
        user=frappe.session.user,
    )


def _till_accounts() -> set[str]:
    """Every cash account that belongs to a cashier's drawer.

    `expected_cash_on_hand` is a raw sum over GL Entry keyed on one of these accounts, so
    anything posted to one of them becomes cash a cashier is told to hand over. A partner
    settlement arrives by bank transfer and passes through no drawer at all.
    """
    return {
        cstr(row.custom_cash_account).strip()
        for row in frappe.get_all(
            "POS Profile",
            filters={"custom_cash_account": ["is", "set"]},
            fields=["custom_cash_account"],
            ignore_permissions=True,
        )
        if cstr(row.custom_cash_account).strip()
    }


def _refuse_till_account(account: str, tills: set[str], *, label: str, code: str):
    if cstr(account).strip() in tills:
        _err(
            _(
                "{0} is a cashier's till account. This money arrived by bank transfer and "
                "passed through no drawer, so posting it there would tell that cashier to "
                "hand over cash they never received. Use a bank account instead."
            ).format(label),
            code,
            account=account,
        )


def refuse_partner_collected_invoices(invoice_names) -> None:
    """Refuse to take a customer's money for an order a delivery partner already collected.

    A "Bill the App Customer" order (`custom_partner_is_inside`) is billed to the real
    customer, so until the partner settles it is an ordinary-looking debt on that customer's
    account. It is NOT one: the partner took the money at their end and hands it over in the
    settlement. Collecting it at the till as well charges the customer twice, and the
    settlement then finds the invoice already closed and cannot clear it.

    Read off the invoice snapshot, never the partner's live flag, so switching a partner
    over cannot re-expose orders sold under the old setting. Once the settlement is stamped
    the invoice is closed anyway, so only unsettled ones are refused. A site that has not
    migrated has no snapshot column and nothing to refuse.
    """
    names = [cstr(n).strip() for n in (invoice_names or []) if cstr(n).strip()]
    if not names or not frappe.db.has_column("Sales Invoice", "custom_partner_is_inside"):
        return

    held = frappe.get_all(
        "Sales Invoice",
        filters={
            "name": ["in", names],
            "custom_partner_is_inside": 1,
            "custom_partner_settlement": ["is", "not set"],
        },
        fields=["name", "custom_delivery_partner", "customer"],
        ignore_permissions=True,
    )
    if held:
        first = held[0]
        _err(
            _(
                "{0} is a {1} order: {1} collected this money from the customer and pays it to "
                "us in their settlement. Do not take payment for it here - the customer would "
                "pay twice."
            ).format(first.name, first.custom_delivery_partner),
            "PARTNER_INVOICE_COLLECTED_BY_PARTNER",
            invoices=[row.name for row in held],
            delivery_partner=first.custom_delivery_partner,
            customer=first.customer,
        )


def _coerce_invoice_names(value) -> list[str]:
    """`["SINV-0001", ...]`, or a list of `{invoice: ...}` rows, deduplicated in order."""
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            value = [value]
    if isinstance(value, (str, dict)):
        value = [value]
    if not isinstance(value, list):
        _err(_("invoices must be a list of Sales Invoice names."), "INVALID_INVOICES")

    names, seen = [], set()
    for row in value:
        name = cstr(row.get("invoice") or row.get("sales_invoice") or row.get("name")).strip() if isinstance(row, dict) else cstr(row).strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


# ---------------------------------------------------------------------------
# create_settlement
# ---------------------------------------------------------------------------


@frappe.whitelist()
@standardize_response
def create_settlement(
    partner=None,
    invoices=None,
    from_date=None,
    to_date=None,
    received_amount=None,
    commission_amount=None,
    adjustment_amount=None,
    received_in=None,
    adjustment_account=None,
    statement_reference=None,
    posting_date=None,
    remarks=None,
    notes=None,
    idempotency_key=None,
    probe=0,
):
    """Collect one partner's covered invoices against one bank receipt.

    `probe=1` answers whether this endpoint is deployed and permitted WITHOUT touching a
    document, because the client hides the Record settlement button until it answers. It
    must stay the first thing this function does: the client treats an argument-shaped
    TypeError as "deployed" and proceeds anyway, so skipping probe support still works -
    it just turns one clear answer into one confusing failure.
    """
    if int(probe or 0):
        return _probe_result()

    try:
        return _create_settlement(
            partner=partner,
            invoices=invoices,
            from_date=from_date,
            to_date=to_date,
            received_amount=received_amount,
            commission_amount=commission_amount,
            adjustment_amount=adjustment_amount,
            received_in=received_in,
            adjustment_account=adjustment_account,
            statement_reference=statement_reference,
            posting_date=posting_date,
            # `notes` is the name this endpoint shipped with for one day; the client sends
            # `remarks`. Accepted either way so nothing in flight breaks.
            remarks=remarks or notes,
            idempotency_key=idempotency_key,
        )
    except Exception:
        # standardize_response SWALLOWS the exception and returns an envelope, so Frappe's
        # request handler would see a clean return and COMMIT a half-written settlement -
        # a Payment Entry with no invoice stamps, or stamps with no Payment Entry. The
        # rollback has to happen here, while the exception is still in flight. Same
        # reasoning as pos.create_pos_sale.
        frappe.db.rollback()
        raise


def _probe_result() -> dict:
    """Deployment AND permission in one flag - the client has one decision to make."""
    permitted = _may_settle_partners()
    result = {"available": permitted}
    if not permitted:
        result["code"] = "SETTLEMENT_NOT_PERMITTED"
        result["reason"] = _("You are not authorized to record delivery partner settlements.")
    return result


def _create_settlement(
    *, partner, invoices, from_date, to_date, received_amount, commission_amount,
    adjustment_amount, received_in, adjustment_account, statement_reference,
    posting_date, remarks, idempotency_key,
):
    _require_settlement_permission()

    partner_doc = _load_partner(partner)
    company = resolve_company(None)

    idempotency_key = cstr(idempotency_key).strip()
    replayed = _find_settlement_by_key(idempotency_key, partner_doc.name)
    if replayed:
        # A retry after a timeout. Without this it pays the partner's account down twice
        # and stamps a second settlement over invoices the first one already closed.
        return _settlement_result(frappe.get_doc("Delivery Partner Settlement", replayed), replayed=True)

    posting_date = getdate(posting_date) if posting_date else getdate(nowdate())
    rows = _resolve_covered_invoices(partner_doc, invoices, from_date, to_date)

    gross = flt(sum(flt(row["gross_amount"]) for row in rows))
    seeded_commission = flt(sum(flt(row["commission_amount"]) for row in rows))
    # `commission_amount` is EDITABLE, deliberately. The stored rate seeds it, but the
    # document being reconciled is the partner's own statement - when the two disagree,
    # the statement wins. Only a value the caller did not send falls back to the seed.
    commission = flt(commission_amount) if cstr(commission_amount).strip() else seeded_commission
    adjustment = flt(adjustment_amount) if cstr(adjustment_amount).strip() else 0.0
    received = flt(received_amount) if cstr(received_amount).strip() else flt(gross - commission - adjustment)

    gap = flt(gross - (received + commission + adjustment))
    if abs(gap) > AMOUNT_EPSILON:
        _err(
            _(
                "This settlement does not tie out. The {0} invoices cover {1}, but received "
                "{2} plus commission {3} plus adjustment {4} comes to {5} - a gap of {6}. "
                "Adjust one of the three figures until they add up to the gross."
            ).format(
                len(rows),
                _money(gross), _money(received), _money(commission),
                _money(adjustment), _money(received + commission + adjustment), _money(gap),
            ),
            "SETTLEMENT_DOES_NOT_TIE_OUT",
            gross_amount=gross,
            received_amount=received,
            commission_amount=commission,
            adjustment_amount=adjustment,
            gap=gap,
        )

    received_in = cstr(received_in).strip()
    if not received_in:
        _err(
            _("Name the bank account this transfer landed in."),
            "SETTLEMENT_RECEIVED_IN_REQUIRED",
        )
    commission_account = cstr(partner_doc.commission_expense_account).strip()
    if not commission_account and abs(commission) + abs(adjustment) > AMOUNT_EPSILON:
        _err(
            _(
                "{0} has no commission expense account set. Without one the commission "
                "cannot be booked and the invoices would stay part-open. Set it on the "
                "delivery partner record."
            ).format(partner_doc.name),
            "SETTLEMENT_COMMISSION_ACCOUNT_MISSING",
            delivery_partner=partner_doc.name,
        )

    # A clawback is not commission. It gets its own account when one is given, and falls
    # back to the commission account only so an adjustment is never silently unbooked.
    adjustment_account_name = cstr(adjustment_account).strip() or commission_account
    if abs(adjustment) > AMOUNT_EPSILON and not adjustment_account_name:
        _err(
            _(
                "This settlement carries an adjustment of {0} but no account to book it "
                "to. Name an adjustment account, or set a commission expense account on {1}."
            ).format(_money(adjustment), partner_doc.name),
            "SETTLEMENT_ADJUSTMENT_ACCOUNT_MISSING",
            delivery_partner=partner_doc.name,
        )

    tills = _till_accounts()
    _refuse_till_account(received_in, tills, label=received_in, code="SETTLEMENT_ACCOUNT_IS_A_TILL")
    if commission_account:
        _refuse_till_account(
            commission_account, tills, label=commission_account, code="SETTLEMENT_COMMISSION_ACCOUNT_IS_A_TILL"
        )
    if adjustment_account_name:
        _refuse_till_account(
            adjustment_account_name, tills, label=adjustment_account_name,
            code="SETTLEMENT_ADJUSTMENT_ACCOUNT_IS_A_TILL",
        )

    settlement = frappe.new_doc("Delivery Partner Settlement")
    settlement.partner = partner_doc.name
    settlement.partner_name = partner_doc.partner_name or partner_doc.name
    settlement.customer = partner_doc.customer
    settlement.posting_date = posting_date
    settlement.from_date = getdate(from_date) if from_date else None
    settlement.to_date = getdate(to_date) if to_date else None
    settlement.gross_amount = gross
    settlement.commission_amount = commission
    settlement.adjustment_amount = adjustment
    settlement.received_amount = received
    settlement.received_in = received_in
    settlement.adjustment_account = adjustment_account_name
    settlement.statement_reference = cstr(statement_reference).strip() or None
    settlement.idempotency_key = idempotency_key or None
    settlement.remarks = cstr(remarks).strip() or None
    settlement.invoice_count = len(rows)
    # What this settlement ACTUALLY worked out at, which is the figure to put beside the
    # partner's statement when they dispute it - not the live rate, not an invoice snapshot.
    settlement.commission_rate = flt(commission * 100.0 / gross) if gross else 0.0
    for row in rows:
        settlement.append("invoices", row)
    settlement.flags.ignore_permissions = True
    settlement.insert(ignore_permissions=True)

    # WHICH document pays depends on who the invoices are ACTUALLY billed to - never on the
    # partner's live `is_inside` flag, which may have been flipped since these were sold.
    # All on the partner's own Customer (every ordinary partner): the Payment Entry this has
    # always posted, untouched. Any on somebody else (a "Bill the App Customer" order): a
    # Payment Entry has ONE party and ERPNext will not let it clear another party's
    # invoice, so a Journal Entry credits each customer's invoice instead.
    parties = _invoice_parties(rows)
    if all(p.customer == partner_doc.customer for p in parties.values()):
        voucher = _post_settlement_payment(
            settlement, partner_doc, company, commission_account, adjustment_account_name, rows
        )
        settlement.db_set("payment_entry", voucher.name, update_modified=False)
        voucher_label = _("Payment Entry {0}").format(voucher.name)
    else:
        voucher = _post_settlement_journal(
            settlement, partner_doc, company, commission_account, adjustment_account_name, rows, parties
        )
        settlement.db_set("journal_entry", voucher.name, update_modified=False)
        voucher_label = _("Journal Entry {0}").format(voucher.name)

    # The stamp is what makes INVOICE_ALREADY_SETTLED possible on the next attempt, and it
    # is written in this same transaction as the Payment Entry that paid for it.
    for row in rows:
        frappe.db.set_value(
            "Sales Invoice", row["sales_invoice"],
            "custom_partner_settlement", settlement.name,
            update_modified=False,
        )

    settlement.add_comment(
        "Comment",
        _(
            "Collected {0} from {1} against {2} invoice(s) totalling {3}, with {4} "
            "commission and {5} adjustment. {6}, banked into {7}."
        ).format(
            _money(received), partner_doc.name, len(rows), _money(gross),
            _money(commission), _money(adjustment), voucher_label, received_in,
        ),
    )

    settlement.reload()
    return _settlement_result(settlement, replayed=False)


def _money(value) -> str:
    return frappe.format_value(flt(value), {"fieldtype": "Currency"})


def _load_partner(partner):
    name = cstr(partner).strip()
    if not name:
        _err(_("Delivery partner is required."), "PARTNER_REQUIRED")
    if not frappe.db.exists("Delivery Partner", name):
        _err(_("Delivery partner {0} was not found.").format(name), "PARTNER_NOT_FOUND", delivery_partner=name)

    doc = frappe.get_doc("Delivery Partner", name)
    if not doc.customer:
        _err(
            _(
                "{0} has no billing customer, so there is no receivable to settle. Open the "
                "partner record and save it to create one."
            ).format(name),
            "PARTNER_CUSTOMER_MISSING",
            delivery_partner=name,
        )
    # A switched-off partner is deliberately still settleable: turning a partner off stops
    # NEW orders, and the last month's takings still have to be collected afterwards.
    return doc


def _find_settlement_by_key(idempotency_key: str, partner: str) -> str | None:
    """The settlement a previous attempt with this key already produced, if any.

    Scoped to the partner as well as the key, so a client that reuses a key carelessly
    cannot be handed somebody else's settlement.
    """
    if not idempotency_key:
        return None
    rows = frappe.get_all(
        "Delivery Partner Settlement",
        filters={"idempotency_key": idempotency_key, "partner": partner, "docstatus": ["<", 2]},
        fields=["name"],
        order_by="creation desc",
        limit_page_length=1,
        ignore_permissions=True,
    )
    return rows[0].name if rows else None


def _resolve_covered_invoices(partner_doc, invoices, from_date, to_date) -> list[dict]:
    """The invoices this settlement closes, each contributing its OUTSTANDING.

    NOT its grand total. A partly paid invoice - the customer settled part of it directly,
    say - must contribute only what it still owes, or the settlement claims to collect
    money that was already collected and the tie-out is wrong by exactly that difference.
    """
    names = _coerce_invoice_names(invoices)
    if not names:
        # No explicit list: everything of this partner's that is still open in the period.
        filters = {
            "custom_delivery_partner": partner_doc.name,
            "docstatus": 1,
            "custom_partner_settlement": ["is", "not set"],
            "outstanding_amount": [">", 0],
        }
        if from_date:
            filters["posting_date"] = [">=", getdate(from_date)]
        candidates = frappe.get_all(
            "Sales Invoice", filters=filters, pluck="name", order_by="posting_date asc", ignore_permissions=True
        )
        if to_date:
            candidates = [
                n for n in candidates
                if getdate(frappe.db.get_value("Sales Invoice", n, "posting_date")) <= getdate(to_date)
            ]
        names = candidates

    if not names:
        _err(
            _("There is nothing outstanding to settle for {0} in this period.").format(partner_doc.name),
            "NO_INVOICES_TO_SETTLE",
            delivery_partner=partner_doc.name,
        )

    rows = []
    for name in names:
        invoice = frappe.db.get_value(
            "Sales Invoice",
            name,
            [
                "name", "docstatus", "customer", "posting_date", "outstanding_amount",
                "custom_delivery_partner", "custom_partner_order_ref",
                "custom_partner_commission_type", "custom_partner_commission_rate",
                "custom_partner_commission_amount", "custom_partner_settlement",
            ],
            as_dict=True,
        )
        if not invoice:
            _err(_("Sales Invoice {0} was not found.").format(name), "INVOICE_NOT_FOUND", invoice=name)
        if invoice.docstatus != 1:
            _err(
                _("Sales Invoice {0} is not submitted, so it owes nothing yet.").format(name),
                "INVOICE_NOT_SUBMITTED", invoice=name,
            )
        if cstr(invoice.custom_delivery_partner).strip() != partner_doc.name:
            _err(
                _("Sales Invoice {0} belongs to {1}, not to {2}.").format(
                    name, invoice.custom_delivery_partner or _("no delivery partner"), partner_doc.name
                ),
                "INVOICE_NOT_FOR_PARTNER",
                invoice=name, delivery_partner=partner_doc.name,
                invoice_partner=invoice.custom_delivery_partner,
            )
        if cstr(invoice.custom_partner_settlement).strip():
            _err(
                _("Sales Invoice {0} was already settled by {1}. Remove it from this settlement.").format(
                    name, invoice.custom_partner_settlement
                ),
                "INVOICE_ALREADY_SETTLED",
                invoice=name, settlement=invoice.custom_partner_settlement,
            )
        outstanding = flt(invoice.outstanding_amount)
        if outstanding <= AMOUNT_EPSILON:
            _err(
                _("Sales Invoice {0} has nothing outstanding, so it cannot be part of a settlement.").format(name),
                "INVOICE_NOT_OUTSTANDING", invoice=name,
            )

        # Everything SNAPSHOTTED off the invoice, never the partner's live figures: the
        # first time a partner renegotiates - or switches between a percentage and a flat
        # fee - reading the live ones would restate every past month.
        #
        # An invoice written before flat fees existed carries no type. It is read as
        # Percentage, which is what it was.
        if cstr(invoice.custom_partner_commission_type).strip() == "Amount":
            # A flat fee does not scale, so it is NOT pro-rated down a partly paid invoice -
            # but it cannot exceed what is still owed either, or the settlement would book
            # more commission than the receivable it is clearing and could never tie out.
            commission = min(flt(invoice.custom_partner_commission_amount), outstanding)
        else:
            commission = flt(outstanding * flt(invoice.custom_partner_commission_rate) / 100.0)

        rows.append({
            "sales_invoice": invoice.name,
            "posting_date": invoice.posting_date,
            "partner_order_ref": invoice.custom_partner_order_ref,
            "gross_amount": outstanding,
            "commission_amount": commission,
        })

    return rows


def _post_settlement_payment(
    settlement, partner_doc, company, commission_account, adjustment_account, rows
):
    """One submitted Receive that closes every covered invoice IN FULL.

    The references carry the gross - what the invoices actually owe - while the money that
    arrived is only `received_amount`. The difference is the commission, and it enters as
    a DEDUCTION so ERPNext's own `validate_difference_amount` proves the arithmetic:
    allocated - received - deductions must come to zero or the Payment Entry refuses to
    submit. That is a second, independent check on the same tie-out this API already
    enforced, which is why the deductions are built from the stored figures rather than
    recomputed.
    """
    party_account = get_party_account("Customer", partner_doc.customer, company)
    if not party_account:
        _err(
            _("No receivable account is configured for {0}.").format(partner_doc.customer),
            "PARTNER_RECEIVABLE_ACCOUNT_MISSING",
            customer=partner_doc.customer,
        )

    pe = frappe.new_doc("Payment Entry")
    pe.payment_type = "Receive"
    pe.company = company
    pe.posting_date = settlement.posting_date
    pe.party_type = "Customer"
    pe.party = partner_doc.customer
    pe.paid_from = party_account
    pe.paid_to = settlement.received_in
    pe.paid_amount = flt(settlement.received_amount)
    pe.received_amount = flt(settlement.received_amount)
    pe.reference_no = settlement.name
    pe.reference_date = settlement.posting_date
    pe.remarks = _("Delivery partner settlement {0} for {1}.").format(settlement.name, partner_doc.name)

    for row in rows:
        pe.append("references", {
            "reference_doctype": "Sales Invoice",
            "reference_name": row["sales_invoice"],
            "allocated_amount": flt(row["gross_amount"]),
        })

    cost_center = frappe.get_cached_value("Company", company, "cost_center")
    for amount, account, description in (
        (flt(settlement.commission_amount), commission_account, _("Delivery partner commission")),
        (flt(settlement.adjustment_amount), adjustment_account, _("Delivery partner settlement adjustment")),
    ):
        if abs(amount) <= AMOUNT_EPSILON:
            continue
        # Two rows, and two ACCOUNTS. A clawback or a rounding difference is not
        # commission; booking both to one account makes the commission figure useless for
        # reporting, which is the whole reason anyone looks at it.
        pe.append("deductions", {
            "account": account,
            "cost_center": cost_center,
            "amount": amount,
            "description": description,
        })

    pe.flags.ignore_permissions = True
    pe.insert(ignore_permissions=True)
    pe.submit()
    return pe


def _invoice_parties(rows) -> dict:
    """Who each covered invoice is billed to, and the receivable it sits in.

    Kept OUT of the settlement rows on purpose: those are appended straight into the child
    table, and the party is not the settlement's to record - it is the invoice's.
    """
    return {
        row["sales_invoice"]: frappe.db.get_value(
            "Sales Invoice", row["sales_invoice"], ["customer", "debit_to"], as_dict=True
        )
        for row in rows
    }


def _post_settlement_journal(
    settlement, partner_doc, company, commission_account, adjustment_account, rows, parties
):
    """The settlement's money as ONE Journal Entry, for invoices billed to many customers.

    The same accounting as `_post_settlement_payment`, spread over one row per invoice
    instead of one party:

        Dr Bank / Cash            received_amount
        Dr Commission Expense     commission_amount
        Dr Adjustment             adjustment_amount   (Cr, if it is negative)
            Cr <debit_to>  party = <invoice customer>  ref = Sales Invoice   per invoice

    Each credit row REFERENCES its invoice, so ERPNext closes it and writes the Payment
    Ledger Entry that customer balances are read from - a bare credit to the customer would
    leave the invoice open and the customer owing money nobody is asking them for.

    `cheque_no` / `cheque_date` are always set: ERPNext insists on both whenever a bank
    account is on the entry, and the settlement name is the reference anyone reconciling
    the bank statement will be looking for.
    """
    cost_center = frappe.get_cached_value("Company", company, "cost_center")

    je = frappe.new_doc("Journal Entry")
    je.voucher_type = (
        "Bank Entry"
        if frappe.get_cached_value("Account", settlement.received_in, "account_type") == "Bank"
        else "Journal Entry"
    )
    je.company = company
    je.posting_date = settlement.posting_date
    je.cheque_no = settlement.name
    je.cheque_date = settlement.posting_date
    je.user_remark = _(
        "Delivery partner settlement {0} for {1}: invoices billed to the app's customers, "
        "collected by the partner."
    ).format(settlement.name, partner_doc.name)

    def _line(account, amount, description=None, **extra):
        # A negative figure is the same line on the other side, never a negative debit.
        amount = flt(amount)
        if abs(amount) <= AMOUNT_EPSILON:
            return
        je.append("accounts", {
            "account": account,
            "cost_center": cost_center,
            "debit_in_account_currency": amount if amount > 0 else 0,
            "credit_in_account_currency": -amount if amount < 0 else 0,
            "user_remark": description,
            **extra,
        })

    _line(settlement.received_in, settlement.received_amount, _("Received from {0}").format(partner_doc.name))
    # Two accounts, as in the Payment Entry: a clawback is not commission.
    _line(commission_account, settlement.commission_amount, _("Delivery partner commission"))
    _line(adjustment_account, settlement.adjustment_amount, _("Delivery partner settlement adjustment"))

    for row in rows:
        party = parties[row["sales_invoice"]]
        _line(
            party.debit_to,
            -flt(row["gross_amount"]),
            _("{0} order {1}").format(partner_doc.name, row.get("partner_order_ref") or row["sales_invoice"]),
            party_type="Customer",
            party=party.customer,
            reference_type="Sales Invoice",
            reference_name=row["sales_invoice"],
        )

    je.flags.ignore_permissions = True
    je.insert(ignore_permissions=True)
    je.submit()
    return je


def _settlement_result(settlement, *, replayed: bool) -> dict:
    return {
        "settlement": settlement.name,
        "partner": settlement.partner,
        "partner_name": settlement.partner_name,
        # The name this endpoint shipped with for one day, kept so an older client that
        # reads it does not start seeing null.
        "delivery_partner": settlement.partner,
        "customer": settlement.customer,
        "statement_reference": settlement.statement_reference,
        "commission_rate": flt(settlement.commission_rate),
        "adjustment_account": settlement.adjustment_account,
        "payment_entry": settlement.payment_entry,
        # Set INSTEAD of payment_entry when the covered orders were billed to the app's
        # customers. `.get` so a site that has not migrated yet still answers.
        "journal_entry": settlement.get("journal_entry"),
        "posting_date": cstr(settlement.posting_date),
        "gross_amount": flt(settlement.gross_amount),
        "commission_amount": flt(settlement.commission_amount),
        "adjustment_amount": flt(settlement.adjustment_amount),
        "received_amount": flt(settlement.received_amount),
        "received_in": settlement.received_in,
        "invoice_count": len(settlement.invoices or []),
        "invoices": [
            {
                "sales_invoice": row.sales_invoice,
                "posting_date": cstr(row.posting_date),
                "partner_order_ref": row.partner_order_ref,
                "gross_amount": flt(row.gross_amount),
                "commission_amount": flt(row.commission_amount),
            }
            for row in settlement.invoices or []
        ],
        # True when a retry found the settlement a previous attempt had already made, so
        # the client can show the result again without paying the partner down twice.
        "replayed": replayed,
    }


# ---------------------------------------------------------------------------
# get_partner_summary
# ---------------------------------------------------------------------------


@frappe.whitelist()
@standardize_response
def get_partner_summary(partner=None, from_date=None, to_date=None):
    """The month's figures for one partner, computed over EVERY matching invoice.

    The client already derives all of this by summing the order list it fetched, and falls
    back to that automatically when this is missing. What the fallback cannot do is see
    past the page it asked for - it requests 500 rows, and a busy partner eventually
    exceeds that, at which point every figure on the detail page silently understates by
    however much fell off the end. This aggregates in SQL, so the page size stops mattering.
    """
    partner_doc = _load_partner(partner)

    conditions = ["si.custom_delivery_partner = %(partner)s", "si.docstatus = 1"]
    values = {"partner": partner_doc.name}
    if from_date:
        conditions.append("si.posting_date >= %(from_date)s")
        values["from_date"] = getdate(from_date)
    if to_date:
        conditions.append("si.posting_date <= %(to_date)s")
        values["to_date"] = getdate(to_date)
    where = " and ".join(conditions)

    totals = frappe.db.sql(
        f"""
        select
            count(*) as order_count,
            coalesce(sum(si.grand_total), 0) as gross_amount,
            coalesce(sum(si.custom_partner_commission_amount), 0) as commission_amount,
            coalesce(sum(si.outstanding_amount), 0) as outstanding_amount,
            coalesce(sum(case when si.custom_partner_settlement is null
                          or si.custom_partner_settlement = '' then 1 else 0 end), 0) as unsettled_count,
            coalesce(sum(case when si.custom_partner_settlement is null
                          or si.custom_partner_settlement = '' then si.outstanding_amount
                          else 0 end), 0) as unsettled_amount
        from `tabSales Invoice` si
        where {where}
        """,
        values,
        as_dict=True,
    )[0]

    last = frappe.get_all(
        "Delivery Partner Settlement",
        filters={"partner": partner_doc.name, "docstatus": ["<", 2]},
        fields=["name", "posting_date", "gross_amount", "commission_amount", "received_amount"],
        order_by="posting_date desc, creation desc",
        limit_page_length=1,
        ignore_permissions=True,
    )

    gross = flt(totals.gross_amount)
    commission = flt(totals.commission_amount)
    return {
        "partner": partner_doc.name,
        "partner_label": partner_doc.partner_name or partner_doc.name,
        "customer": partner_doc.customer,
        "is_active": bool(partner_doc.is_active),
        "is_inside": bool(partner_doc.get("is_inside")),
        # The partner's LIVE terms, for the header. `commission_amount` below is this
        # period's total and has nothing to do with `partner_commission_amount`, which is
        # the flat fee per order - hence the two names rather than one overloaded key.
        "commission_type": cstr(partner_doc.commission_type) or "Percentage",
        "commission_rate": flt(partner_doc.commission_rate),
        "partner_commission_amount": flt(partner_doc.commission_amount),
        "from_date": cstr(getdate(from_date)) if from_date else None,
        "to_date": cstr(getdate(to_date)) if to_date else None,
        "order_count": int(totals.order_count or 0),
        "gross_amount": gross,
        "commission_amount": commission,
        # What the partner still owes us once their commission is taken off the gross.
        "net_amount": flt(gross - commission),
        "outstanding_amount": flt(totals.outstanding_amount),
        "unsettled_count": int(totals.unsettled_count or 0),
        "unsettled_amount": flt(totals.unsettled_amount),
        "settled_count": int(totals.order_count or 0) - int(totals.unsettled_count or 0),
        "last_settlement": (
            {
                "name": last[0].name,
                "posting_date": cstr(last[0].posting_date),
                "gross_amount": flt(last[0].gross_amount),
                "commission_amount": flt(last[0].commission_amount),
                "received_amount": flt(last[0].received_amount),
            }
            if last
            else None
        ),
    }


# ---------------------------------------------------------------------------
# attach_invoice_to_partner
# ---------------------------------------------------------------------------


@frappe.whitelist()
@standardize_response
def attach_invoice_to_partner(
    sales_invoice=None,
    delivery_partner=None,
    partner_order_ref=None,
    partner_customer_name=None,
    partner_commission_rate=None,
    partner_commission_amount=None,
):
    """Make an already-submitted Due sale a delivery partner order, after the fact.

    The partner can otherwise only be picked while the sale is rung up. A cashier who saved
    the sale as Due and only then learned it was an app order had no way back: the invoice
    never reached the partner page or a settlement, and the till would still collect it -
    charging the customer for money the partner already took.

    Only the partner snapshot is stamped, exactly as `create_pos_sale` writes it. Nothing is
    posted: a Due partner sale is an ordinary receivable either way, so the ledger is already
    right. What changes is who is expected to pay it - which is why an invoice that anyone
    has already paid anything against is refused. That money is in a drawer or a bank, and
    the partner's settlement would collect it a second time.
    """
    try:
        return _attach_invoice_to_partner(
            sales_invoice=sales_invoice,
            delivery_partner=delivery_partner,
            partner_order_ref=partner_order_ref,
            partner_customer_name=partner_customer_name,
            partner_commission_rate=partner_commission_rate,
            partner_commission_amount=partner_commission_amount,
        )
    except Exception:
        # standardize_response swallows the exception, so roll back here - see create_settlement.
        frappe.db.rollback()
        raise


def _attach_invoice_to_partner(
    *, sales_invoice, delivery_partner, partner_order_ref, partner_customer_name,
    partner_commission_rate, partner_commission_amount,
):
    # Deferred: pos imports nothing from here at module level, but keep it that way.
    from pet_app.api.pos import _amount_due, _require_pos_operator, _resolve_partner_context

    _require_pos_operator()

    name = cstr(sales_invoice).strip()
    if not name or not frappe.db.exists("Sales Invoice", name):
        _err(_("Sales Invoice {0} was not found.").format(name or "-"), "INVOICE_NOT_FOUND")

    # Locked first, so two cashiers attaching the same sale cannot both pass the checks.
    frappe.db.sql("select name from `tabSales Invoice` where name = %s for update", name)
    doc = frappe.get_doc("Sales Invoice", name)
    if not frappe.has_permission("Sales Invoice", "read", doc=doc):
        _err(
            _("You do not have access to Sales Invoice {0}.").format(name),
            "INVOICE_NOT_PERMITTED",
            invoice=name,
        )

    if doc.docstatus != 1 or doc.is_return:
        _err(
            _("Only a submitted sale can be given to a delivery partner. {0} is not one.").format(name),
            "INVOICE_NOT_SUBMITTED",
            invoice=name,
            docstatus=doc.docstatus,
        )

    existing = cstr(doc.get("custom_delivery_partner")).strip()
    if existing:
        _err(
            _("{0} is already a {1} order.").format(name, existing),
            "PARTNER_ALREADY_ATTACHED",
            invoice=name,
            delivery_partner=existing,
        )

    due = _amount_due(doc)
    outstanding = flt(doc.outstanding_amount)
    if doc.is_pos or flt(doc.paid_amount) > AMOUNT_EPSILON or outstanding + AMOUNT_EPSILON < due:
        _err(
            _("{0} has already been paid in full or in part, so the delivery partner cannot "
              "collect it. Only an unpaid (Due) sale can be given to a partner.").format(name),
            "PARTNER_INVOICE_ALREADY_PAID",
            invoice=name,
            outstanding_amount=outstanding,
            amount_due=due,
        )
    if outstanding <= AMOUNT_EPSILON:
        _err(
            _("{0} has nothing outstanding.").format(name),
            "PARTNER_INVOICE_ALREADY_PAID",
            invoice=name,
            outstanding_amount=outstanding,
        )

    pos_profile = cstr(doc.get("custom_pos_profile")).strip() or None
    ctx = _resolve_partner_context(
        delivery_partner, partner_order_ref, partner_customer_name, partner_commission_rate,
        customer=doc.customer, is_paid=False, tendered=0.0,
        partner_commission_amount=partner_commission_amount,
        pos_profile=pos_profile,
    )
    if not ctx:
        _err(_("Pick the delivery partner."), "PARTNER_REQUIRED")

    if ctx.is_inside and not pos_profile:
        # The till's own walk-in check needs the profile; an older sale may not carry one,
        # so refuse every till's walk-in customer instead.
        walk_ins = {
            cstr(c).strip()
            for c in frappe.get_all("POS Profile", filters={"customer": ["is", "set"]}, pluck="customer")
        }
        if doc.customer in walk_ins:
            _err(
                _("A {0} order is billed to the customer who ordered it, so it cannot stay on "
                  "the walk-in customer {1}.").format(ctx.label, doc.customer),
                "PARTNER_INSIDE_NEEDS_REAL_CUSTOMER",
                delivery_partner=ctx.partner,
                customer=doc.customer,
            )

    # The same arithmetic create_pos_sale uses, on the same figure.
    if ctx.commission_type == "Amount":
        commission = flt(ctx.commission_amount)
        if commission >= due - AMOUNT_EPSILON:
            _err(
                _("{0} keeps {1} on every order, which is not less than this order's {2}.").format(
                    ctx.label,
                    frappe.format_value(commission, {"fieldtype": "Currency"}),
                    frappe.format_value(due, {"fieldtype": "Currency"}),
                ),
                "PARTNER_COMMISSION_EXCEEDS_TOTAL",
                delivery_partner=ctx.partner,
                commission_amount=commission,
                invoice_total=due,
            )
    else:
        commission = flt(due * flt(ctx.commission_rate) / 100.0)

    values = {
        "custom_delivery_partner": ctx.partner,
        "custom_partner_order_ref": ctx.order_ref,
        "custom_partner_customer_name": ctx.customer_name,
        "custom_partner_commission_type": ctx.commission_type,
        "custom_partner_commission_rate": ctx.commission_rate,
        "custom_partner_commission_amount": commission,
        "custom_partner_is_inside": 1 if ctx.is_inside else 0,
    }
    values = {k: v for k, v in values.items() if doc.meta.has_field(k)}
    doc.db_set(values, update_modified=True)

    doc.add_comment(
        "Comment",
        _(
            "Given to delivery partner {0} (order {1}) after the sale, by {2}. {0} collects "
            "this money and pays it in their settlement - do NOT take payment at the till. "
            "Commission {3}."
        ).format(
            ctx.label,
            ctx.order_ref,
            frappe.session.user,
            frappe.format_value(commission, {"fieldtype": "Currency"}),
        ),
    )

    return {
        "invoice": doc.name,
        "customer": doc.customer,
        "delivery_partner": ctx.partner,
        "partner_label": ctx.label,
        "partner_order_ref": ctx.order_ref,
        "is_inside": bool(ctx.is_inside),
        "commission_type": ctx.commission_type,
        "commission_amount": commission,
        "outstanding_amount": outstanding,
    }
