# import frappe
from frappe.model.document import Document
import frappe


# @frappe.whitelist()
# def upload_multiple_files():
# 	files = frappe.request.files.getlist('files')
# 	doctype = frappe.form_dict.get('doctype')
# 	docname = frappe.form_dict.get('docname')
	
# 	uploaded = []
# 	for file in files:
# 		file_doc = frappe.get_doc({
# 			"doctype": "File",
# 			"file_name": file.filename,
# 			"content": file.stream.read(),
# 			"attached_to_doctype": doctype,
# 			"attached_to_name": docname,
# 			"is_private": 0
# 		})
# 		file_doc.save()
# 		uploaded.append(file_doc.file_url)
	
# 	return uploaded

import frappe
from frappe import _

@frappe.whitelist()
def upload_multiple_files():
    """Upload multiple files with safety checks and duplicate prevention"""
    
    try:
        # 1. Check if files are provided
        if not frappe.request.files:
            frappe.throw(_("No files provided"))
        
        files = frappe.request.files.getlist('files')
        
        if not files or len(files) == 0:
            frappe.throw(_("No files found in request"))
        
        # 2. Get and validate doctype and docname
        doctype = frappe.form_dict.get('doctype')
        docname = frappe.form_dict.get('docname')
        
        if not doctype or not docname:
            frappe.throw(_("Missing required parameters: doctype and docname"))
        
        # 3. Check if doctype exists
        if not frappe.db.exists("DocType", doctype):
            frappe.throw(_("Invalid doctype: {0}").format(doctype))
        
        # 4. Check if document exists
        if not frappe.db.exists(doctype, docname):
            frappe.throw(_("Document {0} {1} does not exist").format(doctype, docname))
        
        # 5. Check user permissions
        if not frappe.has_permission(doctype, "write", docname):
            frappe.throw(_("No permission to attach files to {0} {1}").format(doctype, docname))
        
        # 6. Define allowed file types and max file size
        ALLOWED_EXTENSIONS = ['jpg', 'jpeg', 'png', 'gif', 'pdf', 'doc', 'docx', 'xls', 'xlsx', 'txt']
        MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB in bytes
        MAX_FILES = 20  # Maximum number of files per request
        
        # 7. Check number of files
        if len(files) > MAX_FILES:
            frappe.throw(_("Cannot upload more than {0} files at once").format(MAX_FILES))
        
        uploaded = []
        errors = []
        skipped = []
        
        for file in files:
            try:
                # 8. Check if file has a filename
                if not file.filename:
                    errors.append({"file": "unknown", "error": "File has no filename"})
                    continue
                
                # 9. Sanitize filename (remove special characters)
                import re
                safe_filename = re.sub(r'[^\w\s.-]', '', file.filename)
                
                # 10. Check for duplicate files
                existing_file = frappe.db.get_value("File", {
                    "file_name": safe_filename,
                    "attached_to_doctype": doctype,
                    "attached_to_name": docname
                }, ["name", "file_url"])
                
                if existing_file:
                    skipped.append({
                        "file_name": safe_filename,
                        "reason": "File already exists",
                        "existing_file_url": existing_file[1] if isinstance(existing_file, tuple) else None
                    })
                    continue
                
                # 11. Check file extension
                file_extension = file.filename.rsplit('.', 1)[-1].lower()
                if file_extension not in ALLOWED_EXTENSIONS:
                    errors.append({
                        "file": file.filename,
                        "error": f"File type .{file_extension} is not allowed"
                    })
                    continue
                
                # 12. Read file content and check size
                file_content = file.stream.read()
                file_size = len(file_content)
                
                if file_size == 0:
                    errors.append({"file": file.filename, "error": "File is empty"})
                    continue
                
                if file_size > MAX_FILE_SIZE:
                    errors.append({
                        "file": file.filename,
                        "error": f"File size ({file_size / 1024 / 1024:.2f} MB) exceeds maximum allowed size ({MAX_FILE_SIZE / 1024 / 1024} MB)"
                    })
                    continue
                
                # 13. Create file document
                file_doc = frappe.get_doc({
                    "doctype": "File",
                    "file_name": safe_filename,
                    "content": file_content,
                    "attached_to_doctype": doctype,
                    "attached_to_name": docname,
                    "is_private": 0
                })
                
                file_doc.insert(ignore_permissions=False)
                
                uploaded.append({
                    "file_name": file_doc.file_name,
                    "file_url": file_doc.file_url,
                    "name": file_doc.name,
                    "status": "success"
                })
                
            except Exception as e:
                errors.append({
                    "file": file.filename if file.filename else "unknown",
                    "error": str(e)
                })
        
        frappe.db.commit()
        
        # 14. Return detailed response
        return {
            "summary": {
                "total_files": len(files),
                "uploaded": len(uploaded),
                "skipped": len(skipped),
                "failed": len(errors)
            },
            "uploaded": uploaded,
            "skipped": skipped,
            "errors": errors
        }
        
    except Exception as e:
        frappe.log_error(f"Upload Multiple Files Error: {str(e)}")
        frappe.throw(_("Error uploading files: {0}").format(str(e)))

@frappe.whitelist()
def delete_multiple_files(file_names):
    """Delete multiple files at once with detailed status"""
    import json
    
    # Convert string to list if needed
    if isinstance(file_names, str):
        file_names = json.loads(file_names)
    
    results = []
    for file_name in file_names:
        try:
            # Check if file exists first
            if frappe.db.exists("File", file_name):
                frappe.delete_doc("File", file_name, force=1)
                results.append({
                    "file": file_name, 
                    "status": "deleted",
                    "message": "File deleted successfully"
                })
            else:
                results.append({
                    "file": file_name, 
                    "status": "not_found",
                    "message": "File does not exist"
                })
        except Exception as e:
            results.append({
                "file": file_name, 
                "status": "error",
                "message": str(e)
            })
    
    frappe.db.commit()
    
    # Summary
    deleted_count = len([r for r in results if r["status"] == "deleted"])
    not_found_count = len([r for r in results if r["status"] == "not_found"])
    error_count = len([r for r in results if r["status"] == "error"])
    
    return {
        "summary": {
            "total": len(file_names),
            "deleted": deleted_count,
            "not_found": not_found_count,
            "errors": error_count
        },
        "results": results
    }