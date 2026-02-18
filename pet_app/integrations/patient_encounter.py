import frappe
from frappe import _
from pet_app.utils.patient_linking import get_or_create_patient_for_pet


def _resolve_practitioner_from_session():
    return frappe.db.get_value(
        "Healthcare Practitioner",
        {"user_id": frappe.session.user},
        "name"
    )


def _complete_pet_care_service(pet_id: str, care_service_id: str, pcs_id: str | None = None):
    # 1) direct by pcs id
    if pcs_id and frappe.db.exists("PetCareService", pcs_id):
        current = (frappe.db.get_value("PetCareService", pcs_id, "status") or "").lower()
        if current != "completed":
            frappe.db.set_value("PetCareService", pcs_id, "status", "completed")
        return

    # 2) fallback: first pending/overdue by pet + care_service_id
    pcs = frappe.db.get_value(
        "PetCareService",
        {"pet_id": pet_id, "care_service_id": care_service_id, "status": ["in", ["pending", "overdue"]]},
        "name",
        order_by="due_date asc"
    )
    if pcs:
        current = (frappe.db.get_value("PetCareService", pcs, "status") or "").lower()
        if current != "completed":
            frappe.db.set_value("PetCareService", pcs, "status", "completed")


def _apply_desired_total_to_items(items: list[dict], desired_total: float):
    """
    Force invoice total to match desired_total by scaling item rates proportionally.
    This supports BOTH increasing and decreasing totals.

    - items: list of {"qty": x, "rate": y, ...}
    - desired_total: the target total coming from frontend (doc.custom_total_amount)
    """
    desired_total = float(desired_total or 0)
    if desired_total <= 0 or not items:
        return

    current_total = 0.0
    for it in items:
        qty = float(it.get("qty", 0) or 0)
        rate = float(it.get("rate", 0) or 0)
        current_total += qty * rate

    if current_total <= 0:
        return

    factor = desired_total / current_total

    # Scale all rates
    for it in items:
        it["rate"] = float(it.get("rate", 0) or 0) * factor

    # Fix rounding diff on last line to match exactly
    new_total = 0.0
    for it in items:
        qty = float(it.get("qty", 0) or 0)
        rate = float(it.get("rate", 0) or 0)
        new_total += qty * rate

    diff = desired_total - new_total
    if abs(diff) > 0.0001:
        last = items[-1]
        last_qty = float(last.get("qty", 1) or 1)
        last["rate"] = float(last.get("rate", 0) or 0) + (diff / last_qty)


def before_insert(doc, method=None):
    """
    Payload expected:
    {
      custom_pet_id,
      custom_guardian_id,
      custom_services: [
        { pet_care_service_id: "PetCareService-00002" },
        { care_service_id: "CareService-00002" }
      ],
      custom_total_amount,
      encounter_comment (optional)
    }
    """

    pet_id = getattr(doc, "custom_pet_id", None)
    guardian_id = getattr(doc, "custom_guardian_id", None)

    # -------------------------
    # 1) patient mandatory
    # -------------------------
    if not getattr(doc, "patient", None):
        if not pet_id:
            frappe.throw(_("Missing custom_pet_id (required to derive patient)."))

        patient_name = frappe.db.get_value("Pet", pet_id, "patient_id")

        if not patient_name:
            if not guardian_id:
                frappe.throw(_("Missing custom_guardian_id (required to create/link patient)."))
            patient_name = get_or_create_patient_for_pet(pet_id, guardian_id)

        doc.patient = patient_name

    # -------------------------
    # 2) practitioner mandatory
    # -------------------------
    if not getattr(doc, "practitioner", None):
        practitioner = _resolve_practitioner_from_session()
        if not practitioner:
            frappe.throw(_("No Healthcare Practitioner linked to this user (session)."))
        doc.practitioner = practitioner

    # -------------------------
    # 3) Map custom_services -> itself (child table lines normalized)
    # -------------------------
    services = getattr(doc, "custom_services", None) or []
    if not services:
        return

    # reset and rebuild lines
    doc.set("custom_services", [])

    for s in services:
        pcs_id = getattr(s, "pet_care_service_id", None)
        care_id = getattr(s, "care_service_id", None)

        # If row provided pcs_id, derive care_service_id from it
        if pcs_id and not care_id:
            care_id = frappe.db.get_value("PetCareService", pcs_id, "care_service_id")
            if not care_id:
                frappe.throw(_("PetCareService {0} missing care_service_id").format(pcs_id))

        if not care_id:
            frappe.throw(_("Each row in custom_services must have care_service_id or pet_care_service_id."))

        default_rate = frappe.db.get_value("CareService", care_id, "default_price") or 0

        doc.append("custom_services", {
            "care_service_id": care_id,
            "qty": 1,
            "rate": default_rate,
            "line_notes": "",
            "pet_care_service_id": pcs_id
        })


def after_insert(doc, method=None):
    """
    After insert:
    - create & submit Sales Invoice once
    - mark pet care services completed
    - FORCE invoice total to match doc.custom_total_amount (frontend)
    """

    pet_id = getattr(doc, "custom_pet_id", None)
    guardian_id = getattr(doc, "custom_guardian_id", None)
    if not pet_id or not guardian_id:
        return

    # idempotency
    if getattr(doc, "invoiced", 0) or getattr(doc, "custom_sales_invoice", None):
        return

    customer = frappe.db.get_value("Guardian", guardian_id, "customer_id")
    if not customer:
        frappe.throw(_("Guardian has no customer_id. Complete OTP/Profile first."))

    lines = getattr(doc, "custom_services", []) or []
    if not lines:
        return

    items = []
    for row in lines:
        care_id = getattr(row, "care_service_id", None)
        if not care_id:
            continue

        item_code = frappe.db.get_value("CareService", care_id, "item_code")
        if not item_code:
            frappe.throw(_("CareService {0} missing item_code").format(care_id))

        qty = float(getattr(row, "qty", 1) or 1)
        rate = float(getattr(row, "rate", 0) or 0)

        items.append({
            "item_code": item_code,
            "qty": qty,
            "rate": rate,
            "description": getattr(row, "line_notes", "") or "",
        })

        pcs_id = getattr(row, "pet_care_service_id", None)
        _complete_pet_care_service(pet_id, care_id, pcs_id=pcs_id)

    if not items:
        return

    # ✅ Force invoice total to match frontend total
    desired_total = float(getattr(doc, "custom_total_amount", 0) or 0)
    _apply_desired_total_to_items(items, desired_total)

    si = frappe.get_doc({
        "doctype": "Sales Invoice",
        "customer": customer,
        "posting_date": doc.encounter_date,
        "due_date": doc.encounter_date,
        "items": items,

        # optional links (if you created custom fields)
        "custom_patient_encounter": doc.name,
        "custom_pet_id": pet_id,
        "custom_guardian_id": guardian_id,
    })
    si.insert(ignore_permissions=True)
    si.submit()

    doc.db_set("invoiced", 1)
    if doc.meta.has_field("custom_sales_invoice"):
        doc.db_set("custom_sales_invoice", si.name)
