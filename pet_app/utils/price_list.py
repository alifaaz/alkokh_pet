from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, cstr


DEFAULT_VETERINARY_SELLING_PRICE_LIST = "Standard Selling"
VETERINARY_SELLING_PRICE_LIST_CONF_KEY = "pet_app_veterinary_selling_price_list"


def get_veterinary_selling_price_list() -> str:
	price_list = cstr(
		frappe.conf.get(VETERINARY_SELLING_PRICE_LIST_CONF_KEY)
		or DEFAULT_VETERINARY_SELLING_PRICE_LIST
	).strip()
	if not price_list:
		frappe.throw(_("Veterinary selling price list is required."))

	row = frappe.db.get_value("Price List", price_list, ["name", "selling", "enabled"], as_dict=True)
	if not row:
		frappe.throw(_("Price List {0} is required for veterinary invoices.").format(frappe.bold(price_list)))
	if not cint(row.selling):
		frappe.throw(_("Price List {0} must be a selling price list.").format(frappe.bold(price_list)))
	if not cint(row.enabled):
		frappe.throw(_("Price List {0} must be enabled.").format(frappe.bold(price_list)))
	return row.name
