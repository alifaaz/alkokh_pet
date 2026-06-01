from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.utils import cstr

from pet_app.api.permissions import require_doctype_permission, require_restriction_value
from pet_app.utils.guardian_customer import (
    get_guardian_by_customer,
    get_or_create_customer_from_guardian,
)


def _phone_candidates(phone: str) -> list[str]:
    raw_phone = cstr(phone).strip()
    compact_phone = re.sub(r"[\s\-\(\)]+", "", raw_phone)
    candidates = {value for value in (raw_phone, compact_phone) if value}

    for value in tuple(candidates):
        normalized = value.lstrip("+")
        if normalized.startswith("9647") and len(normalized) == 13:
            candidates.add(f"0{normalized[3:]}")
        if normalized.startswith("07") and len(normalized) == 11:
            candidates.add(f"964{normalized[1:]}")
            candidates.add(f"+964{normalized[1:]}")

    return sorted(candidates)


def _find_guardian_by_phone(phone: str):
    candidates = _phone_candidates(phone)
    if not candidates:
        return None

    guardians = frappe.get_all(
        "Guardian",
        filters={"phone": ["in", candidates]},
        fields=["name", "phone", "customer_id"],
        limit=2,
        order_by="creation asc",
    )
    if len(guardians) > 1:
        frappe.throw(_("Multiple Guardians match phone {0}. Manual resolution is required.").format(phone))
    return guardians[0] if guardians else None


def _find_customer_by_phone(phone: str):
    candidates = _phone_candidates(phone)
    if not candidates:
        return None

    customers = frappe.get_all(
        "Customer",
        filters={"mobile_no": ["in", candidates]},
        fields=["name"],
        limit=2,
        order_by="creation asc",
    )
    if len(customers) > 1:
        frappe.throw(_("Multiple Customers match phone {0}. Manual resolution is required.").format(phone))
    return customers[0]["name"] if customers else None


def _apply_customer_link(doc, customer_id: str | None):
    if not customer_id:
        return

    if doc.get("custom_customer") and doc.custom_customer != customer_id:
        frappe.throw(
            _("Appointment customer {0} does not match the resolved customer {1}.").format(
                frappe.bold(doc.custom_customer), frappe.bold(customer_id)
            )
        )

    doc.custom_customer = customer_id
    doc.appointment_with = "Customer"
    doc.party = customer_id

    guardian = get_guardian_by_customer(customer_id)
    if guardian:
        if doc.get("custom_guardian") and doc.custom_guardian != guardian.get("name"):
            frappe.throw(
                _("Appointment guardian {0} does not match customer {1}.").format(
                    frappe.bold(doc.custom_guardian), frappe.bold(customer_id)
                )
            )
        doc.custom_guardian = guardian.get("name")


def link_appointment_identity(doc, method=None):
    if doc.doctype != "Appointment":
        return

    if method == "before_insert" or doc.is_new():
        require_doctype_permission("Appointment", "create")
        require_restriction_value("practitioner", _get_appointment_doctor(doc))

    customer_id = None

    if doc.get("custom_guardian"):
        customer_id = get_or_create_customer_from_guardian(doc.custom_guardian)
        doc.custom_guardian = cstr(doc.custom_guardian).strip()
        _apply_customer_link(doc, customer_id)
        return

    if doc.get("custom_customer"):
        _apply_customer_link(doc, cstr(doc.custom_customer).strip())
        return

    phone = cstr(doc.get("customer_phone_number")).strip()
    if not phone:
        return

    guardian = _find_guardian_by_phone(phone)
    if guardian:
        doc.custom_guardian = guardian["name"]
        customer_id = get_or_create_customer_from_guardian(guardian["name"])
        _apply_customer_link(doc, customer_id)
        return

    customer_id = _find_customer_by_phone(phone)
    if customer_id:
        _apply_customer_link(doc, customer_id)


def _get_appointment_doctor(doc) -> str | None:
    for fieldname in ("practitioner", "custom_doctor", "doctor"):
        if doc.meta.has_field(fieldname) and doc.get(fieldname):
            return doc.get(fieldname)
    return None
