# Copyright (c) 2026, solvers and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

from pet_app.utils.item_price import find_general_price_row
from pet_app.utils.price_list import get_buying_price_list, get_veterinary_selling_price_list

# ERPNext test modules bootstrap master data at import time, which fails on a populated
# site; these tests create the items they need instead.
IGNORE_TEST_RECORD_DEPENDENCIES = ["Item", "Item Group", "UOM", "Price List", "Item Price"]


class TestStockPrice(IntegrationTestCase):
	def setUp(self):
		self.selling = get_veterinary_selling_price_list()
		self.buying = get_buying_price_list()
		self.item = self._make_item()

	def _make_item(self) -> str:
		item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"STKPRC-{frappe.generate_hash(length=8)}",
				"item_name": "Stock Price Test Item",
				"item_group": frappe.get_all("Item Group", filters={"is_group": 0}, pluck="name", limit=1)[0],
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}
		).insert(ignore_permissions=True)
		return item.name

	def _make_doc(self, **row) -> "frappe.Document":
		doc = frappe.new_doc("Stock Price")
		doc.append("items", {"item_code": self.item, **row})
		return doc

	def test_price_lists_resolved_on_save(self):
		doc = self._make_doc(new_selling_rate=100)
		doc.insert(ignore_permissions=True)
		self.assertEqual(doc.selling_price_list, self.selling)
		self.assertEqual(doc.buying_price_list, self.buying)

	def test_blank_and_zero_both_skip(self):
		doc = self._make_doc(new_selling_rate=100, new_buying_rate=0)
		doc.insert(ignore_permissions=True)
		self.assertEqual(doc.items[0].selling_action, "Create")
		self.assertEqual(doc.items[0].buying_action, "Skipped")

	def test_submit_creates_exactly_one_row(self):
		doc = self._make_doc(new_selling_rate=100)
		doc.insert(ignore_permissions=True)
		doc.submit()

		rows = frappe.get_all(
			"Item Price", filters={"item_code": self.item, "price_list": self.selling}, pluck="name"
		)
		self.assertEqual(len(rows), 1)
		self.assertEqual(doc.items[0].selling_item_price, rows[0])
		self.assertEqual(doc.selling_changes, 1)

	def test_update_does_not_spawn_a_twin(self):
		first = self._make_doc(new_selling_rate=100)
		first.insert(ignore_permissions=True)
		first.submit()

		second = self._make_doc(new_selling_rate=250)
		second.insert(ignore_permissions=True)
		second.submit()

		rows = frappe.get_all(
			"Item Price", filters={"item_code": self.item, "price_list": self.selling}, pluck="name"
		)
		self.assertEqual(len(rows), 1, "updating a price must reuse its row, not add a second one")
		self.assertEqual(find_general_price_row(self.item, self.selling).price_list_rate, 250)
		self.assertEqual(second.items[0].previous_selling_rate, 100)

	def test_cancel_deletes_a_created_row(self):
		doc = self._make_doc(new_selling_rate=100)
		doc.insert(ignore_permissions=True)
		doc.submit()
		doc.cancel()

		self.assertIsNone(find_general_price_row(self.item, self.selling))

	def test_cancel_restores_a_replaced_rate(self):
		first = self._make_doc(new_selling_rate=100)
		first.insert(ignore_permissions=True)
		first.submit()

		second = self._make_doc(new_selling_rate=250)
		second.insert(ignore_permissions=True)
		second.submit()
		second.cancel()

		self.assertEqual(find_general_price_row(self.item, self.selling).price_list_rate, 100)

	def test_cancel_refuses_when_the_price_moved_since(self):
		doc = self._make_doc(new_selling_rate=100)
		doc.insert(ignore_permissions=True)
		doc.submit()

		item_price = frappe.get_doc("Item Price", doc.items[0].selling_item_price)
		item_price.price_list_rate = 999
		item_price.save(ignore_permissions=True)

		self.assertRaises(frappe.ValidationError, doc.cancel)

	def test_duplicate_item_rows_are_refused(self):
		doc = self._make_doc(new_selling_rate=100)
		doc.append("items", {"item_code": self.item, "new_selling_rate": 200})
		self.assertRaises(frappe.ValidationError, doc.insert)

	def test_a_draft_of_unchanged_rows_saves_but_will_not_submit(self):
		doc = self._make_doc(new_selling_rate=100)
		doc.insert(ignore_permissions=True)
		doc.submit()

		repeat = self._make_doc(new_selling_rate=100)
		repeat.insert(ignore_permissions=True)  # saving an all-Unchanged draft is fine
		self.assertEqual(repeat.items[0].selling_action, "Unchanged")
		self.assertRaises(frappe.ValidationError, repeat.submit)
