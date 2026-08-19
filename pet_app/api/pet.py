import frappe
import hashlib
import re
import math
from frappe.utils import cint, cstr, sbool
from pet_app.api.response import standardize_response
from pet_app.utils.mortality import apply_pet_visibility_filters

# Per-pet dashboard and doctor suggestions. Implemented in pet_dashboard.py and
# re-exported here because the frontend calls them at pet_app.api.pet.*.
# Deliberately not wrapped in standardize_response: both return a plain dict, which
# is the contract the dashboard client reads.
from pet_app.api.pet_dashboard import get_pet_dashboard, get_pet_insights  # noqa: F401


# ============================================================
# Helpers
# ============================================================

MAX_SIZE = 500 * 1024  # 500 KB


def _guardian_for_current_user() -> str | None:
    if frappe.session.user in ("Guest", "Administrator"):
        return None
    return frappe.db.get_value("Guardian", {"user_id": frappe.session.user}, "name")


def _require_pet_read_access(docname: str | None = None):
    if frappe.session.user == "Guest":
        frappe.throw("Not permitted", frappe.PermissionError)

    guardian = _guardian_for_current_user()
    if guardian:
        if docname and not frappe.db.exists("PetGuardian", {"guardian_id": guardian, "pet_id": docname}):
            frappe.throw("Not permitted", frappe.PermissionError)
        return guardian

    if not frappe.has_permission("Pet", doc=docname, ptype="read") if docname else not frappe.has_permission("Pet", ptype="read"):
        frappe.throw("Not permitted", frappe.PermissionError)
    return None


@frappe.whitelist()
@standardize_response
def list_pet_breeds(animal_type=None, animal_species=None, search=None, page_size=100):
    if frappe.session.user == "Guest":
        frappe.throw("Not permitted", frappe.PermissionError)

    if not frappe.db.exists("DocType", "Pet Breed"):
        return {"data": []}

    if not frappe.has_permission("Pet Breed", ptype="read"):
        frappe.throw("Not permitted", frappe.PermissionError)

    filters = {"enabled": 1}
    if animal_type:
        filters["animal_type"] = animal_type
    if animal_species:
        filters["animal_species"] = animal_species
    if search:
        filters["breed_name"] = ["like", f"%{search}%"]

    page_size = min(cint(page_size) or 100, 500)
    breeds = frappe.get_all(
        "Pet Breed",
        filters=filters,
        fields=["name", "breed_name", "animal_species", "animal_type"],
        order_by="breed_name asc",
        page_length=page_size,
    )

    return {"data": breeds}


def resolve_image_fieldname(doctype: str) -> str:
    """Return the first 'Attach Image' field in the doctype."""
    meta = frappe.get_meta(doctype)
    for df in meta.fields:
        if df.fieldtype == "Attach Image":
            return df.fieldname
    frappe.throw(f"No Attach Image field found in {doctype}. Please add one (e.g. pet_image).")


def _require_doc_write(doctype: str, docname: str):
    """Ensure user has write permission on the document."""
    doc = frappe.get_doc(doctype, docname)
    if not doc.has_permission("write"):
        frappe.throw("No permission to update this document")
    return doc


def _require_file_create():
    if not frappe.has_permission("File", ptype="create"):
        frappe.throw("No permission to create files", frappe.PermissionError)


def _ensure_folder(doctype: str) -> str:
    """Ensure folder Home/{doctype} exists and return folder path."""
    folder_path = f"Home/{doctype}"
    if not frappe.db.exists("File", {"name": folder_path, "is_folder": 1}):
        _require_file_create()
        frappe.get_doc({
            "doctype": "File",
            "file_name": doctype,
            "folder": "Home",
            "is_folder": 1
        }).insert()
    return folder_path


def _safe_filename(name: str) -> str:
    return re.sub(r"[^\w\s.-]", "", name or "file").strip() or "file"


# ============================================================
# 1) Upload Multiple Files  — Folder = Home/Doctype ONLY
# ============================================================

