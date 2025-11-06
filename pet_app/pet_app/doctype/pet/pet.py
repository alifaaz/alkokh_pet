# Copyright (c) 2025, solvers and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document
import frappe


class Pet(Document):
	pass


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