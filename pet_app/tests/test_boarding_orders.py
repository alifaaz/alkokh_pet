from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from pet_app.api.healthcare import boarding as boarding_api
from pet_app.utils.guardian_customer import get_or_create_customer_from_guardian


class TestBoardingOrders(FrappeTestCase):
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
			boarding_id=boarding.name, kind="radiology", template_id=care_service.name
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

	# ----------------------------------------------------------------- guarding

	def test_reserved_boarding_rejects_order(self):
		boarding = self._make_checked_in_boarding(record_status="Reserved")
		care_service = self._make_care_service("Lab")

		res = boarding_api.create_order(
			boarding_id=boarding.name, kind="lab", template_id=care_service.name
		)
		self.assertFalse(res["ok"])
		self.assertEqual(frappe.db.count("Lab", {"source_name": boarding.name}), 0)

	def test_invalid_kind_rejected(self):
		boarding = self._make_checked_in_boarding()
		res = boarding_api.create_order(
			boarding_id=boarding.name, kind="surgery", template_id="anything"
		)
		self.assertFalse(res["ok"])

	def test_missing_template_rejected(self):
		boarding = self._make_checked_in_boarding()
		res = boarding_api.create_order(boarding_id=boarding.name, kind="lab", template_id="")
		self.assertFalse(res["ok"])

	def test_duplicate_order_within_window_is_reused(self):
		boarding = self._make_checked_in_boarding()
		care_service = self._make_care_service("Lab")

		first = boarding_api.create_order(
			boarding_id=boarding.name, kind="lab", template_id=care_service.name
		)
		second = boarding_api.create_order(
			boarding_id=boarding.name, kind="lab", template_id=care_service.name
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

	def _make_item(self, label):
		suffix = frappe.generate_hash(length=8)
		return frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"{label} Item {suffix}",
				"item_name": f"{label} Item {suffix}",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 0,
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
