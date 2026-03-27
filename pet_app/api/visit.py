import frappe
from frappe import _
from frappe.utils import get_table_name

def _admin_only():
    if frappe.session.user != "Administrator":
        raise frappe.PermissionError(_("Not permitted"))

@frappe.whitelist()
def get_encounters(limit_start=0, limit_page_length=20):
    """
    Returns:
      data: [
        {
          name, encounter_date, encounter_time,
          practitioner, practitioner_name, image,
          encounter_comment,
          custom_pet_id, pet_name, pet_image,
          custom_guardian_id, full_name, guardian_image,
          custom_total_amount,
          custom_services: [{id, name}]
        }, ...
      ]

    Security: Administrator only (for now).
    """
    _admin_only()

    limit_start = int(limit_start or 0)
    limit_page_length = int(limit_page_length or 20)

    # 1) Base encounters list
    encounters = frappe.get_all(
        "Patient Encounter",
        fields=[
            "name",
            "encounter_date",
            "encounter_time",
            "practitioner",
            "encounter_comment",
            "custom_pet_id",
            "custom_guardian_id",
            "custom_total_amount",
        ],
        order_by="encounter_date desc, encounter_time desc",
        limit_start=limit_start,
        limit_page_length=limit_page_length,
    )

    if not encounters:
        return {"data": []}

    enc_names = [d["name"] for d in encounters]

    # 2) Resolve child doctype behind custom_services
    pe_meta = frappe.get_meta("Patient Encounter")
    df = pe_meta.get_field("custom_services")
    child_dt = (df.options or "").strip()
    if not child_dt:
        frappe.throw("Patient Encounter.custom_services has no Options (child doctype).")

    child_table = get_table_name(child_dt)

    # 3) Pull child rows for all encounters in one query
    rows = frappe.db.sql(
        f"""
        SELECT parent AS encounter, idx, care_service_id, pet_care_service_id
        FROM `{child_table}`
        WHERE parenttype='Patient Encounter'
          AND parentfield='custom_services'
          AND parent IN %(parents)s
        ORDER BY parent, idx
        """,
        {"parents": tuple(enc_names)},
        as_dict=True,
    )

    # Collect ids for name lookup
    care_ids = sorted({r["care_service_id"] for r in rows if r.get("care_service_id")})
    pcs_ids  = sorted({r["pet_care_service_id"] for r in rows if r.get("pet_care_service_id")})

    # 4) Name maps using your confirmed fields
    care_map = {}
    if care_ids:
        care_map = {
            x["name"]: (x.get("service_name") or x["name"])
            for x in frappe.get_all(
                "CareService",
                filters=[["name", "in", care_ids]],
                fields=["name", "service_name"],
            )
        }

    pcs_map = {}
    if pcs_ids:
        pcs_map = {
            x["name"]: (x.get("pet_service_name") or x["name"])
            for x in frappe.get_all(
                "PetCareService",
                filters=[["name", "in", pcs_ids]],
                fields=["name", "pet_service_name"],
            )
        }

    # 5) Group services by encounter as [{id, name}]
    services_by_enc = {}
    for r in rows:
        enc = r["encounter"]
        services_by_enc.setdefault(enc, [])
        if r.get("pet_care_service_id"):
            sid = r["pet_care_service_id"]
            services_by_enc[enc].append({"id": sid, "name": pcs_map.get(sid, sid)})
        elif r.get("care_service_id"):
            sid = r["care_service_id"]
            services_by_enc[enc].append({"id": sid, "name": care_map.get(sid, sid)})

    # 6) Enrich practitioner / pet / guardian
    prac_ids = sorted({d["practitioner"] for d in encounters if d.get("practitioner")})
    prac_map = {}
    if prac_ids:
        prac_map = {
            p["name"]: p
            for p in frappe.get_all(
                "Healthcare Practitioner",
                filters=[["name", "in", prac_ids]],
                fields=["name", "practitioner_name", "image"],
            )
        }

    pet_ids = sorted({d["custom_pet_id"] for d in encounters if d.get("custom_pet_id")})
    pet_map = {}
    if pet_ids:
        pet_map = {
            p["name"]: p
            for p in frappe.get_all(
                "Pet",
                filters=[["name", "in", pet_ids]],
                fields=["name", "pet_name", "pet_image"],
            )
        }

    guardian_ids = sorted({d["custom_guardian_id"] for d in encounters if d.get("custom_guardian_id")})
    guardian_map = {}
    if guardian_ids:
        guardian_map = {
            g["name"]: g
            for g in frappe.get_all(
                "Guardian",
                filters=[["name", "in", guardian_ids]],
                fields=["name", "full_name", "guardian_image"],
            )
        }

    # 7) Build final response
    out = []
    for d in encounters:
        p = prac_map.get(d.get("practitioner"), {}) if d.get("practitioner") else {}
        pet = pet_map.get(d.get("custom_pet_id"), {}) if d.get("custom_pet_id") else {}
        g = guardian_map.get(d.get("custom_guardian_id"), {}) if d.get("custom_guardian_id") else {}

        out.append({
            "name": d["name"],
            "encounter_date": d.get("encounter_date"),
            "encounter_time": d.get("encounter_time"),

            "practitioner": d.get("practitioner"),
            "practitioner_name": p.get("practitioner_name"),
            "image": p.get("image"),

            "encounter_comment": d.get("encounter_comment"),

            "custom_pet_id": d.get("custom_pet_id"),
            "pet_name": pet.get("pet_name"),
            "pet_image": pet.get("pet_image"),

            "custom_guardian_id": d.get("custom_guardian_id"),
            "full_name": g.get("full_name"),
            "guardian_image": g.get("guardian_image"),

            "custom_total_amount": d.get("custom_total_amount"),

            "custom_services": services_by_enc.get(d["name"], []),
        })

    return {"data": out}
