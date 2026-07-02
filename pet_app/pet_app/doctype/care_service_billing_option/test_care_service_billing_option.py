# Copyright (c) 2026, solvers and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt


EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class IntegrationTestCareServiceBillingOption(IntegrationTestCase):
	"""Integration tests for Care Service Billing Option."""

	def test_frontend_query_shape(self):
		option = self._make_option()
		rows = frappe.get_all(
			"Care Service Billing Option",
			filters=[
				["Care Service Billing Option", "category_care_services", "=", option.category_care_services],
				["Care Service Billing Option", "disabled", "=", 0],
			],
			fields=[
				"name",
				"category_care_services",
				"service_title",
				"option_label",
				"animal_species",
				"animal_type",
				"size_weight_label",
				"min_weight",
				"max_weight",
				"item_code",
				"item_code.item_name",
				"default_rate",
				"disabled",
				"sort_order",
			],
			order_by="sort_order asc, modified desc",
		)

		self.assertTrue(rows)
		self.assertEqual(rows[0].category_care_services, option.category_care_services)
		self.assertEqual(rows[0].animal_type, option.animal_type)
		self.assertTrue(rows[0].item_code)
		self.assertTrue(rows[0].item_name)
		self.assertFalse(rows[0].disabled)

	def test_missing_item_autocreates_service_item_and_price(self):
		option = self._make_option(item_code=None, option_label=f"Autocreated Billing Item {frappe.generate_hash(length=8)}", default_rate=42)
		item = frappe.get_doc("Item", option.item_code)
		price_list = self._price_list()

		self.assertEqual(item.item_name, option.option_label)
		self.assertEqual(flt(item.standard_rate), 42)
		self.assertEqual(flt(item.is_stock_item), 0)
		self.assertEqual(flt(item.is_sales_item), 1)
		self.assertEqual(
			flt(frappe.db.get_value("Item Price", {"item_code": item.name, "price_list": price_list, "selling": 1}, "price_list_rate")),
			42,
		)

	def test_existing_item_is_updated_from_option(self):
		item = self._make_item("Existing Billing Item")
		option = self._make_option(item_code=item.name, option_label=f"Updated Billing Item {frappe.generate_hash(length=8)}", default_rate=55)
		item.reload()

		self.assertEqual(option.item_code, item.name)
		self.assertEqual(item.item_name, option.option_label)
		self.assertEqual(flt(item.standard_rate), 55)

	def test_duplicate_active_parent_item_is_rejected(self):
		option = self._make_option()
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{
					"doctype": "Care Service Billing Option",
					"category_care_services": option.category_care_services,
					"service_title": option.service_title,
					"option_label": f"Duplicate {option.option_label}",
					"animal_species": option.animal_species,
					"animal_type": option.animal_type,
					"size_weight_label": option.size_weight_label,
					"min_weight": flt(option.min_weight),
					"max_weight": flt(option.max_weight),
					"item_code": option.item_code,
					"default_rate": flt(option.default_rate),
					"disabled": 0,
				}
			).insert(ignore_permissions=True)

	def _make_option(self, item_code=None, option_label=None, default_rate=25):
		parent = self._make_parent_service()
		doc = frappe.get_doc(
			{
				"doctype": "Care Service Billing Option",
				"category_care_services": parent.name,
				"service_title": parent.service_name,
				"option_label": option_label or f"Billing Option {frappe.generate_hash(length=8)}",
				"animal_species": "Mammal",
				"animal_type": "Dog",
				"size_weight_label": "Dog 25",
				"min_weight": 0,
				"max_weight": 25,
				"item_code": item_code,
				"default_rate": default_rate,
				"disabled": 0,
			}
		)
		return doc.insert(ignore_permissions=True)

	def _make_parent_service(self):
		suffix = frappe.generate_hash(length=8)
		item = self._make_item(f"Parent Service Item {suffix}")
		category = frappe.get_doc(
			{
				"doctype": "CategoryCareServices",
				"category_name": f"Billing Option Category {suffix}",
			}
		).insert(ignore_permissions=True)
		return frappe.get_doc(
			{
				"doctype": "CareService template",
				"service_name": f"Billing Parent Service {suffix}",
				"animal_species": "Mammal",
				"frequency": "onetime",
				"category_id": category.name,
				"item_code": item.name,
				"default_price": 25,
				"price_list": self._price_list(),
			}
		).insert(ignore_permissions=True)

	def _make_item(self, label):
		suffix = frappe.generate_hash(length=8)
		return frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"{label} {suffix}",
				"item_name": f"{label} {suffix}",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 0,
				"is_sales_item": 1,
			}
		).insert(ignore_permissions=True, ignore_mandatory=True)

	def _price_list(self):
		for price_list in ("Clinic", "Standard Selling"):
			if frappe.db.exists("Price List", price_list):
				return price_list
		price_list = frappe.db.get_value("Price List", {"selling": 1, "enabled": 1}, "name")
		if not price_list:
			self.skipTest("No enabled selling price list exists")
		return price_list
