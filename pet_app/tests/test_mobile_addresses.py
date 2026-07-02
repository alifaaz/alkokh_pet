from __future__ import annotations

from frappe.tests.utils import FrappeTestCase

from pet_app.api.mobile import addresses


class TestMobileAddresses(FrappeTestCase):
	def test_create_address_validation_applies_delivery_defaults(self):
		fields = addresses._validated_address_fields(
			{
				"title": "Home",
				"address_line1": "Street 1",
				"city": "Baghdad",
			},
			require_required=True,
			apply_defaults=True,
		)

		self.assertEqual(fields["address_title"], "Home")
		self.assertEqual(fields["address_type"], "Shipping")
		self.assertEqual(fields["country"], "Iraq")

	def test_update_address_validation_does_not_apply_defaults(self):
		fields = addresses._validated_address_fields({})

		self.assertEqual(fields, {})

	def test_cities_returns_mobile_city_dtos(self):
		response = addresses.cities()

		self.assertTrue(response["ok"], response)
		self.assertTrue(response["data"]["items"])
		self.assertIn("id", response["data"]["items"][0])
		self.assertIn("name", response["data"]["items"][0])
