"""Shared custody, authorization and transaction boundary for driver fulfillment.

Quantities in the custody ledger are always stock-UOM quantities. Source rows are
immutable submitted Stock Entry Details, not mutable Driver master values.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import math
from functools import wraps

import frappe
from frappe.utils import cint, cstr, flt

from pet_app.api.response import standardize_response
from pet_app.utils.invoice_reuse import resolve_company

EPS = 0.000001
SETTINGS = "Pet App Accounting Settings"
ARRANGEMENTS = ("Cash on Delivery", "Prepaid", "On Account", "Card on Delivery", "Wallet on Delivery")


class DriverSalesInvoiceMixin:
    def _driver_source_row(self, row):
        if self.get("is_return") and row.get("sales_invoice_item"):
            return frappe.get_doc("Sales Invoice Item", row.sales_invoice_item)
        if row.get("so_detail"):
            return frappe.get_doc("Sales Order Item", row.so_detail)

    def set_missing_item_details(self, for_validate=False):
        super().set_missing_item_details(for_validate=for_validate)
        if not self.get("custom_driver_flow"):
            return
        # Core refreshes item_tax_rate from today's template even when provided.
        # Restore the agreed order/return row before taxes and totals are computed.
        for row in self.items:
            source = self._driver_source_row(row)
            if source:
                for field in ("rate", "price_list_rate", "discount_percentage", "discount_amount",
                              "uom", "stock_uom", "conversion_factor", "item_tax_template", "item_tax_rate"):
                    row.set(field, source.get(field))

    def calculate_taxes_and_totals(self):
        if not self.get("custom_driver_flow") or not self.items:
            return super().calculate_taxes_and_totals()
        sources = [self._driver_source_row(row) for row in self.items]
        if not all(sources):  # A new van sale uses today's normal tax calculation.
            return super().calculate_taxes_and_totals()
        from erpnext.controllers.taxes_and_totals import calculate_taxes_and_totals

        class AgreedOrderTaxes(calculate_taxes_and_totals):
            def validate_item_tax_template(self):
                # The order already selected and validated its applicable template.
                return

            def update_item_tax_map(self):
                for row, source in zip(self.doc.items, sources):
                    row.item_tax_rate = source.get("item_tax_rate") or "{}"
                    row.item_tax_template = source.get("item_tax_template")

        AgreedOrderTaxes(self)
        self.calculate_commission()
        self.calculate_contribution()


def fail(message, code="DRIVER_ORDER_INVALID"):
    exc = frappe.ValidationError(message)
    exc.code = code
    raise exc


def number(value, *, zero=False):
    try:
        result = float(value)
    except (TypeError, ValueError):
        fail("A numeric quantity or amount is required.")
    if not math.isfinite(result) or result < 0 or (not zero and result == 0):
        fail("Amounts and quantities must be finite and positive (or explicitly allowed zero).")
    return result


def rows(value, *, empty=False):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            fail("items must be valid JSON.")
    if not isinstance(value, list) or any(not isinstance(r, dict) for r in value):
        fail("items must be an array of objects.")
    if not empty and not value:
        fail("At least one item is required.")
    return value


def permit(doctype, ptype="read", doc=None):
    # driver_self acts for a signed-in driver on his own order only, after checking the
    # order is his. The driver login holds no document permissions by design.
    if getattr(frappe.local, "driver_self_scope", None):
        return
    if not frappe.has_permission(doctype, ptype=ptype, doc=doc):
        frappe.throw(f"Not permitted to {ptype} {doctype}.", frappe.PermissionError)


def enabled():
    return frappe.get_meta(SETTINGS).has_field("custom_enable_driver_orders") and cint(
        frappe.db.get_single_value(SETTINGS, "custom_enable_driver_orders")
    )


def profile_context(name):
    from pet_app.api.accounting.cashier import _get_authorized_profile
    from pet_app.utils.branch import resolve_branch_for_pos
    from pet_app.utils.branch_warehouse import branch_warehouse
    from pet_app.api.permissions import require_restriction_value

    profile = _get_authorized_profile(name)
    company = resolve_company()
    if profile.company != company:
        fail("POS Profile belongs to a different company.")
    if profile.currency != frappe.db.get_value("Company", company, "default_currency"):
        fail("Driver orders currently require a POS Profile in company currency.")
    branch, _ = resolve_branch_for_pos(profile.name, None)
    warehouse = branch_warehouse(branch)
    if not branch or not warehouse or warehouse != profile.warehouse:
        fail("POS Profile and Branch must identify the same stock warehouse.", "DRIVER_BRANCH_CONFIGURATION")
    require_restriction_value("warehouse", warehouse)
    validate_warehouse(warehouse, company)
    # `till` is the profile only when the caller stands behind it. Payment Entries are
    # stamped with it, and the cashier till summary counts Payment Entries by that stamp.
    return frappe._dict(profile=profile, till=profile.name, company=company, branch=branch, warehouse=warehouse)


def doc_context(doc):
    """Act on an existing driver document. Orders are never tied to a till."""
    from pet_app.utils.branch_warehouse import branch_warehouse

    if doc.company != resolve_company():
        fail("This document belongs to a different company.")
    # No till: the document's own company/branch. Access is the caller's native
    # permission on the document (permit / has_permission applies User Permissions).
    return frappe._dict(profile=None, till=None,
        company=doc.company, branch=doc.branch, warehouse=branch_warehouse(doc.branch))


# Defaults a till used to supply. Without one they come from the site's own settings.

def ctx_currency(ctx):
    return (ctx.profile.currency if ctx.profile else None) or frappe.db.get_value("Company", ctx.company, "default_currency")


def ctx_price_list(ctx):
    return (ctx.profile.selling_price_list if ctx.profile else None) or frappe.db.get_single_value("Selling Settings", "selling_price_list")


def ctx_taxes_template(ctx):
    if ctx.profile:
        return ctx.profile.get("taxes_and_charges")
    return frappe.db.get_value("Sales Taxes and Charges Template", {"company": ctx.company, "is_default": 1, "disabled": 0})


def ctx_allows_partial(ctx):
    # A till decides for itself; without one, part-payments are allowed (the rest stays owed).
    return bool(ctx.profile.allow_partial_payment) if ctx.profile else True


def ctx_mode(ctx, mode=None):
    """The Mode of Payment: the one sent, else the till's default, else the site default cash mode."""
    from pet_app.api.pos import _resolve_mode_of_payment
    if mode or ctx.profile:
        return _resolve_mode_of_payment(mode, ctx.profile)
    default = frappe.db.get_single_value(SETTINGS, "default_cash_mode_of_payment")
    if not default:
        fail("Choose a Mode of Payment.", "PAYMENT_MODE_REQUIRED")
    return default


