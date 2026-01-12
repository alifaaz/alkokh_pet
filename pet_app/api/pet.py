import frappe
import hashlib
import re
import math
from frappe.utils import cint

# ============================================================
# 1) Upload Multiple Files  — Folder = Home/Doctype ONLY
# ============================================================


@frappe.whitelist()
def upload_multiple_files():
    try:
        # ----------------------------------------------------
        # 1) Validate request
        # ----------------------------------------------------
        if not frappe.request.files:
            frappe.throw("No files provided")

        files = frappe.request.files.getlist("files")
        doctype = frappe.form_dict.get("doctype")
        docname = frappe.form_dict.get("docname")
        fieldname = frappe.form_dict.get("fieldname")   # ⭐ نفس السنكل
        custom_is_default_flag = frappe.form_dict.get("custom_is_default")  # "1" or "0"

        if not doctype or not docname:
            frappe.throw("Missing doctype or docname")

        if not fieldname:
            frappe.throw("Missing fieldname")

        if not frappe.db.exists(doctype, docname):
            frappe.throw("Document does not exist")

        # ----------------------------------------------------
        # 2) Validate field exists
        # ----------------------------------------------------
        meta = frappe.get_meta(doctype)
        if not meta.has_field(fieldname):
            frappe.throw(f"Field '{fieldname}' does not exist in {doctype}")

        MAX_SIZE = 500 * 1024  # 500 KB
        uploaded, skipped, errors = [], [], []

        # ----------------------------------------------------
        # 3) Ensure folder exists
        # ----------------------------------------------------
        folder_path = f"Home/{doctype}"
        if not frappe.db.exists("File", {"name": folder_path, "is_folder": 1}):
            frappe.get_doc({
                "doctype": "File",
                "file_name": doctype,
                "folder": "Home",
                "is_folder": 1
            }).insert(ignore_permissions=True)

        # ----------------------------------------------------
        # 4) Check existing default
        # ----------------------------------------------------
        existing_default = bool(frappe.db.exists(
            "File",
            {
                "attached_to_doctype": doctype,
                "attached_to_name": docname,
                "custom_is_default": 1
            }
        ))

        # ----------------------------------------------------
        # 5) Upload loop
        # ----------------------------------------------------
        for file in files:
            safe_filename = re.sub(r"[^\w\s.-]", "", file.filename or "file")
            content = file.stream.read()

            if not content:
                errors.append({"file": safe_filename, "error": "Empty file"})
                continue

            if len(content) > MAX_SIZE:
                errors.append({"file": safe_filename, "error": "File too large"})
                continue

            sha1 = hashlib.sha1(content).hexdigest()

            # Duplicate check
            duplicate = frappe.db.get_value(
                "File",
                {
                    "attached_to_doctype": doctype,
                    "attached_to_name": docname,
                    "custom_sha1_hash": sha1
                },
                "name"
            )

            if duplicate:
                skipped.append({
                    "file_name": safe_filename,
                    "reason": "Duplicate file",
                    "existing": duplicate
                })
                continue

            # ------------------------------------------------
            # Create File
            # ------------------------------------------------
            file_doc = frappe.get_doc({
                "doctype": "File",
                "file_name": safe_filename,
                "content": content,
                "attached_to_doctype": doctype,
                "attached_to_name": docname,
                "attached_to_field": fieldname,   # ⭐ نفس السنكل
                "folder": folder_path,
                "is_private": 0,
                "custom_sha1_hash": sha1,
                "custom_is_default": 0
            })

            file_doc.insert(ignore_permissions=True)

            # ------------------------------------------------
            # Default logic
            # ------------------------------------------------
            should_default = False

            if custom_is_default_flag == "1":
                should_default = True
            elif not existing_default:
                should_default = True

            if should_default:
                # Reset other defaults
                frappe.db.sql("""
                    UPDATE `tabFile`
                    SET custom_is_default = 0
                    WHERE attached_to_doctype=%s
                      AND attached_to_name=%s
                      AND name!=%s
                """, (doctype, docname, file_doc.name))

                # Set this as default
                frappe.db.set_value("File", file_doc.name, "custom_is_default", 1)

                # ⭐ Update target field (Attach / Attach Image)
                frappe.db.set_value(
                    doctype,
                    docname,
                    fieldname,
                    file_doc.file_url
                )

                existing_default = True
                file_doc.reload()

            uploaded.append({
                "name": file_doc.name,
                "file_url": file_doc.file_url,
                "file_name": safe_filename,
                "custom_is_default": file_doc.custom_is_default
            })

        frappe.db.commit()

        return {
            "uploaded": uploaded,
            "skipped": skipped,
            "errors": errors
        }

    except Exception:
        frappe.log_error("UPLOAD_MULTIPLE_FILES_ERROR", frappe.get_traceback())
        frappe.throw("Upload failed")

