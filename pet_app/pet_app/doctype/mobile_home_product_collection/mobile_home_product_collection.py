from __future__ import annotations

import json
import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr


class MobileHomeProductCollection(Document):
	def autoname(self):
		self.collection_id = _clean_id(self.collection_id or self.name)
		self.name = self.collection_id

	def validate(self):
		self.collection_id = _clean_id(self.collection_id or self.name)
		if not self.collection_id:
			frappe.throw(_("Collection ID is required."))
		self.title_en = cstr(self.title_en).strip()
		self.title_ar = cstr(self.title_ar).strip()
		if not self.title_en or not self.title_ar:
			frappe.throw(_("English and Arabic collection titles are required."))
		products = _parse_product_ids(self.product_ids)
		if cint(self.enabled) and not products:
			frappe.throw(_("Enabled mobile home collections need at least one product."))
		self.product_ids = json.dumps(products, ensure_ascii=False, indent=2)


def _clean_id(value) -> str:
	value = cstr(value).strip().lower()
	value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
	return value


def _parse_product_ids(value) -> list[str]:
	if not value:
		return []
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except json.JSONDecodeError:
			frappe.throw(_("Product IDs must be a JSON array."))
	if not isinstance(value, list):
		frappe.throw(_("Product IDs must be a JSON array."))
	result = []
	seen = set()
	for item in value:
		product = cstr(item).strip()
		if product and product not in seen:
			result.append(product)
			seen.add(product)
	return result
