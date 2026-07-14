from __future__ import annotations

import frappe


DEFAULT_COUNTRY = "Iraq"
DEFAULT_CURRENCY = "IQD"
DEFAULT_TIME_ZONE = "Asia/Baghdad"
PRICE_LISTS = ("Standard Buying", "Standard Selling")


def execute():
	ensure_iqd_defaults()


def ensure_iqd_defaults():
	ensure_currency()
	ensure_country()
	set_global_defaults()
	set_system_settings()
	set_company_defaults()
	set_price_list_currencies()
	set_item_price_currencies()
	remove_inr_currency_exchanges()
	frappe.clear_cache()


def ensure_currency():
	if frappe.db.exists("Currency", DEFAULT_CURRENCY):
		return
	frappe.get_doc(
		{
			"doctype": "Currency",
			"currency_name": DEFAULT_CURRENCY,
			"enabled": 1,
		}
	).insert(ignore_permissions=True)


def ensure_country():
	if frappe.db.exists("Country", DEFAULT_COUNTRY):
		return
	frappe.get_doc({"doctype": "Country", "country_name": DEFAULT_COUNTRY}).insert(ignore_permissions=True)


def set_global_defaults():
	if frappe.db.exists("DocType", "Global Defaults"):
		frappe.db.set_single_value("Global Defaults", "default_currency", DEFAULT_CURRENCY, update_modified=False)
	frappe.db.set_default("currency", DEFAULT_CURRENCY)
	frappe.db.set_default("country", DEFAULT_COUNTRY)


def set_system_settings():
	if not frappe.db.exists("DocType", "System Settings"):
		return

	values = {}
	meta = frappe.get_meta("System Settings")
	if meta.has_field("country"):
		values["country"] = DEFAULT_COUNTRY
	if meta.has_field("time_zone"):
		values["time_zone"] = DEFAULT_TIME_ZONE
	if values:
		frappe.db.set_value("System Settings", "System Settings", values, update_modified=False)


def set_company_defaults():
	if not frappe.db.exists("DocType", "Company"):
		return

	company_meta = frappe.get_meta("Company")
	for company in frappe.get_all("Company", pluck="name", ignore_permissions=True):
		values = {"default_currency": DEFAULT_CURRENCY}
		if company_meta.has_field("country"):
			values["country"] = DEFAULT_COUNTRY
		frappe.db.set_value("Company", company, values, update_modified=False)


def set_price_list_currencies():
	if not frappe.db.exists("DocType", "Price List"):
		return

	for price_list in PRICE_LISTS:
		if frappe.db.exists("Price List", price_list):
			frappe.db.set_value(
				"Price List",
				price_list,
				{"enabled": 1, "currency": DEFAULT_CURRENCY},
				update_modified=False,
			)


def set_item_price_currencies():
	if not frappe.db.exists("DocType", "Item Price"):
		return

	for row in frappe.get_all(
		"Item Price",
		fields=["name", "currency"],
		ignore_permissions=True,
	):
		if row.currency in {"INR", "", None}:
			frappe.db.set_value("Item Price", row.name, "currency", DEFAULT_CURRENCY, update_modified=False)


def remove_inr_currency_exchanges():
	if not frappe.db.exists("DocType", "Currency Exchange"):
		return

	for row in frappe.get_all(
		"Currency Exchange",
		fields=["name", "from_currency", "to_currency"],
		ignore_permissions=True,
	):
		if row.from_currency == "INR" or row.to_currency == "INR":
			frappe.delete_doc("Currency Exchange", row.name, ignore_permissions=True, force=True)
