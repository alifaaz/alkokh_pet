import json
import frappe

CARE_SERVICE_ROLES = ("System Manager", "Doctor", "Guardian", "Healthcare", "Guardians")


def _require_care_service_access(pet_id: str):
    if frappe.session.user == "Administrator":
        return
    if not frappe.has_permission("Pet", doc=pet_id, ptype="read"):
        frappe.throw("Not permitted", frappe.PermissionError)

    user_roles = set(frappe.get_roles(frappe.session.user) or [])
    if user_roles.intersection(CARE_SERVICE_ROLES):
        return

    frappe.throw("Not permitted", frappe.PermissionError)


@frappe.whitelist()
def get_pet_and_services(pet_id, filters=None, limit_start=0, limit_page_length=20):
    limit_start = int(limit_start or 0)
    limit_page_length = int(limit_page_length or 20)
    _require_care_service_access(pet_id)

    # 1) derive species
    pet = frappe.get_value("Pet", pet_id, ["animal_species"], as_dict=True)
    if not pet:
        frappe.throw("Pet not found")
    animal_species = pet.animal_species

    # 2) parse filters like /api/resource
    # expected: [["service_name","like","%a%"]]
    extra_filters = []
    if filters:
        if isinstance(filters, str):
            extra_filters = json.loads(filters)
        else:
            extra_filters = filters

    # 3) base filters for CareService
    care_filters = [["animal_species", "=", animal_species]]
    # add extra filters (for service_name like ... etc)
    if extra_filters:
        care_filters.extend(extra_filters)

    # 4) pending PetCareService (no pagination)
    pet_services = frappe.get_all(
        "PetCareService",
        filters={"pet_id": pet_id, "status": "pending"},
        fields=[
            "name",
            "pet_service_name",
            "pet_id",
            "care_service_id",
            "due_date",
            "provider",
            "description"
        ],
        order_by="due_date asc"
    )

    # 5) CareService with pagination exactly like resource
    master_services = frappe.get_all(
        "CareService",
        filters=care_filters,
        fields=[
            "name",
            "service_name",
            "item_code",
            "default_price",
            "frequency",
            "category_id",
            "description"
        ],
        order_by="service_name asc",
        limit_start=limit_start,
        limit_page_length=limit_page_length
    )

    # 6) enrich pet services with care_service_name + price + category
    care_ids = list({ps["care_service_id"] for ps in pet_services if ps.get("care_service_id")})
    care_map = {}
    if care_ids:
        rows = frappe.get_all(
            "CareService",
            filters={"name": ["in", care_ids]},
            fields=["name", "service_name", "default_price", "category_id"]
        )
        care_map = {r["name"]: r for r in rows}

    # categories name map (optional)
    cat_ids = set()
    for cs in master_services:
        if cs.get("category_id"):
            cat_ids.add(cs["category_id"])
    for r in care_map.values():
        if r.get("category_id"):
            cat_ids.add(r["category_id"])

    category_map = {}
    if cat_ids:
        label_field = "category_name"
        meta = frappe.get_meta("CategoryCareServices")
        if not meta.get_field(label_field):
            label_field = "name"
        cats = frappe.get_all(
            "CategoryCareServices",
            filters={"name": ["in", list(cat_ids)]},
            fields=["name"] if label_field == "name" else ["name", label_field]
        )
        for c in cats:
            category_map[c["name"]] = c.get(label_field) or c["name"]

    result = []

    for ps in pet_services:
        info = care_map.get(ps.get("care_service_id"), {})
        cat_id = info.get("category_id")
        result.append({
            "name": ps.get("name"),
            "pet_service_name": ps.get("pet_service_name"),
            "pet_id": ps.get("pet_id"),
            "care_service_id": ps.get("care_service_id"),
            "service_name": info.get("service_name"),
            "price": info.get("default_price"),
            "category_id": cat_id,
            "category_name": category_map.get(cat_id),
            "due_date": ps.get("due_date"),
            "provider": ps.get("provider"),
            "description": ps.get("description"),
        })

    for cs in master_services:
        cat_id = cs.get("category_id")
        result.append({
            "name": cs.get("name"),
            "service_name": cs.get("service_name"),
            "item_code": cs.get("item_code"),
            "default_price": cs.get("default_price"),
            "frequency": cs.get("frequency"),
            "category_id": cat_id,
            "category_name": category_map.get(cat_id),
            "description": cs.get("description"),
        })

    
    frappe.response["data"] = result
    return
