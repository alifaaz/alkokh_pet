# Copyright (c) 2026, solvers and contributors
# For license information, please see license.txt

import frappe
from frappe.tests.utils import FrappeTestCase


class TestMedication(FrappeTestCase):
	def test_create_medication_auto_links_or_creates_item(self):
		medication_name = "Test Medication Auto Item"
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

