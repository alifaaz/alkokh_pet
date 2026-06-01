from __future__ import annotations

from contextlib import contextmanager

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr


DEFAULT_COUNTRY = "Iraq"
DEFAULT_ADDRESS_TYPE = "Billing"
LOCK_TIMEOUT_SECONDS = 15


def log_customer_event(
    event_type: str,
    *,
    guardian_name: str | None = None,
    customer_id: str | None = None,
    phone: str | None = None,
    details: str | None = None,
    level: str = "info",
):
    payload = {
        "guardian": guardian_name,
        "customer": customer_id,
        "phone": phone,
        "details": details or "",
    }
    logger = frappe.logger()
    log_method = getattr(logger, level, None) or logger.info
    log_method(f"[{event_type}] {frappe.as_json(payload)}")


def get_guardian_record(guardian) -> dict:
    if not guardian:
        frappe.throw(_("Guardian is required."))

    if isinstance(guardian, Document):
        if guardian.doctype != "Guardian":
            frappe.throw(_("Invalid guardian document."))
        if guardian.name and not guardian.is_new():
            record = frappe.db.get_value("Guardian", guardian.name, "*", as_dict=True)
            if record:
                return record
        return guardian.as_dict()

    if isinstance(guardian, dict):
        if guardian.get("name"):
            record = frappe.db.get_value("Guardian", guardian.get("name"), "*", as_dict=True)
            if record:
                return record
        if guardian.get("phone"):
            record = frappe.db.get_value("Guardian", {"phone": guardian.get("phone")}, "*", as_dict=True)
            if record:
                return record
        return guardian

    if isinstance(guardian, str):
        if frappe.db.exists("Guardian", guardian):
            record = frappe.db.get_value("Guardian", guardian, "*", as_dict=True)
            if record:
                return record

        record = frappe.db.get_value("Guardian", {"phone": guardian}, "*", as_dict=True)
        if record:
            return record

    frappe.throw(_("Guardian was not found."))


def get_guardian_by_user(user_id: str):
    if not user_id:
        return None
    return frappe.db.get_value("Guardian", {"user_id": user_id}, "*", as_dict=True)


def get_guardian_by_customer(customer_id: str):
    if not customer_id:
        return None
    return frappe.db.get_value("Guardian", {"customer_id": customer_id}, "*", as_dict=True)


def _lock_name_for_phone(phone: str) -> str:
    phone = cstr(phone).strip()
    return f"guardian_customer::{phone}"


@contextmanager
def guardian_customer_lock(phone: str):
    with guardian_customer_locks([phone]):
        yield


@contextmanager
def guardian_customer_locks(phones):
    lock_names = sorted(
        {
            _lock_name_for_phone(phone)
            for phone in phones
            if cstr(phone).strip()
        }
    )
    acquired_locks = []
    try:
        for lock_name in lock_names:
            result = frappe.db.sql("SELECT GET_LOCK(%s, %s)", (lock_name, LOCK_TIMEOUT_SECONDS))
            acquired = int(result[0][0]) if result and result[0] else 0

            if acquired != 1:
                frappe.throw(_("Could not acquire Guardian/Customer identity lock. Please retry."))

            acquired_locks.append(lock_name)
        yield
    finally:
        for lock_name in reversed(acquired_locks):
            try:
                frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_name,))
            except Exception:
                frappe.logger().warning(f"[CUSTOMER_LOCK_RELEASE_FAILED] {lock_name}")


def _customer_name_for_guardian(guardian: dict, fallback_phone: str) -> str:
    return (
        cstr(guardian.get("full_name")).strip()
        or cstr(guardian.get("customer_name")).strip()
        or fallback_phone
    )


def _ensure_customer_not_linked_to_other_guardian(guardian_name: str, customer_id: str):
    other_guardian = frappe.db.get_value(
        "Guardian",
        {
            "customer_id": customer_id,
            "name": ["!=", guardian_name],
        },
        ["name", "phone"],
        as_dict=True,
    )
    if other_guardian:
        log_customer_event(
            "CUSTOMER_CONFLICT",
            guardian_name=guardian_name,
            customer_id=customer_id,
            phone=other_guardian.get("phone"),
            details=f"linked_to_other_guardian={other_guardian.get('name')}",
            level="error",
        )
        frappe.throw(_("Customer {0} is already linked to Guardian {1}.").format(customer_id, other_guardian.get("name")))


