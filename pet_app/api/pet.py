# import frappe
from frappe.model.document import Document
import frappe


@frappe.whitelist()
def upload_multiple_files():
	files = frappe.request.files.getlist('files')
	doctype = frappe.form_dict.get('doctype')
	docname = frappe.form_dict.get('docname')
	
	uploaded = []
	for file in files:
		file_doc = frappe.get_doc({
			"doctype": "File",
			"file_name": file.filename,
			"content": file.stream.read(),
			"attached_to_doctype": doctype,
			"attached_to_name": docname,
			"is_private": 0
		})
		file_doc.save()
		uploaded.append(file_doc.file_url)
	
	return uploaded

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