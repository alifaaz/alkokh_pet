from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from pet_app.api.pet import get_guardian_pets
from pet_app.permissions.petguardian import get_permission_query_conditions


def _payload(response):
	"""Unwrap `standardize_response`, which nests the result under `data`."""
	return response["data"] if isinstance(response.get("data"), dict) else response


def _pet_ids(response):
	return [row["pet_id"] for row in _payload(response)["data"]]


class TestGuardianPetSelection(FrappeTestCase):
	"""The guardian-scoped picker: dead animals out by default, opt-in for mortality.

	The bug these cover: the boarding picker offered a deceased pet because it
	never called a method at all - it assembled the guardian's pets from a raw
	`/api/resource/PetGuardian` query, where no server-side rule can run. So the
	tests check both halves: the method filters, AND the permission query stops
	the raw path returning more than the method would.
	"""

	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")

	# ------------------------------------------------------------------
	# The filter
	# ------------------------------------------------------------------

	def test_deceased_pet_is_absent_by_default(self):
		guardian, living, dead = self._make_guardian_with_living_and_dead_pet()
		ids = _pet_ids(get_guardian_pets(guardians=guardian.name))
		self.assertIn(living.name, ids)
		self.assertNotIn(dead.name, ids)

	def test_include_deceased_returns_it(self):
		guardian, living, dead = self._make_guardian_with_living_and_dead_pet()
		ids = _pet_ids(get_guardian_pets(guardians=guardian.name, include_deceased=True))
		self.assertIn(living.name, ids)
		self.assertIn(dead.name, ids)

	def test_include_deceased_accepts_the_string_a_query_param_actually_sends(self):
		"""`cint("true")` is 0, so this would silently filter the mortality picker."""
		guardian, _living, dead = self._make_guardian_with_living_and_dead_pet()
		for truthy in (True, 1, "1", "true", "True"):
			with self.subTest(include_deceased=truthy):
				ids = _pet_ids(get_guardian_pets(guardians=guardian.name, include_deceased=truthy))
				self.assertIn(dead.name, ids)
		for falsy in (False, 0, "0", "false", None, ""):
			with self.subTest(include_deceased=falsy):
				ids = _pet_ids(get_guardian_pets(guardians=guardian.name, include_deceased=falsy))
				self.assertNotIn(dead.name, ids)

	def test_death_markers_ride_along_on_the_filtered_response(self):
		"""So a picker can grey a row out rather than lose the animal silently."""
		guardian, _living, _dead = self._make_guardian_with_living_and_dead_pet()
		row = _payload(get_guardian_pets(guardians=guardian.name))["data"][0]
		for field in ("is_deceased", "death_date", "death_record"):
			self.assertIn(field, row)

	def test_search_is_applied_server_side(self):
		guardian, living, _dead = self._make_guardian_with_living_and_dead_pet()
		ids = _pet_ids(get_guardian_pets(guardians=guardian.name, search=living.pet_name))
		self.assertEqual(ids, [living.name])
		self.assertEqual(_pet_ids(get_guardian_pets(guardians=guardian.name, search="zzz-no-match")), [])

	# ------------------------------------------------------------------
	# Shape
	# ------------------------------------------------------------------

	def test_accepts_one_guardian_or_many_in_every_encoding(self):
		first, living_a, _dead_a = self._make_guardian_with_living_and_dead_pet()
		second, living_b, _dead_b = self._make_guardian_with_living_and_dead_pet()
		expected = {living_a.name, living_b.name}
		for guardians in (
			[first.name, second.name],
			f"{first.name},{second.name}",
			frappe.as_json([first.name, second.name]),
		):
			with self.subTest(guardians=guardians):
				self.assertEqual(set(_pet_ids(get_guardian_pets(guardians=guardians))), expected)

	def test_a_shared_pet_appears_once_with_every_link_listed(self):
		"""Nothing constrains pet_id to be unique on PetGuardian - two owners of
		one animal must not put that animal in the picker twice."""
		first, pet, _dead = self._make_guardian_with_living_and_dead_pet()
		second = self._make_guardian()
		frappe.get_doc({
			"doctype": "PetGuardian", "pet_id": pet.name, "guardian_id": second.name, "role": "owner",
		}).insert(ignore_permissions=True)

		rows = _payload(get_guardian_pets(guardians=[first.name, second.name]))["data"]
		matching = [row for row in rows if row["pet_id"] == pet.name]
		self.assertEqual(len(matching), 1)
		self.assertEqual(matching[0]["role"], "primary_owner")
		self.assertEqual({link["guardian_id"] for link in matching[0]["links"]}, {first.name, second.name})

	def test_unknown_guardian_is_empty_not_an_error(self):
		self.assertEqual(_payload(get_guardian_pets(guardians="GUARDIAN-does-not-exist"))["total"], 0)
		self.assertEqual(_payload(get_guardian_pets(guardians=""))["total"], 0)
		self.assertEqual(_payload(get_guardian_pets(guardians=None))["total"], 0)

	# ------------------------------------------------------------------
	# Access
	# ------------------------------------------------------------------

	def test_guardian_user_cannot_read_another_guardians_pets(self):
		mine, my_pet, _dead = self._make_guardian_with_living_and_dead_pet()
		theirs, their_pet, _their_dead = self._make_guardian_with_living_and_dead_pet()
		user = self._make_user("picker.guardian")
		frappe.db.set_value("Guardian", mine.name, "user_id", user.name)

		frappe.set_user(user.name)
		self.assertEqual(_pet_ids(get_guardian_pets(guardians=theirs.name)), [])
		self.assertNotIn(their_pet.name, _pet_ids(get_guardian_pets(guardians=[mine.name, theirs.name])))
		self.assertIn(my_pet.name, _pet_ids(get_guardian_pets()))

	def test_guest_is_refused(self):
		guardian = self._make_guardian()
		frappe.set_user("Guest")
		self.assertIs(get_guardian_pets(guardians=guardian.name).get("ok"), False)

	# ------------------------------------------------------------------
	# The other half: the raw path
	# ------------------------------------------------------------------

	def test_permission_query_scopes_a_guardian_user_to_their_own_links(self):
		guardian = self._make_guardian()
		user = self._make_user("picker.scoped")
		frappe.db.set_value("Guardian", guardian.name, "user_id", user.name)

		condition = get_permission_query_conditions(user=user.name)
		self.assertIn("`tabPetGuardian`.`guardian_id`", condition)
		self.assertIn(guardian.name, condition)
		# and it is valid SQL against the real table
		frappe.db.sql(f"SELECT COUNT(*) FROM `tabPetGuardian` WHERE {condition}")

	def test_permission_query_leaves_staff_and_admins_unscoped(self):
		self.assertEqual(get_permission_query_conditions(user="Administrator"), "")
		staff = self._make_user("picker.staff", ["Reception"])
		self.assertEqual(get_permission_query_conditions(user=staff.name), "")

	def test_a_staff_member_who_owns_a_pet_is_not_locked_out_of_their_job(self):
		"""Clinic staff have pets too. Full access wins over the guardian link."""
		guardian = self._make_guardian()
		user = self._make_user("picker.vet", ["System Manager"])
		frappe.db.set_value("Guardian", guardian.name, "user_id", user.name)
		self.assertEqual(get_permission_query_conditions(user=user.name), "")

	# ------------------------------------------------------------------
	# Fixtures
	# ------------------------------------------------------------------

	def _make_user(self, prefix, roles=None):
		suffix = frappe.generate_hash(length=8)
		user = frappe.get_doc({
			"doctype": "User",
			"email": f"{prefix}.{suffix}@example.com",
			"first_name": prefix,
			"send_welcome_email": 0,
			"roles": [{"role": role} for role in (roles or [])],
		}).insert(ignore_permissions=True)
		return user

	def _make_guardian(self):
		suffix = frappe.generate_hash(length=8)
		digits = "".join(c for c in suffix if c.isdigit()).ljust(9, "1")[:9]
		return frappe.get_doc({
			"doctype": "Guardian",
			"phone": f"07{digits}",
			"full_name": f"Guardian {suffix}",
			"email_id": f"guardian.{suffix}@example.com",
		}).insert(ignore_permissions=True)

	def _make_pet(self, guardian, deceased=False):
		suffix = frappe.generate_hash(length=8)
		pet = frappe.get_doc({
			"doctype": "Pet",
			"pet_name": f"Pet {suffix}",
			"animal_species": "Mammal",
			"animal_type": "Dog",
			"birth_date": "2020-01-01",
			"weight": 10,
			"pet_status": "Approved",
		}).insert(ignore_permissions=True)
		frappe.get_doc({
			"doctype": "PetGuardian",
			"pet_id": pet.name,
			"guardian_id": guardian.name,
			"role": "primary_owner",
		}).insert(ignore_permissions=True)
		if deceased:
			frappe.db.set_value("Pet", pet.name, {
				"is_deceased": 1,
				"status": "Deceased",
				"death_date": frappe.utils.nowdate(),
			})
		return pet

	def _make_guardian_with_living_and_dead_pet(self):
		guardian = self._make_guardian()
		return guardian, self._make_pet(guardian), self._make_pet(guardian, deceased=True)
