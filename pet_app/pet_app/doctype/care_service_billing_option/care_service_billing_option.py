# Copyright (c) 2026, solvers and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr, flt

from pet_app.utils.price_list import get_veterinary_selling_price_list


class CareServiceBillingOption(Document):
	def validate(self):
		self._set_normalized_values()
		self._validate_weight_range()
		self._resolve_item()
		self._validate_item()
		self._sync_item_defaults()
		self._validate_duplicate_active_option()

	def _set_normalized_values(self):
		self.service_title = cstr(self.service_title).strip()
		self.option_label = cstr(self.option_label).strip()
		self.size_weight_label = cstr(self.size_weight_label).strip()
		self.animal_species = cstr(self.animal_species).strip()
		self.animal_type = cstr(self.get("animal_type")).strip()
		self.item_code = cstr(self.item_code).strip()
		if not self.category_care_services:
			frappe.throw(_("Parent Care Service is required."))
		if not self.service_title:
			frappe.throw(_("Service Name is required."))
		if not self.option_label:
			frappe.throw(_("Option Label is required."))
		if not self.animal_type:
			frappe.throw(_("Animal Type is required."))
		if self.default_rate in (None, ""):
			frappe.throw(_("Default Rate is required."))
		if flt(self.default_rate) < 0:
			frappe.throw(_("Default Rate must not be negative."))

	def _validate_weight_range(self):
		has_min = self.min_weight not in (None, "")
		has_max = self.max_weight not in (None, "")
		if has_min and has_max and flt(self.min_weight) > flt(self.max_weight):
			frappe.throw(_("Min Weight must be less than or equal to Max Weight."))

	def _resolve_item(self):
		if self.item_code:
			return

		match = find_matching_item(self.option_label)
		self.item_code = match or create_item_for_billing_option(self)

	def _validate_item(self):
		if not self.item_code:
			frappe.throw(_("Linked Item could not be resolved."))

		item = frappe.db.get_value("Item", self.item_code, ["name", "disabled", "is_sales_item", "is_stock_item"], as_dict=True)
		if not item:
			frappe.throw(_("Item {0} does not exist.").format(frappe.bold(self.item_code)))
		if cint(item.get("disabled")):
			frappe.throw(_("Item {0} is disabled.").format(frappe.bold(self.item_code)))
		if item.get("is_stock_item") is not None and cint(item.get("is_stock_item")):
			frappe.throw(_("Item {0} must be a non-stock service item.").format(frappe.bold(self.item_code)))

	def _sync_item_defaults(self):
		if not self.item_code:
			return

		item = frappe.get_doc("Item", self.item_code)
		changed = False

		full_name = f"{self.service_title} - {self.option_label}" if self.option_label else self.service_title
		if item.item_name != full_name:
			item.item_name = full_name
			changed = True

		description = build_item_description(self)
		if description and item.description != description:
			item.description = description
			changed = True

		if not item.item_group:
			item.item_group = get_billing_option_item_group(self)
			changed = True

		if not item.stock_uom:
			item.stock_uom = "Nos"
			ensure_uom(item.stock_uom)
			changed = True

		if item.get("is_sales_item") is not None and not cint(item.is_sales_item):
			item.is_sales_item = 1
			changed = True

		if flt(item.standard_rate) != flt(self.default_rate):
			item.standard_rate = flt(self.default_rate)
			changed = True

		if changed:
			item.flags.ignore_permissions = True
			item.save()

		upsert_item_price(self.item_code, flt(self.default_rate), get_billing_option_price_list(self))

	def _validate_duplicate_active_option(self):
		if cint(self.disabled):
			return

		existing = frappe.db.get_value(
			"Care Service Billing Option",
			{
				"category_care_services": self.category_care_services,
				"item_code": self.item_code,
				"disabled": 0,
				"name": ["!=", self.name],
			},
			"name",
		)
		if existing:
			frappe.throw(
				_("Active billing option {0} already links this parent care service to item {1}.").format(
					frappe.bold(existing),
					frappe.bold(self.item_code),
				)
			)


