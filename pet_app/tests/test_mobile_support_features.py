from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from pet_app.api.mobile import devices, favorites, pets, profile, reviews, search_history


class TestMobileSupportFeatures(FrappeTestCase):
	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_new_protected_mobile_endpoints_require_auth(self):
		frappe.set_user("Guest")
		cases = [
			(favorites.list_favorites, {}),
			(favorites.toggle_favorite, {"product": "PRODUCT-DOES-NOT-EXIST"}),
			(search_history.list_recent, {}),
			(search_history.save_recent, {"q": "cat"}),
			(devices.register_device, {"fcm_token": "fcm-token"}),
			(devices.delete_device, {"fcm_token": "fcm-token"}),
			(profile.change_password, {"current_password": "oldpass", "new_password": "newpass1"}),
			(profile.phone_change_start, {"new_phone": "07700000002"}),
			(profile.upload_avatar, {}),
			(reviews.upsert_product_review, {"product": "PRODUCT-DOES-NOT-EXIST", "rating": 5}),
			(pets.list_medical_records, {"pet": "PET-DOES-NOT-EXIST"}),
			(pets.upload_photo, {"pet": "PET-DOES-NOT-EXIST"}),
		]

		for fn, kwargs in cases:
			response = fn(**kwargs)
			self.assertEqual(response["error"]["code"], "auth.wrong_credentials", fn.__name__)

	def test_recent_search_normalization(self):
		self.assertEqual(search_history._normalize_query("  cat    food  "), "cat food")

		with self.assertRaises(search_history.MobileSearchError) as raised:
			search_history._normalize_query("")
		self.assertEqual(raised.exception.code, "search.request_invalid")

	def test_medical_record_helpers_use_existing_doctypes(self):
		updates = pets._medical_updates("vaccination", {"name": "Rabies"}, require_required_field=True)

		self.assertEqual(updates["vaccine_name"], "Rabies")
		self.assertIn("administered_on", updates)

		payload = pets._medical_payload(
			"deworming",
			frappe._dict(
				{
					"name": "DEW-TEST",
					"pet": "PET-TEST",
					"guardian": "GUARDIAN-TEST",
					"medication_name": "Dewormer",
					"administered_on": "2026-06-29",
					"reminder_enabled": 1,
				}
			),
		)

		self.assertEqual(payload["id"], "DEW-TEST")
		self.assertEqual(payload["type"], "deworming")
		self.assertEqual(payload["summary"], "Dewormer")
		self.assertTrue(payload["reminder_enabled"])

	def test_review_and_device_payloads_match_sdk_shape(self):
		review = reviews._review_payload(
			frappe._dict(
				{
					"name": "RATING-TEST",
					"overall_rating": 5,
					"notes": "Great",
					"rated_by": "test@example.com",
					"rated_at": "2026-06-29 10:00:00",
					"performer_name": None,
				}
			)
		)
		self.assertEqual(review["id"], "RATING-TEST")
		self.assertEqual(review["rating"], 5)

		device = devices._device_payload(
			frappe._dict(
				{
					"name": "DEVICE-TEST",
					"fcm_token": "fcm-token",
					"platform": "android",
					"device_id": "device-1",
					"last_seen_at": "2026-06-29 10:00:00",
					"disabled": 0,
				}
			)
		)
		self.assertEqual(device["id"], "DEVICE-TEST")
		self.assertEqual(device["fcm_token"], "fcm-token")
		self.assertFalse(device["disabled"])

	def test_favorites_toggle_list_and_remove(self):
		guardian, user = self._make_guardian_user()
		product = self._make_product()
		frappe.set_user(user.name)

		created = favorites.toggle_favorite(product=product.name)
		self.assertTrue(created["ok"], created)
		self.assertTrue(created["data"]["is_favorite"])
		self.assertEqual(created["data"]["product_id"], product.name)

		listed = favorites.list_favorites()
		self.assertTrue(listed["ok"], listed)
		self.assertEqual(len(listed["data"]["items"]), 1)
		self.assertEqual(listed["data"]["items"][0]["product_id"], product.name)

		removed = favorites.remove_favorite(product=product.name)
		self.assertTrue(removed["ok"], removed)
		self.assertFalse(removed["data"]["is_favorite"])
		self.assertFalse(frappe.db.exists("Mobile Favorite", {"guardian": guardian.name, "product": product.name}))

	def test_recent_searches_keep_latest_ten(self):
		self._make_guardian_user_and_login()

		for index in range(12):
			response = search_history.save_recent(q=f"cat food {index}")
			self.assertTrue(response["ok"], response)

		recent = search_history.list_recent()
		self.assertTrue(recent["ok"], recent)
		items = recent["data"]["items"]
		self.assertEqual(len(items), 10)
		self.assertEqual(items[0]["query"], "cat food 11")
		self.assertNotIn("cat food 0", [item["query"] for item in items])

		cleared = search_history.clear_recent()
		self.assertTrue(cleared["ok"], cleared)
		self.assertEqual(cleared["data"]["items"], [])

	def test_device_register_upserts_and_delete_disables(self):
		guardian, _user = self._make_guardian_user_and_login()

		first = devices.register_device(fcm_token="fcm-token", platform="android", device_id="device-1")
		self.assertTrue(first["ok"], first)
		second = devices.register_device(fcm_token="fcm-token", platform="ios", device_id="device-2")
		self.assertTrue(second["ok"], second)
		self.assertEqual(first["data"]["id"], second["data"]["id"])
		self.assertEqual(frappe.db.count("Mobile Device", {"fcm_token": "fcm-token"}), 1)

		deleted = devices.delete_device(fcm_token="fcm-token")
		self.assertTrue(deleted["ok"], deleted)
		self.assertTrue(deleted["data"]["disabled"])
		self.assertEqual(
			frappe.db.get_value("Mobile Device", {"guardian": guardian.name, "fcm_token": "fcm-token"}, "disabled"),
			1,
		)

	def test_product_review_upsert_and_list(self):
		self._make_guardian_user_and_login()
		product = self._make_product()

		created = reviews.upsert_product_review(product=product.name, rating=5, notes="Great")
		self.assertTrue(created["ok"], created)
		updated = reviews.upsert_product_review(product=product.name, rating=3, notes="Okay")
		self.assertTrue(updated["ok"], updated)
		self.assertEqual(created["data"]["id"], updated["data"]["id"])
		self.assertEqual(updated["data"]["rating"], 3)

		listed = reviews.list_product_reviews(product=product.name)
		self.assertTrue(listed["ok"], listed)
		self.assertEqual(listed["data"]["summary"]["count"], 1)
		self.assertEqual(listed["data"]["summary"]["average"], 3)
		self.assertEqual(listed["data"]["items"][0]["notes"], "Okay")

	def test_pet_medical_record_crud_uses_existing_records(self):
		guardian, _user = self._make_guardian_user_and_login()
		pet = self._make_pet(guardian)

		created = pets.add_medical_record(
			pet=pet.name,
			record_type="vaccination",
			vaccine_name="Rabies",
			administered_on="2026-06-29",
		)
		self.assertTrue(created["ok"], created)
		record_id = created["data"]["id"]
		self.assertTrue(frappe.db.exists("Pet Vaccination Record", record_id))

		updated = pets.update_medical_record(pet=pet.name, record=record_id, notes="Done")
		self.assertTrue(updated["ok"], updated)
		self.assertEqual(updated["data"]["notes"], "Done")

		listed = pets.list_medical_records(pet=pet.name, record_type="vaccination")
		self.assertTrue(listed["ok"], listed)
		self.assertEqual(listed["data"]["items"][0]["id"], record_id)

		deleted = pets.delete_medical_record(pet=pet.name, record=record_id)
		self.assertTrue(deleted["ok"], deleted)
		self.assertTrue(deleted["data"]["deleted"])
		self.assertFalse(frappe.db.exists("Pet Vaccination Record", record_id))

	def _make_guardian_user_and_login(self):
		guardian, user = self._make_guardian_user()
		frappe.set_user(user.name)
		return guardian, user

	def _make_guardian_user(self):
		suffix = frappe.generate_hash(length=8)
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"mobile.{suffix}@example.com",
				"first_name": "Mobile",
				"enabled": 1,
				"send_welcome_email": 0,
			}
		).insert(ignore_permissions=True)
		guardian = frappe.get_doc(
			{
				"doctype": "Guardian",
				"phone": f"07{self._digits(suffix, 9)}",
				"full_name": f"Mobile Guardian {suffix}",
				"user_id": user.name,
				"is_active": 1,
				"otp_verified": 1,
			}
		).insert(ignore_permissions=True)
		return guardian, user

	def _make_product(self):
		suffix = frappe.generate_hash(length=8)
		current_user = frappe.session.user
		frappe.set_user("Administrator")
		try:
			return frappe.get_doc(
				{
					"doctype": "Product",
					"product_name": f"Mobile Product {suffix}",
					"sku": f"MOB-SKU-{suffix}",
					"price": 1000,
					"in_stock": 1,
					"status": "Active",
				}
			).insert(ignore_permissions=True)
		finally:
			frappe.set_user(current_user)

	def _make_pet(self, guardian):
		suffix = frappe.generate_hash(length=8)
		pet = frappe.get_doc(
			{
				"doctype": "Pet",
				"pet_name": f"Mobile Pet {suffix}",
				"animal_species": "Mammal",
				"animal_type": "Dog",
				"pet_status": "Approved",
			}
		).insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "PetGuardian",
				"pet_id": pet.name,
				"guardian_id": guardian.name,
				"role": "primary_owner",
			}
		).insert(ignore_permissions=True)
		return pet

	def _digits(self, value: str, length: int) -> str:
		digits = "".join(str(ord(char) % 10) for char in value)
		return digits[:length].ljust(length, "0")
