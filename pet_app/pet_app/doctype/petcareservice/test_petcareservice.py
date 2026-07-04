# Copyright (c) 2025, solvers and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import nowdate


class TestPetCareService(FrappeTestCase):
	"""Tests for PetCareService controller behavior."""

	def test_barcode_defaults_to_document_name_on_create(self):
		service = self._make_service()

		self.assertEqual(service.barcode, service.name)
		self.assertEqual(frappe.db.get_value("PetCareService", service.name, "barcode"), service.name)

	def test_manual_barcode_is_preserved_on_create(self):
		service = self._make_service(barcode="MANUAL-PET-SERVICE-BARCODE")

		self.assertEqual(service.barcode, "MANUAL-PET-SERVICE-BARCODE")
		self.assertEqual(
			frappe.db.get_value("PetCareService", service.name, "barcode"),
			"MANUAL-PET-SERVICE-BARCODE",
		)

	def _make_service(self, **overrides):
		guardian, pet = self._make_guardian_pet()
		category = self._make_category()
		doc = frappe.get_doc(
			{
				"doctype": "PetCareService",
				"pet_service_name": f"Barcode Test Service {frappe.generate_hash(length=8)}",
				"pet_id": pet.name,
				"guardian_id": guardian.name,
				"category": category.name,
				"due_date": nowdate(),
				"status": "pending",
			}
		)
		doc.update(overrides)
		return doc.insert(ignore_permissions=True)

	def _make_guardian_pet(self):
		suffix = frappe.generate_hash(length=8)
		digits = "".join(ch for ch in suffix if ch.isdigit()).ljust(9, "0")[:9]
		guardian = frappe.get_doc(
			{
				"doctype": "Guardian",
				"phone": f"07{digits}",
				"full_name": f"Barcode Guardian {suffix}",
				"email_id": f"barcode.guardian.{suffix}@example.com",
			}
		).insert(ignore_permissions=True)
		pet = frappe.get_doc(
			{
				"doctype": "Pet",
				"pet_name": f"Barcode Pet {suffix}",
				"animal_species": "Mammal",
				"animal_type": "Dog",
				"pet_status": "Approved",
			}
		).insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "PetGuardian",
				"pet_id": pet.name,
				"guardian_id": guardian.name,
				"role": "primary_owner",
			}
		).insert(ignore_permissions=True)
		return guardian, pet

	def _make_category(self):
		return frappe.get_doc(
			{
				"doctype": "CategoryCareServices",
				"category_name": f"Barcode Category {frappe.generate_hash(length=8)}",
			}
		).insert(ignore_permissions=True)