def _ensure_phone_not_used_by_other_customer(phone: str, customer_id: str | None):
    if not phone:
        return

    filters = {"mobile_no": phone}
    if customer_id:
        filters["name"] = ["!=", customer_id]

    conflict = frappe.get_all("Customer", filters=filters, fields=["name"], limit=1)
    if conflict:
        frappe.throw(
            _("Phone {0} is already used by Customer {1}.").format(phone, conflict[0]["name"])
        )


def validate_guardian_customer_mapping(guardian):
    guardian_row = get_guardian_record(guardian)
    guardian_name = guardian_row.get("name")
    customer_id = guardian_row.get("customer_id")
    phone = cstr(guardian_row.get("phone")).strip()

    if not guardian_name or not customer_id:
        return

    if frappe.db.exists("Customer", customer_id):
        _ensure_customer_not_linked_to_other_guardian(guardian_name, customer_id)
        _ensure_phone_not_used_by_other_customer(phone, customer_id)


def upsert_primary_address_from_guardian(guardian: dict, customer_id: str) -> str | None:
    address_line1 = cstr(guardian.get("address_line1")).strip()
    city = cstr(guardian.get("city")).strip()
    country = cstr(guardian.get("country")).strip() or DEFAULT_COUNTRY
    full_name = _customer_name_for_guardian(guardian, guardian.get("phone"))

    existing = frappe.db.sql(
        """
        SELECT a.name
        FROM `tabAddress` a
        INNER JOIN `tabDynamic Link` dl
          ON dl.parent = a.name
         AND dl.parenttype = 'Address'
         AND dl.link_doctype = 'Customer'
         AND dl.link_name = %s
        WHERE a.is_primary_address = 1
        LIMIT 1
        """,
        (customer_id,),
        as_dict=False,
    )

    if existing:
        addr_name = existing[0][0]
        updates = {
            "address_title": full_name,
            "address_type": DEFAULT_ADDRESS_TYPE,
            "country": country,
            "is_primary_address": 1,
            "address_line1": address_line1,
            "city": city,
        }

        frappe.db.set_value("Address", addr_name, updates, update_modified=False)
        return addr_name

    if not (address_line1 and city):
        return None

    address = frappe.get_doc(
        {
            "doctype": "Address",
            "address_title": full_name,
            "address_type": DEFAULT_ADDRESS_TYPE,
            "address_line1": address_line1,
            "city": city,
            "country": country,
            "is_primary_address": 1,
            "links": [{"link_doctype": "Customer", "link_name": customer_id}],
        }
    )
    address.insert(ignore_permissions=True)
    return address.name


def sync_customer_from_guardian(guardian, customer_id: str | None = None) -> str | None:
    guardian_row = get_guardian_record(guardian)
    guardian_name = guardian_row.get("name")
    phone = cstr(guardian_row.get("phone")).strip()
    customer_id = customer_id or guardian_row.get("customer_id")

    if not guardian_name or not customer_id or not frappe.db.exists("Customer", customer_id):
        return customer_id

    _ensure_customer_not_linked_to_other_guardian(guardian_name, customer_id)
    _ensure_phone_not_used_by_other_customer(phone, customer_id)

    updates = {}
    if phone:
        updates["mobile_no"] = phone

    updates["customer_name"] = _customer_name_for_guardian(guardian_row, phone)

    email_id = cstr(guardian_row.get("email_id")).strip()
    updates["email_id"] = email_id or None

    if updates:
        current = frappe.db.get_value("Customer", customer_id, list(updates.keys()), as_dict=True) or {}
        changed = {
            fieldname: value
            for fieldname, value in updates.items()
            if cstr(current.get(fieldname)) != cstr(value)
        }
        if changed:
            frappe.db.set_value("Customer", customer_id, changed, update_modified=False)

    address_name = upsert_primary_address_from_guardian(guardian_row, customer_id)

    log_customer_event(
        "CUSTOMER_SYNCED",
        guardian_name=guardian_name,
        customer_id=customer_id,
        phone=phone,
        details=f"address={address_name or ''}",
    )
    return customer_id


