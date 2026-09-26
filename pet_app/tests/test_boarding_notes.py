"""Check-in / check-out notes are stored AND returned, and settings defaults are readable.

The stored half and the returned half are tested separately on purpose. The bug this
covers had the note reaching the endpoint and being dropped before the write; the bug
that recurs in this codebase is the opposite - a field stored correctly but left out of
an endpoint's select list, which reads on screen as lost data. Asserting on the response
dict rather than on the reloaded doc is what catches the second one.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from pet_app.api.healthcare import boarding as boarding_api
from pet_app.utils.guardian_customer import get_or_create_customer_from_guardian


class TestBoardingNotes(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.reload_doc("pet_app", "doctype", "pet_boarding")
		frappe.reload_doc("pet_app", "doctype", "pet_boarding_settings")

	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")

	# ------------------------------------------------------------------ check-in

	def test_check_in_stores_note_under_canonical_key(self):
		boarding = self._make_reserved_boarding()

		res = boarding_api.check_in_boarding(boarding_id=boarding.name, check_in_note="checkin")

		self.assertTrue(res["ok"], res)
		boarding.reload()
		self.assertEqual(boarding.check_in_note, "checkin")

	def test_check_in_accepts_the_shipped_builds_plural_spelling(self):
		"""The deployed desk build sends `notes`, not `check_in_note`."""
		boarding = self._make_reserved_boarding()

		boarding_api.check_in_boarding(boarding_id=boarding.name, notes="from the shipped build")

		boarding.reload()
		self.assertEqual(boarding.check_in_note, "from the shipped build")

	def test_check_in_note_survives_alongside_the_deposit(self):
		"""Both fields ride the same request; the deposit always persisted, the note did not."""
		boarding = self._make_reserved_boarding()

		boarding_api.check_in_boarding(boarding_id=boarding.name, deposit=10000, check_in_note="with deposit")

		boarding.reload()
		self.assertEqual(boarding.check_in_note, "with deposit")
		self.assertEqual(boarding.deposit, 10000)

	def test_blank_check_in_note_does_not_clear_an_existing_one(self):
		boarding = self._make_reserved_boarding()
		boarding.db_set("check_in_note", "typed earlier")

		boarding_api.check_in_boarding(boarding_id=boarding.name, check_in_note="   ", notes=None)

		boarding.reload()
		self.assertEqual(boarding.check_in_note, "typed earlier")

	# ----------------------------------------------------------------- check-out

	def test_check_out_stores_note_under_every_accepted_spelling(self):
		for key in ("check_out_note", "checkout_note", "checkout_notes"):
			with self.subTest(key=key):
				boarding = self._make_reserved_boarding()
				boarding_api.check_in_boarding(boarding_id=boarding.name)

				boarding_api.check_out_boarding(**{"boarding_id": boarding.name, key: f"left via {key}"})

				boarding.reload()
				self.assertEqual(boarding.check_out_note, f"left via {key}")

	# ------------------------------------------------------- returned, not just stored

	def test_get_boarding_detail_returns_both_notes(self):
		boarding = self._make_reserved_boarding()
		boarding_api.check_in_boarding(boarding_id=boarding.name, check_in_note="arrived fine")
		boarding_api.check_out_boarding(boarding_id=boarding.name, check_out_note="went home fine")

		res = boarding_api.get_boarding_detail(boarding_id=boarding.name)

		self.assertTrue(res["ok"], res)
		detail = res["data"]
		self.assertEqual(detail["check_in_note"], "arrived fine")
		self.assertEqual(detail["check_out_note"], "went home fine")
		# The nested booking shape the client also reads.
		self.assertEqual(detail["boarding"]["check_in_note"], "arrived fine")
		self.assertEqual(detail["boarding"]["check_out_note"], "went home fine")

	def test_detail_also_returns_the_spellings_the_shipped_build_reads(self):
		boarding = self._make_reserved_boarding()
		boarding_api.check_in_boarding(boarding_id=boarding.name, check_in_note="arrived fine")
		boarding_api.check_out_boarding(boarding_id=boarding.name, check_out_note="went home fine")

		detail = boarding_api.get_boarding_detail(boarding_id=boarding.name)["data"]

		self.assertEqual(detail["check_in_notes"], "arrived fine")
		self.assertEqual(detail["checkout_notes"], "went home fine")
		self.assertEqual(detail["checkout_note"], "went home fine")

	def test_list_boarding_records_returns_both_notes(self):
		boarding = self._make_reserved_boarding()
		boarding_api.check_in_boarding(boarding_id=boarding.name, check_in_note="on the list")

		res = boarding_api.list_boarding_records(search=boarding.name)

		self.assertTrue(res["ok"], res)
		# standardize_response nests the endpoint's own {"data": [...], "total": n}.
		rows = [row for row in res["data"]["data"] if row["name"] == boarding.name]
		self.assertEqual(len(rows), 1, res["data"])
		self.assertEqual(rows[0]["check_in_note"], "on the list")
		self.assertIn("check_out_note", rows[0])

	def test_check_in_response_carries_the_note_back(self):
		"""The dialog refetches from this response, so a missing key reads as a lost note."""
		boarding = self._make_reserved_boarding()

		res = boarding_api.check_in_boarding(boarding_id=boarding.name, check_in_note="echoed")

		self.assertEqual(res["data"]["boarding"]["check_in_note"], "echoed")

	def test_reservation_note_is_not_the_check_in_note(self):
		"""Two different fields. Merging them was explicitly out of scope."""
		boarding = self._make_reserved_boarding(note="bring his own bed")
		boarding_api.check_in_boarding(boarding_id=boarding.name, check_in_note="arrived without the bed")

		detail = boarding_api.get_boarding_detail(boarding_id=boarding.name)["data"]

		self.assertEqual(detail["note"], "bring his own bed")
		self.assertEqual(detail["check_in_note"], "arrived without the bed")

	# ------------------------------------------------------- the rename, completed

	def test_renamed_fields_are_the_only_ones_on_the_meta(self):
		"""The old fieldnames must be gone from the meta, not merely unused.

		Both were renamed in the desk UI, which adds the new columns and leaves the old
		ones in the table. Code that guards on `meta.has_field("checkout_notes")` then
		goes quietly False instead of erroring - which is exactly how the death cascade
		stopped recording its closure note without anyone noticing.
		"""
		meta = frappe.get_meta("Pet Boarding")
		self.assertTrue(meta.has_field("check_in_note"))
		self.assertTrue(meta.has_field("check_out_note"))
		self.assertFalse(meta.has_field("boarding_note"))
		self.assertFalse(meta.has_field("checkout_notes"))

	def test_death_cascade_guard_passes_and_note_appends(self):
		"""The cascade's has_field guard, and its append-not-replace behaviour."""
		from pet_app.utils.boarding_death_cascade import _death_checkout_note

		boarding = self._make_reserved_boarding()
		self.assertTrue(
			boarding.meta.has_field("check_out_note"),
			"the death cascade writes behind this guard; False makes it a silent no-op",
		)

		death_doc = frappe._dict(
			name="PDR-TEST-0001",
			death_datetime="2026-08-31 12:00:00",
			death_reason_category="Illness",
			death_reason=None,
		)

		first = _death_checkout_note(boarding, death_doc)
		self.assertIn("PDR-TEST-0001", first)

		boarding.check_out_note = first
		second = _death_checkout_note(boarding, death_doc)
		self.assertTrue(second.startswith(first), second)
		self.assertGreater(len(second), len(first), "an existing note must be kept, not replaced")

	# ------------------------------------------------------------------ defaults

	def test_get_boarding_defaults_returns_capacity_and_type(self):
		res = boarding_api.get_boarding_defaults()

		self.assertTrue(res["ok"], res)
		data = res["data"]
		self.assertEqual(
			data["max_pets_per_booking"],
			frappe.db.get_single_value("Pet Boarding Settings", "max_pets_per_booking"),
		)
		self.assertIn(data["default_boarding_type"], ("Travel", "Treatment"))
		self.assertEqual(data["boarding_types"], ["Travel", "Treatment"])

	def test_get_boarding_defaults_works_for_non_admin_staff(self):
		"""The reported symptom: staff saw capacity 1 while the setting said 7."""
		user = self._make_boarding_user()
		frappe.set_user(user)
		try:
			data = boarding_api.get_boarding_defaults()["data"]
		finally:
			frappe.set_user("Administrator")

		self.assertEqual(
			data["max_pets_per_booking"],
			frappe.db.get_single_value("Pet Boarding Settings", "max_pets_per_booking"),
		)
		self.assertEqual(data["default_boarding_type"], "Travel")

	def test_get_boarding_defaults_does_not_depend_on_settings_permission(self):
		"""B2 must stand on its own, with the B1 grant taken away.

		The `All` read row is what unblocks the SHIPPED client, which still reads the
		Single directly. This endpoint is the fix that stays fixed, so it is tested with
		that row removed - otherwise the grant would be silently carrying it, and a later
		permission cleanup would break booking again with the tests still green.
		"""
		user = self._make_boarding_user()
		frappe.db.delete("Custom DocPerm", {"parent": "Pet Boarding Settings", "role": "All"})
		frappe.clear_cache()
		try:
			self.assertFalse(
				frappe.has_permission("Pet Boarding Settings", "read", user=user),
				"the All grant should be gone for this test",
			)
			frappe.set_user(user)
			data = boarding_api.get_boarding_defaults()["data"]
		finally:
			frappe.set_user("Administrator")
			frappe.clear_cache()

		self.assertEqual(
			data["max_pets_per_booking"],
			frappe.db.get_single_value("Pet Boarding Settings", "max_pets_per_booking"),
		)

	def test_settings_link_fields_ignore_user_permissions(self):
		"""A branch-scoped user must still be able to READ the boarding policy.

		This is the second, non-obvious half of the permission bug. Granting the DocPerm
		is not enough: has_user_permission walks a document's Link fields and refuses the
		whole document when one names a value the reader is not permitted. The Single's
		`boarding_branch` is "hotel" while front-desk staff are scoped to their own branch,
		so the read failed at document level even for a System Manager. The field's own
		description says the boarding branch is never taken from the user processing the
		stay, so scoping a reader by it was always wrong.
		"""
		meta = frappe.get_meta("Pet Boarding Settings")
		links = [df for df in meta.fields if df.fieldtype == "Link"]
		self.assertTrue(links, "expected Link fields on the settings Single")
		unscoped = [df.fieldname for df in links if not df.ignore_user_permissions]
		self.assertEqual(
			unscoped,
			[],
			f"these Link fields can still refuse a scoped reader: {unscoped}",
		)

	# ------------------------------------------------------------------ fixtures

	def _make_boarding_user(self):
		suffix = frappe.generate_hash(length=8)
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"boarding.{suffix}@example.com",
				"first_name": f"Boarding {suffix}",
				"send_welcome_email": 0,
			}
		).insert(ignore_permissions=True)
		user.add_roles("Healthcare Practitioner")
		return user.name

	def _make_reserved_boarding(self, note=None):
		guardian, pet = self._make_guardian_pet()
		customer = get_or_create_customer_from_guardian(guardian.name)
		room = self._make_service_room()
		boarding = frappe.get_doc(
			{
				"doctype": "Pet Boarding",
				"service_room": room.name,
				"pet": pet.name,
				"guardian": guardian.name,
				"customer": customer,
				"boarding_type": "Treatment",
				"record_status": "Reserved",
				"status": "Open",
				"workflow_state": "Reserved",
				"reserved_at": frappe.utils.now_datetime(),
				"note": note,
				"billing_status": "Unbilled",
			}
		)
		boarding.insert(ignore_permissions=True)
		return boarding

	def _make_service_room(self):
		suffix = frappe.generate_hash(length=8)
		return frappe.get_doc(
			{
				"doctype": "Service Room",
				"room_code": f"ROOM-{suffix}",
				"room_name": f"Room {suffix}",
				"status": "Active",
			}
		).insert(ignore_permissions=True)

	def _make_guardian_pet(self):
		suffix = frappe.generate_hash(length=8)
		digits = "".join(ch for ch in suffix if ch.isdigit()).ljust(9, "0")[:9]
		guardian = frappe.get_doc(
			{
				"doctype": "Guardian",
				"phone": f"07{digits}",
				"full_name": f"Guardian {suffix}",
				"email_id": f"guardian.{suffix}@example.com",
			}
		).insert(ignore_permissions=True)
		pet = frappe.get_doc(
			{
				"doctype": "Pet",
				"pet_name": f"Pet {suffix}",
				"animal_species": "Mammal",
				"animal_type": "Dog",
				"birth_date": "2020-01-01",
				"weight": 10,
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
		return guardian, pet