def mode_account(mode, company):
    account = frappe.db.get_value("Mode of Payment Account", {"parent": mode, "company": company}, "default_account")
    if not account:
        fail(f"Mode of Payment {mode} has no account for {company}.", "PAYMENT_MODE_ACCOUNT_MISSING")
    return account


def money_account(company, mode, pos_profile=None):
    """Where money the shop takes lands: (account, till).

    With a till, ANY enabled till of the company (no assignment check): a Cash mode lands
    in that till's cash account, a non-cash mode in the mode's account; the Payment Entry
    is stamped with the till so its summary and count see it. Without a till: the chosen
    Mode of Payment's company account."""
    if not pos_profile:
        return mode_account(mode, company), None
    from pet_app.api.accounting.cashier import _resolve_payment_account
    row = frappe.db.get_value("POS Profile", pos_profile, ["name", "company", "disabled"], as_dict=True)
    if not row or row.disabled or row.company != company:
        fail(f"Till {pos_profile} is not an enabled till of {company}.", "DRIVER_TILL_INVALID")
    profile = frappe.get_cached_doc("POS Profile", pos_profile)
    return _resolve_payment_account(company, mode, profile), pos_profile


def branch_context(branch=None, till_hint=None):
    """Context for creating orders and van loads. Never tied to a till: the branch sent,
    else the user's own, else (a convenience only) the branch of a till the client
    happens to have selected, else the site's only branch."""
    from pet_app.utils.branch import assert_can_write_to_branch, get_current_branch, _sole_branch
    from pet_app.utils.branch_warehouse import branch_warehouse
    from pet_app.api.permissions import require_restriction_value

    hinted = frappe.db.get_value("POS Profile", till_hint, "branch") if till_hint else None
    branch = cstr(branch).strip() or get_current_branch() or hinted or _sole_branch()
    if not branch:
        fail("Choose a branch (no till was selected and your login covers several branches).", "DRIVER_BRANCH_REQUIRED")
    assert_can_write_to_branch(branch)
    company = resolve_company()
    warehouse = branch_warehouse(branch)
    if not warehouse:
        fail(f"Branch {branch} has no stock warehouse.", "DRIVER_BRANCH_CONFIGURATION")
    require_restriction_value("warehouse", warehouse)
    validate_warehouse(warehouse, company)
    return frappe._dict(profile=None, till=None, company=company, branch=branch, warehouse=warehouse)


