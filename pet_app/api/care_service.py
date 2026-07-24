import frappe
from frappe import _
from frappe.utils import cstr, flt, now_datetime
from frappe.model.document import Document
from pet_app.api.link_aliases import with_link_aliases
from pet_app.api.response import standardize_response
from pet_app.utils.visit_billing import (
    assert_boarding_billable_item_can_cancel,
    assert_visit_billable_item_can_cancel,
    cancel_boarding_billable_item_by_link,
    cancel_visit_billable_item_by_link,
)


class CareserviceTemplate(Document):

    def validate(self):
        if not self.service_name:
            frappe.throw(_("Service Name is required."))
        _ensure_care_service_item(self)

    def after_insert(self):
        _ensure_care_service_item_price(self)

    def on_update(self):
        _ensure_care_service_item_price(self)


SERVICE_CANCELLATION_TERMINAL_STATUSES = {"cancelled", "completed"}


# ─────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────

def _ensure_care_service_item(doc):
    if doc.item_code and frappe.db.exists("Item", doc.item_code):
        item = frappe.get_doc("Item", doc.item_code)
        if item.item_name != doc.service_name:
            item.item_name = doc.service_name
            item.flags.ignore_permissions = True
            item.save()
        return item

    item             = frappe.new_doc("Item")
    item.item_code   = doc.service_name
    item.item_name   = doc.service_name
    item.item_group  = "Services"
    item.stock_uom   = "Nos"
    item.is_stock_item = 0

    item.flags.ignore_permissions = True
    item.insert()

    doc.item_code = item.name
    return item


def _ensure_care_service_item_price(doc):
    rate       = flt(doc.default_price)
    item_code  = doc.item_code
    price_list = "Standard Selling"
    currency   = frappe.defaults.get_global_default("currency") or "IQD"

    if rate <= 0 or not item_code:
        return

    filters = {
        "item_code":  item_code,
        "price_list": price_list,
        "selling":    1,
    }
    existing_prices = frappe.get_all("Item Price", filters=filters, pluck="name")

    if len(existing_prices) > 1:
        frappe.log_error(
            title="CARE_SERVICE_ITEM_PRICE_DUPLICATE",
            message=f"Multiple Item Prices for {item_code} — skipped"
        )
        return

    existing = existing_prices[0] if existing_prices else None
    ip = frappe.get_doc("Item Price", existing) if existing else frappe.new_doc("Item Price")

    if not existing:
        ip.item_code  = item_code
        ip.price_list = price_list
        ip.selling    = 1

    ip.price_list_rate = rate
    ip.currency        = currency

    ip.flags.ignore_permissions = True
    ip.save() if existing else ip.insert()


@frappe.whitelist()
@standardize_response
def get_providers_for_category(category: str) -> list:
    """
    Returns Healthcare Practitioners for a category.
    Service Provider: filtered by service_categories
      (if empty = handles all)
    Medical categories: return Doctors + Nurses
    """
    cat_doc = frappe.db.get_value(
        "CategoryCareServices",
        category,
        "category_name",
    )
    if not cat_doc:
        return []

    cat_name = (cat_doc or "").lower()
    MEDICAL = ["lab", "radiology", "medication", "sonar", "checkup", "general"]
    is_medical = any(k in cat_name for k in MEDICAL)
    fields = ["name", "practitioner_name", "practitioner_type", "photo", "user_id"]

    if is_medical:
        return frappe.get_all(
            "Healthcare Practitioner",
            filters={
                "disabled": 0,
                "practitioner_type": ["in", ["Doctor", "Nurse"]],
            },
            fields=fields,
            order_by="practitioner_name asc",
        )

    providers = frappe.get_all(
        "Healthcare Practitioner",
        filters={
            "disabled": 0,
            "practitioner_type": "Service Provider",
        },
        fields=fields,
        order_by="practitioner_name asc",
    )

    result = []
    for p in providers:
        assigned = frappe.get_all(
            "Healthcare Practitioner Service Category",
            filters={
                "parent": p.name,
                "parenttype": "Healthcare Practitioner",
            },
            pluck="category",
        )
        if not assigned or category in assigned:
            result.append(p)

    return result


