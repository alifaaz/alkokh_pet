import frappe
import hashlib
import re
import math
from frappe.utils import cint
from pet_app.api.response import standardize_response

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
def list_pets(page=1, page_size=10, search=None):
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
