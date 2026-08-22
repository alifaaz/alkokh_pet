from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from pet_app.api.healthcare import boarding as boarding_api
from pet_app.utils.guardian_customer import get_or_create_customer_from_guardian


class TestBoardingOrders(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.reload_doc("pet_app", "doctype", "pet_billable_item")
		frappe.reload_doc("pet_app", "doctype", "pet_care_plan_item")
		frappe.reload_doc("pet_app", "doctype", "pet_boarding")

	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")

	# ------------------------------------------------------------------ success

	def test_create_lab_order_links_boarding_and_bills(self):
		boarding = self._make_checked_in_boarding()
		care_service = self._make_care_service("Lab")

		res = boarding_api.create_order(
			boarding_id=boarding.name,
			kind="lab",
			pet=boarding.pet,
			template_id=care_service.name,
			priority="High",
			note="Fasting sample",
		)

		self.assertTrue(res["ok"], res)
		data = res["data"]
		self.assertEqual(data["kind"], "lab")
		self.assertEqual(data["boarding_id"], boarding.name)
		self.assertEqual(data["linked_doctype"], "Lab")

		lab = frappe.get_doc("Lab", data["order_id"])
		self.assertFalse(lab.visit)
		self.assertEqual(lab.source_doctype, "Pet Boarding")
		self.assertEqual(lab.source_name, boarding.name)
		self.assertEqual(lab.pet, boarding.pet)
		self.assertEqual(lab.care_service, care_service.name)
		self.assertEqual(lab.priority, "High")
		# the pet is part of the order identity, not merely a field on it
		self.assertEqual(data["pet"], boarding.pet)
		self.assertTrue(lab.order_id.endswith(f"-{boarding.pet}"), lab.order_id)
		self.assertEqual(lab.status, "Ordered")

		# A matching billable row is appended to the boarding for checkout.
		boarding.reload()
		bill_rows = [r for r in boarding.billable_items if r.linked_name == lab.name]
		self.assertEqual(len(bill_rows), 1)
		row = bill_rows[0]
		self.assertEqual(row.item_type, "Lab")
		self.assertEqual(row.item_code, lab.item_code)
		self.assertEqual(row.qty, 1)
		self.assertEqual(row.rate, 25)
		self.assertEqual(row.status, "Billable")
		self.assertEqual(boarding.total_cost, 25)

	def test_create_radiology_order_creates_imaging(self):
		boarding = self._make_checked_in_boarding()
		care_service = self._make_care_service("Imaging")

		res = boarding_api.create_order(
			boarding_id=boarding.name, kind="radiology", pet=boarding.pet, template_id=care_service.name
		)

		self.assertTrue(res["ok"], res)
		imaging = frappe.get_doc("Imaging", res["data"]["order_id"])
		self.assertEqual(imaging.doctype, "Imaging")
		self.assertEqual(imaging.source_name, boarding.name)
		self.assertEqual(imaging.priority, "Routine")
		boarding.reload()
		self.assertTrue(any(r.item_type == "Imaging" and r.linked_name == imaging.name for r in boarding.billable_items))

	def test_create_service_order_creates_pet_care_service(self):
		boarding = self._make_checked_in_boarding()
		care_service = self._make_care_service("Grooming")

		res = boarding_api.create_order(
			boarding_id=boarding.name,
			kind="service",
			pet=boarding.pet,
			template_id=care_service.name,
			care_service_id=care_service.name,
		)

		self.assertTrue(res["ok"], res)
		service = frappe.get_doc("PetCareService", res["data"]["order_id"])
		self.assertEqual(service.source_doctype, "Pet Boarding")
		self.assertEqual(service.source_name, boarding.name)
		self.assertEqual(service.pet_id, boarding.pet)
		self.assertEqual(service.care_service_id, care_service.name)
		self.assertEqual(service.status, "pending")
		boarding.reload()
		self.assertTrue(any(r.item_type == "Service" and r.linked_name == service.name for r in boarding.billable_items))

	def test_create_medication_order_adds_boarding_billable_case_context(self):
		boarding = self._make_checked_in_boarding()
		episode = self._make_episode_for_boarding(boarding)
		medication = self._make_medication("Boarding Medication Order")

		res = boarding_api.create_order(
			boarding_id=boarding.name,
			kind="medication",
			pet=boarding.pet,
			template_id=medication.name,
			note="Boarding dose",
		)

		self.assertTrue(res["ok"], res)
		data = res["data"]
		self.assertEqual(data["kind"], "medication")
		self.assertEqual(data["boarding_id"], boarding.name)
		self.assertEqual(data["linked_doctype"], "Medication")
		self.assertEqual(data["linked_name"], medication.name)
		boarding.reload()
		rows = [row for row in boarding.billable_items if row.linked_doctype == "Medication" and row.linked_name == medication.name]
		self.assertEqual(len(rows), 1)
		row = rows[0]
		self.assertEqual(row.item_type, "Medication")
		self.assertEqual(row.item_code, medication.linked_item)
		self.assertEqual(row.status, "Billable")
		self.assertEqual(row.care_episode, episode.name)
		self.assertEqual(row.dispense_status, "Pending Dispense")
		self.assertEqual(row.dispensed_qty, 0)

	def test_dispense_boarding_medication_updates_row_without_stock_entry(self):
		boarding = self._make_checked_in_boarding()
		self._make_episode_for_boarding(boarding)
		medication = self._make_medication("Boarding Medication Dispense")
		order = boarding_api.create_order(
			boarding_id=boarding.name,
			kind="medication",
			pet=boarding.pet,
			template_id=medication.name,
		)
		self.assertTrue(order["ok"], order)
		item_id = order["data"]["item_id"]
		stock_entries_before = frappe.db.count("Stock Entry")

		dispensed = boarding_api.dispense_medication(
			boarding=boarding.name,
			item_id=item_id,
			note="Handed to boarding team",
		)

		self.assertTrue(dispensed["ok"], dispensed)
		self.assertEqual(dispensed["data"]["item"]["dispense_status"], "Dispensed")
		self.assertEqual(dispensed["data"]["item"]["dispensed_qty"], 1)
		self.assertEqual(dispensed["data"]["item"]["dispensed_by"], "Administrator")
		self.assertTrue(dispensed["data"]["item"]["dispensed_at"])
		self.assertIn("Handed to boarding team", dispensed["data"]["item"]["note"])
		self.assertEqual(frappe.db.count("Stock Entry"), stock_entries_before)

		second = boarding_api.dispense_medication(boarding=boarding.name, item_id=item_id)
		self.assertTrue(second["ok"], second)
		self.assertTrue(second["data"]["idempotent"])
		self.assertEqual(second["data"]["item"]["dispensed_qty"], 1)
		self.assertEqual(frappe.db.count("Stock Entry"), stock_entries_before)

	def test_record_medication_given_marks_plan_done_idempotently(self):
		boarding = self._make_checked_in_boarding()
		episode = self._make_episode_for_boarding(boarding)
		doctor = self._make_doctor()
		visit = self._make_visit_for_episode(episode, doctor)
		plan = self._make_medication_plan_item(episode, visit, doctor)
		episode_status_before = frappe.db.get_value("Pet Care Episode", episode.name, "episode_status")

		given = boarding_api.record_medication_given(
			boarding=boarding.name,
			plan_item=plan.name,
			note="Ate with food",
			given_at="2026-07-25 14:00:00",
		)

		self.assertTrue(given["ok"], given)
		self.assertFalse(given["data"]["idempotent"])
		self.assertEqual(given["data"]["plan_item"]["status"], "Done")
		self.assertEqual(given["data"]["plan_item"]["completed_by"], "Administrator")
		self.assertEqual(given["data"]["plan_item"]["completion_note"], "Ate with food")
		self.assertEqual(frappe.db.get_value("Pet Care Episode", episode.name, "episode_status"), episode_status_before)
		self.assertNotIn(episode_status_before, {"Closed", "Cancelled"})

		second = boarding_api.record_medication_given(boarding=boarding.name, plan_item=plan.name)
		self.assertTrue(second["ok"], second)
		self.assertTrue(second["data"]["idempotent"])
		self.assertEqual(second["data"]["plan_item"]["status"], "Done")

	def test_record_medication_given_rejects_wrong_pet_case_and_non_medication(self):
		boarding = self._make_checked_in_boarding()
		active_episode = self._make_episode_for_boarding(boarding)
		doctor = self._make_doctor()
		active_visit = self._make_visit_for_episode(active_episode, doctor)
		non_medication = self._make_monitoring_plan_item(active_episode, active_visit, doctor)

		closed_episode = self._make_episode_for_boarding(boarding, primary_doctor=doctor, status="Closed")
		closed_visit = self._make_visit_for_episode(closed_episode, doctor)
		wrong_case = self._make_medication_plan_item(closed_episode, closed_visit, doctor, save_visit=False)

		other_boarding = self._make_checked_in_boarding()
		other_episode = self._make_episode_for_boarding(other_boarding, primary_doctor=doctor)
		other_visit = self._make_visit_for_episode(other_episode, doctor)
		wrong_pet = self._make_medication_plan_item(other_episode, other_visit, doctor)

		for plan in (non_medication, wrong_case, wrong_pet):
			result = boarding_api.record_medication_given(boarding=boarding.name, plan_item=plan.name)
			self.assertFalse(result["ok"], result)
			self.assertEqual(result["meta"]["code"], "VALIDATION_ERROR")
			self.assertNotEqual(frappe.db.get_value("Pet Care Plan Item", plan.name, "status"), "Done")

	# ----------------------------------------------------------------- guarding

	def test_reserved_boarding_rejects_order(self):
		boarding = self._make_checked_in_boarding(record_status="Reserved")
		care_service = self._make_care_service("Lab")

		res = boarding_api.create_order(
			boarding_id=boarding.name, kind="lab", pet=boarding.pet, template_id=care_service.name
		)
		self.assertFalse(res["ok"])
		self.assertEqual(frappe.db.count("Lab", {"source_name": boarding.name}), 0)

	def test_invalid_kind_rejected(self):
		boarding = self._make_checked_in_boarding()
		res = boarding_api.create_order(
			boarding_id=boarding.name, kind="surgery", pet=boarding.pet, template_id="anything"
		)
		self.assertFalse(res["ok"])

	def test_missing_template_rejected(self):
		boarding = self._make_checked_in_boarding()
		res = boarding_api.create_order(boarding_id=boarding.name, kind="lab", pet=boarding.pet, template_id="")
		self.assertFalse(res["ok"])

	def test_duplicate_order_within_window_is_reused(self):
		boarding = self._make_checked_in_boarding()
		care_service = self._make_care_service("Lab")

		first = boarding_api.create_order(
			boarding_id=boarding.name, kind="lab", pet=boarding.pet, template_id=care_service.name
		)
		second = boarding_api.create_order(
			boarding_id=boarding.name, kind="lab", pet=boarding.pet, template_id=care_service.name
		)

		self.assertTrue(first["ok"])
		self.assertTrue(second["ok"])
		self.assertEqual(first["data"]["order_id"], second["data"]["order_id"])
		self.assertTrue(second["data"]["reused"])
		self.assertEqual(frappe.db.count("Lab", {"source_name": boarding.name}), 1)
		boarding.reload()
		self.assertEqual(len([r for r in boarding.billable_items if r.item_type == "Lab"]), 1)

	def test_lab_without_visit_or_source_is_rejected(self):
		"""Regression: a Lab with neither a visit nor a source link must fail."""
		care_service = self._make_care_service("Lab")
		_, pet = self._make_guardian_pet()
		lab = frappe.get_doc(
			{"doctype": "Lab", "pet": pet.name, "care_service": care_service.name, "status": "Ordered"}
		)
		with self.assertRaises(frappe.ValidationError):
			lab.insert(ignore_permissions=True)

	# ------------------------------------------------------------------ helpers

	# ------------------------------------------------------------------ provider

	def test_create_order_stores_provider_on_service_and_billable_row(self):
		"""The reported defect: the operator picks an assignee and the record keeps it."""
		boarding = self._make_checked_in_boarding()
		care_service = self._make_care_service("Service")
		practitioner = self._make_doctor()

		res = boarding_api.create_order(
			boarding_id=boarding.name,
			kind="service",
			pet=boarding.pet,
			template_id=care_service.name,
			provider=practitioner.name,
		)

		self.assertTrue(res["ok"], res)
		data = res["data"]
		# On the order document, because PetCareService has somewhere to put it.
		service = frappe.get_doc("PetCareService", data["order_id"])
		self.assertEqual(service.provider, practitioner.name)
		# And echoed on the billable row, which is the only proof the client gets.
		rows = [r for r in data["billable_items"] if r["linked_name"] == service.name]
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["provider"], practitioner.name)
		self.assertEqual(rows[0]["provider_name"], practitioner.practitioner_name)

	def test_create_order_without_provider_leaves_it_unset(self):
		"""Absence stays valid and must never become the calling user.

		A defaulted assignee is indistinguishable from a real one, and the desk would lose
		the ability to see what still needs assigning.
		"""
		boarding = self._make_checked_in_boarding()
		care_service = self._make_care_service("Service")

		res = boarding_api.create_order(
			boarding_id=boarding.name,
			kind="service",
			pet=boarding.pet,
			template_id=care_service.name,
		)

		self.assertTrue(res["ok"], res)
		data = res["data"]
		service = frappe.get_doc("PetCareService", data["order_id"])
		self.assertFalse(service.provider)
		rows = [r for r in data["billable_items"] if r["linked_name"] == service.name]
		self.assertFalse(rows[0]["provider"])
		self.assertFalse(rows[0]["provider_name"])

	def test_create_order_accepts_user_email_and_resolves_to_practitioner(self):
		boarding = self._make_checked_in_boarding()
		care_service = self._make_care_service("Service")
		practitioner = self._make_doctor()
		user = self._make_user()
		practitioner.db_set("user_id", user, update_modified=False)

		res = boarding_api.create_order(
			boarding_id=boarding.name,
			kind="service",
			pet=boarding.pet,
			template_id=care_service.name,
			provider=user,
		)

		self.assertTrue(res["ok"], res)
		# Resolved, not stored: what comes back is always the docname.
		service = frappe.get_doc("PetCareService", res["data"]["order_id"])
		self.assertEqual(service.provider, practitioner.name)

	def test_create_order_refuses_unknown_provider_and_creates_nothing(self):
		"""A bad assignee refuses the call rather than being dropped.

		Resolution happens before the first write, so a refusal cannot leave an order
		document behind and fail on the boarding save.
		"""
		boarding = self._make_checked_in_boarding()
		care_service = self._make_care_service("Lab")
		before_labs = frappe.db.count("Lab")
		before_rows = len(frappe.get_doc("Pet Boarding", boarding.name).billable_items or [])

		res = boarding_api.create_order(
			boarding_id=boarding.name,
			kind="lab",
			pet=boarding.pet,
			template_id=care_service.name,
			provider="HCP-DOES-NOT-EXIST",
		)

		self.assertFalse(res["ok"], res)
		self.assertIn("Healthcare Practitioner", res["errors"][0]["message"])
		self.assertEqual(frappe.db.count("Lab"), before_labs)
		self.assertEqual(len(frappe.get_doc("Pet Boarding", boarding.name).billable_items or []), before_rows)

	def test_create_lab_order_records_provider_on_the_billable_row(self):
		"""Lab has a `doctor`, not a `provider`, so the row is where the assignment lives."""
		boarding = self._make_checked_in_boarding()
		care_service = self._make_care_service("Lab")
		practitioner = self._make_doctor()

		res = boarding_api.create_order(
			boarding_id=boarding.name,
			kind="lab",
			pet=boarding.pet,
			template_id=care_service.name,
			provider=practitioner.name,
		)

		self.assertTrue(res["ok"], res)
		data = res["data"]
		rows = [r for r in data["billable_items"] if r["linked_name"] == data["order_id"]]
		self.assertEqual(rows[0]["provider"], practitioner.name)
		self.assertEqual(rows[0]["provider_name"], practitioner.practitioner_name)

	def test_provider_does_not_change_what_is_charged(self):
		boarding = self._make_checked_in_boarding()
		care_service = self._make_care_service("Service")
		practitioner = self._make_doctor()

		unassigned = boarding_api.create_order(
			boarding_id=boarding.name, kind="service", pet=boarding.pet, template_id=care_service.name
		)
		assigned = boarding_api.create_order(
			boarding_id=boarding.name,
			kind="service",
			pet=boarding.pet,
			template_id=self._make_care_service("Service").name,
			provider=practitioner.name,
		)

		def _row(res):
			return next(r for r in res["data"]["billable_items"] if r["linked_name"] == res["data"]["order_id"])

		self.assertEqual(_row(unassigned)["rate"], _row(assigned)["rate"])
		self.assertEqual(_row(unassigned)["amount"], _row(assigned)["amount"])

	def _make_user(self):
		email = f"provider-{frappe.generate_hash(length=8)}@example.com"
		frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": "Provider", "send_welcome_email": 0}
		).insert(ignore_permissions=True)
		return email

	def _make_checked_in_boarding(self, record_status="Checked In"):
		guardian, pet = self._make_guardian_pet()
		customer = get_or_create_customer_from_guardian(guardian.name)
		room = self._make_service_room()
		status = "Open" if record_status in ("Reserved", "Checked In") else "Closed"
		boarding = frappe.get_doc(
			{
				"doctype": "Pet Boarding",
				"service_room": room.name,
				"pet": pet.name,
				"guardian": guardian.name,
				"customer": customer,
				"boarding_type": "Treatment",
				"record_status": record_status,
				"status": status,
			}
		)
		if record_status == "Checked In":
			boarding.check_in = frappe.utils.now_datetime()
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

	def _ensure_clinic_price_list(self):
		if frappe.db.exists("Price List", "Standard Selling"):
			return "Standard Selling"
		return frappe.get_doc(
			{
				"doctype": "Price List",
				"price_list_name": "Standard Selling",
				"enabled": 1,
				"selling": 1,
				"currency": "USD",
			}
		).insert(ignore_permissions=True, ignore_mandatory=True).name

	def _make_item(self, label, *, item_group=None, is_stock_item=0, standard_rate=0):
		suffix = frappe.generate_hash(length=8)
		return frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"{label} Item {suffix}",
				"item_name": f"{label} Item {suffix}",
				"item_group": item_group or "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": is_stock_item,
				"is_sales_item": 1,
				"is_purchase_item": 1,
				"standard_rate": standard_rate,
			}
		).insert(ignore_permissions=True, ignore_mandatory=True)

	def _make_care_service(self, label):
		suffix = frappe.generate_hash(length=8)
		item = self._make_item(label)
		category = frappe.get_doc(
			{"doctype": "CategoryCareServices", "category_name": f"{label} Category {suffix}"}
		).insert(ignore_permissions=True)
		return frappe.get_doc(
			{
				"doctype": "CareService template",
				"service_name": f"{label} {suffix}",
				"animal_species": "Mammal",
				"frequency": "onetime",
				"category_id": category.name,
				"item_code": item.name,
				"default_price": 25,
				"price_list": self._ensure_clinic_price_list(),
			}
		).insert(ignore_permissions=True)

	def _make_episode_for_boarding(self, boarding, *, primary_doctor=None, status="Open"):
		primary_doctor = primary_doctor or self._make_doctor()
		return frappe.get_doc(
			{
				"doctype": "Pet Care Episode",
				"pet": boarding.pet,
				"guardian": boarding.guardian,
				"customer": boarding.customer,
				"primary_doctor": primary_doctor.name,
				"episode_title": "Boarding Medication Case",
				"episode_type": "Boarding Medical Case",
				"episode_status": status,
			}
		).insert(ignore_permissions=True)

	def _make_visit_for_episode(self, episode, doctor):
		data = {
			"doctype": "Vet Visit",
			"guardian": episode.guardian,
			"customer": episode.customer,
			"animal_patient": episode.pet,
			"doctor": doctor.name,
			"status": "In Progress",
			"priority": "Normal",
			"visit_type": "Consultation",
			"visit_datetime": frappe.utils.now_datetime(),
		}
		meta = frappe.get_meta("Vet Visit")
		if meta.has_field("primary_practitioner"):
			data["primary_practitioner"] = doctor.name
		if meta.has_field("care_episode"):
			data["care_episode"] = episode.name
		return frappe.get_doc(data).insert(ignore_permissions=True)

	def _make_medication_plan_item(self, episode, visit, doctor, *, save_visit=True):
		item = self._make_item("Boarding Scheduled Dose")
		row_data = {
			"doctype": "Vet Visit Medication Item",
			"parent": visit.name,
			"parenttype": "Vet Visit",
			"parentfield": "prescribed_medications",
			"idx": len(visit.get("prescribed_medications") or []) + 1,
			"medication_item": item.name,
			"qty": 1,
			"rate": 10,
			"dosage": "1 tablet",
			"frequency": "BID",
			"duration_days": 3,
		}
		if save_visit:
			visit.append(
				"prescribed_medications",
				{
					"medication_item": item.name,
					"qty": 1,
					"rate": 10,
					"dosage": "1 tablet",
					"frequency": "BID",
					"duration_days": 3,
				},
			)
			visit.save(ignore_permissions=True)
			visit.reload()
			medication_row = visit.prescribed_medications[-1]
		else:
			medication_row = frappe.get_doc(row_data)
			medication_row.set_new_name()
			medication_row.db_insert()
		return frappe.get_doc(
			{
				"doctype": "Pet Care Plan Item",
				"pet": episode.pet,
				"guardian": episode.guardian,
				"customer": episode.customer,
				"care_episode": episode.name,
				"source_visit": visit.name,
				"doctor": doctor.name,
				"plan_type": "Medication",
				"title": "Give scheduled medication",
				"status": "Planned",
				"priority": "Normal",
				"linked_doctype": "Vet Visit Medication Item",
				"linked_name": medication_row.name,
			}
		).insert(ignore_permissions=True)

	def _make_monitoring_plan_item(self, episode, visit, doctor):
		return frappe.get_doc(
			{
				"doctype": "Pet Care Plan Item",
				"pet": episode.pet,
				"guardian": episode.guardian,
				"customer": episode.customer,
				"care_episode": episode.name,
				"source_visit": visit.name,
				"doctor": doctor.name,
				"plan_type": "Monitoring",
				"title": "Check appetite",
				"status": "Planned",
				"priority": "Normal",
			}
		).insert(ignore_permissions=True)

	def _make_medication(self, label):
		suffix = frappe.generate_hash(length=8)
		item = self._make_item(
			label,
			item_group=self._leaf_item_group(),
			is_stock_item=1,
			standard_rate=15,
		)
		return frappe.get_doc(
			{
				"doctype": "Medication",
				"medication_name": f"{label} {suffix}",
				"linked_item": item.name,
				"item_group": item.item_group,
				"dosage_form_or_unit": item.stock_uom,
				"default_price": 15,
			}
		).insert(ignore_permissions=True)

	def _make_doctor(self):
		suffix = frappe.generate_hash(length=8)
		digits = "".join(ch for ch in suffix if ch.isdigit()).ljust(9, "0")[:9]
		return frappe.get_doc(
			{
				"doctype": "Healthcare Practitioner",
				"practitioner_name": f"Practitioner {suffix}",
				"practitioner_type": "Doctor",
				"phone": f"07{digits}",
			}
		).insert(ignore_permissions=True)

	def _leaf_item_group(self):
		groups = frappe.get_all("Item Group", filters={"is_group": 0}, pluck="name", limit=1)
		return groups[0] if groups else "All Item Groups"
