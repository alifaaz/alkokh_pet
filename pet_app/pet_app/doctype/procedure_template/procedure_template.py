# Copyright (c) 2026, solvers and contributors
from __future__ import annotations
import frappe
from frappe import _
from frappe.utils import flt, cint, cstr
from frappe.model.document import Document


class ProcedureTemplate(Document):

	def validate(self):
		self.procedure_name = cstr(self.procedure_name).strip()
		self.code = cstr(self.code).strip()
		self.species = cstr(self.species).strip()
		self.category = cstr(self.category).strip()
		if not self.procedure_name:
			frappe.throw(_("Procedure Name is required."))
		if not self.default_duration_minutes:
			self.default_duration_minutes = 30
		if cint(self.default_duration_minutes) < 0:
			frappe.throw(_("Default Duration Minutes must be zero or greater."))
		if self.consent_required and not self.consent_template:
			self.consent_template = frappe.db.get_value(
				"Pet Consent Template",
				{"procedure_template": self.name, "active": 1},
				"name",
			)
		self._validate_duplicate_code()
		self._validate_duplicate_name_species()
		self._ensure_item()

	def after_insert(self):
		self._ensure_item_price()

	def on_update(self):
		self._ensure_item_price()

	def _ensure_item(self):
		if self.item_code and frappe.db.exists("Item", self.item_code):
			item = frappe.get_doc("Item", self.item_code)
			if item.item_name != self.procedure_name:
				item.item_name = self.procedure_name
				item.flags.ignore_permissions = True
				item.save()
			return
		if frappe.db.exists("Item Group", "Veterinary Services"):
			item_group = "Veterinary Services"
		elif frappe.db.exists("Item Group", "Services"):
			item_group = "Services"
		else:
			frappe.throw(_("Create an Item Group named Services or Veterinary Services first."))
		item = frappe.new_doc("Item")
		item.item_code = self.procedure_name
		item.item_name = self.procedure_name
		item.item_group = item_group
		item.stock_uom = "Nos"
		item.is_stock_item = 0
		item.flags.ignore_permissions = True
		item.insert()
		self.item_code = item.name

	def _ensure_item_price(self):
		rate = flt(self.price)
		item_code = self.item_code
		price_list = "Standard Selling"
		currency = frappe.defaults.get_global_default("currency") or "IQD"
		if rate <= 0 or not item_code:
			return
		filters = {"item_code": item_code, "price_list": price_list, "selling": 1}
		existing_prices = frappe.get_all("Item Price", filters=filters, pluck="name")
		if len(existing_prices) > 1:
			return
		existing = existing_prices[0] if existing_prices else None
		ip = frappe.get_doc("Item Price", existing) if existing else frappe.new_doc("Item Price")
		if not existing:
			ip.item_code = item_code
			ip.price_list = price_list
			ip.selling = 1
		ip.price_list_rate = rate
		ip.currency = currency
		ip.flags.ignore_permissions = True
		ip.save() if existing else ip.insert()

	def _validate_duplicate_code(self):
		if not self.code:
			return
		if frappe.db.exists("Procedure Template", {"code": self.code, "name": ["!=", self.name]}):
			frappe.throw(_("Procedure Template Code {0} is already in use.").format(frappe.bold(self.code)))

	def _validate_duplicate_name_species(self):
		filters = {
			"procedure_name": self.procedure_name,
			"species": self.species,
			"name": ["!=", self.name],
		}
		if frappe.db.exists("Procedure Template", filters):
			frappe.throw(_("A Procedure Template already exists for this name and species."))