def visible_branches():
    """Branch filter value for till-less reads: None (all) or ["in", [...]]."""
    from pet_app.utils.branch import get_user_branches, user_sees_all_branches

    if user_sees_all_branches():
        return None
    return ["in", get_user_branches()]


def validate_warehouse(name, company):
    row = frappe.db.get_value("Warehouse", name, ["company", "disabled", "is_group"], as_dict=True)
    if not row or row.company != company or row.disabled or row.is_group:
        fail(f"Warehouse {name} must be an enabled leaf in {company}.")


def validate_cash(name, company, driver=None):
    row = frappe.db.get_value("Account", name, ["company", "root_type", "account_type", "is_group", "disabled", "account_currency"], as_dict=True)
    if not row or row.company != company or row.root_type != "Asset" or row.account_type != "Cash" or row.is_group or row.disabled:
        fail(f"Driver cash account {name} must be an enabled Asset/Cash ledger in {company}.", "DRIVER_CASH_ACCOUNT_INVALID")
    if driver and frappe.db.exists("Driver", {"name": ["!=", driver], "custom_cash_account": name}):
        fail("Each driver must have a distinct cash account.")
    if row.account_currency != frappe.db.get_value("Company", company, "default_currency"):
        fail("Driver cash account must use the company currency.")


def provision_driver(name, company, repair=False):
    """Explicit setup; never provision from a sale or a stock hook."""
    driver = frappe.get_doc("Driver", name)
    settings = frappe.get_single(SETTINGS)
    cash = driver.custom_cash_account
    valid = frappe.db.get_value("Account", cash, ["root_type", "account_type"], as_dict=True)
    if not valid or valid.root_type != "Asset" or valid.account_type != "Cash":
        if not repair or frappe.db.exists("GL Entry", {"account": cash}):
            fail(f"Driver {name} needs a reviewed cash account; existing ledger history cannot be repointed.")
        parent = frappe.get_doc("Account", settings.custom_driver_cash_parent)
        if parent.company != company or not parent.is_group or parent.root_type != "Asset":
            fail("Configure an Asset group for driver cash accounts.")
        account_name = f"Driver Cash {name}"
        cash = frappe.db.get_value("Account", {"company": company, "account_name": account_name})
        if not cash:
            cash = frappe.get_doc({"doctype": "Account", "account_name": account_name, "parent_account": parent.name,
                "company": company, "account_type": "Cash", "root_type": "Asset", "is_group": 0,
                "account_currency": frappe.db.get_value("Company", company, "default_currency")}).insert(ignore_permissions=True).name
    validate_cash(cash, company, name)
    warehouse = driver.get("custom_warehouse")
    if not warehouse:
        parent = frappe.get_doc("Warehouse", settings.custom_driver_warehouse_parent)
        if parent.company != company or not parent.is_group or parent.disabled:
            fail("Configure an enabled driver warehouse group in the company.")
        warehouse_name = f"Driver {name}"
        warehouse = frappe.db.get_value("Warehouse", {"company": company, "warehouse_name": warehouse_name})
        if not warehouse:
            warehouse = frappe.get_doc({"doctype": "Warehouse", "warehouse_name": warehouse_name,
                "company": company, "parent_warehouse": parent.name, "is_group": 0}).insert(ignore_permissions=True).name
    validate_warehouse(warehouse, company)
    if frappe.db.exists("Driver", {"name": ["!=", name], "custom_warehouse": warehouse}):
        fail("Each driver must have a distinct warehouse.")
    frappe.db.set_value("Driver", name, {"custom_cash_account": cash, "custom_warehouse": warehouse})
    fee = provision_fee_account(name, company) if fee_config() else None
    return {"driver": name, "warehouse": warehouse, "cash_account": cash, "fee_account": fee}


