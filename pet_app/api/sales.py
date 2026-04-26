from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, nowdate

from pet_app.utils.guardian_customer import get_guardian_record, get_or_create_customer_from_guardian

INVOICE_CREATION_ROLES = {
    "System Manager",
    "Accounts Manager",
    "Accounts User",
    "Accounting",
    "Doctor",
    "Healthcare",
    "POS",
}


def _validate_invoice_permission():
    if frappe.session.user == "Administrator":
        return
    user_roles = set(frappe.get_roles(frappe.session.user) or [])
    if user_roles.isdisjoint(INVOICE_CREATION_ROLES):
        raise frappe.PermissionError(_("Not permitted to create Sales Invoice."))
    if not frappe.has_permission("Sales Invoice", ptype="create"):
        raise frappe.PermissionError(_("Not permitted to create Sales Invoice."))


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

        invoice_items.append(
            {
                "item_code": item_code,
                "qty": qty,
                "rate": rate,
                "description": row.get("description"),
            }
        )

    posting_date = getdate(posting_date) if posting_date else getdate(nowdate())
    due_date = getdate(due_date) if due_date else posting_date

    invoice = frappe.get_doc(
        {
            "doctype": "Sales Invoice",
            "customer": customer_id,
            "posting_date": posting_date,
            "due_date": due_date,
            "is_pos": cint(is_pos),
            "items": invoice_items,
        }
    )

    if pos_profile:
        invoice.pos_profile = pos_profile

    guardian_field = _set_optional_guardian_reference(invoice, guardian_row.get("name"))
    invoice.flags.from_custom_flow = True
    invoice.insert()
    invoice.add_comment(
        "Comment",
        _("Sales Invoice created via guarded guardian billing API by {0}.").format(frappe.session.user),
    )
    _log_invoice_event(
        "GUARDIAN_INVOICE_CREATED",
        sales_invoice=invoice.name,
        guardian=guardian_row.get("name"),
        customer=customer_id,
        created_by=frappe.session.user,
    )

    return {
        "sales_invoice": invoice.name,
        "guardian": guardian_row.get("name"),
        "guardian_reference_field": guardian_field,
        "customer": customer_id,
    }
