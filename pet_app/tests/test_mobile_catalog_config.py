from __future__ import annotations

from frappe.tests.utils import FrappeTestCase

from pet_app.api.mobile import catalog, config


class TestMobileCatalogConfig(FrappeTestCase):
	def test_config_reports_safe_feature_flags(self):
		response = config.get_config()

		self.assertTrue(response["ok"], response)
		data = response["data"]
		self.assertEqual(data["currency"], "IQD")
		self.assertTrue(data["feature_flags"]["auth"])
		self.assertTrue(data["feature_flags"]["orders"])
		self.assertTrue(data["feature_flags"]["catalog"])
		self.assertFalse(data["feature_flags"]["cart"])

	def test_empty_suggest_query_returns_empty_items(self):
		response = catalog.suggest(q="")

		self.assertTrue(response["ok"], response)
		self.assertEqual(response["data"], {"items": []})

	def test_category_payload_uses_mobile_dto_keys(self):
		payload = catalog._categories_payload()

		self.assertIn("items", payload)
		if payload["items"]:
			first = payload["items"][0]
			self.assertIn("id", first)
			self.assertIn("name", first)
			self.assertIn("enabled", first)

	def test_home_v2_uses_frontend_block_contract(self):
		response = catalog.home_v2()

		self.assertTrue(response["ok"], response)
		data = response["data"]
		self.assertEqual(data["schema_version"], 2)
		self.assertEqual(data["version"], 2)
		self.assertIn("updated_at", data)
		self.assertEqual(data["cache_ttl_seconds"], 300)
		self.assertIn(data["locale"], {"en", "ar"})
		self.assertIsInstance(data["filters"], list)
		self.assertIsInstance(data["blocks"], list)
		self.assertEqual(data["filters"][0]["key"], "all")

		for block in data["blocks"]:
			self.assertIn("id", block)
			self.assertIn("type", block)
			self.assertIn("data", block)
			self.assertNotIn("sort_order", block)

	def test_unknown_home_product_list_is_rejected(self):
		response = catalog.list_products(**{"list": "does-not-exist"})

		self.assertNotIn("ok", response)
		self.assertEqual(response["error"]["code"], "catalog.request_invalid")

	def test_unknown_home_filter_falls_back_to_all(self):
		"""A stale chip key must not empty the screen.

		This asserted a `catalog.request_invalid` refusal until the Filter chips tab became
		the source of the chip list. The client caches that list, so a chip removed in the
		admin this morning is still on someone's phone this afternoon - and refusing it
		turned a removed label into a blank shop. Unknown now degrades to "all", which shows
		products rather than a failure and corrects itself on the next refresh.

		The unknown-LIST case above is deliberately still a refusal: a bad `list` id is a
		client bug, not a value the admin can invalidate underneath a cached app.
		"""
		response = catalog.list_products(**{"filter": "horse"})

		self.assertTrue(response.get("ok"))
		self.assertTrue(response["data"]["items"])

	def test_configured_chips_are_served_and_filter(self):
		payload = catalog.home_v2()
		data = payload.get("data") or payload
		keys = [row["key"] for row in data["filters"]]

		# "all" is synthesised; the rest are the enabled rows of Mobile Home Filter.
		self.assertEqual(keys[0], "all")
		self.assertTrue(data["filters"][0].get("built_in"))
		for row in data["filters"]:
			self.assertIn("label_en", row)
			self.assertIn("label_ar", row)

		configured = [
			catalog._filter_slug(row.get("filter_key") or row.get("name"))
			for row in catalog._mobile_filter_chips()
		]
		if configured:
			self.assertEqual(keys[1:], configured)

	def test_product_filter_and_tag_helpers_use_mobile_contract(self):
		dog_row = {
			"name": "PROD-DOG",
			"product_name": "Adult Dog Food",
			"category": "Dog Food",
			"tags": "dog,dry-food,best-seller",
		}
		hotdog_row = {
			"name": "PROD-HOTDOG",
			"product_name": "Snack",
			"category": "Treats",
			"tags": "hotdog,best-seller",
		}

		# Chip membership is the admin's `mobile_home_filter` Link and nothing else.
		# This used to assert that a row NAMED "Adult Dog Food" classified as "dog" purely
		# from its text. That guess is gone by decision: a filter that shows everything is
		# not a filter, and a product filed under a chip because of a word in its name was
		# never assigned to that chip by anyone.
		self.assertEqual(catalog._product_filter(dog_row), "")
		self.assertFalse(catalog._product_matches_filter(dog_row, "dog"))
		# ...but an unclassified product is still everything's business under "all".
		self.assertTrue(catalog._product_matches_filter(dog_row, "all"))

		assigned_row = dict(dog_row, mobile_home_filter="dog")
		self.assertEqual(catalog._product_filter(assigned_row), "dog")
		self.assertTrue(catalog._product_matches_filter(assigned_row, "dog"))
		self.assertFalse(catalog._product_matches_filter(assigned_row, "cat"))

		# Tags are a separate axis and are unchanged - still a free-text substring match.
		self.assertTrue(catalog._product_matches_tag(dog_row, "best-seller"))
		self.assertTrue(catalog._product_matches_tag(dog_row, "DOG"))
		self.assertFalse(catalog._product_matches_tag(hotdog_row, "dog"))
