from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from pet_app.pet_app.doctype.healthcare_practitioner.healthcare_practitioner import (
	HealthcarePractitioner,
	PRACTITIONER_TYPE_ROLES,
	PRACTITIONER_TYPES,
)


class TestHealthcarePractitioner(FrappeTestCase):
	def test_practitioner_type_options_include_operational_roles(self):
		self.assertEqual(
			PRACTITIONER_TYPES,
			("Doctor", "Nurse", "Service Provider", "Coordinator", "Other"),
		)
		self.assertEqual(PRACTITIONER_TYPE_ROLES["Doctor"], ("Doctor",))
		self.assertEqual(PRACTITIONER_TYPE_ROLES["Nurse"], ("Nursing User",))
		self.assertEqual(PRACTITIONER_TYPE_ROLES["Service Provider"], ("Service Provider",))
		self.assertEqual(PRACTITIONER_TYPE_ROLES["Coordinator"], ("Coordinator",))
		self.assertEqual(PRACTITIONER_TYPE_ROLES["Other"], ())

	def test_operational_practitioner_types_validate(self):
		for practitioner_type in ("Service Provider", "Coordinator"):
			with self.subTest(practitioner_type=practitioner_type):
				doc = frappe.get_doc(
					{
						"doctype": "Healthcare Practitioner",
						"practitioner_name": "Test Practitioner",
						"practitioner_type": practitioner_type,
						"phone": "0790000000",
					}
				)
				with (
					patch.object(HealthcarePractitioner, "_enforce_admin_access", return_value=None),
					patch.object(
						HealthcarePractitioner,
						"_resolve_or_create_user",
						return_value="test@example.com",
					),
					patch.object(HealthcarePractitioner, "_validate_user_mapping", return_value=None),
				):
					doc.validate()

				self.assertEqual(doc.user_id, "test@example.com")
