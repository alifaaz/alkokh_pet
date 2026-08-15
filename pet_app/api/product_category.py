from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, cstr

from pet_app.pet_app.doctype.product_category.product_category import (
	PRODUCT_CATEGORY_DOCTYPE,
)
from pet_app.api.response import standardize_response


@frappe.whitelist(allow_guest=False)
@standardize_response
def list_product_categories(parent=None, search=None, enabled_only=1):
	_require_doctype_permission(PRODUCT_CATEGORY_DOCTYPE, "read")

	filters = {}
	if cint(enabled_only):
		filters["enabled"] = 1
	if parent is not None:
		parent = cstr(parent).strip()
		filters["parent_product_category"] = parent or ["in", ["", None]]

	or_filters = None
	if search:
		search = cstr(search).strip()
		or_filters = [
			["Product Category", "name", "like", f"%{search}%"],
			["Product Category", "category_name", "like", f"%{search}%"],
			["Product Category", "description", "like", f"%{search}%"],
		]

	query = {
		"filters": filters,
		"fields": [
			"name",
			"category_name",
			"parent_product_category",
			"is_group",
			"enabled",
			"image",
			"description",
			"display_order",
			"item_group",
			"lft",
			"rgt",
		],
		"order_by": "lft asc, display_order asc, category_name asc",
	}
	if or_filters:
		query["or_filters"] = or_filters

	return frappe.get_all(PRODUCT_CATEGORY_DOCTYPE, **query)


@frappe.whitelist(allow_guest=False)
@standardize_response
def get_product_category(name):
	name = _require_name(name)
	doc = frappe.get_doc(PRODUCT_CATEGORY_DOCTYPE, name)
	_require_doc_permission(doc, "read")
	return _category_to_dict(doc)


@frappe.whitelist(allow_guest=False)
@standardize_response
def save_product_category(data=None, **kwargs):
	payload = _coerce_payload(data, kwargs)
	name = cstr(payload.get("name") or "").strip()
	category_name = cstr(payload.get("category_name") or payload.get("categoryName") or "").strip()

	if name and frappe.db.exists(PRODUCT_CATEGORY_DOCTYPE, name):
		doc = frappe.get_doc(PRODUCT_CATEGORY_DOCTYPE, name)
		_require_doc_permission(doc, "write")
		if category_name and category_name != doc.name:
			name = frappe.rename_doc(PRODUCT_CATEGORY_DOCTYPE, doc.name, category_name)
			doc = frappe.get_doc(PRODUCT_CATEGORY_DOCTYPE, name)
	else:
		if not category_name:
			frappe.throw(_("Category Name is required."))
		_require_doctype_permission(PRODUCT_CATEGORY_DOCTYPE, "create")
		doc = frappe.new_doc(PRODUCT_CATEGORY_DOCTYPE)
		doc.category_name = category_name

	_apply_payload(doc, payload)
	doc.save()

	return _category_to_dict(doc)


@frappe.whitelist(allow_guest=False)
@standardize_response
def delete_product_category(name):
	name = _require_name(name)
	doc = frappe.get_doc(PRODUCT_CATEGORY_DOCTYPE, name)
	_require_doc_permission(doc, "delete")
	frappe.delete_doc(PRODUCT_CATEGORY_DOCTYPE, name)
	return {"deleted": name}


def _apply_payload(doc, payload):
	field_map = {
		"parent": "parent_product_category",
		"parent_product_category": "parent_product_category",
		"is_group": "is_group",
		"enabled": "enabled",
		"image": "image",
		"description": "description",
		"display_order": "display_order",
	}
	for source, target in field_map.items():
		if source not in payload:
			continue
		value = payload.get(source)
		if target in {"is_group", "enabled", "display_order"}:
			value = cint(value)
		doc.set(target, value)


def _category_to_dict(doc):
	data = doc.as_dict(no_nulls=False)
	data["child_count"] = frappe.db.count(PRODUCT_CATEGORY_DOCTYPE, {"parent_product_category": doc.name})
	data["product_count"] = frappe.db.count("Product", {"category": doc.name})
	return data


# _require_write_permissions is gone. It demanded Item Group create/write/delete for
# every Product Category save, a leftover from when saving a category wrote an Item
# Group. Nothing here touches Item Group any more, and the demand locked out roles that
# hold full CRUD on Product Category but only read on Item Group. Product Category
# permission is now the whole rule.


def _require_doctype_permission(doctype, ptype, name=None):
	if name:
		doc = frappe.get_doc(doctype, name)
		if doc.has_permission(ptype):
			return
	elif frappe.has_permission(doctype, ptype=ptype):
		return
	frappe.throw(_("Not permitted"), frappe.PermissionError)


def _require_doc_permission(doc, ptype):
	if doc.has_permission(ptype):
		return
	frappe.throw(_("Not permitted"), frappe.PermissionError)


def _require_name(name):
	name = cstr(name).strip()
	if not name:
		frappe.throw(_("Product Category name is required."))
	if not frappe.db.exists(PRODUCT_CATEGORY_DOCTYPE, name):
		frappe.throw(_("Product Category {0} does not exist.").format(frappe.bold(name)))
	return name


def _coerce_payload(data, kwargs):
	payload = {}
	if isinstance(data, str) and data.strip():
		payload.update(json.loads(data))
	elif isinstance(data, dict):
		payload.update(data)
	payload.update(kwargs or {})
	return payload