@frappe.whitelist()
@standardize_response
def upload_multiple_files():
    try:
        if not frappe.request.files:
            frappe.throw("No files provided")

        files = frappe.request.files.getlist("files")
        doctype = frappe.form_dict.get("doctype")
        docname = frappe.form_dict.get("docname")
        custom_is_default_flag = frappe.form_dict.get("custom_is_default")  # "1" or "0"

        if not doctype or not docname:
            frappe.throw("Missing doctype or docname")

        if not frappe.db.exists(doctype, docname):
            frappe.throw("Document does not exist")

        # Permission
        _require_doc_write(doctype, docname)
        _require_file_create()

        # Auto-resolve Attach Image field
        fieldname = resolve_image_fieldname(doctype)

        uploaded, skipped, errors = [], [], []
        folder_path = _ensure_folder(doctype)

        # Is there already a default file for this doc?
        existing_default = bool(frappe.db.exists(
            "File",
            {"attached_to_doctype": doctype, "attached_to_name": docname, "custom_is_default": 1}
        ))

        for f in files:
            safe_name = _safe_filename(getattr(f, "filename", None))
            content = f.stream.read()

            if not content:
                errors.append({"file": safe_name, "error": "Empty file"})
                continue

            if len(content) > MAX_SIZE:
                errors.append({"file": safe_name, "error": "File too large"})
                continue

            sha1 = hashlib.sha1(content).hexdigest()

            duplicate = frappe.db.get_value(
                "File",
                {"attached_to_doctype": doctype, "attached_to_name": docname, "custom_sha1_hash": sha1},
                "name"
            )
            if duplicate:
                skipped.append({"file_name": safe_name, "reason": "Duplicate file", "existing": duplicate})
                continue

            file_doc = frappe.get_doc({
                "doctype": "File",
                "file_name": safe_name,
                "content": content,
                "attached_to_doctype": doctype,
                "attached_to_name": docname,
                "attached_to_field": fieldname,
                "folder": folder_path,
                "is_private": 0,
                "custom_sha1_hash": sha1,
                "custom_is_default": 0
            })
            file_doc.insert()

            # Set default logic:
            # - if custom_is_default=1 => make it default
            # - else if no default exists yet => make first uploaded default
            should_default = (custom_is_default_flag == "1") or (not existing_default)

            if should_default:
                frappe.db.sql("""
                    UPDATE `tabFile`
                    SET custom_is_default = 0
                    WHERE attached_to_doctype=%s AND attached_to_name=%s AND name!=%s
                """, (doctype, docname, file_doc.name))

                frappe.db.set_value("File", file_doc.name, "custom_is_default", 1)
                frappe.db.set_value(doctype, docname, fieldname, file_doc.file_url)

                existing_default = True
                file_doc.reload()

            uploaded.append({
                "name": file_doc.name,
                "file_url": file_doc.file_url,
                "file_name": safe_name,
                "custom_is_default": file_doc.custom_is_default
            })

        frappe.db.commit()
        return {"uploaded": uploaded, "skipped": skipped, "errors": errors}

    except Exception:
        frappe.log_error("UPLOAD_MULTIPLE_FILES_ERROR", frappe.get_traceback())
        frappe.throw("Upload failed")


