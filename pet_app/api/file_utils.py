import frappe
import hashlib
import os
from PIL import Image
import io

# ═══════════════════════════════════════════════════════════════════
# SECTION 1: Configuration
# ═══════════════════════════════════════════════════════════════════

FOLDER_CONFIG = {
    "pet": {
        "max_size_kb": 500,
        "allowed_extensions": [".jpg", ".jpeg", ".png", ".gif", ".webp"],
        "description": "Pet photos (max 500KB, images only)"
    },
    
    "product": {
        "max_size_kb": 1024,
        "allowed_extensions": [".jpg", ".jpeg", ".png"],
        "description": "Product images (max 1MB)"
    },
    
    "guardians": {
        "max_size_kb": 300,
        "allowed_extensions": [".jpg", ".jpeg", ".png"],
        "description": "Guardian photos (max 300KB)"
    },
    
    "clinic": {
        "max_size_kb": 2048,
        "allowed_extensions": [".jpg", ".jpeg", ".png", ".pdf"],
        "description": "Clinic documents (max 2MB)"
    },
    
    "medical_records": {
        "max_size_kb": 5120,
        "allowed_extensions": [".pdf", ".jpg", ".png"],
        "description": "Medical records (max 5MB)"
    }
}


# ═══════════════════════════════════════════════════════════════════
# SECTION 2: Folder Management
# ═══════════════════════════════════════════════════════════════════

def ensure_folder_exists(folder_path):
    parts = folder_path.split('/')
    current_path = ""
    parent = ""
    
    for i, part in enumerate(parts):
        current_path = "/".join(parts[:i+1])
        
        exists = frappe.db.exists("File", {
            "is_folder": 1,
            "name": current_path
        })
        
        if not exists:
            try:
                folder_doc = frappe.get_doc({
                    "doctype": "File",
                    "is_folder": 1,
                    "file_name": part,
                    "folder": parent if parent else "Home"
                })
                folder_doc.insert(ignore_permissions=True)
                frappe.db.commit()
            except Exception as e:

                if "duplicate" not in str(e).lower():
                    frappe.log_error(f"Folder creation error: {current_path} - {str(e)}")
        
        parent = current_path
    
    return folder_path


def get_document_folder_path(doctype, docname):

    doctype = doctype.strip()
    docname = docname.strip()
    
    if '/' in docname or '\\' in docname or '..' in docname:
        frappe.throw(f"❌ Invalid document name: {docname}")
    
    folder_path = f"Home/{doctype}/{docname}"
    
    ensure_folder_exists(folder_path)
    
    return folder_path


# ═══════════════════════════════════════════════════════════════════
# SECTION 3: Validation
# ═══════════════════════════════════════════════════════════════════

def validate_file_size(content, max_size_kb):

    file_size_kb = len(content) / 1024
    if file_size_kb > max_size_kb:
        return False, f"File too large ({file_size_kb:.1f}KB). Max: {max_size_kb}KB"
    return True, None


def validate_file_extension(filename, allowed_extensions):
    ext = os.path.splitext(filename)[1].lower()
    if ext not in allowed_extensions:
        return False, f"Type '{ext}' not allowed. Allowed: {', '.join(allowed_extensions)}"
    return True, None


def validate_image_integrity(content):
    try:
        image = Image.open(io.BytesIO(content))
        image.verify()
        image = Image.open(io.BytesIO(content))
        image.load()
        return True, None
    except Exception as e:
        return False, f"Invalid or corrupted image: {str(e)}"


def get_folder_config(folder):
    folder = folder.strip().lower() if folder else "pet"
    
    if folder not in FOLDER_CONFIG:
        available = ', '.join(FOLDER_CONFIG.keys())
        frappe.throw(f"❌ Unknown folder '{folder}'. Available: {available}")
    
    return FOLDER_CONFIG[folder]


def validate_file(filename, content, folder):
    config = get_folder_config(folder)
    
    valid, error = validate_file_extension(filename, config["allowed_extensions"])
    if not valid:
        return False, error
    
    valid, error = validate_file_size(content, config["max_size_kb"])
    if not valid:
        return False, error
    
    ext = os.path.splitext(filename)[1].lower()
    if ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp']:
        valid, error = validate_image_integrity(content)
        if not valid:
            return False, error
    
    return True, None


def is_file_duplicate(doctype, docname, filename, file_hash=None):
    """فحص التكرار"""
    if file_hash:
        if frappe.db.exists("File", {
            "content_hash": file_hash,
            "attached_to_doctype": doctype,
            "attached_to_name": docname
        }):
            return True
    
    if frappe.db.exists("File", {
        "file_name": filename,
        "attached_to_doctype": doctype,
        "attached_to_name": docname
    }):
        return True
    
    return False
# ═══════════════════════════════════════════════════════════════════
# SECTION 4: Pet Integration
# ═══════════════════════════════════════════════════════════════════

