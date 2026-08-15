from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from pet_app.pet_app.doctype.product_category.product_category import (
	PRODUCT_CATEGORY_DOCTYPE,
	STORE_ROOT_CATEGORY,
	apply_product_category_to_product,
	get_store_root_category,
	resolve_product_category,
)


class TestProductCategoryIsolation(IntegrationTestCase):
	"""Product Category must never reshape the clinical Item Group tree.

	These replace the original suite, which asserted the mirror that was severed: that a
	new category creates an Item Group under "Pet Supplies", that a child category creates
	a child Item Group, and that deleting a category deletes its Item Group. All three
	behaviours are gone by design, so the tests now assert their absence.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_creating_a_category_does_not_create_an_item_group_under_pet_supplies(self):
		before = frappe.db.count("Item Group", {"parent_item_group": "Pet Supplies"})
		category = self._make_category()
		self.assertEqual(
			frappe.db.count("Item Group", {"parent_item_group": "Pet Supplies"}),
			before,
			"a storefront category must not add a child to the clinical root",
		)
		self.assertFalse(frappe.db.exists("Item Group", category.name))

	def test_child_category_does_not_create_a_child_item_group(self):
		parent = self._make_category(is_group=1)
		child = self._make_category(parent=parent.name)
		self.assertFalse(frappe.db.exists("Item Group", child.name))

	def test_renaming_a_category_does_not_rename_any_item_group(self):
		category = self._make_category()
		old_name = category.name
		new_name = f"{old_name} Renamed"
		frappe.rename_doc(PRODUCT_CATEGORY_DOCTYPE, old_name, new_name)
		self.assertFalse(frappe.db.exists("Item Group", new_name))
		self.assertFalse(frappe.db.exists("Item Group", old_name))

	def test_deleting_a_category_does_not_delete_any_item_group(self):
		item_group = self._make_item_group_under_pet_supplies()
		category = self._make_category()
		frappe.db.set_value(PRODUCT_CATEGORY_DOCTYPE, category.name, "item_group", item_group.name)
		frappe.delete_doc(PRODUCT_CATEGORY_DOCTYPE, category.name)
		self.assertFalse(frappe.db.exists(PRODUCT_CATEGORY_DOCTYPE, category.name))
		self.assertTrue(
			frappe.db.exists("Item Group", item_group.name),
			"deleting a category must never cascade into the Item Group tree",
		)

	def test_deleting_category_with_linked_product_is_blocked(self):
		category = self._make_category()
		self._make_product(category.name)
		with self.assertRaises(frappe.ValidationError):
			frappe.delete_doc(PRODUCT_CATEGORY_DOCTYPE, category.name)

	def test_saving_a_product_does_not_mint_a_category_from_an_item_group(self):
		"""The reverse writer is gone: an Item Group name is no longer a valid category."""
		item_group = self._make_item_group_under_pet_supplies()
		self.assertIsNone(resolve_product_category(item_group.name))

		product = frappe.get_doc(
			{
				"doctype": "Product",
				"product_name": f"Test Product {frappe.generate_hash(length=8)}",
				"sku": f"TEST-SKU-{frappe.generate_hash(length=8)}",
				"category": item_group.name,
				"status": "Draft",
				"price": 0,
				"has_variants": 0,
			}
		)
		with self.assertRaises(frappe.ValidationError):
			product.insert()
		self.assertFalse(frappe.db.exists(PRODUCT_CATEGORY_DOCTYPE, item_group.name))

	def test_product_takes_its_item_group_from_the_category_link(self):
		category = self._make_category()
		item_group = self._make_item_group_under_pet_supplies()
		frappe.db.set_value(PRODUCT_CATEGORY_DOCTYPE, category.name, "item_group", item_group.name)
		product = self._make_product(category.name)
		self.assertEqual(product.item_group, item_group.name)

	def test_apply_product_category_clears_item_group_when_no_category(self):
		doc = frappe._dict(category=None, item_group="Anything")
		self.assertIsNone(apply_product_category_to_product(doc))
		self.assertIsNone(doc.item_group)

	def test_store_root_resolves_to_the_declared_constant(self):
		if frappe.db.exists(PRODUCT_CATEGORY_DOCTYPE, STORE_ROOT_CATEGORY):
			self.assertEqual(get_store_root_category(), STORE_ROOT_CATEGORY)
		else:
			self.assertIsNone(get_store_root_category())

	def _make_category(self, parent=None, is_group=0):
		name = f"Test Product Category {frappe.generate_hash(length=8)}"
		doc = frappe.get_doc(
			{
				"doctype": PRODUCT_CATEGORY_DOCTYPE,
				"category_name": name,
				"parent_product_category": parent,
				"is_group": is_group,
				"enabled": 1,
			}
		)
		doc.insert()
		return doc

	def _make_item_group_under_pet_supplies(self):
		name = f"Test Clinical Item Group {frappe.generate_hash(length=8)}"
		doc = frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": name,
				"parent_item_group": "Pet Supplies",
				"is_group": 0,
			}
		)
		doc.insert()
		return doc

	def _make_product(self, category):
		product = frappe.get_doc(
			{
				"doctype": "Product",
				"product_name": f"Test Product {frappe.generate_hash(length=8)}",
				"sku": f"TEST-SKU-{frappe.generate_hash(length=8)}",
				"category": category,
				"status": "Draft",
				"price": 0,
				"has_variants": 0,
			}
		)
		product.insert()
		return product
