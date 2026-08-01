from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from pet_app.api.mobile import catalog, home_builder


class TestMobileHomeBuilder(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self._clear_builder_state()

	def tearDown(self):
		frappe.set_user("Administrator")
		self._clear_builder_state()
		super().tearDown()

	def test_get_builder_creates_revision_zero_layout(self):
		response = home_builder.get_builder()

		self.assertTrue(response["ok"], response)
		data = response["data"]
		self.assertEqual(data["revision"], 0)
		self.assertEqual(data["draft"]["schema_version"], 2)
		self.assertEqual(data["draft"]["filters"][0]["key"], "all")
		self.assertIsNone(data["active_publication"])

	def test_builder_rejects_guest_access(self):
		frappe.set_user("Guest")

		response = home_builder.get_builder()

		self.assertFalse(response["ok"], response)
		self.assertEqual(response["meta"]["code"], "PERMISSION_DENIED")

	def test_save_draft_detects_stale_revision(self):
		product = self._make_product("Stale Revision Food")
		config = self._manual_home_config(product.name)

		first = home_builder.save_draft(config=config, expected_revision=0)
		self.assertTrue(first["ok"], first)
		self.assertEqual(first["data"]["revision"], 1)

		second = home_builder.save_draft(config=config, expected_revision=0)
		self.assertFalse(second["ok"], second)
		self.assertEqual(second["meta"]["code"], "STALE_DRAFT")
		self.assertEqual(second["meta"]["current_revision"], 1)

	def test_builder_filters_are_derived_from_records_and_product_links(self):
		cat_filter = self._make_filter("cat", "Cat", "Cat AR", display_order=10)
		dog_filter = self._make_filter("dog", "Dog", "Dog AR", display_order=20)
		self._make_filter("bird", "Bird", "Bird AR", display_order=30, enabled=0)
		product = self._make_product("Linked Dog Filter Food", mobile_home_filter=dog_filter.name)

		response = home_builder.get_builder()

		self.assertTrue(response["ok"], response)
		filters = response["data"]["draft"]["filters"]
		self.assertEqual([row["key"] for row in filters], ["all", cat_filter.name, dog_filter.name])
		self.assertEqual(filters[1]["product_ids"], [])
		self.assertEqual(filters[2]["product_ids"], [product.name])

	def test_legacy_filter_product_ids_seed_filter_records_and_empty_product_links(self):
		product = self._make_product("Legacy Filter Food")
		config = self._manual_home_config(product.name)

		saved = home_builder.save_draft(config=config, expected_revision=0)

		self.assertTrue(saved["ok"], saved)
		self.assertTrue(frappe.db.exists("Mobile Home Filter", "dog"))
		self.assertEqual(frappe.db.get_value("Product", product.name, "mobile_home_filter"), "dog")
		dog_filter = next(row for row in saved["data"]["draft"]["filters"] if row["key"] == "dog")
		self.assertEqual(dog_filter["product_ids"], [product.name])

	def test_home_draft_returns_saved_draft_without_publish(self):
		product = self._make_product("Draft Preview Food")
		config = self._manual_home_config(product.name)
		saved = home_builder.save_draft(config=config, expected_revision=0)
		self.assertTrue(saved["ok"], saved)

		draft_home = catalog.home_draft(locale="en", filter_key="dog")
		live_home = catalog.home_v2(locale="en", filter_key="dog")

		self.assertTrue(draft_home["ok"], draft_home)
		self.assertTrue(draft_home["data"]["preview"])
		self.assertEqual(draft_home["data"]["draft_revision"], saved["data"]["revision"])
		self.assertEqual(self._product_ids_from_home(draft_home), [product.name])
		self.assertTrue(live_home["ok"], live_home)
		self.assertEqual(live_home["data"]["version"], 2)

	def test_home_draft_rejects_guest_access(self):
		frappe.set_user("Guest")

		response = catalog.home_draft(locale="en")

		self.assertNotIn("ok", response)
		self.assertEqual(response["error"]["code"], "PERMISSION_DENIED")

	def test_migration_patch_converts_existing_draft_filters(self):
		from pet_app.patches import mobile_home_product_linked_filters

		product = self._make_product("Migrated Filter Food")
		layout = frappe.new_doc("Mobile Home Layout")
		layout.schema_version = 2
		layout.revision = 0
		layout.draft_json = home_builder._json_pretty(self._manual_home_config(product.name))
		layout.flags.ignore_permissions = True
		layout.insert(ignore_permissions=True)

		mobile_home_product_linked_filters.execute()

		self.assertTrue(frappe.db.exists("Mobile Home Filter", "dog"))
		self.assertEqual(frappe.db.get_value("Product", product.name, "mobile_home_filter"), "dog")
		draft = home_builder._load_json(
			frappe.db.get_value("Mobile Home Layout", "mobile-home", "draft_json"),
			home_builder._empty_config(),
		)
		dog_filter = next(row for row in draft["filters"] if row["key"] == "dog")
		self.assertEqual(dog_filter["product_ids"], [product.name])

	def test_publish_switches_home_v2_to_builder_output_and_show_all(self):
		product = self._make_product("Builder Home Food")
		config = self._manual_home_config(product.name)

		saved = home_builder.save_draft(config=config, expected_revision=0)
		self.assertTrue(saved["ok"], saved)
		validated = home_builder.validate_draft(config=saved["data"]["draft"])
		self.assertTrue(validated["ok"], validated)
		self.assertTrue(validated["data"]["valid"], validated)

		published = home_builder.publish(
			expected_revision=saved["data"]["revision"],
			validation_checksum=validated["data"]["checksum"],
		)
		self.assertTrue(published["ok"], published)
		self.assertTrue(published["data"]["published"], published)

		home = catalog.home_v2(locale="en")
		self.assertTrue(home["ok"], home)
		home_data = home["data"]
		self.assertEqual(home_data["schema_version"], 2)
		self.assertEqual(home_data["version"], published["data"]["publication"]["version"])
		self.assertEqual(home_data["blocks"][0]["id"], "featured-products")
		self.assertEqual(home_data["blocks"][0]["data"]["products"][0]["id"], product.name)

		block = catalog.home_block_v2(block_id="featured-products", locale="en", limit_page_length=1)
		self.assertTrue(block["ok"], block)
		self.assertEqual(block["data"]["block_id"], "featured-products")
		self.assertEqual(block["data"]["total"], 1)
		self.assertEqual(block["data"]["data"]["products"][0]["id"], product.name)

	def test_published_filter_membership_is_snapshotted_until_republish(self):
		dog_filter = self._make_filter("dog", "Dog", "Dog AR", display_order=10)
		cat_filter = self._make_filter("cat", "Cat", "Cat AR", display_order=20)
		product = self._make_product("Snapshot Filter Food", mobile_home_filter=dog_filter.name)
		config = self._manual_home_config(product.name, filters=[])

		saved = home_builder.save_draft(config=config, expected_revision=0)
		self.assertTrue(saved["ok"], saved)
		published = self._publish(saved)
		self.assertTrue(published["data"]["published"], published)

		dog_home = catalog.home_v2(locale="en", filter_key=dog_filter.name)
		cat_home = catalog.home_v2(locale="en", filter_key=cat_filter.name)
		self.assertEqual(self._product_ids_from_home(dog_home), [product.name])
		self.assertEqual(self._product_ids_from_home(cat_home), [])

		frappe.db.set_value("Product", product.name, "mobile_home_filter", cat_filter.name)
		home_builder.clear_home_cache()

		dog_home = catalog.home_v2(locale="en", filter_key=dog_filter.name)
		cat_home = catalog.home_v2(locale="en", filter_key=cat_filter.name)
		self.assertEqual(self._product_ids_from_home(dog_home), [product.name])
		self.assertEqual(self._product_ids_from_home(cat_home), [])

		revalidated = home_builder.validate_draft(config=saved["data"]["draft"])
		self.assertTrue(revalidated["ok"], revalidated)
		republished = home_builder.publish(
			expected_revision=saved["data"]["revision"],
			validation_checksum=revalidated["data"]["checksum"],
		)
		self.assertTrue(republished["data"]["published"], republished)

		dog_home = catalog.home_v2(locale="en", filter_key=dog_filter.name)
		cat_home = catalog.home_v2(locale="en", filter_key=cat_filter.name)
		self.assertEqual(self._product_ids_from_home(dog_home), [])
		self.assertEqual(self._product_ids_from_home(cat_home), [product.name])

	def test_legacy_home_v2_still_returns_without_publication(self):
		response = catalog.home_v2(locale="en")

		self.assertTrue(response["ok"], response)
		self.assertEqual(response["data"]["schema_version"], 2)
		self.assertEqual(response["data"]["version"], 2)
		self.assertIsInstance(response["data"]["blocks"], list)

	def _manual_home_config(self, product_id: str, filters=None) -> dict:
		if filters is None:
			filters = [{"key": "dog", "label": {"en": "Dog", "ar": "Dog AR"}, "product_ids": [product_id]}]
		return {
			"schema_version": 2,
			"filters": filters,
			"blocks": [
				{
					"id": "featured-products",
					"type": "product_list",
					"enabled": True,
					"order": 10,
					"title": {"en": "Featured", "ar": "Featured AR"},
					"source": "manual",
					"manual_product_ids": [product_id],
					"max_items": 10,
					"show_all": True,
					"respects_filter": True,
				}
			],
		}

	def _make_filter(self, key: str, label_en: str, label_ar: str, *, display_order=10, enabled=1):
		doc = frappe.new_doc("Mobile Home Filter")
		doc.filter_key = key
		doc.enabled = enabled
		doc.label_en = label_en
		doc.label_ar = label_ar
		doc.display_order = display_order
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
		return doc

	def _make_product(self, title: str, *, mobile_home_filter=None):
		doc = frappe.new_doc("Product")
		suffix = frappe.generate_hash(length=10)
		doc.product_name = f"{title} {suffix}"
		doc.sku = suffix
		doc.status = "Active"
		doc.image = "https://example.test/product.png"
		doc.price = 10000
		doc.discounted_price = 8000
		doc.in_stock = 1
		if mobile_home_filter:
			doc.mobile_home_filter = mobile_home_filter
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
		return doc

	def _publish(self, saved):
		validated = home_builder.validate_draft(config=saved["data"]["draft"])
		self.assertTrue(validated["ok"], validated)
		self.assertTrue(validated["data"]["valid"], validated)
		return home_builder.publish(
			expected_revision=saved["data"]["revision"],
			validation_checksum=validated["data"]["checksum"],
		)

	def _product_ids_from_home(self, response):
		self.assertTrue(response["ok"], response)
		for block in response["data"]["blocks"]:
			if block.get("id") == "featured-products":
				return [product["id"] for product in block["data"]["products"]]
		return []

	def _clear_builder_state(self):
		if frappe.db.exists("DocType", "Product"):
			try:
				if frappe.get_meta("Product").has_field("mobile_home_filter"):
					frappe.db.sql("update `tabProduct` set mobile_home_filter = null")
			except Exception:
				pass
		if frappe.db.exists("DocType", "Mobile Home Filter"):
			frappe.db.delete("Mobile Home Filter")
		if frappe.db.exists("DocType", "Mobile Home Layout"):
			frappe.db.delete("Mobile Home Layout")
		if frappe.db.exists("DocType", "Mobile Home Publication"):
			frappe.db.delete("Mobile Home Publication")
		frappe.db.commit()
