# Copyright (c) 2025, solvers and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase


class UnitTestPet(UnitTestCase):
	"""
	Unit tests for Pet.
	Use this class for testing individual functions and methods.
	"""

	pass


class IntegrationTestPet(IntegrationTestCase):
	"""
	Integration tests for Pet.
	Use this class for testing interactions between components.
	"""

	def setUp(self):
		"""Set up test data before each test"""
		pass

	def tearDown(self):
		"""Clean up test data after each test"""
		pass

	def test_create_pet(self):
		"""Test creating a new pet"""
		pet = frappe.get_doc({
			"doctype": "Pet",
			"pet_name": "Test Dog",
			"species": "Dog",
			"breed": "Labrador",
			"age": 3,
			"status": "Active"
		})
		pet.insert()

		self.assertEqual(pet.pet_name, "Test Dog")
		self.assertEqual(pet.species, "Dog")

		# Clean up
		pet.delete()

	def test_negative_age_validation(self):
		"""Test that negative age raises an error"""
		pet = frappe.get_doc({
			"doctype": "Pet",
			"pet_name": "Test Cat",
			"species": "Cat",
			"age": -1
		})

		with self.assertRaises(frappe.exceptions.ValidationError):
			pet.insert()
