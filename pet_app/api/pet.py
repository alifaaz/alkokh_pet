import frappe
import json
from pet_app.api.file_utils import upload_files


# ═══════════════════════════════════════════════════════════════════
# SECTION 1: Upload Photos
# ═══════════════════════════════════════════════════════════════════

@frappe.whitelist()
def upload_pet_photos():

    frappe.form_dict['doctype'] = 'Pet'
    frappe.form_dict['folder'] = 'pet'
    return upload_files()


# ═══════════════════════════════════════════════════════════════════
# SECTION 2: Get Pet with Photos (Formatted)
# ═══════════════════════════════════════════════════════════════════

@frappe.whitelist()
def get_pet_with_photos(docname):

    if not docname:
        frappe.throw("❌ docname required")
    
    if not frappe.db.exists("Pet", docname):
        frappe.throw(f"❌ Pet {docname} not found")
    
    pet = frappe.get_doc("Pet", docname)
    pet_dict = pet.as_dict()
    
    photos = pet.get("photos") or []
    base_url = frappe.utils.get_url()
    photo_list = []
    
    for p in photos:
        photo_url = getattr(p, "pet_photo", None)
        if not photo_url:
            continue
        
        file_doc = frappe.db.get_value(
            "File",
            {
                "file_url": photo_url,
                "attached_to_doctype": "Pet",
                "attached_to_name": docname
            },
            ["name", "file_name", "file_size"],
            as_dict=True
        )
        
        photo_list.append({
            "photo": photo_url,
            "full_url": f"{base_url}{photo_url}",
            "is_default": bool(getattr(p, "is_default", 0)),
            "file_name": file_doc.get("name") if file_doc else None,
            "original_file_name": file_doc.get("file_name") if file_doc else None,
            "file_size": file_doc.get("file_size") if file_doc else 0,
            "file_size_kb": round((file_doc.get("file_size") or 0) / 1024, 2) if file_doc else 0
        })
    
    pet_dict.pop("photos", None)
    
    return {
        "success": True,
        "pet": pet_dict,
        "photos": photo_list,
        "total_photos": len(photo_list),
        "folder": f"Home/Pet/{docname}"
    }


# ═══════════════════════════════════════════════════════════════════
# SECTION 3: Delete Multiple Photos
# ═══════════════════════════════════════════════════════════════════

@frappe.whitelist()
def delete_multiple_photos(docname, photo_rows):

    if not docname:
        frappe.throw("❌ docname required")
    
    if not photo_rows:
        frappe.throw("❌ photo_rows required (array)")
    
    if isinstance(photo_rows, str):
        photo_rows = json.loads(photo_rows)
    
    if not isinstance(photo_rows, list):
        frappe.throw("❌ photo_rows must be an array")
    
    if not frappe.db.exists("Pet", docname):
        frappe.throw(f"❌ Pet {docname} not found")
    
    pet = frappe.get_doc("Pet", docname)
    deleted = []
    failed = []
    
    for row_name in photo_rows:
        try:
            photo_row = None
            for row in pet.get("photos") or []:
                if row.name == row_name:
                    photo_row = row
                    break
            
            if not photo_row:
                failed.append({
                    "row_name": row_name,
                    "reason": "Photo row not found in Pet"
                })
                continue
            
            photo_url = getattr(photo_row, "pet_photo", None)
            if not photo_url:
                failed.append({
                    "row_name": row_name,
                    "reason": "No photo URL in this row"
                })
                continue
            
            file_doc = frappe.db.get_value(
                "File",
                {
                    "file_url": photo_url,
                    "attached_to_doctype": "Pet",
                    "attached_to_name": docname
                },
                "name"
            )
            
            if file_doc:
                frappe.delete_doc("File", file_doc, ignore_permissions=True)
            
            pet.remove(photo_row)
            deleted.append(row_name)
        
        except Exception as e:
            failed.append({
                "row_name": row_name,
                "reason": str(e)
            })
    
    remaining_photos = pet.get("photos") or []
    if remaining_photos:
        has_default = any(getattr(p, "is_default", 0) for p in remaining_photos)
        if not has_default:
            remaining_photos[0].is_default = 1
            pet.custom_image = remaining_photos[0].pet_photo
    else:
        pet.custom_image = None
    
    pet.save(ignore_permissions=True)
    frappe.db.commit()
    
    return {
        "success": True,
        "message": f"{len(deleted)} photo(s) deleted successfully",
        "deleted": deleted,
        "failed": failed,
        "total_deleted": len(deleted),
        "total_failed": len(failed)
    }

# ═══════════════════════════════════════════════════════════════════
# SECTION 4: Set Default Photo
# ═══════════════════════════════════════════════════════════════════

@frappe.whitelist()
def set_default_photo(docname, photo_row):
    if not docname or not photo_row:
        frappe.throw("❌ docname and photo_row required")
    
    if not frappe.db.exists("Pet", docname):
        frappe.throw(f"❌ Pet {docname} not found")
    
    pet = frappe.get_doc("Pet", docname)
    
    target_row = None
    for row in pet.get("photos") or []:
        if row.name == photo_row:
            target_row = row
            break
    
    if not target_row:
        frappe.throw(f"❌ Photo row {photo_row} not found in Pet")
    
    photo_url = getattr(target_row, "pet_photo", None)
    if not photo_url:
        frappe.throw(f"❌ No photo URL in this row")
    
    for row in pet.get("photos") or []:
        row.is_default = 0
    
    target_row.is_default = 1
    
    pet.custom_image = photo_url
    
    pet.save(ignore_permissions=True)
    frappe.db.commit()
    
    return {
        "success": True,
        "message": "Default photo updated successfully",
        "custom_image": photo_url,
        "photo_row": photo_row
    }