def _should_auto_create_customer_for_web_admin_guardian(guardian) -> bool:
    if getattr(guardian.flags, "skip_guardian_customer_auto_create", False):
        return False

    if (
        getattr(frappe.flags, "in_patch", False)
        or getattr(frappe.flags, "in_migrate", False)
        or getattr(frappe.flags, "in_install", False)
    ):
        return False

    if frappe.session.user == "Guest":
        return False

    if guardian.get("customer_id"):
        return False

    if cstr(guardian.get("pending_password_hash")).strip() and not cint(guardian.get("otp_verified")):
        return False

    return bool(cstr(guardian.get("phone")).strip())


def ensure_customer_for_web_admin_guardian(guardian) -> str | None:
    """Create/link a Customer when a Guardian is created directly from Desk/admin flows."""
    if not _should_auto_create_customer_for_web_admin_guardian(guardian):
        return guardian.get("customer_id")

    customer_id = get_or_create_customer_from_guardian(guardian)
    if customer_id:
        guardian.customer_id = customer_id
        log_customer_event(
            "WEB_ADMIN_GUARDIAN_CUSTOMER_LINKED",
            guardian_name=guardian.name,
            customer_id=customer_id,
            phone=guardian.get("phone"),
        )
    return customer_id


def _create_customer_for_guardian(guardian: dict) -> str:
    phone = cstr(guardian.get("phone")).strip()
    customer = frappe.get_doc(
        {
            "doctype": "Customer",
            "customer_name": _customer_name_for_guardian(guardian, phone),
            "mobile_no": phone,
            "customer_type": "Individual",
            "email_id": cstr(guardian.get("email_id")).strip() or None,
        }
    )
    customer.flags.from_guardian_resolver = True
    customer.flags.ignore_validate = True
    customer.insert(ignore_permissions=True, ignore_mandatory=True)
    return customer.name


def validate_customer_identity_projection(doc, method=None):
    if doc.doctype != "Customer":
        return

    if _allow_system_customer_insert(doc):
        return

    guardian = get_guardian_by_customer(doc.name) if doc.name else None
    if guardian:
        phone = cstr(guardian.get("phone")).strip()
        doc.customer_type = "Individual"
        doc.customer_name = _customer_name_for_guardian(guardian, phone)
        doc.mobile_no = phone or None
        doc.email_id = cstr(guardian.get("email_id")).strip() or None
        return

    if not doc.is_new():
        return

    if cstr(doc.customer_type).strip() == "Individual":
        frappe.throw(_("Create a Guardian first. Individual Customers must be created through the Guardian identity flow."))


def _allow_system_customer_insert(doc) -> bool:
    if getattr(doc.flags, "from_guardian_resolver", False):
        return True
    if getattr(doc.flags, "ignore_guardian_identity_validation", False):
        return True
    return bool(
        getattr(frappe.flags, "in_test", False)
        or getattr(frappe.flags, "in_patch", False)
        or getattr(frappe.flags, "in_migrate", False)
        or getattr(frappe.flags, "in_install", False)
    )


def get_or_create_customer_from_guardian(guardian) -> str:
    guardian_row = get_guardian_record(guardian)
    guardian_name = guardian_row.get("name")
    phone = cstr(guardian_row.get("phone")).strip()

    if not guardian_name:
        frappe.throw(_("Guardian is required."))
    if not phone:
        frappe.throw(_("Guardian phone is required."))

    with guardian_customer_lock(phone):
        guardian_row = frappe.db.get_value("Guardian", guardian_name, "*", as_dict=True)
        if not guardian_row:
            frappe.throw(_("Guardian {0} was not found.").format(guardian_name))

        linked_customer_id = guardian_row.get("customer_id")
        if linked_customer_id and frappe.db.exists("Customer", linked_customer_id):
            _ensure_customer_not_linked_to_other_guardian(guardian_name, linked_customer_id)
            sync_customer_from_guardian(guardian_row, linked_customer_id)
            log_customer_event(
                "CUSTOMER_REUSED",
                guardian_name=guardian_name,
                customer_id=linked_customer_id,
                phone=phone,
                details="linked_customer",
            )
            return linked_customer_id

        matches = frappe.get_all(
            "Customer",
            filters={"mobile_no": phone},
            fields=["name", "creation"],
            order_by="creation asc",
        )

        if len(matches) > 1:
            customer_ids = ", ".join(row["name"] for row in matches)
            log_customer_event(
                "CUSTOMER_CONFLICT",
                guardian_name=guardian_name,
                phone=phone,
                details=f"multiple_customers=[{customer_ids}]",
                level="error",
            )
            frappe.throw(_("Multiple Customers exist for this phone. Manual resolution is required."))

        if len(matches) == 1:
            customer_id = matches[0]["name"]
            _ensure_customer_not_linked_to_other_guardian(guardian_name, customer_id)
            frappe.db.set_value("Guardian", guardian_name, "customer_id", customer_id, update_modified=False)
            guardian_row["customer_id"] = customer_id
            sync_customer_from_guardian(guardian_row, customer_id)
            log_customer_event(
                "CUSTOMER_RESOLVED_FROM_PHONE",
                guardian_name=guardian_name,
                customer_id=customer_id,
                phone=phone,
            )
            return customer_id

        customer_id = _create_customer_for_guardian(guardian_row)
        frappe.db.set_value("Guardian", guardian_name, "customer_id", customer_id, update_modified=False)
        guardian_row["customer_id"] = customer_id
        sync_customer_from_guardian(guardian_row, customer_id)
        log_customer_event(
            "CUSTOMER_CREATED",
            guardian_name=guardian_name,
            customer_id=customer_id,
            phone=phone,
        )
        return customer_id