def fee_config():
    """Booked-fee accounts, or None while unconfigured (fee then stays off the books,
    the pre-2026-09 rule, so an unmigrated site keeps working)."""
    meta = frappe.get_meta(SETTINGS)
    if not meta.has_field("custom_driver_fee_income_account") or not frappe.get_meta("Driver").has_field("custom_fee_account"):
        return None
    settings = frappe.get_cached_doc(SETTINGS)
    income, expense = settings.get("custom_driver_fee_income_account"), settings.get("custom_driver_fee_expense_account")
    if not income or not expense:
        return None
    return frappe._dict(income=income, expense=expense,
        shortage=settings.get("custom_driver_shortage_account"), overage=settings.get("custom_driver_overage_account"))


def provision_fee_account(name, company):
    """The liability the shop owes one driver for delivery fees: 'Driver Fees {driver}'."""
    if not frappe.get_meta("Driver").has_field("custom_fee_account"):
        return None
    current = frappe.db.get_value("Driver", name, "custom_fee_account")
    if current:
        return current
    parent = frappe.db.get_single_value(SETTINGS, "custom_driver_fee_payable_parent")
    if not parent:
        fail("Configure the Driver Fee Payable Parent account group.", "DRIVER_FEE_NOT_CONFIGURED")
    group = frappe.db.get_value("Account", parent, ["company", "is_group", "root_type"], as_dict=True)
    if not group or group.company != company or not group.is_group or group.root_type != "Liability":
        fail("Driver Fee Payable Parent must be a Liability group in the company.", "DRIVER_FEE_NOT_CONFIGURED")
    account_name = f"Driver Fees {name}"
    account = frappe.db.get_value("Account", {"company": company, "account_name": account_name})
    if not account:
        account = frappe.get_doc({"doctype": "Account", "account_name": account_name, "parent_account": parent,
            "company": company, "root_type": "Liability", "is_group": 0,
            "account_currency": frappe.db.get_value("Company", company, "default_currency")}).insert(ignore_permissions=True).name
    frappe.db.set_value("Driver", name, "custom_fee_account", account)
    return account


def fee_balance(account):
    """What the shop owes the driver in fees (credit balance of his fee account)."""
    return -cash_balance(account) if account else 0


def driver_context(name, company):
    driver = frappe.get_doc("Driver", name)
    permit("Driver", doc=driver)
    if driver.status != "Active":
        fail("Driver must be Active.")
    validate_warehouse(driver.get("custom_warehouse"), company)
    validate_cash(driver.custom_cash_account, company, driver.name)
    return driver


