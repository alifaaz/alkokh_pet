# Copyright (c) 2026, solvers and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

from pet_app.pet_app.doctype.pre_define_item.test_pre_define_item import (
	ARABIC_NAME,
	PARENT_GROUP,
	make_item_group,
	make_pre_item,
)
from pet_app.utils.staging_migration import migrate_each

# ERPNext test modules bootstrap master data at import time, which fails on a populated
# site; the tests below create their own groups, items and entries instead.
IGNORE_TEST_RECORD_DEPENDENCIES = ["Item", "Item Group", "UOM", "Warehouse", "Company", "Stock Entry", "Stock Entry Type"]


class TestPreDefineStockEntry(IntegrationTestCase):
	def setUp(self):
		self.suffix = frappe.generate_hash(length=6)
		self.company = frappe.defaults.get_global_default("company")
		self.assertTrue(self.company, "a default company is required for this test")
		self.warehouse = frappe.get_all(
			"Warehouse", filters={"is_group": 0, "company": self.company}, pluck="name", order_by="name", limit=1
		)[0]
		self.group_warehouse = frappe.get_all(
			"Warehouse", filters={"is_group": 1, "company": self.company}, pluck="name", limit=1
		)[0]
		self.pre_item = make_pre_item(
			item_code=f"STG-STE-{self.suffix}",
			english_name="Staging Stock Item",
			category=f"Staging Group {self.suffix}",
			valuation_rate=7,
		)

	def make_entry(self, **values):
		doc = frappe.get_doc(
			{
				"doctype": "Pre Define Stock Entry",
				"stock_entry_type": "Material Receipt",
				"company": self.company,
				"items": [{"pre_item": self.pre_item.name, "qty": 5}],
				**values,
			}
		)
		return doc.insert()

	def test_material_receipt_requires_target_warehouse(self):
		with self.assertRaises(frappe.ValidationError):
			self.make_entry()

	def test_material_issue_requires_source_warehouse(self):
		with self.assertRaises(frappe.ValidationError):
			self.make_entry(stock_entry_type="Material Issue", to_warehouse=self.warehouse)

	def test_group_warehouse_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.make_entry(to_warehouse=self.group_warehouse)

	def test_purpose_and_row_defaults_are_filled(self):
		doc = self.make_entry(to_warehouse=self.warehouse)

		self.assertTrue(doc.name.startswith("PRE-STE-"))
		self.assertEqual(doc.purpose, "Material Receipt")
		self.assertEqual(doc.status, "Draft")
		row = doc.items[0]
		self.assertEqual(row.t_warehouse, self.warehouse)
		self.assertFalse(row.s_warehouse)
		self.assertEqual(row.uom, "Nos")
		self.assertEqual(row.conversion_factor, 1)
		self.assertEqual(row.item_name, ARABIC_NAME)

	def test_migrate_creates_draft_stock_entry_and_migrates_items(self):
		doc = self.make_entry(
			to_warehouse=self.warehouse,
			posting_date="2026-01-15",
			items=[
				{
					"pre_item": self.pre_item.name,
					"qty": 5,
					"basic_rate": 12,
					"batch_no": "B-1",
					"expiry_date": "2027-01-01",
				}
			],
		)

		result = migrate_each("Pre Define Stock Entry", [doc.name])

		self.assertEqual(result["failed"], {})
		doc.reload()
		self.assertEqual(doc.status, "Migrated")
		self.assertTrue(doc.stock_entry)
		self.pre_item.reload()
		self.assertEqual(self.pre_item.status, "Migrated")
		self.assertEqual(doc.items[0].item_code, self.pre_item.item)

		entry = frappe.get_doc("Stock Entry", doc.stock_entry)
		self.assertEqual(entry.docstatus, 0)
		self.assertEqual(entry.stock_entry_type, "Material Receipt")
		self.assertEqual(entry.purpose, "Material Receipt")
		self.assertEqual(entry.company, self.company)
		self.assertEqual(str(entry.posting_date), "2026-01-15")
		self.assertEqual(len(entry.items), 1)
		row = entry.items[0]
		self.assertEqual(row.item_code, self.pre_item.item)
		self.assertEqual(row.qty, 5)
		self.assertEqual(row.uom, "Nos")
		self.assertEqual(row.t_warehouse, self.warehouse)
		self.assertEqual(row.basic_rate, 12)
		self.assertIn("B-1", entry.remarks)
		self.assertIn("2027-01-01", entry.remarks)

	def test_row_rate_falls_back_to_pre_item_valuation_rate(self):
		doc = self.make_entry(to_warehouse=self.warehouse)
		migrate_each("Pre Define Stock Entry", [doc.name])
		doc.reload()

		self.assertEqual(
			frappe.db.get_value("Stock Entry Detail", {"parent": doc.stock_entry}, "basic_rate"), 7
		)

	def test_second_migrate_is_a_noop(self):
		doc = self.make_entry(to_warehouse=self.warehouse)
		migrate_each("Pre Define Stock Entry", [doc.name])
		first = frappe.db.get_value("Pre Define Stock Entry", doc.name, "stock_entry")

		result = migrate_each("Pre Define Stock Entry", [doc.name])

		self.assertEqual(result["migrated"], [doc.name])
		self.assertEqual(frappe.db.get_value("Pre Define Stock Entry", doc.name, "stock_entry"), first)
		self.assertEqual(frappe.db.count("Stock Entry", {"remarks": ("like", f"%{doc.name}%")}), 0)

	def test_migrated_entry_is_locked(self):
		doc = self.make_entry(to_warehouse=self.warehouse)
		migrate_each("Pre Define Stock Entry", [doc.name])
		doc.reload()

		doc.remarks = "changed after migration"
		with self.assertRaises(frappe.ValidationError):
			doc.save()

	def test_item_failure_fails_the_entry_and_rolls_back(self):
		group_node = make_item_group(f"Node {self.suffix}", is_group=1)
		bad_item = make_pre_item(item_code=f"STG-BAD-{self.suffix}", category=group_node.name)
		doc = self.make_entry(to_warehouse=self.warehouse, items=[{"pre_item": bad_item.name, "qty": 1}])

		result = migrate_each("Pre Define Stock Entry", [doc.name])

		self.assertIn("group node", result["failed"][doc.name])
		doc.reload()
		self.assertEqual(doc.status, "Error")
		self.assertFalse(doc.stock_entry)
		self.assertFalse(frappe.db.exists("Item", f"STG-BAD-{self.suffix}"))
		self.assertEqual(frappe.db.get_value("Pre Define Item", bad_item.name, "status"), "Draft")
		self.assertEqual(PARENT_GROUP, frappe.db.get_value("Item Group", group_node.name, "parent_item_group"))
