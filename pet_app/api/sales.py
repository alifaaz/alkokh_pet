from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, getdate, nowdate

from pet_app.api.link_aliases import with_link_aliases
from pet_app.api.permissions import require_doctype_permission, require_restriction_value
from pet_app.utils.guardian_customer import get_guardian_record, get_or_create_customer_from_guardian
from pet_app.api.response import standardize_response
from pet_app.utils.api_response import raise_api_error
from pet_app.utils.invoice_source import find_billed_invoice
from pet_app.utils.invoice_reuse import (
    get_or_create_open_invoice,
    resolve_branch,
    resolve_company,
)

INVOICE_CREATION_ROLES = {
    "System Manager",
    "Accounts Manager",
    "Accounts User",
    "Accounting",
    "Healthcare Practitioner",
    "Doctor",
    "Healthcare",
    "POS",
}


def _validate_invoice_permission():
    require_doctype_permission("Sales Invoice", "create")


def _get_item_rate(item_code: str):
    rate = frappe.db.get_value(
        "Item Price",
        {"item_code": item_code, "price_list": "Standard Selling", "selling": 1},
        "price_list_rate",
    )
    if rate is None:
        rate = frappe.db.get_value("Item", item_code, "standard_rate")
    return flt(rate or 0)


def _set_optional_guardian_reference(invoice, guardian_name: str):
    for fieldname in ("guardian_id", "guardian", "custom_guardian_id", "custom_guardian"):
        if invoice.meta.has_field(fieldname):
            invoice.set(fieldname, guardian_name)
            return fieldname
    return None


def _log_invoice_event(event: str, **context):
    frappe.logger("pet_app.sales").info({"event": event, **context})


def _coerce_items(items):
    if isinstance(items, str):
        items = json.loads(items)
    if not isinstance(items, list):
        frappe.throw(_("Items payload must be a list."))
    return items


def _validate_pos_profile(pos_profile: str | None):
    if pos_profile and not frappe.db.exists("POS Profile", pos_profile):
        frappe.throw(_("POS Profile {0} does not exist.").format(frappe.bold(pos_profile)))


