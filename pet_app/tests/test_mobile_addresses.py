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

	def test_area_writes_to_the_county_column(self):
		"""`area` is the API key; `county` stays the stored fieldname, like notes/custom_notes."""
		fields = addresses._validated_address_fields({"area": "Karrada"})

		self.assertEqual(fields["county"], "Karrada")
		self.assertNotIn("area", fields)

	def test_county_on_input_is_refused_not_ignored(self):
		"""An old build must fail loudly rather than lose the neighbourhood.

		Unrecognised keys are dropped here without complaint, so silence would mean
		ok: true with the value gone - the failure shape this rename exists to avoid.
		"""
		for payload in (
			{"county": "Adhamiyah"},
			{"county": "Adhamiyah", "city": "Basra"},
			{"county": ""},
			{"county": None},
			{"county": "Adhamiyah", "area": "Mansour"},
		):
			with self.subTest(payload=payload):
				with self.assertRaises(addresses.MobileAddressError) as caught:
					addresses._validated_address_fields(payload)
				self.assertEqual(caught.exception.code, addresses.ADDRESS_REQUEST_INVALID)
				self.assertEqual(caught.exception.http_status, 400)
				# The message names the replacement so whoever hits it knows the fix.
				self.assertIn("area", caught.exception.message)

	def test_cities_returns_mobile_city_dtos(self):
		response = addresses.cities()

		self.assertTrue(response["ok"], response)
		self.assertTrue(response["data"]["items"])
		self.assertIn("id", response["data"]["items"][0])
		self.assertIn("name", response["data"]["items"][0])
