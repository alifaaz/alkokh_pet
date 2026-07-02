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