@frappe.whitelist()
@standardize_response
def create_sales_invoice_for_guardian(
    guardian,
    items,
    posting_date=None,
    due_date=None,
    is_pos=1,
    pos_profile=None,
    customer=None,
):
    _validate_invoice_permission()

    guardian_row = get_guardian_record(guardian)
    if not frappe.has_permission("Guardian", doc=guardian_row.get("name"), ptype="read"):
        raise frappe.PermissionError(_("Not permitted to access Guardian {0}.").format(frappe.bold(guardian_row.get("name"))))
    customer_id = get_or_create_customer_from_guardian(guardian_row)
    if customer and customer != customer_id:
        frappe.throw(_("Customer does not match the resolved Guardian identity."))

    items = _coerce_items(items)
    _validate_pos_profile(pos_profile)
    if pos_profile:
        require_doctype_permission("POS Profile", "read")
    require_restriction_value("cashier_profile", pos_profile)

    if not items:
        frappe.throw(_("At least one item is required."))

    invoice_items = []
    skipped = []
    for row in items:
        if not isinstance(row, dict):
            frappe.throw(_("Each item row must be an object."))
        item_code = row.get("item_code")
        qty = flt(row.get("qty") or 0)
        if not item_code or not frappe.db.exists("Item", item_code):
            frappe.throw(_("Item {0} does not exist.").format(item_code))
        if qty <= 0:
            frappe.throw(_("Invalid qty for item {0}.").format(item_code))

        rate = flt(row.get("rate")) if row.get("rate") is not None else _get_item_rate(item_code)
        if rate <= 0:
            frappe.throw(_("Price is not configured for item {0}.").format(item_code))

        line = {
            "item_code": item_code,
            "qty": qty,
            "rate": rate,
            "description": row.get("description"),
        }
        # Optional per-line provenance. Without it every line billed here carries the
        # same [alkokh-source-group:Guardian:<id>] marker, so three services for one
        # guardian are indistinguishable and none can be reversed on its own. The caller
        # (the coordinator page) supplies the record that produced the charge; validated
        # so a marker can never point at something that does not exist.
        source_doctype = cstr(row.get("source_doctype")).strip()
        source_name = cstr(row.get("source_name")).strip()
        if source_doctype or source_name:
            if not (source_doctype and source_name):
                frappe.throw(_("Both source_doctype and source_name are required to attribute a line."))
            if not frappe.db.exists("DocType", source_doctype):
                frappe.throw(_("Source DocType {0} does not exist.").format(frappe.bold(source_doctype)))
            if not frappe.db.exists(source_doctype, source_name):
                frappe.throw(
                    _("Source {0} {1} does not exist.").format(_(source_doctype), frappe.bold(source_name))
                )
            # Idempotency. The same source must never be charged twice, and this endpoint
            # is now reachable twice for one service: the Service Provider screen calls it
            # explicitly after close_service, and close_service itself now bills a
            # Vaccination or Deworming service. That screen filters by due date only - not
            # by category or provider - so it CAN list a vaccination, and without this the
            # two calls would put two identical lines on one invoice. Keyed on the
            # invoice-line marker rather than the order's `billed` flag, because the marker
            # is written by every billing path while the flag is not.
            already_on = find_billed_invoice(source_doctype, source_name)
            if already_on:
                skipped.append((source_doctype, source_name, already_on))
                continue
            line["source_doctype"] = source_doctype
            line["source_name"] = source_name
        invoice_items.append(line)

    if not invoice_items:
        # Every attributed row was already billed. Returning the invoice they sit on is the
        # honest answer - the caller asked for these charges to exist, and they do - and it
        # is what makes a retry safe instead of duplicating.
        existing = skipped[0][2]
        _log_invoice_event(
            "GUARDIAN_INVOICE_ALREADY_BILLED",
            sales_invoice=existing,
            guardian=guardian_row.get("name"),
            skipped=[f"{doctype} {name}" for doctype, name, _inv in skipped],
        )
        return with_link_aliases(
            {
                "sales_invoice": existing,
                "guardian": guardian_row.get("name"),
                "guardian_reference_field": None,
                "customer": customer_id,
                "already_billed": True,
            },
            guardian_field="guardian",
            include_pet=False,
            include_doctor=False,
            include_provider=False,
        )

    posting_date = getdate(posting_date) if posting_date else getdate(nowdate())
    due_date = getdate(due_date) if due_date else posting_date

    # is_pos is unchanged and still defaults to 1. A POS invoice always creates: it
    # demands immediate payment, so it must neither absorb an open draft nor be found by
    # a later lookup. Only is_pos = 0 participates in reuse.
    result = get_or_create_open_invoice(
        customer=customer_id,
        items=invoice_items,
        source_doctype="Guardian",
        source_name=guardian_row.get("name"),
        posting_date=posting_date,
        due_date=due_date,
        guardian=guardian_row.get("name"),
        is_pos=cint(is_pos),
        pos_profile=pos_profile,
        remarks=_("Guardian billing for {0}.").format(guardian_row.get("name")),
    )
    invoice = result.invoice
    guardian_field = result.guardian_reference_field
    invoice.add_comment(
        "Comment",
        _("Sales Invoice {0} via guarded guardian billing API by {1}.").format(
            _("created") if result.created else _("extended"), frappe.session.user
        ),
    )
    _log_invoice_event(
        "GUARDIAN_INVOICE_CREATED",
        sales_invoice=invoice.name,
        guardian=guardian_row.get("name"),
        customer=customer_id,
        created_by=frappe.session.user,
    )

    response = {
        "sales_invoice": invoice.name,
        "guardian": guardian_row.get("name"),
        "guardian_reference_field": guardian_field,
        "customer": customer_id,
    }
    return with_link_aliases(response, guardian_field="guardian", include_pet=False, include_doctor=False, include_provider=False)


# ---------------------------------------------------------------------------
# The accountant's manual door.
# ---------------------------------------------------------------------------


# Deliberately NARROWER than accounting.cashier._is_accounting_user, which also admits
# "POS Page" and "Cashiers Page". Those are page-access roles held by every till cashier,
# and a cashier's authority is to ring up what is in front of them, not to raise an
# arbitrary invoice against any customer. This is the back-office set only.
MANUAL_INVOICE_ROLES = {
    "System Manager",
    "Accounts Manager",
    "Accounts User",
    "Accounting",
}


