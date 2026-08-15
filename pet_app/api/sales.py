from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, getdate, nowdate

from pet_app.api.link_aliases import with_link_aliases
from pet_app.api.permissions import require_doctype_permission, require_restriction_value
from pet_app.utils.guardian_customer import get_guardian_record, get_or_create_customer_from_guardian
from pet_app.api.response import standardize_response
from pet_app.utils.invoice_reuse import get_or_create_open_invoice

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
            line["source_doctype"] = source_doctype
            line["source_name"] = source_name
        invoice_items.append(line)

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
