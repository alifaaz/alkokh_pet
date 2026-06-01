import frappe
from frappe import _
from frappe.utils import cstr, flt


PET_CARE_SERVICE_SOURCE_PREFIX = "[alkokh-source:PetCareService:"
PET_CARE_SERVICE_SOURCE_SUFFIX = "]"

def fix_due_date(doc, method=None):
    if doc.due_date and doc.posting_date:
        from frappe.utils import getdate
        if getdate(doc.due_date) < getdate(doc.posting_date):
            doc.due_date = doc.posting_date

def before_insert(doc, method=None):
    if frappe.session.user == "Administrator":
        return

    if getattr(doc, "from_custom_flow", False) or getattr(doc.flags, "from_custom_flow", False):
        return

    if _is_pet_care_service_draft_invoice(doc):
        doc.flags.from_custom_flow = True
        return

    frappe.throw(_("Direct Sales Invoice creation is not allowed."), frappe.PermissionError)


def _is_pet_care_service_draft_invoice(doc) -> bool:
    if doc.docstatus != 0 or not doc.get("customer"):
        return False
    if not doc.get("items"):
        return False

    for row in doc.get("items") or []:
        service_name = _source_service_name(row.get("description"))
        if not service_name:
            return False
        if not _row_matches_pet_care_service(doc, row, service_name):
            return False
    return True


def _source_service_name(description: str | None) -> str | None:
    description = cstr(description)
    start = description.find(PET_CARE_SERVICE_SOURCE_PREFIX)
    if start < 0:
        return None
    start += len(PET_CARE_SERVICE_SOURCE_PREFIX)
    end = description.find(PET_CARE_SERVICE_SOURCE_SUFFIX, start)
    if end < 0:
        return None
    return description[start:end].strip() or None


def _row_matches_pet_care_service(invoice, row, service_name: str) -> bool:
    service = frappe.db.get_value(
        "PetCareService",
        service_name,
        ["name", "guardian_id", "item_code", "price"],
        as_dict=True,
    )
    if not service or not service.get("guardian_id"):
        return False

    customer = frappe.db.get_value("Guardian", service.guardian_id, "customer_id")
    if not customer or customer != invoice.customer:
        return False

    if service.get("item_code") and row.get("item_code") != service.item_code:
        return False
    if service.get("price") not in (None, "") and flt(row.get("rate")) != flt(service.price):
        return False
    return True
