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

import frappe

@frappe.whitelist()
def delete_multiple_files(file_names):
    """Delete multiple files at once"""
    import json
    
    # Convert string to list if needed
    if isinstance(file_names, str):
        file_names = json.loads(file_names)
    
    results = []
    for file_name in file_names:
        try:
            frappe.delete_doc("File", file_name, force=1)
            results.append({"file": file_name, "status": "deleted"})
        except Exception as e:
            results.append({"file": file_name, "status": "failed", "error": str(e)})
    
    frappe.db.commit()
    return {"message": "Files processed", "results": results}