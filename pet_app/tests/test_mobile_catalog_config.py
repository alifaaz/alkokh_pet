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

	def test_unknown_home_filter_is_rejected(self):
		response = catalog.list_products(**{"filter": "horse"})

		self.assertNotIn("ok", response)
		self.assertEqual(response["error"]["code"], "catalog.request_invalid")

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

		self.assertEqual(catalog._product_filter(dog_row), "dog")
		self.assertTrue(catalog._product_matches_filter(dog_row, "dog"))
		self.assertTrue(catalog._product_matches_filter(dog_row, "all"))
		self.assertTrue(catalog._product_matches_tag(dog_row, "best-seller"))
		self.assertTrue(catalog._product_matches_tag(dog_row, "DOG"))
		self.assertFalse(catalog._product_matches_tag(hotdog_row, "dog"))