def update_pet_photos_table(pet_docname, file_url, make_default=False):
    """تحديث Pet Photos child table"""
    pet = frappe.get_doc("Pet", pet_docname)
    
    for row in (pet.get("photos") or []):
        if getattr(row, "pet_photo", None) == file_url:
            if make_default:
                for other_row in pet.get("photos"):
                    other_row.is_default = 0
                row.is_default = 1
            return
    
    if make_default:
        for row in (pet.get("photos") or []):
            row.is_default = 0
    
    pet.append("photos", {
        "pet_photo": file_url,
        "is_default": 1 if make_default else 0
    })
    
    if make_default:
        pet.custom_image = file_url
    
    pet.save(ignore_permissions=True)


# ═══════════════════════════════════════════════════════════════════
# SECTION 5: Main Upload API
# ═══════════════════════════════════════════════════════════════════

@frappe.whitelist()
def upload_files():
    
    files = frappe.request.files.getlist('files')
    doctype = frappe.form_dict.get('doctype')
    docname = frappe.form_dict.get('docname')
    folder = frappe.form_dict.get('folder')
    set_first_as_default = frappe.form_dict.get('set_first_as_default') == '1'
    
    # Validation
    if not files:
        frappe.throw("❌ No files uploaded")
    
    if not all([doctype, docname, folder]):
        frappe.throw("❌ Missing: doctype, docname, folder")
    
    if not frappe.db.exists(doctype, docname):
        frappe.throw(f"❌ {doctype} {docname} does not exist")
    
    config = get_folder_config(folder)
    
    folder_path = get_document_folder_path(doctype, docname)
    
    uploaded = []
    skipped = []
    
    for i, file in enumerate(files):
        filename = file.filename
        
        try:
            content = file.stream.read()
            
            valid, error = validate_file(filename, content, folder)
            if not valid:
                skipped.append({
                    "file_name": filename,
                    "reason": error,
                    "validation_failed": True
                })
                continue
            
            file_hash = hashlib.sha256(content).hexdigest()
            if is_file_duplicate(doctype, docname, filename, file_hash):
                skipped.append({
                    "file_name": filename,
                    "reason": "Duplicate (already exists)",
                    "validation_failed": False
                })
                continue
            
            file_doc = frappe.get_doc({
                "doctype": "File",
                "file_name": filename,
                "attached_to_doctype": doctype,
                "attached_to_name": docname,
                "folder": folder_path,
                "content": content,
                "is_private": 0,
                "content_hash": file_hash
            })
            file_doc.save(ignore_permissions=True)
            
            if doctype == "Pet":
                make_default = (i == 0 and set_first_as_default)
                update_pet_photos_table(docname, file_doc.file_url, make_default)
            
            uploaded.append({
                "file_name": filename,
                "file_url": file_doc.file_url,
                "file_size_kb": round(len(content) / 1024, 2),
                "folder": folder_path
            })
            
        except Exception as e:
            frappe.log_error(f"Upload error: {filename} - {str(e)}")
            skipped.append({
                "file_name": filename,
                "reason": f"Upload failed: {str(e)}",
                "validation_failed": False
            })
    
    frappe.db.commit()
    
    return {
        "success": True,
        "message": f"{len(uploaded)} uploaded, {len(skipped)} skipped",
        "uploaded": uploaded,
        "skipped": skipped,
        "folder": folder_path,
        "folder_config": config,
        "validation_summary": {
            "total": len(files),
            "successful": len(uploaded),
            "skipped": len(skipped),
            "validation_failed": len([s for s in skipped if s.get("validation_failed")])
        }
    }

# ═══════════════════════════════════════════════════════════════════
# SECTION 6: Helper APIs
# ═══════════════════════════════════════════════════════════════════

@frappe.whitelist()
def get_config():
    """GET /api/method/pet_app.api.file_utils.get_config"""
    return {
        "success": True,
        "folders": FOLDER_CONFIG
    }

@frappe.whitelist()
def create_base_folders():
    try:
        base_doctypes = ["Pet", "Product", "Guardian", "Clinic"]
        created = []
        
        for doctype in base_doctypes:
            try:
                folder_path = f"Home/{doctype}"
                ensure_folder_exists(folder_path)
                created.append(folder_path)
            except Exception as e:
                frappe.log_error(f"Error creating {doctype} folder: {str(e)}")
        
        frappe.db.commit()
        
        return {
            "success": True,
            "message": f"Created {len(created)} base folders",
            "folders": created
        }
    except Exception as e:
        return {
            "success": False,
            "message": str(e)
        }


# ═══════════════════════════════════════════════════════════════════
# SECTION 7: Hooks
# ═══════════════════════════════════════════════════════════════════

def create_document_folder_hook(doc, method=None):
   
    try:
        doctype = doc.doctype
        docname = doc.name
        
        folder_path = get_document_folder_path(doctype, docname)
        
        frappe.logger().info(f"✅ Auto-created folder: {folder_path}")
    
    except Exception as e:
        frappe.log_error(
            f"Hook error: {doc.doctype} {doc.name} - {str(e)}",
            "Document Folder Hook"
        )