def operation(fn):
    """One company mutex, durable unique replay key, one all-or-nothing transaction.

    Comment is an existing, durable audit document. Integration Request is unsuitable:
    Frappe routinely deletes its old rows, losing retry protection.
    """
    signature = inspect.signature(fn)

    @wraps(fn)
    def transaction(*args, **kwargs):
        previous = getattr(frappe.local, "driver_orders_operation", None)
        try:
            # No till gate: each operation checks native doctype permissions itself.
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            if not enabled():
                fail("Driver orders are disabled pending configuration and staging verification.", "DRIVER_ORDERS_DISABLED")
            payload = dict(bound.arguments)
            key = cstr(payload.pop("idempotency_key", None)).strip()
            if not key or len(key) > 140:
                fail("Provide a stable idempotency_key of 1–140 characters.", "IDEMPOTENCY_KEY_REQUIRED")
            company = resolve_company()
            # Serializes order, load, payment and handover mutations in one lock order.
            frappe.db.sql("select name from tabCompany where name=%s for update", company)
            request_hash = hashlib.sha256(json.dumps([fn.__name__, payload], sort_keys=True, default=str).encode()).hexdigest()
            unique = hashlib.sha256(f"{company}:{frappe.session.user}:{key}".encode()).hexdigest()
            # A request may have established a REPEATABLE READ snapshot during auth,
            # before waiting for the company mutex. Locking reads see the commit we
            # just waited for; an ordinary SELECT could miss its retry record.
            existing = frappe.db.get_value("Comment", {"custom_driver_operation_key": unique}, ["name", "custom_driver_request_hash", "custom_driver_result"], as_dict=True, for_update=True)
            if existing:
                if existing.custom_driver_request_hash != request_hash:
                    fail("This idempotency key was already used for a different request.", "IDEMPOTENCY_CONFLICT")
                result = json.loads(existing.custom_driver_result)
                for ref in result.get("documents", []):
                    permit(ref["doctype"], doc=frappe.get_doc(ref["doctype"], ref["name"], for_update=True))
                result["replayed"] = True
                return result
            frappe.local.driver_orders_operation = unique
            audit = frappe.get_doc({"doctype": "Comment", "comment_type": "Info", "reference_doctype": "Company",
                "reference_name": company, "content": f"Driver operation: {fn.__name__}",
                "custom_driver_operation_key": unique, "custom_driver_request_hash": request_hash}).insert(ignore_permissions=True)
            frappe.local.driver_orders_operation = audit.name
            result = fn(*args, **kwargs)
            result.update(operation=audit.name, replayed=False)
            audit.db_set("custom_driver_result", json.dumps(result, default=str))
            return result
        except Exception:
            frappe.db.rollback()
            raise
        finally:
            frappe.local.driver_orders_operation = previous

    @wraps(fn)
    def retry_transaction(*args, **kwargs):
        # MariaDB snapshot isolation can refuse a locking read after a waiter sees a
        # newer commit (error 1020, exposed by Frappe as QueryDeadlockError). Restart
        # the entire already-rolled-back transaction, never only its last statement.
        for attempt in range(3):
            try:
                return transaction(*args, **kwargs)
            except frappe.QueryDeadlockError:
                if attempt == 2:
                    fail("Another cashier changed these records. Retry with the same idempotency key.", "DRIVER_BUSY_RETRY")
    return standardize_response(retry_transaction)


def stamp(doc, *, driver=None, branch=None, warehouse=None, cash=None, profile=None):
    doc.custom_driver_flow = 1
    doc.custom_driver_operation = frappe.local.driver_orders_operation
    doc.custom_driver = driver
    doc.branch = branch
    doc.custom_fulfillment_warehouse = warehouse
    doc.custom_driver_cash_account = cash
    if profile:
        doc.custom_pos_profile = profile
    return doc


def submit(doc):
    permit(doc.doctype, "create")
    permit(doc.doctype, "submit")
    # API validated the profile, branch, source document and custody; do not grant
    # blanket Warehouse User Permissions just to let a cashier serve a driver.
    doc.flags.ignore_permissions = True
    if doc.is_new():
        doc.insert(ignore_permissions=True)
    doc.submit()
    doc.reload()
    return doc


def managed_doc(doctype, name, ctx=None):
    doc = frappe.get_doc(doctype, name, for_update=bool(getattr(frappe.local, "driver_orders_operation", None)))
    permit(doctype, doc=doc)
    if not doc.get("custom_driver_flow") or doc.docstatus != 1:
        fail("Use a submitted document created by the driver-order workflow.")
    if doc.company != resolve_company():
        fail("This document belongs to a different company.")
    return doc


def stock_available(lines):
    wanted = {}
    for r in lines:
        key = (r["item_code"], r["warehouse"])
        wanted[key] = wanted.get(key, 0) + number(r["stock_qty"])
    for (item, warehouse), qty in sorted(wanted.items()):
        found = frappe.db.sql("select actual_qty from tabBin where item_code=%s and warehouse=%s for update", (item, warehouse))
        available = flt(found[0][0]) if found else 0
        if qty > available + EPS:
            fail(f"{item}: only {available} stock units available in {warehouse}; requested {qty}.", "INSUFFICIENT_STOCK")