# ============================================================
# 2) Delete Files (SECURE)
# ============================================================
@frappe.whitelist()
@standardize_response
def delete_multiple_files(file_names):
    import json

    if isinstance(file_names, str):
        file_names = json.loads(file_names)

    result = []
    for fid in file_names:
        try:
            if not frappe.db.exists("File", fid):
                result.append({"file": fid, "status": "not_found"})
                continue

            file_doc = frappe.get_doc("File", fid)

            # نجيب doctype و docname من الملف نفسه
            doctype = file_doc.attached_to_doctype
            docname = file_doc.attached_to_name

            # نتحقق من الصلاحية على الـ document
            _require_doc_write(doctype, docname)

            deleted_a_default = bool(file_doc.custom_is_default)

            frappe.delete_doc("File", fid, force=1)
            result.append({"file": fid, "status": "deleted", "doctype": doctype, "docname": docname})

            # إذا كان default نعين غيره
            if deleted_a_default:
                fieldname = resolve_image_fieldname(doctype)
                remaining = frappe.get_all(
                    "File",
                    filters={
                        "attached_to_doctype": doctype,
                        "attached_to_name": docname,
                        "attached_to_field": fieldname
                    },
                    fields=["name", "file_url"],
                    order_by="creation asc",
                    limit=1
                )
                new_url = remaining[0]["file_url"] if remaining else None
                if remaining:
                    frappe.db.set_value("File", remaining[0]["name"], "custom_is_default", 1)
                frappe.db.set_value(doctype, docname, fieldname, new_url, update_modified=False)
                _sync_user_image_from_upload(doctype, docname, new_url)

        except Exception as e:
            result.append({"file": fid, "status": "error", "error": str(e)})

    frappe.db.commit()
    return result
# ============================================================
# 3) Set Default (SECURE + updates Attach Image field)
# ============================================================

@frappe.whitelist()
@standardize_response
def set_default_file(file_id, doctype, docname):
    if not doctype or not docname:
        frappe.throw("Missing doctype or docname")

    if not frappe.db.exists(doctype, docname):
        frappe.throw("Document does not exist")

    _require_doc_write(doctype, docname)

    if not frappe.db.exists("File", file_id):
        frappe.throw("File does not exist")

    file_doc = frappe.get_doc("File", file_id)
    if file_doc.attached_to_doctype != doctype or file_doc.attached_to_name != docname:
        frappe.throw("File does not belong to this document")

    # Use the doctype's first Attach Image field
    fieldname = resolve_image_fieldname(doctype)

    # Optional strict check: only allow setting default for the same attached_to_field
    if (file_doc.attached_to_field or "") != (fieldname or ""):
        frappe.throw(f"This file is not attached to the image field ({fieldname}).")

    # reset others
    frappe.db.sql("""
        UPDATE `tabFile`
        SET custom_is_default = 0
        WHERE attached_to_doctype = %s
          AND attached_to_name = %s
          AND name != %s
    """, (doctype, docname, file_id))

    # set this as default
    frappe.db.set_value("File", file_id, "custom_is_default", 1)

    # update doctype Attach Image field
    frappe.db.set_value(doctype, docname, fieldname, file_doc.file_url)

    frappe.db.commit()
    return {"message": "Default updated successfully", "file": file_id, "file_url": file_doc.file_url}


# ============================================================
# 4) GET FILES (SECURE - no guest)
# ============================================================

@frappe.whitelist()
@standardize_response
def get_pet_images(doctype, docname):
    if not doctype or not docname:
        frappe.throw("Missing doctype or docname")

    if not frappe.db.exists(doctype, docname):
        frappe.throw("Document not found")

    # Read permission is enough to view images
    doc = frappe.get_doc(doctype, docname)
    if not doc.has_permission("read"):
        frappe.throw("No permission to read this document")

    fieldname = resolve_image_fieldname(doctype)

    files = frappe.get_all(
        "File",
        filters={
            "attached_to_doctype": doctype,
            "attached_to_name": docname,
            "attached_to_field": fieldname
        },
        fields=["name", "file_name", "file_url", "custom_is_default", "creation", "folder"],
        order_by="custom_is_default desc, creation asc"
    )

    return {"fieldname": fieldname, "total": len(files), "images": files}


# ============================================================
# 5) List Pets with Pagination and Search (images filtered by attached_to_field)
# ============================================================

