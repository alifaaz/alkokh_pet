# Copyright (c) 2026, solvers and contributors
# For license information, please see license.txt

import frappe
from frappe.tests.utils import FrappeTestCase


class TestMedication(FrappeTestCase):
	def test_create_medication_auto_links_or_creates_item(self):
		medication_name = "Test Medication Auto Item"
		self._ensure_uom("Tablet")
		if frappe.db.exists("Medication", medication_name):
			frappe.delete_doc("Medication", medication_name, force=1)
		if frappe.db.exists("Item", medication_name):
			frappe.delete_doc("Item", medication_name, force=1)

		doc = frappe.get_doc(
			{
				"doctype": "Medication",
				"medication_name": medication_name,
				"default_price": 25,
				"dosage_form_or_unit": "Tablet",
			}
		).insert()

		self.assertEqual(doc.name, medication_name)
		self.assertEqual(doc.linked_item, medication_name)
		self.assertTrue(frappe.db.exists("Item", medication_name))


	def test_medication_unit_and_item_group_sync_to_linked_item(self):
		medication_name = "Test Medication Item Sync"
		item_group = self._ensure_item_group("Test Medication Sync Group")

		for doctype in ("Medication", "Item"):
			if frappe.db.exists(doctype, medication_name):
				frappe.delete_doc(doctype, medication_name, force=1)

		doc = frappe.get_doc(
			{
				"doctype": "Medication",
				"medication_name": medication_name,
				"dosage_form_or_unit": "Nos",
				"item_group": item_group,
			}
		).insert()

		self.assertEqual(frappe.db.get_value("Item", doc.linked_item, "stock_uom"), "Nos")
		self.assertEqual(frappe.db.get_value("Item", doc.linked_item, "item_group"), item_group)

		updated_group = self._ensure_item_group("Test Medication Updated Group")
		self._ensure_uom("Unit")
		doc.dosage_form_or_unit = "Unit"
		doc.item_group = updated_group
		doc.save()

		self.assertEqual(frappe.db.get_value("Item", doc.linked_item, "stock_uom"), "Nos")
		self.assertEqual(doc.dosage_form_or_unit, "Nos")
		self.assertEqual(frappe.db.get_value("Item", doc.linked_item, "item_group"), updated_group)

		item = frappe.get_doc("Item", doc.linked_item)
		if not any(row.uom == "Unit" for row in item.get("uoms") or []):
			item.append("uoms", {"uom": "Unit", "conversion_factor": 1})
		item.stock_uom = "Unit"
		item.save(ignore_permissions=True)
		doc.reload()
		doc.save()

		self.assertEqual(frappe.db.get_value("Item", doc.linked_item, "stock_uom"), "Unit")
		self.assertEqual(doc.dosage_form_or_unit, "Unit")

	def test_linked_item_hydrates_medication_unit_and_item_group(self):
		item_group = self._ensure_item_group("Test Medication Hydrate Group")
		item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": "Test Medication Hydrate Item",
				"item_name": "Test Medication Hydrate Item",
				"item_group": item_group,
				"stock_uom": "Nos",
			}
		)
		if not frappe.db.exists("Item", item.item_code):
			item.insert(ignore_permissions=True)

		medication_name = "Test Medication Hydrate"
		if frappe.db.exists("Medication", medication_name):
			frappe.delete_doc("Medication", medication_name, force=1)

		doc = frappe.get_doc(
			{
				"doctype": "Medication",
				"medication_name": medication_name,
				"linked_item": "Test Medication Hydrate Item",
			}
		).insert()

		self.assertEqual(doc.dosage_form_or_unit, "Nos")
		self.assertEqual(doc.item_group, item_group)

	def _ensure_uom(self, name):
		if not frappe.db.exists("UOM", name):
			frappe.get_doc({"doctype": "UOM", "uom_name": name}).insert(ignore_permissions=True)
		return name

	def _ensure_item_group(self, name):
		if not frappe.db.exists("Item Group", name):
			frappe.get_doc(
				{
					"doctype": "Item Group",
					"item_group_name": name,
					"parent_item_group": "All Item Groups",
					"is_group": 0,
				}
			).insert(ignore_permissions=True)
		return name