def loads(driver, branch=None):
    current_read = bool(getattr(frappe.local, "driver_orders_operation", None))
    lock = " for update" if current_read else ""
    filters = {"custom_driver": driver, "custom_driver_flow": 1, "custom_driver_movement": "Load", "docstatus": 1}
    if branch:
        filters["branch"] = branch
    parents = frappe.db.get_values("Stock Entry", filters=filters, fieldname=["name", "branch", "custom_fulfillment_warehouse", "custom_driver_cash_account", "custom_sales_order"], order_by="creation asc, name asc", as_dict=True, for_update=current_read)
    result = []
    for parent in parents:
        for row in frappe.db.get_values("Stock Entry Detail", filters={"parent": parent.name}, fieldname=["name", "item_code", "stock_uom", "transfer_qty", "s_warehouse", "custom_driver_order_item"], order_by="idx asc", as_dict=True, for_update=current_read):
            used = frappe.db.sql("""select coalesce(sum(i.stock_qty),0) from `tabSales Invoice Item` i
                join `tabSales Invoice` p on p.name=i.parent where p.docstatus=1 and i.custom_driver_load_row=%s"""+lock, row.name)[0][0]
            returned = frappe.db.sql("""select coalesce(sum(i.transfer_qty),0) from `tabStock Entry Detail` i
                join `tabStock Entry` p on p.name=i.parent where p.docstatus=1 and i.custom_driver_load_row=%s"""+lock, row.name)[0][0]
            row.update(load=parent.name, branch=parent.branch, warehouse=parent.custom_fulfillment_warehouse,
                cash_account=parent.custom_driver_cash_account, order=parent.custom_sales_order,
                sold_qty=flt(used), returned_qty=flt(returned), available_qty=flt(row.transfer_qty)-flt(used)-flt(returned))
            result.append(row)
    return result


def allocate(available, item_code, stock_qty, *, order_item=None, load_row=None):
    left = number(stock_qty)
    allocated = []
    for row in available:
        if row.item_code != item_code or (row.custom_driver_order_item or None) != order_item:
            continue
        if load_row and row.name != load_row:
            continue
        qty = min(max(0, row.available_qty), left)
        if qty > EPS:
            allocated.append((row, qty))
            row.available_qty -= qty
            left -= qty
        if left <= EPS:
            break
    if left > EPS:
        fail(f"Insufficient unconsumed driver stock for {item_code}; short by {left} stock units.", "DRIVER_STOCK_ALLOCATED")
    return allocated


def cash_balance(account):
    lock = " for update" if getattr(frappe.local, "driver_orders_operation", None) else ""
    return flt(frappe.db.sql("select coalesce(sum(debit-credit),0) from `tabGL Entry` where account=%s and is_cancelled=0"+lock, account)[0][0])