@frappe.whitelist()
@standardize_response
def list_pets(page=1, page_size=10, search=None, include_deceased=0):
    """The selection list. Deceased pets are excluded unless explicitly asked for.

    This is the list every clinical and billable flow picks from - boarding, visits,
    appointments all draw on it, there is no separate picker per flow - so it is the one
    place where hiding the dead actually prevents the mistake. PET-00191 was offered here
    like any other animal, chosen, and only refused at submit.

    `include_deceased=1` is for the views that legitimately need them: medical history,
    the death record itself, mortality reporting.
    """
    guardian = _require_pet_read_access()
    page = cint(page) or 1
    page_size = cint(page_size) or 10
    if page < 1:
        page = 1
    if page_size < 1:
        page_size = 10

    filters = {}
    if guardian:
        linked_pets = frappe.get_all("PetGuardian", filters={"guardian_id": guardian}, pluck="pet_id")
        if not linked_pets:
            return {
                "page": page,
                "page_size": page_size,
                "total": 0,
                "total_pages": 0,
                "has_next": False,
                "has_prev": page > 1,
                "data": [],
            }
        filters["name"] = ["in", linked_pets]
    if search:
        filters["pet_name"] = ["like", f"%{search}%"]

    # Applied before `total` as well as before the fetch: counting on one filter set and
    # fetching on another gives a page that says 20 results and returns 19.
    filters = apply_pet_visibility_filters(filters, exclude_deceased=not cint(include_deceased))

    total = frappe.db.count("Pet", filters=filters)
    start = (page - 1) * page_size

    pets = frappe.get_all(
        "Pet",
        filters=filters,
        fields=[
            "name", "pet_name", "animal_species", "animal_type", "breed", "status", "birth_date",
            "registration_date", "color", "gender", "weight", "hight",
            "blood_type", "play", "activity_exercise",
            "food_brand",
            "food_type", "food_type.type_name",
            "description", "note"
        ],
        start=start,
        page_length=page_size
    )

    remove_keys = [
        "creation", "modified", "modified_by", "owner", "docstatus", "idx",
        "_comments", "_assign", "_liked_by", "_user_tags"
    ]

    # Attach Image field for Pet (first Attach Image)
    pet_image_field = resolve_image_fieldname("Pet")

    for pet in pets:
        for key in remove_keys:
            pet.pop(key, None)

        images = frappe.get_all(
            "File",
            filters={
                "attached_to_doctype": "Pet",
                "attached_to_name": pet["name"],
                "attached_to_field": pet_image_field
            },
            fields=["name", "file_url", "file_name", "custom_is_default"],
            order_by="custom_is_default desc, creation asc"
        )

        pet["images"] = images
        pet["image"] = next((img["file_url"] for img in images if img.get("custom_is_default")), None)
        pet["pet_id"] = pet.get("name")
        pet["pet_name"] = pet.get("pet_name") or pet.get("name")

    total_pages = math.ceil(total / page_size) if page_size else 1

    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_prev": page > 1,
        "data": pets,
    }



# ============================================================
# 5b) Pets of one or more guardians - THE guardian-scoped selection list
# ============================================================

# The fields every caller of the old raw PetGuardian query reads, in one place.
# Deliberately includes the death markers even in the default (living-only)
# response: a picker that can SEE a pet is deceased can grey the row out and say
# why, where one that simply never receives it leaves the operator wondering
# where the animal went.
GUARDIAN_PET_FIELDS = [
    "name", "pet_name", "pet_image",
    "animal_species", "animal_type", "breed",
    "gender", "weight", "hight", "birth_date", "color", "blood_type",
    "status", "pet_status",
    "is_deceased", "death_date", "death_record",
]


def _flag(value) -> int:
    """Read a boolean request argument that may arrive as a bool, int or string.

    `cint("true")` is 0. A GET query param is always a string, so a mortality
    picker asking `include_deceased=true` would have been handed a list with the
    dead silently filtered out - the exact failure this endpoint exists to stop,
    pointed the other way. `sbool` maps "true"/"1"/"false"/"0" and passes
    anything else through for `cint` to reduce.
    """
    return cint(sbool(value))