def change_guardian_phone_number(guardian, new_phone: str) -> dict:
    guardian_row = get_guardian_record(guardian)
    guardian_name = guardian_row.get("name")
    old_phone = cstr(guardian_row.get("phone")).strip()
    new_phone = cstr(new_phone).strip()

    if not guardian_name:
        frappe.throw(_("Guardian is required."))
    if not old_phone:
        frappe.throw(_("Guardian current phone is required."))
    if not new_phone:
        frappe.throw(_("New phone is required."))
    if old_phone == new_phone:
        frappe.throw(_("New phone is the same as the current phone."))

    try:
        with guardian_customer_locks([old_phone, new_phone]):
            guardian_row = frappe.db.get_value("Guardian", guardian_name, "*", as_dict=True)
            if not guardian_row:
                frappe.throw(_("Guardian {0} was not found.").format(guardian_name))

            old_phone = cstr(guardian_row.get("phone")).strip()
            if old_phone == new_phone:
                frappe.throw(_("New phone is the same as the current phone."))

            existing_guardian = frappe.db.get_value(
                "Guardian",
                {"phone": new_phone, "name": ["!=", guardian_name]},
                "name",
            )
            if existing_guardian:
                log_customer_event(
                    "PHONE_CHANGE_CONFLICT",
                    guardian_name=guardian_name,
                    phone=old_phone,
                    details=f"new_phone={new_phone}; existing_guardian={existing_guardian}",
                    level="error",
                )
                frappe.throw(_("Phone already in use by another account"))

            customer_id = guardian_row.get("customer_id")
            customer_filters = {"mobile_no": new_phone}
            if customer_id:
                customer_filters["name"] = ["!=", customer_id]
            conflicting_customer = frappe.get_all(
                "Customer",
                filters=customer_filters,
                fields=["name"],
                limit=1,
            )
            if conflicting_customer:
                log_customer_event(
                    "PHONE_CHANGE_CONFLICT",
                    guardian_name=guardian_name,
                    customer_id=customer_id,
                    phone=old_phone,
                    details=f"new_phone={new_phone}; existing_customer={conflicting_customer[0]['name']}",
                    level="error",
                )
                frappe.throw(_("Phone already in use by another Customer"))

            if customer_id and frappe.db.exists("Customer", customer_id):
                _ensure_customer_not_linked_to_other_guardian(guardian_name, customer_id)

            frappe.db.set_value("Guardian", guardian_name, "phone", new_phone, update_modified=False)
            guardian_row["phone"] = new_phone

            if customer_id and frappe.db.exists("Customer", customer_id):
                sync_customer_from_guardian(guardian_row, customer_id)

            validate_guardian_customer_mapping(guardian_row)
            log_customer_event(
                "PHONE_CHANGE_SUCCESS",
                guardian_name=guardian_name,
                customer_id=customer_id,
                phone=new_phone,
                details=f"old_phone={old_phone}; new_phone={new_phone}",
            )

            return {
                "guardian_id": guardian_name,
                "customer_id": customer_id,
                "old_phone": old_phone,
                "new_phone": new_phone,
            }
    except Exception:
        log_customer_event(
            "PHONE_CHANGE_FAILED",
            guardian_name=guardian_name,
            phone=old_phone,
            details=f"new_phone={new_phone}",
            level="error",
        )
        raise