def snapshot(driver, branch=None):
    for doctype in ("Sales Order", "Sales Invoice", "Stock Entry", "Payment Entry"):
        permit(doctype)
    doc = frappe.get_doc("Driver", driver)
    permit("Driver", doc=doc)
    if frappe.db.get_value("Account", doc.custom_cash_account, "company") != resolve_company():
        fail("Driver belongs to a different company.")
    stock = loads(driver, branch)
    accounts = {doc.custom_cash_account} | {r.cash_account for r in loads(driver) if r.cash_account}
    accounts |= set(frappe.get_all("Payment Entry", filters={"custom_driver": driver, "docstatus": 1, "custom_driver_flow": 1}, pluck="custom_driver_cash_account"))
    balances = [{"account": a, "cash_owed": cash_balance(a)} for a in sorted(a for a in accounts if a)]
    orders = frappe.get_all("Sales Order", filters={"custom_driver_flow": 1, "custom_driver": driver, "docstatus": 1,
        "custom_delivery_state": ["not in", ["Completed", "Cancelled"]], **({"branch": branch} if branch else {})},
        fields=["name", "branch", "custom_delivery_state", "grand_total", "custom_delivery_fee"])
    payments = frappe.get_all("Payment Entry", filters={"custom_driver_flow": 1, "custom_driver": driver, "docstatus": 1},
        fields=["name", "custom_driver_payment_kind", "paid_from", "paid_to", "paid_amount", "received_amount", "posting_date"])
    invoices = frappe.get_all("Sales Invoice", filters={"custom_driver_flow": 1, "custom_driver": driver, "docstatus": 1,
        **({"branch": branch} if branch else {})}, fields=["name", "customer", "outstanding_amount", "custom_direct_delivery_fee"])
    from frappe.utils import nowdate
    today = [r for r in payments if str(r.posting_date) == nowdate()]
    return {"driver": driver, "cash_owed": sum(r["cash_owed"] for r in balances), "cash_accounts": balances,
        "stock": [dict(r) for r in stock if r.available_qty > EPS], "open_orders": orders, "payments": payments,
        "customer_receivables": sum(max(0, flt(r.outstanding_amount)) for r in invoices),
        "customer_credit": sum(max(0, -flt(r.outstanding_amount)) for r in invoices),
        "unpaid_invoices": [r for r in invoices if abs(flt(r.outstanding_amount)) > EPS],
        "direct_fees_earned": sum(flt(r.custom_direct_delivery_fee) for r in invoices),
        **_fee_position(doc),
        "today_cash_collected": sum(flt(r.received_amount) for r in today if r.custom_driver_payment_kind == "Receipt" and r.paid_to in accounts),
        "today_cash_handed_over": sum(flt(r.paid_amount) for r in today if r.custom_driver_payment_kind == "Handover")}


def _fee_position(driver):
    account = driver.get("custom_fee_account")
    if not account:
        return {"fee_account": None, "fees_owed_to_driver": 0, "fees_paid_to_driver": 0}
    paid = flt(frappe.db.sql("select coalesce(sum(debit),0) from `tabGL Entry` where account=%s and is_cancelled=0", account)[0][0])
    return {"fee_account": account, "fees_owed_to_driver": fee_balance(account), "fees_paid_to_driver": paid}


def guard_document(doc, method=None):
    """Protect managed ledgers from Desk/API bypass, including cancellation."""
    active = getattr(frappe.local, "driver_orders_operation", None)
    old = doc.get_doc_before_save()
    managed = doc.get("custom_driver_flow") or (old and old.get("custom_driver_flow"))
    if managed:
        if method == "before_cancel" and active and doc.flags.get("driver_orders_amend"):
            # update_pos_order replaces an undispatched order by cancel + amend.
            return
        if method in ("before_cancel", "on_trash"):
            fail("Use a goods return, customer refund or order remainder closure; posted custody history cannot be deleted.", "DRIVER_DOCUMENT_LOCKED")
        if not active:
            fail("Modify driver documents through the driver-order APIs.", "DRIVER_DOCUMENT_LOCKED")
        if not doc.get("custom_driver_operation"):
            fail("Driver document is missing its operation reference.")
    elif doc.doctype == "Payment Entry" and doc.get("custom_pos_profile") and not doc.get("custom_driver_flow"):
        # Cashier endpoints intentionally create Payment Entries for till settlement and
        # on-profile cash operations. Those should follow cashier authorization, not
        # the driver order lock path used for custody movement.
        pass
    elif not active and frappe.get_meta("Driver").has_field("custom_warehouse"):
        warehouses = {r.get("warehouse") or r.get("s_warehouse") for r in doc.get("items") or []}
        warehouses |= {r.get("t_warehouse") for r in doc.get("items") or []}
        accounts = {doc.get("paid_from"), doc.get("paid_to")}
        accounts |= {r.get("account") for r in doc.get("accounts") or []}
        for warehouse in filter(None, warehouses):
            if frappe.db.exists("Driver", {"custom_warehouse": warehouse}) or frappe.db.exists("Stock Entry", {"custom_driver_flow": 1, "custom_fulfillment_warehouse": warehouse, "docstatus": 1}):
                fail("Driver warehouse movements must use the driver-order APIs.")
        for account in filter(None, accounts):
            if frappe.db.exists("Payment Entry", {"custom_driver_flow": 1, "custom_driver_cash_account": account, "docstatus": 1}) or (frappe.db.exists("Driver", {"custom_cash_account": account}) and enabled()):
                fail("Driver cash movements must use the driver-order APIs.")
            if frappe.get_meta("Driver").has_field("custom_fee_account") and frappe.db.exists("Driver", {"custom_fee_account": account}) and enabled():
                fail("Driver fee postings must use the driver-order APIs.")