def find_matching_item(option_label: str, service_title: str = "") -> str | None:
	option_label = cstr(option_label).strip()
	service_title = cstr(service_title).strip()
	full_name = f"{service_title} - {option_label}" if option_label else service_title
	if not full_name:
		return None

	if frappe.db.exists("Item", full_name):
		return full_name

	exact_name_match = frappe.db.sql(
		"""
		select name
		from `tabItem`
		where disabled = 0 and lower(item_name) = lower(%s)
		order by modified desc
		limit 1
		""",
		(option_label,),
	)
	if exact_name_match:
		return exact_name_match[0][0]

	return None


def create_item_for_billing_option(doc: CareServiceBillingOption) -> str:
	ensure_uom("Nos")

	full_name = f"{doc.service_title} - {doc.option_label}" if doc.option_label else doc.service_title
	item = frappe.new_doc("Item")
	item.item_code = full_name
	item.item_name = full_name
	item.item_group = get_billing_option_item_group(doc)
	item.stock_uom = "Nos"
	item.is_stock_item = 0
	item.is_sales_item = 1
	item.is_purchase_item = 0
	item.description = build_item_description(doc)
	item.standard_rate = flt(doc.default_rate)
	item.flags.ignore_permissions = True
	item.insert()

	upsert_item_price(item.name, flt(doc.default_rate), get_billing_option_price_list(doc))
	return item.name


def build_item_description(doc: CareServiceBillingOption) -> str:
	service_name = frappe.db.get_value("CareService template", doc.category_care_services, "service_name") or doc.category_care_services
	parts = [
		_("Care Service: {0}").format(service_name) if service_name else None,
		_("Size / Weight: {0}").format(doc.size_weight_label) if doc.size_weight_label else None,
		_("Animal Species: {0}").format(doc.animal_species) if doc.animal_species else None,
		_("Animal Type: {0}").format(doc.animal_type) if doc.animal_type else None,
	]
	return "\n".join(part for part in parts if part)


def get_billing_option_item_group(doc: CareServiceBillingOption) -> str:
	parent_item = frappe.db.get_value("CareService template", doc.category_care_services, "item_code")
	if parent_item:
		parent_group = frappe.db.get_value("Item", parent_item, "item_group")
		if parent_group and frappe.db.exists("Item Group", parent_group):
			return parent_group

	for item_group in ("Veterinary Services", "Services", "All Item Groups"):
		if frappe.db.exists("Item Group", item_group):
			return item_group

	row = frappe.get_all("Item Group", pluck="name", limit=1)
	return row[0] if row else "All Item Groups"


def get_billing_option_price_list(doc: CareServiceBillingOption) -> str | None:
	parent_price_list = frappe.db.get_value("CareService template", doc.category_care_services, "price_list")
	if is_enabled_selling_price_list(parent_price_list):
		return parent_price_list
	return get_veterinary_selling_price_list()


def is_enabled_selling_price_list(price_list: str | None) -> bool:
	price_list = cstr(price_list).strip()
	if not price_list:
		return False

	row = frappe.db.get_value("Price List", price_list, ["name", "selling", "enabled"], as_dict=True)
	if not row:
		return False
	if row.get("selling") is not None and not cint(row.selling):
		return False
	if row.get("enabled") is not None and not cint(row.enabled):
		return False
	return True


def upsert_item_price(item_code: str, rate: float, price_list: str | None):
	if not item_code or not price_list:
		return

	existing = frappe.db.get_value(
		"Item Price",
		{"item_code": item_code, "price_list": price_list, "selling": 1},
		"name",
	)
	item_price = frappe.get_doc("Item Price", existing) if existing else frappe.new_doc("Item Price")
	item_price.item_code = item_code
	item_price.price_list = price_list
	item_price.selling = 1
	item_price.price_list_rate = flt(rate)
	item_price.flags.ignore_permissions = True
	item_price.save()


def ensure_uom(uom: str):
	uom = cstr(uom).strip()
	if not uom or frappe.db.exists("UOM", uom):
		return
	frappe.get_doc({"doctype": "UOM", "uom_name": uom}).insert(ignore_permissions=True)