def _normalize_guardian_ids(guardians) -> list[str]:
    """Accept one guardian, a JSON array, or a comma-separated string.

    The six callers this replaces each pass a single id; the mortality and
    workspace views want several at once. Taking both here means neither side
    has to build a request body for the one-guardian case.
    """
    if guardians is None:
        return []
    if isinstance(guardians, str):
        text = guardians.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                guardians = frappe.parse_json(text)
            except Exception:
                frappe.throw("Invalid guardians value")
        else:
            guardians = text.split(",")
    if not isinstance(guardians, (list, tuple, set)):
        guardians = [guardians]

    seen: list[str] = []
    for value in guardians:
        gid = cstr(value).strip()
        if gid and gid not in seen:
            seen.append(gid)
    return seen


def _require_guardian_pets_access(guardian_ids: list[str]) -> list[str]:
    """Resolve which of the requested guardians the caller may actually read.

    A guardian user is silently narrowed to themselves rather than refused: the
    portal has no business asking for another guardian, and a 403 on a picker
    is a worse failure than an empty one. Staff go through the same Pet read
    check `_require_pet_read_access` uses, so this endpoint grants nothing that
    `list_pets` does not already grant.
    """
    if frappe.session.user == "Guest":
        frappe.throw("Not permitted", frappe.PermissionError)

    own_guardian = _guardian_for_current_user()
    if own_guardian:
        return [own_guardian] if (not guardian_ids or own_guardian in guardian_ids) else []

    if not frappe.has_permission("Pet", ptype="read"):
        frappe.throw("Not permitted", frappe.PermissionError)
    if not frappe.has_permission("PetGuardian", ptype="read"):
        frappe.throw("Not permitted", frappe.PermissionError)
    return guardian_ids


@frappe.whitelist()
@standardize_response
def get_guardian_pets(
    guardians=None,
    search=None,
    include_deceased=False,
    include_archived=False,
    page=1,
    page_size=0,
):
    """The pets of one or more guardians. Deceased excluded unless asked for.

    This exists because the guardian-scoped picker was the one selection surface
    with no method behind it: the client issued a raw
    `/api/resource/PetGuardian?fields=[...pet_id.status...]` and assembled the
    list itself, so none of the deceased filtering applied to the other
    selection endpoints could reach it. PET-00191 was offered for boarding that
    way. A rule that lives in a method only binds the callers that call it,
    which is why this ships alongside a PetGuardian permission query.

    `include_deceased=1` is the mortality path - the death report picker and the
    guardian's own memorial view legitimately want the dead, and they are the
    reason this is an opt-in rather than a filter nobody can turn off.

    Returns one row per PET, not per link row. `pet_id` is unique on
    PetGuardian today only by convention - nothing constrains it - so asking for
    several guardians who share an animal must not put that animal in the picker
    twice. The link that names it is chosen by the same primary_owner-first rule
    the rest of the codebase uses, and every matching link is kept in `links`.
    """
    guardian_ids = _normalize_guardian_ids(guardians)
    allowed = _require_guardian_pets_access(guardian_ids)
    if not allowed:
        return _empty_guardian_pets(guardian_ids, page, page_size)

    links = frappe.get_all(
        "PetGuardian",
        filters={"guardian_id": ["in", allowed]},
        fields=["name", "pet_id", "guardian_id", "role"],
    )
    links = [row for row in links if row.get("pet_id")]
    if not links:
        return _empty_guardian_pets(allowed, page, page_size)

    # Applied to the count as well as the fetch, for the reason list_pets spells
    # out: counting on one filter set and paging on another reports a total the
    # pages cannot produce.
    filters = {"name": ["in", sorted({row["pet_id"] for row in links})]}
    if not _flag(include_archived):
        filters["pet_status"] = ["!=", "Archived"]
    if search:
        filters["pet_name"] = ["like", f"%{cstr(search).strip()}%"]
    filters = apply_pet_visibility_filters(filters, exclude_deceased=not _flag(include_deceased))

    pets = frappe.get_all(
        "Pet",
        filters=filters,
        fields=GUARDIAN_PET_FIELDS,
        order_by="pet_name asc, name asc",
    )

    links_by_pet: dict[str, list[dict]] = {}
    for row in links:
        links_by_pet.setdefault(row["pet_id"], []).append(row)

    rows = []
    for pet in pets:
        pet_links = links_by_pet.get(pet["name"], [])
        primary = next((l for l in pet_links if l.get("role") == "primary_owner"), None) or (pet_links[0] if pet_links else {})
        rows.append({
            **pet,
            "pet_id": pet["name"],
            "pet_name": pet.get("pet_name") or pet["name"],
            "is_deceased": cint(pet.get("is_deceased")),
            "link": primary.get("name"),
            "role": primary.get("role"),
            "guardian_id": primary.get("guardian_id"),
            "links": [
                {"link": l["name"], "guardian_id": l["guardian_id"], "role": l.get("role")}
                for l in pet_links
            ],
        })

    total = len(rows)
    page = cint(page) or 1
    if page < 1:
        page = 1
    page_size = cint(page_size)
    if page_size > 0:
        start = (page - 1) * page_size
        data = rows[start:start + page_size]
        total_pages = math.ceil(total / page_size)
    else:
        # The default. Guardians hold 1.4 pets on average and 11 at the most on
        # this site, so paging a picker that small only buys the six callers a
        # loop they would each have to write.
        data = rows
        total_pages = 1 if total else 0

    return {
        "guardians": allowed,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_prev": page > 1,
        "data": data,
    }