def guard_audit(doc, method=None):
    if doc.get("custom_driver_operation_key") and (method == "on_trash" or not getattr(frappe.local, "driver_orders_operation", None)):
        fail("Driver operation audit records are immutable.")


def fulfillment_warehouse(doc):
    if not doc.get("custom_driver_flow"):
        return None
    guard_document(doc)
    warehouse = doc.get("custom_fulfillment_warehouse")
    validate_warehouse(warehouse, doc.company)
    # Validate persisted load provenance rather than the Driver's current warehouse.
    for item in doc.items:
        source = frappe.db.sql("""select d.item_code,p.custom_driver,p.custom_fulfillment_warehouse,p.branch,p.docstatus
            from `tabStock Entry Detail` d join `tabStock Entry` p on p.name=d.parent where d.name=%s for update""",
            item.get("custom_driver_load_row"), as_dict=True)
        if not source or source[0].docstatus != 1 or source[0].item_code != item.item_code or source[0].custom_driver != doc.custom_driver or source[0].branch != doc.branch or source[0].custom_fulfillment_warehouse != warehouse:
            fail("Invoice item does not match its submitted driver load.")
    return warehouse


def guard_driver(doc, method=None):
    if not doc.meta.has_field("custom_warehouse"):
        return
    old = doc.get_doc_before_save()
    if method == "on_trash":
        if (doc.get("custom_warehouse") and frappe.db.exists("Bin", {"warehouse": doc.custom_warehouse, "actual_qty": ["!=", 0]})) or cash_balance(doc.custom_cash_account):
            fail("Driver still holds stock or cash and cannot be deleted.")
        if frappe.db.exists("Stock Entry", {"custom_driver": doc.name, "docstatus": 1}):
            fail("Deactivate a driver with posted history instead of deleting it.")
    if old and (old.get("custom_warehouse") != doc.get("custom_warehouse") or old.custom_cash_account != doc.custom_cash_account):
        if (old.get("custom_warehouse") and frappe.db.exists("Bin", {"warehouse": old.custom_warehouse, "actual_qty": ["!=", 0]})) or cash_balance(old.custom_cash_account):
            fail("Settle the driver's stock and cash before changing custody accounts.")
    if doc.get("custom_warehouse"):
        validate_warehouse(doc.custom_warehouse, resolve_company())
        validate_cash(doc.custom_cash_account, resolve_company(), doc.name)
    number(doc.get("custom_delivery_fee") or 0, zero=True)


def notify_driver(driver, subject, doctype, name, body=None):
    """Tell the driver on his login: an Alert in his bell, mirrored to push by
    notifications.push. Runs inside the calling operation, so a rolled-back action
    notifies nobody and a replayed one never re-notifies. Never breaks the action."""
    try:
        user = frappe.db.get_value("Driver", driver, "user") if driver else None
        if not user or user in ("Administrator", "Guest", frappe.session.user):
            return
        if not frappe.db.get_value("User", user, "enabled"):
            return
        from pet_app.notifications.push import _frontend_base_url
        log = frappe.new_doc("Notification Log")
        log.update({"for_user": user, "from_user": frappe.session.user, "type": "Alert",
            "subject": subject, "email_content": body, "document_type": doctype, "document_name": name,
            # The driver cannot open a Sales Order form: a tap lands on his own page.
            "link": f"{_frontend_base_url() or ''}/drivers/me"})
        log.insert(ignore_permissions=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Driver notification failed")