# ============================================================
# 2) Delete Files
# ============================================================

@frappe.whitelist()
def delete_multiple_files(file_names):
    import json
    if isinstance(file_names, str):
        file_names = json.loads(file_names)

    result = []
    for f in file_names:
        try:
            if frappe.db.exists("File", f):
                frappe.delete_doc("File", f, force=1)
                result.append({"file": f, "status": "deleted"})
            else:
                result.append({"file": f, "status": "not_found"})
        except Exception as e:
            result.append({"file": f, "status": "error", "error": str(e)})

    frappe.db.commit()
    return result


# ============================================================
# 3) Set Default - مصلّح
# ============================================================

@frappe.whitelist()
def set_default_file(file_id, doctype, docname):
    if not frappe.db.exists("File", file_id):
        frappe.throw("File does not exist")

    # تأكد إن الملف فعلاً تابع لهذا الـ document
    file_doc = frappe.get_doc("File", file_id)
    if file_doc.attached_to_doctype != doctype or file_doc.attached_to_name != docname:
        frappe.throw("File does not belong to this document")

    # 1) احذف كل الـ defaults القديمة (ما عدا هذا الملف)
    frappe.db.sql("""
        UPDATE `tabFile`
        SET custom_is_default = 0
        WHERE attached_to_doctype = %s 
        AND attached_to_name = %s
        AND name != %s
    """, (doctype, docname, file_id))

    # 2) عيّن هذا الملف كـ default
    frappe.db.set_value("File", file_id, "custom_is_default", 1)

    frappe.db.commit()
    
    return {"message": "Default updated successfully", "file": file_id}


# ============================================================
# 4) GET FILES
# ============================================================

@frappe.whitelist(allow_guest=True)
def get_pet_images(doctype, docname):
    if not frappe.db.exists(doctype, docname):
        frappe.throw("Document not found")

    files = frappe.get_all("File",
        filters={
            "attached_to_doctype": doctype,
            "attached_to_name": docname
        },
        fields=["name", "file_name", "file_url", "custom_is_default", "creation", "folder"],
        order_by="custom_is_default desc, creation asc"
    )

    return {"total": len(files), "images": files}

# ============================================================
# 5) List Pets with Pagination and Search
# ============================================================
@frappe.whitelist(allow_guest=True)
def list_pets(page=1, page_size=10, search=None):
    """
    Get paginated pets list with optional search by pet_name
    and default image for each pet.
    """
    page = cint(page) or 1
    page_size = cint(page_size) or 10

    if page < 1:
        page = 1
    if page_size < 1:
        page_size = 10

    filters = {}
    if search:
        filters["pet_name"] = ["like", f"%{search}%"]

    total = frappe.db.count("Pet", filters=filters)
    start = (page - 1) * page_size

    pets = frappe.get_all(
        "Pet",
        filters=filters,
        fields=[ "name", "pet_name", "animal_species", "animal_type", "breed", "status", "birth_date",
         "registration_date", "color", "gender", "weight", "hight",
          "blood_type", "play", "activity_exercise","food_brand", "food_brand.brand_name", "food_type",
           "food_type.type_name", "description", "note" ],
        start=start,
        page_length=page_size
    )
    remove_keys = [
        "creation","modified","modified_by","owner","docstatus","idx",
        "_comments","_assign","_liked_by","_user_tags"
    ]
    for pet in pets:
        for key in remove_keys:
            pet.pop(key, None)

        images = frappe.get_all(
            "File",
            filters={
                "attached_to_doctype": "Pet",
                "attached_to_name": pet["name"]
            },
            fields=["name", "file_url", "file_name", "custom_is_default"],
            order_by="custom_is_default desc, creation asc"
        )

        pet["images"] = images
        # Set the default image
        default_image = next((img["file_url"] for img in images if img["custom_is_default"]), None)
        pet["image"] = default_image

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
# 6) Get Single Pet (same format as list_pets)
# ============================================================

