from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from pet_app.patches import product_category_item_group_sync
from pet_app.pet_app.doctype.product_category.product_category import ROOT_ITEM_GROUP


class TestProductCategory(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		product_category_item_group_sync.execute()

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_root_category_creates_item_group_under_pet_supplies(self):
		category = self._make_category()
		self.assertTrue(category.item_group)
		self.assertEqual(
			frappe.db.get_value("Item Group", category.item_group, "parent_item_group"),
			ROOT_ITEM_GROUP,
		)

	def test_child_category_creates_child_item_group_under_parent_item_group(self):
		parent = self._make_category(is_group=1)
		child = self._make_category(parent=parent.name)
		self.assertEqual(
			frappe.db.get_value("Item Group", child.item_group, "parent_item_group"),
			parent.item_group,
		)

	def test_product_creation_with_product_category_uses_synced_item_group(self):
		category = self._make_category()
		product = self._make_product(category.name)
		self.assertEqual(product.item_group, category.item_group)
		self.assertEqual(
			frappe.db.get_value("Item", product.sku, "item_group"),
			category.item_group,
		)

	def test_product_creation_with_legacy_item_group_still_works(self):
		item_group = self._make_legacy_item_group()
		product = self._make_product(item_group.name)
		self.assertEqual(product.item_group, item_group.name)
		self.assertTrue(frappe.db.exists("Product Category", product.category))
		self.assertEqual(
			frappe.db.get_value("Product Category", product.category, "item_group"),
			item_group.name,
		)

	def test_deleting_category_with_linked_product_is_blocked(self):
		category = self._make_category()
		self._make_product(category.name)
		with self.assertRaises(frappe.ValidationError):
			frappe.delete_doc("Product Category", category.name)

	def test_deleting_unused_category_deletes_linked_item_group(self):
		category = self._make_category()
		item_group = category.item_group
		frappe.delete_doc("Product Category", category.name)
		self.assertFalse(frappe.db.exists("Product Category", category.name))
		self.assertFalse(frappe.db.exists("Item Group", item_group))

	def test_migration_can_run_twice_without_duplicate_category_item_group_links(self):
		product_category_item_group_sync.execute()
		product_category_item_group_sync.execute()
		duplicates = frappe.db.sql(
			"""
			SELECT COUNT(*)
			FROM (
				SELECT item_group, COUNT(*) AS duplicate_count
				FROM `tabProduct Category`
				WHERE IFNULL(item_group, '') != ''
				GROUP BY item_group
				HAVING duplicate_count > 1
			) duplicate_links
			"""
		)[0][0]
		self.assertEqual(duplicates, 0)

	def _make_category(self, parent=None, is_group=0):
		name = f"Test Product Category {frappe.generate_hash(length=8)}"
		doc = frappe.get_doc(
			{
				"doctype": "Product Category",
				"category_name": name,
				"parent_product_category": parent,
				"is_group": is_group,
				"enabled": 1,
			}
		)
		doc.insert()
		return doc

	def _make_legacy_item_group(self):
		name = f"Test Legacy Item Group {frappe.generate_hash(length=8)}"
		doc = frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": name,
				"parent_item_group": ROOT_ITEM_GROUP,
				"is_group": 0,
			}
		)
		doc.insert()
		return doc

	def _make_product(self, category):
		name = f"Test Product {frappe.generate_hash(length=8)}"
		sku = f"TEST-SKU-{frappe.generate_hash(length=8)}"
		product = frappe.get_doc(
			{
				"doctype": "Product",
				"product_name": name,
				"sku": sku,
				"category": category,
				"status": "Draft",
				"price": 0,
				"has_variants": 0,
			}
		)
		product.insert()
		return product