def _empty_guardian_pets(guardian_ids, page, page_size) -> dict:
    page = cint(page) or 1
    return {
        "guardians": list(guardian_ids or []),
        "page": page,
        "page_size": cint(page_size),
        "total": 0,
        "total_pages": 0,
        "has_next": False,
        "has_prev": page > 1,
        "data": [],
    }


# ============================================================
# 6) Get Single Pet (same format as list_pets) - images filtered by attached_to_field
# ============================================================

@frappe.whitelist()
@standardize_response
def get_pet(pet_id):
    _require_pet_read_access(pet_id)
    if not frappe.db.exists("Pet", pet_id):
        frappe.throw("Pet not found")

    fields = [
        "name", "pet_name", "animal_species", "animal_type", "breed", "status",
        "birth_date", "registration_date",
        "color", "gender", "weight", "hight",
        "blood_type", "play", "activity_exercise",
        "food_brand",
        "food_type", "food_type.type_name",
        "description", "note"
    ]

    pet_list = frappe.get_all(
        "Pet",
        filters={"name": pet_id},
        fields=fields,
        limit_page_length=1
    )

    if not pet_list:
        frappe.throw("Pet not found")

    pet = pet_list[0]

    pet_image_field = resolve_image_fieldname("Pet")

    images = frappe.get_all(
        "File",
        filters={
            "attached_to_doctype": "Pet",
            "attached_to_name": pet_id,
            "attached_to_field": pet_image_field
        },
        fields=["name", "file_url", "file_name", "custom_is_default"],
        order_by="custom_is_default desc, creation asc"
    )

    pet["images"] = images
    pet["image"] = next((img["file_url"] for img in images if img.get("custom_is_default")), None)
    pet["pet_id"] = pet.get("name")
    pet["pet_name"] = pet.get("pet_name") or pet.get("name")

    return {"data": pet}


# ============================================================
# 7) Upload Single File with Duplicate Check and Field Update (SECURE)
# ============================================================
def _get_linked_user_for_doc(doctype: str, docname: str) -> str | None:
    """Return linked user_id for Guardian / Healthcare Practitioner."""
    if doctype == "Guardian":
        return frappe.db.get_value("Guardian", docname, "user_id")
    if doctype == "Healthcare Practitioner":
        return frappe.db.get_value("Healthcare Practitioner", docname, "user_id")
    return None


