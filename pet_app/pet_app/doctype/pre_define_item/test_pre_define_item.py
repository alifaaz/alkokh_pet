# Copyright (c) 2026, solvers and contributors
# For license information, please see license.txt

import json

import frappe
from frappe.tests import IntegrationTestCase

from pet_app.pet_app.doctype.pre_define_item.pre_define_item import migrate_pre_define_items
from pet_app.utils.staging_migration import migrate_each

# ERPNext test modules bootstrap master data at import time, which fails on a populated
# site; the tests below create their own groups, items and entries instead.
IGNORE_TEST_RECORD_DEPENDENCIES = ["Item", "Item Group", "UOM", "Warehouse", "Company", "Stock Entry", "Stock Entry Type"]

PARENT_GROUP = "Pet Supplies"
ARABIC_NAME = "صنف اختبار"


def make_pre_item(**values):
	doc = frappe.get_doc(
		{
			"doctype": "Pre Define Item",
			"arabic_name": ARABIC_NAME,
			"stock_uom": "Nos",
			"parent_item_group": PARENT_GROUP,
			**values,
		}
	)
	return doc.insert()


def make_item_group(name, is_group=0):
	return frappe.get_doc(
		{
			"doctype": "Item Group",
			"item_group_name": name,
			"parent_item_group": PARENT_GROUP,
			"is_group": is_group,
		}
	).insert()


class TestPreDefineItem(IntegrationTestCase):
	def setUp(self):
		self.suffix = frappe.generate_hash(length=6)
		self.code = f"STG-{self.suffix}"
		self.category = f"Staging Group {self.suffix}"

	def test_duplicate_item_code_is_refused(self):
		make_pre_item(item_code=self.code)
		with self.assertRaises(frappe.DuplicateEntryError):
			make_pre_item(item_code=self.code)

	def test_effective_code_falls_back_to_names(self):
		self.assertEqual(make_pre_item(english_name="Only English").effective_code(), "Only English")
		self.assertEqual(make_pre_item().effective_code(), ARABIC_NAME)

	def test_migrate_creates_group_and_item_with_arabic_names(self):
		doc = make_pre_item(
			item_code=self.code,
			english_name="Staging Test Item",
			category=self.category,
			group_arabic="مجموعة اختبار",
			barcode=f"BC{self.suffix}",
		)

		result = migrate_each("Pre Define Item", [doc.name])

		self.assertEqual(result["failed"], {})
		self.assertEqual(result["migrated"], [doc.name])
		doc.reload()
		self.assertEqual(doc.status, "Migrated")
		self.assertEqual(doc.item, self.code)
		self.assertEqual(doc.item_group, self.category)

		item = frappe.get_doc("Item", doc.item)
		self.assertEqual(item.item_name, "Staging Test Item")
		self.assertEqual(item.arabic_name, ARABIC_NAME)
		self.assertEqual(item.item_group, self.category)
		self.assertEqual(item.stock_uom, "Nos")
		self.assertEqual(item.custom_barcode, f"BC{self.suffix}")
		self.assertTrue(item.is_stock_item)

		group = frappe.db.get_value(
			"Item Group", self.category, ["parent_item_group", "is_group", "arabic_name"], as_dict=True
		)
		self.assertEqual(group.parent_item_group, PARENT_GROUP)
		self.assertEqual(group.is_group, 0)
		self.assertEqual(group.arabic_name, "مجموعة اختبار")

	def test_item_name_falls_back_to_arabic(self):
		doc = make_pre_item(item_code=self.code, category=self.category)
		migrate_each("Pre Define Item", [doc.name])
		self.assertEqual(frappe.db.get_value("Item", self.code, "item_name"), ARABIC_NAME)

	def test_second_migrate_is_a_noop(self):
		doc = make_pre_item(item_code=self.code, category=self.category)
		migrate_each("Pre Define Item", [doc.name])

		result = migrate_each("Pre Define Item", [doc.name])

		self.assertEqual(result["migrated"], [doc.name])
		self.assertEqual(frappe.db.count("Item", {"item_code": self.code}), 1)

	def test_existing_item_is_linked_not_duplicated(self):
		leaf = make_item_group(f"Leaf {self.suffix}")
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": self.code,
				"item_name": "Existing",
				"item_group": leaf.name,
				"stock_uom": "Nos",
			}
		).insert()
		doc = make_pre_item(item_code=self.code, category=self.category, arabic_name="موجود")

		result = migrate_each("Pre Define Item", [doc.name])

		self.assertEqual(result["failed"], {})
		doc.reload()
		self.assertEqual(doc.item, self.code)
		self.assertEqual(frappe.db.get_value("Item", self.code, "item_name"), "Existing")
		self.assertEqual(frappe.db.get_value("Item", self.code, "arabic_name"), "موجود")
		self.assertEqual(frappe.db.count("Item", {"item_code": self.code}), 1)

	def test_migrated_row_is_locked(self):
		doc = make_pre_item(item_code=self.code, category=self.category)
		migrate_each("Pre Define Item", [doc.name])
		doc.reload()

		doc.english_name = "Changed after migration"
		with self.assertRaises(frappe.ValidationError):
			doc.save()

	def test_failure_is_isolated_and_recorded(self):
		group_node = make_item_group(f"Node {self.suffix}", is_group=1)
		bad = make_pre_item(item_code=f"STG-BAD-{self.suffix}", category=group_node.name)
		good = make_pre_item(item_code=self.code, category=self.category)

		result = migrate_each("Pre Define Item", [bad.name, good.name])

		self.assertEqual(result["migrated"], [good.name])
		self.assertIn("group node", result["failed"][bad.name])
		self.assertFalse(frappe.db.exists("Item", f"STG-BAD-{self.suffix}"))
		self.assertEqual(frappe.db.get_value("Pre Define Item", good.name, "status"), "Migrated")
		bad.reload()
		self.assertEqual(bad.status, "Error")
		self.assertIn("group node", bad.migration_error)

		# a manual save of a failed row is a retry request
		bad.category = self.category
		bad.save()
		self.assertEqual(bad.status, "Draft")
		self.assertFalse(bad.migration_error)

	def test_whitelisted_entry_point_accepts_json(self):
		doc = make_pre_item(item_code=self.code, category=self.category)

		result = migrate_pre_define_items(json.dumps([doc.name]))

		self.assertEqual(result["migrated"], [doc.name])