@frappe.whitelist()
@standardize_response
def bulk_create_pet_care_services(entries=None, services=None):
    import json

    from frappe.utils import today

    raw = entries or services
    if not raw:
        frappe.throw(_("No service entries provided"))

    items = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(items, list):
        frappe.throw(_("services must be a list."))

    created = []
    failed = []

    for item in items:
        if not isinstance(item, dict):
            failed.append({"pet_id": None, "care_service_id": None, "reason": _("Invalid service entry.")})
            continue
        if not item.get("pet_id") or not item.get("care_service_id"):
            failed.append(
                {
                    "pet_id": item.get("pet_id"),
                    "care_service_id": item.get("care_service_id"),
                    "reason": _("Pet and care service are required."),
                }
            )
            continue

        try:
            care_service = (
                frappe.db.get_value(
                    "CareService template",
                    item["care_service_id"],
                    ["service_name", "category_id"],
                    as_dict=True,
                )
                or {}
            )
            option = {}
            if item.get("service_option"):
                option = (
                    frappe.db.get_value(
                        "Care Service Billing Option",
                        item.get("service_option"),
                        ["item_code", "default_rate", "category_care_services", "animal_type"],
                        as_dict=True,
                    )
                    or {}
                )

            svc = frappe.new_doc("PetCareService")
            svc.pet_service_name = (
                item.get("pet_service_name")
                or item.get("service_name")
                or care_service.get("service_name")
                or item["care_service_id"]
            )
            svc.pet_id = item["pet_id"]
            svc.care_service_id = item["care_service_id"]
            svc.category = (
                item.get("category")
                or care_service.get("category_id")
                or option.get("category_care_services")
            )
            svc.guardian_id = item.get("guardian_id")
            svc.provider = item.get("provider")
            svc.doctor = item.get("doctor") or item.get("practitioner")
            svc.service_option = item.get("service_option")
            svc.item_code = item.get("item_code") or option.get("item_code")
            svc.status = item.get("status") or "pending"
            if item.get("price") is not None:
                svc.price = item.get("price")
            elif option:
                svc.price = option.get("default_rate")
            svc.due_date = item.get("due_date") or today()
            svc.insert(ignore_permissions=True)
            frappe.db.commit()
            created.append(svc.name)
        except Exception as e:
            failed.append(
                {
                    "pet_id": item.get("pet_id"),
                    "care_service_id": item.get("care_service_id"),
                    "reason": str(e),
                }
            )

    return {"created": created, "failed": failed}


@frappe.whitelist(methods=["POST"])
@standardize_response
def cancel_service(service_name: str = None, cancellation_reason: str = None):
    service_name = cstr(service_name).strip()
    reason = cstr(cancellation_reason).strip()

    if not service_name:
        frappe.throw(_("service_name is required."))
    if not reason:
        frappe.throw(_("cancellation_reason is required."))
    if not frappe.db.exists("PetCareService", service_name):
        frappe.throw(_("PetCareService {0} does not exist.").format(frappe.bold(service_name)))

    service = frappe.get_doc("PetCareService", service_name)
    current_status = cstr(service.get("status")).strip()
    if current_status.casefold() in SERVICE_CANCELLATION_TERMINAL_STATUSES or service.get("end_date"):
        frappe.throw(_("This service is already completed and can't be cancelled."))

    _assert_service_billing_cancellable(service)

    user = frappe.session.user
    service.status = "Cancelled"
    service.cancellation_reason = reason
    service.cancelled_at = now_datetime()
    service.cancelled_by = frappe.db.get_value("User", frappe.session.user, "full_name") or frappe.session.user
    service.save(ignore_permissions=True)
    if service.get("source_doctype") == "Pet Boarding":
        _cancel_service_billable(service)
    service.add_comment(
        "Comment",
        _("Service cancelled by {0}. Reason: {1}").format(user, reason),
    )

    return _pet_care_service_payload(service)


def _assert_service_billing_cancellable(service):
    linked_service_id = f"PetCareService::{service.name}"
    if service.get("visit"):
        assert_visit_billable_item_can_cancel(
            service.visit,
            linked_service_id=linked_service_id,
            linked_doctype="PetCareService",
            linked_name=service.name,
            order_id=service.get("order_id"),
            item_type="Service",
            allow_legacy_service_fallback=True,
            require_match=True,
        )
    elif service.get("source_doctype") == "Pet Boarding":
        assert_boarding_billable_item_can_cancel(
            service.get("source_name"),
            linked_service_id=linked_service_id,
            linked_doctype="PetCareService",
            linked_name=service.name,
            order_id=service.get("order_id"),
            item_type="Service",
        )


def _cancel_service_billable(service):
    linked_service_id = f"PetCareService::{service.name}"
    if service.get("visit"):
        cancel_visit_billable_item_by_link(
            service.visit,
            linked_service_id=linked_service_id,
            linked_doctype="PetCareService",
            linked_name=service.name,
            order_id=service.get("order_id"),
            item_type="Service",
            allow_legacy_service_fallback=True,
            require_match=True,
        )
    elif service.get("source_doctype") == "Pet Boarding":
        cancel_boarding_billable_item_by_link(
            service.get("source_name"),
            linked_service_id=linked_service_id,
            linked_doctype="PetCareService",
            linked_name=service.name,
            order_id=service.get("order_id"),
            item_type="Service",
        )


def _cancel_service_visit_order(service):
    if not service.get("visit") or not service.get("order_id"):
        return
    visit = frappe.get_doc("Vet Visit", service.visit)
    changed = False
    for row in visit.get("orders") or []:
        if row.order_id == service.order_id:
            row.status = "Cancelled"
            row.linked_doctype = "PetCareService"
            row.linked_name = service.name
            changed = True
    if changed:
        visit.flags.ignore_billing_lock = True
        visit.save(ignore_permissions=True)


def _pet_care_service_payload(service) -> dict:
    return with_link_aliases(
        service.as_dict(no_nulls=False),
        pet_field="pet_id",
        guardian_field="guardian_id",
        doctor_field="doctor",
        provider_field="provider",
    )
