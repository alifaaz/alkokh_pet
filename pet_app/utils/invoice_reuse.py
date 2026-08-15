"""One open Draft per customer, per company, per branch, per stock kind.

Before this, all four backend creation paths called frappe.get_doc({...}).insert()
unconditionally: nothing looked for an existing Draft, and two identical invoices for one
customer on one day was the normal outcome rather than a race. This is the only place
that decides whether to create or append.

The match key is deliberate:

  customer   - the grain the rule is stated on.
  company    - never left to ERPNext's default here; the lookup filter and the created
               row must agree, and all four call sites used to omit it entirely.
  branch     - a live accounting dimension. Merging the clinic's and the boarding
               facility's charges onto one invoice would misattribute revenue in the
               ledger, not merely in a report.
  update_stock - a document-level flag with no per-row equivalent. Appending a stock line
               to a non-stock draft would leave that line moving no stock; flipping the
               flag would retroactively change lines already agreed. Keying on it means a
               customer may hold two open drafts at once, which is correct - they are not
               mergeable documents.

POS invoices are excluded in both directions: is_pos = 1 always creates, is never looked
up, and is never returned by a lookup. They demand immediate payment and must not absorb
or be absorbed. Returns (is_return = 1) are excluded for the same structural reason.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, cstr, getdate, nowdate

from pet_app.utils.branch import BRANCH_AUTHORISED_FLAG
from pet_app.utils.invoice_source import append_marker

GUARDIAN_FIELD_CANDIDATES = ("guardian_id", "guardian", "custom_guardian_id", "custom_guardian")


def resolve_company(company: str | None = None) -> str:
    """Explicit company, always.

    All four creation sites omitted it and relied on ERPNext's default. That is fine
    until the value is also a lookup filter - then a default that differs from what
    lands on the row would make an invoice unfindable by the next caller.
    """
    company = cstr(company).strip()
    if company:
        return company

    company = (
        frappe.db.get_single_value("Pet App Accounting Settings", "default_company")
        or frappe.defaults.get_user_default("Company")
        or frappe.defaults.get_global_default("company")
    )
    if not company:
        frappe.throw(_("No Company is configured; a Sales Invoice cannot be raised."))
    return company


def resolve_branch(branch: str | None = None) -> str | None:
    """The branch the insert will actually land on.

    Sales Invoice is a branch-scoped doctype, so utils.branch.stamp_branch_on_insert
    resolves one on before_insert whenever the caller supplies none. If the lookup
    filtered on a blank branch while the row was stamped "main", the next caller would
    never find the invoice and reuse would silently never happen. So the branch is
    resolved here, before the lookup, and passed in explicitly - the hook honours a
    supplied value (after checking the user may write to it).

    Mirrors the hook's order: caller's value, then the user's own branch, then the sole
    branch if the site has exactly one. If none of those resolve, the value stays None
    and the hook is left to decide - including to throw, which is its job, not ours.
    """
    branch = cstr(branch).strip()
    if branch:
        return branch

    from pet_app.utils.branch import _sole_branch, get_current_branch

    return get_current_branch() or _sole_branch() or None


def _lock_customer(customer: str):
    """Serialise invoice creation for one customer.

    The row that wants locking - the invoice - does not exist on the first call, so
    there is nothing to lock on. The Customer row always exists and is exactly the grain
    the match is on. Held for the rest of the transaction, so lookup -> decide ->
    insert/append cannot interleave with another writer for the same customer.

    Only effective while every writer goes through this helper; a future path that
    inserts a Sales Invoice directly reintroduces the race.
    """
    frappe.db.sql("SELECT name FROM `tabCustomer` WHERE name = %s FOR UPDATE", (customer,))


def find_open_invoice(
    *,
    customer: str,
    company: str,
    branch: str | None = None,
    requires_stock: bool = False,
) -> str | None:
    """The newest reusable Draft for this match key, or None."""
    filters = {
        "docstatus": 0,
        "customer": customer,
        "company": company,
        "is_pos": 0,
        "is_return": 0,
        "update_stock": 1 if requires_stock else 0,
    }
    # A blank branch is its own bucket, not a wildcard - otherwise an unbranched draft
    # would silently absorb branched charges.
    filters["branch"] = branch or ""

    rows = frappe.get_all(
        "Sales Invoice",
        filters=filters,
        fields=["name"],
        order_by="creation desc",
        limit_page_length=1,
        ignore_permissions=True,
    )
    return rows[0].name if rows else None


SOURCE_ROW_KEYS = ("source_doctype", "source_name")


def _mark_items(items: list[dict], source_doctype: str, source_name: str) -> list[dict]:
    """Marker per line, with a per-row override.

    A row may carry its own source_doctype/source_name, in which case the line is
    attributed to that record rather than to the call-level source. This is what lets
    one call bill several distinct records - three coordinator services for one guardian
    become three lines with three markers, each individually reversible, instead of three
    identical [alkokh-source-group:Guardian:<id>] lines that nothing can tell apart.

    The override keys are stripped before the row reaches the Sales Invoice Item, which
    has no such fields.
    """
    marked = []
    for row in items:
        row = dict(row)
        row_doctype = cstr(row.pop("source_doctype", "")).strip() or source_doctype
        row_name = cstr(row.pop("source_name", "")).strip() or source_name
        row["description"] = append_marker(row.get("description"), row_doctype, row_name)
        marked.append(row)
    return marked


def _append_remark(invoice, remark: str | None):
    """Document-level trail, one line per contribution.

    The per-line marker is the durable record; this is what makes a merged invoice
    readable at a glance, and it is what let the duplicate be diagnosed in the first
    place - the invoices that had no remark were the ones the backend created.
    """
    if not remark:
        return
    existing = cstr(invoice.remarks).rstrip()
    if remark in existing:
        return
    invoice.remarks = f"{existing}\n{remark}" if existing else remark


def _set_guardian(invoice, guardian: str | None) -> str | None:
    if not guardian:
        return None
    for fieldname in GUARDIAN_FIELD_CANDIDATES:
        if invoice.meta.has_field(fieldname):
            invoice.set(fieldname, guardian)
            return fieldname
    return None


def get_or_create_open_invoice(
    *,
    customer: str,
    items: list[dict],
    source_doctype: str,
    source_name: str,
    company: str | None = None,
    branch: str | None = None,
    posting_date=None,
    due_date=None,
    selling_price_list: str | None = None,
    ignore_pricing_rule: int = 0,
    remarks: str | None = None,
    guardian: str | None = None,
    requires_stock: bool | None = None,
    is_pos: int = 0,
    pos_profile: str | None = None,
    ignore_permissions: bool = False,
    branch_authorised: bool = False,
) -> frappe._dict:
    """Append to the customer's open Draft, or create one.

    Returns {invoice, created, guardian_reference_field, appended_row_count}.

    `branch_authorised` is the elevation for service-owned branches: the work happened
    where the service lives, so the invoice belongs to that branch even when the person
    processing it belongs to another. Callers set it only after authorising the operation
    themselves, and only when `branch` was derived from a record or a site setting rather
    than accepted from the request - see `utils.branch.BRANCH_AUTHORISED_FLAG`.

    It is threaded through the CREATE path specifically. The append path below has always
    run `save(ignore_permissions=True)`, and `before_insert` does not re-run on a save, so
    appending to an existing cross-branch draft already worked. Only the first invoice for
    that branch threw - which made the failure look intermittent and order-dependent
    rather than absolute, and is why a fix confined to `insert()` is not one.

    Deliberately does NOT write back-links. A Vet Visit with sales_invoice set is locked
    by _validate_sales_invoice_lock and the frontend treats it as billed, so linking a
    still-open record to a shared draft would make it uncompletable. Each caller links at
    its own terminal billing moment, which the existing code already does correctly.
    """
    customer = cstr(customer).strip()
    if not customer:
        frappe.throw(_("Customer is required to raise a Sales Invoice."))
    if not items:
        frappe.throw(_("At least one line is required to raise a Sales Invoice."))

    company = resolve_company(company)
    # Resolved BEFORE the lookup so the filter and the created row cannot disagree.
    branch = resolve_branch(branch)
    posting_date = getdate(posting_date) if posting_date else getdate(nowdate())
    due_date = getdate(due_date) if due_date else posting_date

    if requires_stock is None:
        requires_stock = any(row.get("warehouse") for row in items)
    requires_stock = bool(requires_stock)

    marked_items = _mark_items(items, source_doctype, source_name)

    # POS never participates: not looked up, not found, always new.
    if cint(is_pos):
        return _create_invoice(
            customer=customer, company=company, branch=branch, items=marked_items,
            posting_date=posting_date, due_date=due_date,
            selling_price_list=selling_price_list, ignore_pricing_rule=ignore_pricing_rule,
            remarks=remarks, guardian=guardian, requires_stock=requires_stock,
            is_pos=1, pos_profile=pos_profile, ignore_permissions=ignore_permissions,
            branch_authorised=branch_authorised,
        )

    # Taken BEFORE the lookup and held for the transaction.
    _lock_customer(customer)

    existing = find_open_invoice(
        customer=customer, company=company, branch=branch, requires_stock=requires_stock
    )
    if not existing:
        return _create_invoice(
            customer=customer, company=company, branch=branch, items=marked_items,
            posting_date=posting_date, due_date=due_date,
            selling_price_list=selling_price_list, ignore_pricing_rule=ignore_pricing_rule,
            remarks=remarks, guardian=guardian, requires_stock=requires_stock,
            is_pos=0, pos_profile=None, ignore_permissions=ignore_permissions,
            branch_authorised=branch_authorised,
        )

    invoice = frappe.get_doc("Sales Invoice", existing)
    for row in marked_items:
        invoice.append("items", row)
    _append_remark(invoice, remarks)
    guardian_field = _set_guardian(invoice, guardian) if guardian else None

    invoice.flags.from_custom_flow = True
    invoice.flags.ignore_permissions = True
    invoice.save(ignore_permissions=True)
    invoice.add_comment(
        "Comment",
        _("{0} lines from {1} {2} appended by {3}.").format(
            len(marked_items), _(source_doctype), source_name, frappe.session.user
        ),
    )
    return frappe._dict(
        invoice=invoice,
        created=False,
        guardian_reference_field=guardian_field,
        appended_row_count=len(marked_items),
    )


def _create_invoice(
    *, customer, company, branch, items, posting_date, due_date, selling_price_list,
    ignore_pricing_rule, remarks, guardian, requires_stock, is_pos, pos_profile,
    ignore_permissions, branch_authorised=False,
) -> frappe._dict:
    payload = {
        "doctype": "Sales Invoice",
        "customer": customer,
        "company": company,
        "posting_date": posting_date,
        "due_date": due_date,
        "update_stock": 1 if requires_stock else 0,
        "is_pos": cint(is_pos),
        "items": items,
    }
    if selling_price_list:
        payload["selling_price_list"] = selling_price_list
    if ignore_pricing_rule:
        payload["ignore_pricing_rule"] = 1
    if remarks:
        payload["remarks"] = remarks

    invoice = frappe.get_doc(payload)
    # Written after construction so it lands even when the field is a custom one.
    if branch and invoice.meta.has_field("branch"):
        invoice.branch = branch
    if pos_profile and invoice.meta.has_field("pos_profile"):
        invoice.pos_profile = pos_profile
    guardian_field = _set_guardian(invoice, guardian)

    invoice.flags.from_custom_flow = True

    if branch_authorised:
        # Both halves are required and neither substitutes for the other.
        #
        # The flag below is read by utils.branch.stamp_branch_on_insert, a before_insert
        # doc_event that calls assert_can_write_to_branch unconditionally. doc_events run
        # beside Document.check_permission, not inside it, so ignore_permissions never
        # reached that throw - which is the whole reason the earlier attempt at this was
        # reverted.
        #
        # ignore_permissions covers the other half: Sales Invoice.branch ships with
        # ignore_user_permissions = 0, so Frappe's own has_user_permission would reject
        # "hotel" on a document inserted by a user permitted only "main", independently of
        # any hook.
        invoice.flags[BRANCH_AUTHORISED_FLAG] = True
        invoice.flags.ignore_permissions = True
        invoice.insert(ignore_permissions=True)
    elif ignore_permissions:
        invoice.flags.ignore_permissions = True
        invoice.insert(ignore_permissions=True)
    else:
        invoice.insert()

    return frappe._dict(
        invoice=invoice,
        created=True,
        guardian_reference_field=guardian_field,
        appended_row_count=len(items),
    )