def _require_manual_invoice_permission():
    """Raising an invoice by hand is a back-office act, so it needs a back-office role.

    `sales_invoice_guard.before_insert` closes the generic `frappe.client.insert` path
    because a PROGRAMMATIC creator that skips `get_or_create_open_invoice` produces
    invoice sprawl - ten invoices for one customer in sixty-eight minutes. A person
    deliberately clicking "create invoice" once is not that failure mode, and blocking it
    outright would leave the accounting team with no way to raise a correction, a
    wholesale bill, or any charge that did not originate at a visit or a till.

    So the door stays open, but as a named endpoint with a role behind it rather than as
    a hole in the guard - the same shape as the guardian and POS doors.
    """
    user = frappe.session.user
    if user == "Administrator":
        return
    if set(frappe.get_roles(user)) & MANUAL_INVOICE_ROLES:
        return
    raise_api_error(
        _("Only accounting users can raise a Sales Invoice by hand."),
        code="MANUAL_INVOICE_NOT_PERMITTED",
    )


@frappe.whitelist()
@standardize_response
def create_manual_sales_invoice(
    customer=None,
    items=None,
    company=None,
    branch=None,
    posting_date=None,
    due_date=None,
    selling_price_list=None,
    warehouse=None,
    update_stock=0,
    remarks=None,
    allow_duplicate=0,
):
    """Add manually entered charges to the branch's regular draft. Never submits.

    allow_duplicate is accepted for older clients but no longer creates a second
    regular draft. POS, returns and driver operations retain their own entry points.
    """

    _require_manual_invoice_permission()
    _validate_invoice_permission()

    customer = cstr(customer).strip()
    if not customer:
        raise_api_error(_("Customer is required."), code="CUSTOMER_REQUIRED")
    if not frappe.db.exists("Customer", customer):
        raise_api_error(
            _("Customer {0} was not found.").format(customer),
            code="CUSTOMER_NOT_FOUND",
            details={"customer": customer},
        )

    rows = _coerce_items(items or [])
    if not rows:
        raise_api_error(_("At least one item is required."), code="NO_ITEMS")

    company = resolve_company(company)
    branch = resolve_branch(branch)
    requires_stock = bool(cint(update_stock))
    default_warehouse = cstr(warehouse).strip()

    invoice_items = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise_api_error(_("Each item row must be an object."), code="ITEM_ROW_INVALID")
        item_code = cstr(row.get("item_code")).strip()
        if not item_code or not frappe.db.exists("Item", item_code):
            raise_api_error(
                _("Item {0} does not exist.").format(item_code or index + 1),
                code="ITEM_NOT_FOUND",
                details={"item_code": item_code},
            )
        qty = flt(row.get("qty"))
        if qty <= 0:
            raise_api_error(
                _("Invalid qty for item {0}.").format(item_code),
                code="ITEM_ROW_INVALID",
                details={"item_code": item_code},
            )
        rate = flt(row.get("rate")) if row.get("rate") is not None else _get_item_rate(item_code)
        if rate < 0:
            raise_api_error(
                _("Item {0} must not have a negative rate.").format(item_code),
                code="ITEM_ROW_INVALID",
                details={"item_code": item_code},
            )

        line = {
            "item_code": item_code,
            "qty": qty,
            "rate": rate,
            "description": cstr(row.get("description")).strip() or item_code,
        }
        row_warehouse = cstr(row.get("warehouse")).strip() or default_warehouse
        if row_warehouse:
            line["warehouse"] = row_warehouse
        invoice_items.append(line)

    posting_date = getdate(posting_date) if posting_date else getdate(nowdate())
    due_date = getdate(due_date) if due_date else posting_date

    result = get_or_create_open_invoice(
        customer=customer,
        items=invoice_items,
        source_doctype="User",
        source_name=frappe.session.user,
        company=company,
        branch=branch,
        posting_date=posting_date,
        due_date=due_date,
        selling_price_list=cstr(selling_price_list).strip() or None,
        remarks=cstr(remarks).strip() or None,
        requires_stock=requires_stock,
        is_pos=0,
    )
    invoice = result.invoice
    invoice.add_comment(
        "Comment",
        _("Sales Invoice raised by hand by {0}.").format(frappe.session.user),
    )
    _log_invoice_event(
        "MANUAL_INVOICE_CREATED",
        sales_invoice=invoice.name,
        customer=customer,
        created_by=frappe.session.user,
    )

    return {
        "sales_invoice": invoice.name,
        "customer": customer,
        "branch": invoice.get("branch"),
        "docstatus": invoice.docstatus,
        "created": result.created,
        "grand_total": flt(invoice.grand_total),
    }
