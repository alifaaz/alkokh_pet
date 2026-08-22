from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from pet_app.api.mobile import catalog, home_builder


# Doctypes this suite creates rows in, directly or through the builder it exercises.
BUILDER_DOCTYPES = ("Mobile Home Publication", "Mobile Home Layout", "Mobile Home Filter")


class TestMobileHomeBuilder(FrappeTestCase):
	"""Home builder tests. Nothing here may outlive the class, in either direction.

	This suite used to clear `Mobile Home Filter`, `Mobile Home Layout` and
	`Mobile Home Publication` outright and then `frappe.db.commit()`, and it ran that in
	setUp AND tearDown. Pointed at a working site it deleted the owner's three configured
	filter chips permanently, nulled `mobile_home_filter` on every product in the catalogue,
	and left every Product it created behind. The commit is what made it permanent:
	`IntegrationTestCase` registers its rollback with `addClassCleanup`, so without a commit
	every write here is undone when the class finishes.

	So there are two rules, and the second is the one that was missing:

	  1. Never commit. `SHOW_TRANSACTION_COMMIT_WARNINGS` below makes a reintroduced commit
	     announce itself instead of silently re-arming the same bug.
	  2. Delete only what this suite created. `setUp` records the rows that already existed;
	     `tearDown` removes the difference and nothing else, so a row that was on the site
	     before the run is never a candidate for deletion in the first place.
	"""

	SHOW_TRANSACTION_COMMIT_WARNINGS = True

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self._preexisting = self._snapshot_builder_state()
		self._created_products = []
		# Isolation, not cleanup: several tests assert an EXACT filter list, and two create
		# rows named "cat"/"dog" that would collide with live chips of the same name. This
		# empties the tables for the duration of the transaction only - the rows come back
		# at class rollback because nothing here commits.
		self._isolate_builder_state()

	def tearDown(self):
		frappe.set_user("Administrator")
		self._remove_created_state()
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
		# Tracked so tearDown can remove it. Untracked, these leaked: 14 fixture products
		# with names like "Stale Revision Food f492674b71" were left in a live catalogue.
		self._created_products.append(doc.name)
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

	def _snapshot_builder_state(self) -> dict[str, set[str]]:
		"""The rows that existed before this test. Everything else is ours to remove."""
		snapshot = {}
		for doctype in BUILDER_DOCTYPES:
			if frappe.db.exists("DocType", doctype):
				snapshot[doctype] = set(frappe.get_all(doctype, pluck="name", ignore_permissions=True))
		return snapshot

	def _isolate_builder_state(self):
		"""Empty the builder tables for this transaction. Deliberately NOT committed.

		Publication before Layout before Filter: a publication points at a layout, and the
		legacy import path points a product at a filter, so removing them in the other order
		trips link validation on a site that has real data.
		"""
		for doctype in BUILDER_DOCTYPES:
			if frappe.db.exists("DocType", doctype):
				frappe.db.delete(doctype)
		# NOT `update tabProduct set mobile_home_filter = null` - that rewrote every product
		# in the catalogue to clean up after the handful this suite creates. The products it
		# creates are tracked and deleted instead, and no other product is touched.

	def _remove_created_state(self):
		"""Remove what this test created, and only that.

		The difference against the setUp snapshot covers rows the builder created on its own
		- `get_builder` writes a revision-zero Layout, and the legacy import seeds Filter
		records - which a list of explicitly-created fixtures would miss.
		"""
		for product in reversed(self._created_products):
			if frappe.db.exists("Product", product):
				frappe.delete_doc("Product", product, force=True, ignore_permissions=True, delete_permanently=True)
		self._created_products = []

		for doctype in BUILDER_DOCTYPES:
			if not frappe.db.exists("DocType", doctype):
				continue
			before = self._preexisting.get(doctype, set())
			created = [
				name
				for name in frappe.get_all(doctype, pluck="name", ignore_permissions=True)
				if name not in before
			]
			if created:
				frappe.db.delete(doctype, {"name": ("in", created)})