@frappe.whitelist(allow_guest=True)
def get_pet(pet_id):

    if not frappe.db.exists("Pet", pet_id):
        frappe.throw("Pet not found")

    fields = [
        "name", "pet_name", "animal_species", "animal_type", "breed", "status",
        "birth_date", "registration_date",
        "color", "gender", "weight", "hight",
        "blood_type", "play", "activity_exercise",
        "food_brand", "food_brand.brand_name",
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

    # fetch images
    images = frappe.get_all(
        "File",
        filters={
            "attached_to_doctype": "Pet",
            "attached_to_name": pet_id
        },
        fields=["name", "file_url", "file_name", "custom_is_default"],
        order_by="custom_is_default desc, creation asc"
    )

    pet["images"] = images

    # default image
    pet["image"] = next(
        (img["file_url"] for img in images if img.get("custom_is_default")), None
    )

    return {"data": pet}
# ============================================================
# 7) Upload Single File with Duplicate Check and Field Update
# ============================================================ 
@frappe.whitelist()
def upload_single_file():
    try:
        # ----------------------------------------------------
        # 1) Validate request
        # ----------------------------------------------------
        if not frappe.request.files:
            frappe.throw("No file provided")

        file = list(frappe.request.files.values())[0]

        doctype = frappe.form_dict.get("doctype")
        docname = frappe.form_dict.get("docname")
        fieldname = frappe.form_dict.get("fieldname")

        if not doctype or not docname:
            frappe.throw("Missing doctype or docname")

        if not fieldname:
            frappe.throw("Missing fieldname")

        if not frappe.db.exists(doctype, docname):
            frappe.throw("Document does not exist")

        # ----------------------------------------------------
        # 2) Validate field exists in DocType
        # ----------------------------------------------------
        meta = frappe.get_meta(doctype)
        if not meta.has_field(fieldname):
            frappe.throw(f"Field '{fieldname}' does not exist in {doctype}")

        # ----------------------------------------------------
        # 3) Read & validate file
        # ----------------------------------------------------
        content = file.stream.read()
        if not content:
            frappe.throw("Empty file")

        MAX_SIZE = 500 * 1024  # 500 KB
        if len(content) > MAX_SIZE:
            frappe.throw("File too large")

        sha1 = hashlib.sha1(content).hexdigest()

        # ----------------------------------------------------
        # 4) Check duplicate (same doc + same content)
        # ----------------------------------------------------
        duplicate = frappe.db.get_value(
            "File",
            {
                "attached_to_doctype": doctype,
                "attached_to_name": docname,
                "custom_sha1_hash": sha1
            },
            ["name", "file_url"],
            as_dict=True
        )

        if duplicate:
            frappe.db.set_value(doctype, docname, fieldname, duplicate.file_url)
            return {
                "message": "duplicate",
                "file": duplicate
            }

        # ----------------------------------------------------
        # 5) Ensure folder exists
        # ----------------------------------------------------
        folder_path = f"Home/{doctype}"

        if not frappe.db.exists("File", {"name": folder_path, "is_folder": 1}):
            frappe.get_doc({
                "doctype": "File",
                "file_name": doctype,
                "folder": "Home",
                "is_folder": 1
            }).insert(ignore_permissions=True)

        # ----------------------------------------------------
        # 6) Remove old files linked to this field
        # ----------------------------------------------------
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

        # ----------------------------------------------------
        # 7) Generate safe filename
        # ----------------------------------------------------
        import time, re
        safe_filename = re.sub(r"[^\w\s.-]", "", file.filename or "file")
        safe_filename = f"{int(time.time())}-{safe_filename}"

        # ----------------------------------------------------
        # 8) Create File document
        # ----------------------------------------------------
        file_doc = frappe.get_doc({
            "doctype": "File",
            "file_name": safe_filename,
            "content": content,
            "attached_to_doctype": doctype,
            "attached_to_name": docname,
            "attached_to_field": fieldname,
            "folder": folder_path,
            "is_private": 0,
            "custom_sha1_hash": sha1,
            "custom_is_default": 1
        })

        file_doc.insert(ignore_permissions=True)

        # ----------------------------------------------------
        # 9) Update target field
        # ----------------------------------------------------
        frappe.db.set_value(doctype, docname, fieldname, file_doc.file_url)

        frappe.db.commit()

        # ----------------------------------------------------
        # 10) Response
        # ----------------------------------------------------
        return {
            "message": "uploaded",
            "file": {
                "name": file_doc.name,
                "file_url": file_doc.file_url,
                "file_name": safe_filename
            }
        }

    except Exception as e:
        frappe.log_error("UPLOAD_SINGLE_FILE_ERROR", frappe.get_traceback())
        frappe.throw(f"Upload failed: {str(e)}")