def _sync_user_image_from_upload(doctype: str, docname: str, new_file_url: str | None):
    """
    One-way sync فقط:
    Guardian/Practitioner image -> User.user_image
    Triggered مباشرة بعد الرفع/الحذف.
    """
    user_id = _get_linked_user_for_doc(doctype, docname)
    if not user_id:
        return
    if not frappe.db.exists("User", user_id):
        return

    current = frappe.db.get_value("User", user_id, "user_image")

    # normalize empty strings
    if not new_file_url:
        new_file_url = None

    if current == new_file_url:
        return

    frappe.db.set_value("User", user_id, "user_image", new_file_url, update_modified=False)
@frappe.whitelist()
@standardize_response
def upload_single_file():
    try:
        if not frappe.request.files:
            frappe.throw("No file provided")

        file = list(frappe.request.files.values())[0]
        doctype = frappe.form_dict.get("doctype")
        docname = frappe.form_dict.get("docname")

        if not doctype or not docname:
            frappe.throw("Missing doctype or docname")

        if not frappe.db.exists(doctype, docname):
            frappe.throw("Document does not exist")

        _require_doc_write(doctype, docname)
        _require_file_create()

        fieldname = resolve_image_fieldname(doctype)

        content = file.stream.read()
        if not content:
            frappe.throw("Empty file")

        if len(content) > MAX_SIZE:
            frappe.throw("File too large")

        sha1 = hashlib.sha1(content).hexdigest()

        duplicate = frappe.db.get_value(
            "File",
            {"attached_to_doctype": doctype, "attached_to_name": docname, "custom_sha1_hash": sha1},
            ["name", "file_url"],
            as_dict=True
        )
        if duplicate:
            frappe.db.set_value(doctype, docname, fieldname, duplicate.file_url, update_modified=False)

            # ✅ SYNC (duplicate case)
            _sync_user_image_from_upload(doctype, docname, duplicate.file_url)

            return {"message": "duplicate", "file": duplicate}

        folder_path = _ensure_folder(doctype)

        # delete old files for same field only
        old_files = frappe.get_all(
            "File",
            filters={
                "attached_to_doctype": doctype,
                "attached_to_name": docname,
                "attached_to_field": fieldname
            },
            fields=["name"]
        )
        for f in old_files:
            frappe.delete_doc("File", f.name, force=1)

        import time
        safe_name = _safe_filename(getattr(file, "filename", None))
        safe_name = f"{int(time.time())}-{safe_name}"

        file_doc = frappe.get_doc({
            "doctype": "File",
            "file_name": safe_name,
            "content": content,
            "attached_to_doctype": doctype,
            "attached_to_name": docname,
            "attached_to_field": fieldname,
            "folder": folder_path,
            "is_private": 0,
            "custom_sha1_hash": sha1,
            "custom_is_default": 1
        })
        file_doc.insert()

        # set image url on the main doc
        frappe.db.set_value(doctype, docname, fieldname, file_doc.file_url, update_modified=False)

        # Also keep File table default consistent for this doc
        frappe.db.sql("""
            UPDATE `tabFile`
            SET custom_is_default = CASE WHEN name=%s THEN 1 ELSE 0 END
            WHERE attached_to_doctype=%s AND attached_to_name=%s
        """, (file_doc.name, doctype, docname))

        # ✅ SYNC (normal upload)
        _sync_user_image_from_upload(doctype, docname, file_doc.file_url)

        # ⚠️ ملاحظة: عادةً ما تحتاج frappe.db.commit() داخل request
        # خليه فقط إذا أنت متأكد محتاجه
        # frappe.db.commit()

        return {
            "message": "uploaded",
            "file": {"name": file_doc.name, "file_url": file_doc.file_url, "file_name": safe_name},
            "fieldname": fieldname
        }

    except Exception as e:
        frappe.log_error("UPLOAD_SINGLE_FILE_ERROR", frappe.get_traceback())
        frappe.throw(f"Upload failed: {str(e)}")
