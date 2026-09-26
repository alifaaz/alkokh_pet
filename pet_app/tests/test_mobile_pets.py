from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from pet_app.api.mobile import pets


class TestMobilePets(FrappeTestCase):
	def test_pet_payload_uses_mobile_dto_keys(self):
		row = frappe._dict(
			{
				"name": "PET-TEST",
				"pet_name": "Luna",
				"animal_species": "Mammal",
				"animal_type": "Cat",
				"breed": "Domestic",
				"gender": "Female",
				"weight": 4.2,
				"pet_image": "/files/luna.png",
				"pet_status": "Approved",
				"is_deceased": 0,
				"death_date": None,
			}
		)

		payload = pets._pet_payload(row)

		self.assertEqual(payload["id"], "PET-TEST")
		self.assertEqual(payload["name"], "Luna")
		self.assertEqual(payload["species"], "Mammal")
		self.assertEqual(payload["type"], "Cat")
		self.assertEqual(payload["image"], "/files/luna.png")
		self.assertEqual(payload["pet_status"], "Approved")
		self.assertFalse(payload["is_disabled"])
		self.assertFalse(payload["is_deceased"])

	def test_pet_payload_marks_archived_pet_disabled(self):
		row = frappe._dict(
			{
				"name": "PET-TEST",
				"pet_name": "Luna",
				"pet_status": "Archived",
			}
		)

		payload = pets._pet_payload(row)

		self.assertTrue(payload["is_disabled"])

	def test_pet_updates_accept_mobile_aliases_only(self):
		updates = pets._pet_updates(
			{
				"name": "Luna",
				"species": "Mammal",
				"type": "Cat",
				"height": 22,
				"pet_status": "Archived",
			}
		)

		self.assertEqual(updates["pet_name"], "Luna")
		self.assertEqual(updates["animal_species"], "Mammal")
		self.assertEqual(updates["animal_type"], "Cat")
		self.assertNotIn("pet_status", updates)

	def test_list_breeds_returns_mobile_dtos(self):
		if not frappe.db.exists("DocType", "Pet Breed"):
			self.skipTest("Pet Breed DocType is not installed.")

		breed_name = f"Mobile Breed {frappe.generate_hash(length=8)}"
		frappe.get_doc(
			{
				"doctype": "Pet Breed",
				"breed_name": breed_name,
				"arabic_name": "Arabic Breed",
				"animal_species": "Mammal",
				"animal_type": "Cat",
				"enabled": 1,
			}
		).insert(ignore_permissions=True)

		with patch.object(pets, "_current_guardian", return_value="GUARDIAN-TEST"):
			response = pets.list_breeds(animal_type="Cat", search=breed_name, limit=5)

		self.assertTrue(response["ok"], response)
		items = response["data"]["items"]
		self.assertEqual(len(items), 1)
		self.assertEqual(items[0]["id"], breed_name)
		self.assertEqual(items[0]["name"], breed_name)
		self.assertEqual(items[0]["arabic_name"], "Arabic Breed")
		self.assertEqual(items[0]["species"], "Mammal")
		self.assertEqual(items[0]["type"], "Cat")
