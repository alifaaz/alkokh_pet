from __future__ import annotations

import hashlib
import re

import frappe
from frappe import _
from frappe.utils import cint, cstr


MAX_UPLOAD_SIZE = 500 * 1024
FILE_REQUEST_INVALID = "file.request_invalid"


class MobileFileError(Exception):
	def __init__(self, code: str, message: str, http_status: int = 400):
		super().__init__(message)
		self.code = code
		self.message = message
		self.http_status = http_status


def _file_has_field(fieldname: str) -> bool:
	return bool(frappe.get_meta("File").has_field(fieldname))


def _safe_filename(name: str | None) -> str:
	filename = re.sub(r"[^\w\s.-]", "", cstr(name or "upload").strip())
	return filename or "upload"


def _ensure_folder(folder_path: str) -> str:
	if frappe.db.exists("File", {"name": folder_path, "is_folder": 1}):
		return folder_path

	parent = "Home"
	folder_name = folder_path.split("/")[-1]
	doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": folder_name,
			"folder": parent,
			"is_folder": 1,
		}
	)
	doc.insert(ignore_permissions=True)
	return folder_path


def get_first_uploaded_file():
	request = getattr(frappe, "request", None)
	files = getattr(request, "files", None)
	if not files:
		raise MobileFileError(FILE_REQUEST_INVALID, _("No file was uploaded."))

	uploaded = files.get("file")
	if uploaded:
		return uploaded

	for key in files:
		value = files.get(key)
		if value:
			return value
	raise MobileFileError(FILE_REQUEST_INVALID, _("No file was uploaded."))


def attach_public_image(doctype: str, docname: str, fieldname: str, uploaded_file, *, folder: str | None = None) -> dict:
	if not doctype or not docname or not frappe.db.exists(doctype, docname):
		raise MobileFileError(FILE_REQUEST_INVALID, _("Document was not found."), 404)

	meta = frappe.get_meta(doctype)
	if not meta.has_field(fieldname):
		raise MobileFileError(FILE_REQUEST_INVALID, _("{0} has no image field {1}.").format(doctype, fieldname))

	filename = _safe_filename(getattr(uploaded_file, "filename", None))
	content = uploaded_file.stream.read()
	if not content:
		raise MobileFileError(FILE_REQUEST_INVALID, _("Uploaded file is empty."))
	if len(content) > MAX_UPLOAD_SIZE:
		raise MobileFileError(FILE_REQUEST_INVALID, _("Uploaded file is too large. Maximum size is 500 KB."))

	sha1 = hashlib.sha1(content).hexdigest()
	file_values = {
		"doctype": "File",
		"file_name": filename,
		"content": content,
		"attached_to_doctype": doctype,
		"attached_to_name": docname,
		"attached_to_field": fieldname,
		"folder": _ensure_folder(folder or f"Home/{doctype}"),
		"is_private": 0,
	}
	if _file_has_field("custom_sha1_hash"):
		duplicate = frappe.db.get_value(
			"File",
			{
				"attached_to_doctype": doctype,
				"attached_to_name": docname,
				"custom_sha1_hash": sha1,
			},
			["name", "file_url", "file_name", "custom_is_default"],
			as_dict=True,
		)
		if duplicate:
			frappe.db.set_value(doctype, docname, fieldname, duplicate.file_url, update_modified=True)
			return {
				"id": duplicate.name,
				"name": duplicate.name,
				"file_name": duplicate.file_name,
				"file_url": duplicate.file_url,
				"is_default": bool(cint(duplicate.get("custom_is_default"))),
				"duplicate": True,
			}
		file_values["custom_sha1_hash"] = sha1
	if _file_has_field("custom_is_default"):
		file_values["custom_is_default"] = 1

	file_doc = frappe.get_doc(file_values)
	file_doc.insert(ignore_permissions=True)

	if _file_has_field("custom_is_default"):
		frappe.db.sql(
			"""
			UPDATE `tabFile`
			SET custom_is_default = 0
			WHERE attached_to_doctype = %s
				AND attached_to_name = %s
				AND name != %s
			""",
			(doctype, docname, file_doc.name),
		)
		frappe.db.set_value("File", file_doc.name, "custom_is_default", 1, update_modified=False)

	frappe.db.set_value(doctype, docname, fieldname, file_doc.file_url, update_modified=True)
	file_doc.reload()
	return {
		"id": file_doc.name,
		"name": file_doc.name,
		"file_name": file_doc.file_name,
		"file_url": file_doc.file_url,
		"is_private": bool(cint(file_doc.is_private)),
		"is_default": bool(cint(file_doc.get("custom_is_default"))) if _file_has_field("custom_is_default") else True,
		"duplicate": False,
	}
