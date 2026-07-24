from __future__ import annotations

from datetime import datetime, timedelta

import frappe
from frappe.tests.utils import FrappeTestCase

from pet_app.api import (
	boarding_updates,
	delivery,
	guardian_portal,
	import_tools,
	inventory,
	membership,
	mortality,
	scheduling,
)
from pet_app.api.v1 import guardian as v1_guardian
from pet_app.tasks import reminders
from pet_app.utils.guardian_customer import get_or_create_customer_from_guardian


class TestP2GrowthAndMortality(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_guardian_portal_access_and_released_result_visibility(self):
		visit = self._make_visit()
		guardian_user = self._make_user("p2.guardian", ["Guardians"])
		frappe.db.set_value("Guardian", visit.guardian, "user_id", guardian_user.name)
		service = self._make_care_service("P2 Portal Lab")
		released = frappe.get_doc({"doctype": "Lab", "visit": visit.name, "care_service": service.name}).insert(ignore_permissions=True)
		hidden = frappe.get_doc({"doctype": "Lab", "visit": visit.name, "care_service": service.name}).insert(ignore_permissions=True)
		frappe.db.set_value(
			"Lab",
			released.name,
			{"status": "Released", "result": "Owner safe result", "result_visibility": "Guardian Visible", "released_at": frappe.utils.now_datetime()},
		)
		frappe.db.set_value("Lab", hidden.name, {"status": "Result Entered", "result": "Internal draft", "result_visibility": "Internal Only"})
		other = self._make_visit()

		frappe.set_user(guardian_user.name)
		timeline = guardian_portal.get_pet_medical_timeline(pet=visit.animal_patient)
		self.assertTrue(timeline["ok"])
		lab_names = {event["name"] for event in timeline["data"]["events"] if event["type"] == "lab"}
		self.assertIn(released.name, lab_names)
		self.assertNotIn(hidden.name, lab_names)

		blocked = guardian_portal.get_pet_medical_timeline(pet=other.animal_patient)
		self.assertFalse(blocked["ok"])
		self.assertEqual(blocked["meta"]["code"], "PERMISSION_ERROR")

	def test_reminder_deduplication(self):
		guardian, pet = self._make_guardian_pet()
		record = frappe.get_doc(
			{
				"doctype": "Pet Vaccination Record",
				"pet": pet.name,
				"guardian": guardian.name,
				"vaccine_name": "Rabies",
				"next_due_date": frappe.utils.nowdate(),
				"reminder_enabled": 1,
			}
		).insert(ignore_permissions=True)

		reminders.enqueue_due_reminders()
		reminders.enqueue_due_reminders()

		self.assertEqual(frappe.db.count("Pet Reminder", {"reference_doctype": "Pet Vaccination Record", "reference_name": record.name}), 1)

	def test_membership_discount_status(self):
		guardian, _pet = self._make_guardian_pet()
		plan = frappe.get_doc(
			{
				"doctype": "Pet Membership Plan",
				"plan_name": f"P2 Plan {frappe.generate_hash(length=8)}",
				"discount_percent": 12,
				"points_multiplier": 2,
				"active": 1,
			}
		).insert(ignore_permissions=True)

		result = membership.subscribe_guardian(guardian=guardian.name, plan=plan.name)
		self.assertTrue(result["ok"])
		status = membership.get_subscription_status(guardian=guardian.name)
		self.assertEqual(status["data"]["subscription"]["discount_percent"], 12)

	def test_scheduling_conflict_and_offline_idempotency(self):
		guardian, pet = self._make_guardian_pet()
		doctor = self._make_doctor()
		start = datetime.now() + timedelta(days=1, hours=2)
		payload = {
			"guardian": guardian.name,
			"pet": pet.name,
			"doctor": doctor.name,
			"scheduled_time": start,
			"duration_minutes": 30,
			"idempotency_key": f"p2-{frappe.generate_hash(length=10)}",
		}

		first = scheduling.book_appointment(data=payload)
		second = scheduling.book_appointment(data=payload)
		conflict = scheduling.book_appointment(data={**payload, "idempotency_key": f"p2-{frappe.generate_hash(length=10)}"})

		self.assertTrue(first["ok"])
		self.assertTrue(second["ok"])
		self.assertEqual(first["data"]["appointment"]["name"], second["data"]["appointment"]["name"])
		self.assertFalse(conflict["ok"])
		self.assertEqual(conflict["meta"]["code"], "CONFLICT")

	def test_inventory_low_stock_alert(self):
		item = self._make_item("P2 Low Stock")
		rule = frappe.get_doc(
			{
				"doctype": "Inventory Alert Rule",
				"rule_name": f"P2 Low Stock {frappe.generate_hash(length=8)}",
				"alert_type": "Low Stock",
				"item_code": item.name,
				"min_qty": 5,
				"active": 1,
			}
		).insert(ignore_permissions=True)

		result = inventory.get_low_stock_alerts()
		self.assertTrue(result["ok"])
		self.assertIn(rule.name, {row["rule"] for row in result["data"]["alerts"]})

	def test_boarding_owner_update_privacy(self):
		boarding = self._make_boarding()
		guardian_user = self._make_user("p2.boarding.guardian", ["Guardians"])
		frappe.db.set_value("Guardian", boarding.guardian, "user_id", guardian_user.name)
		frappe.get_doc(
			{
				"doctype": "Pet Boarding Daily Log",
				"boarding": boarding.name,
				"pet": boarding.pet,
				"log_datetime": frappe.utils.now_datetime(),
				"summary": "Ate dinner",
				"internal_note": "Private staff note",
			}
		).insert(ignore_permissions=True)

		frappe.set_user(guardian_user.name)
		result = boarding_updates.get_owner_boarding_updates(boarding=boarding.name)
		self.assertTrue(result["ok"])
		self.assertNotIn("internal_note", result["data"]["updates"][0])

	def test_delivery_proof_upload(self):
		assignment = frappe.get_doc({"doctype": "Delivery Assignment", "status": "Assigned"}).insert(
			ignore_permissions=True, ignore_mandatory=True
		)
		proof = delivery.upload_delivery_proof(assignment=assignment.name, file="/files/p2-proof.jpg")
		self.assertTrue(proof["ok"])
		self.assertEqual(proof["data"]["proof"]["file"], "/files/p2-proof.jpg")

	def test_import_dry_run_and_commit(self):
		suffix = frappe.generate_hash(length=8)
		dry = import_tools.create_import_job(import_type="Guardians", rows=[{"phone": f"077{self._digits(suffix, 8)}", "full_name": "Dry Run"}], dry_run=1)
		self.assertTrue(dry["ok"])
		self.assertEqual(dry["meta"]["total_errors"], 0)

		phone = f"078{self._digits(suffix, 8)}"
		commit = import_tools.create_import_job(import_type="Guardians", rows=[{"phone": phone, "full_name": "Committed"}], dry_run=0)
		self.assertTrue(commit["ok"])
		self.assertTrue(frappe.db.exists("Guardian", {"phone": phone}))

	def test_versioned_guardian_api_compatibility(self):
		guardian, _pet = self._make_guardian_pet()
		user = self._make_user("p2.v1.guardian", ["Guardians"])
		frappe.db.set_value("Guardian", guardian.name, "user_id", user.name)

		frappe.set_user(user.name)
		result = v1_guardian.get_my_pets_dashboard()
		self.assertTrue(result["ok"])
		self.assertIn("pets", result["data"])

	def test_report_pet_death_from_visit(self):
		visit = self._make_visit()
		reason = self._make_death_reason("Disease / Illness")

		result = mortality.report_pet_death(pet=visit.animal_patient, death_reason=reason.name, source_doctype="Vet Visit", source_name=visit.name)

		self.assertTrue(result["ok"])
		self.assertEqual(frappe.db.get_value("Vet Visit", visit.name, "death_record"), result["data"]["death_record"]["name"])

	def test_report_pet_death_from_procedure(self):
		procedure = self._make_procedure()
		reason = self._make_death_reason("Procedure Complication")

		result = mortality.report_pet_death(pet=procedure.pet, death_reason=reason.name, source_doctype="Pet Procedure", source_name=procedure.name)

		self.assertTrue(result["ok"])
		self.assertEqual(frappe.db.get_value("Pet Procedure", procedure.name, "death_record"), result["data"]["death_record"]["name"])

	def test_report_pet_death_from_boarding(self):
		boarding = self._make_boarding()
		reason = self._make_death_reason("Boarding Incident")

		result = mortality.report_pet_death(pet=boarding.pet, death_reason=reason.name, source_doctype="Pet Boarding", source_name=boarding.name)

		self.assertTrue(result["ok"])
		self.assertEqual(frappe.db.get_value("Pet Boarding", boarding.name, "death_record"), result["data"]["death_record"]["name"])

	def test_boarding_death_cascade_marks_pet_deceased_and_closes_boarding(self):
		boarding = self._make_checked_in_boarding()
		reason = self._make_death_reason("Boarding Incident")

		result = mortality.report_pet_death(
			pet=boarding.pet,
			death_reason=reason.name,
			source_doctype="Pet Boarding",
			source_name=boarding.name,
		)
		self.assertTrue(result["ok"])
		record = result["data"]["death_record"]["name"]

		pet = frappe.db.get_value("Pet", boarding.pet, ["is_deceased", "status", "death_record"], as_dict=True)
		self.assertEqual(pet.is_deceased, 1)
		self.assertEqual(pet.status, "Deceased")
		self.assertEqual(pet.death_record, record)

		bd = frappe.db.get_value(
			"Pet Boarding",
			boarding.name,
			["record_status", "status", "boarding_outcome", "death_during_boarding", "death_record"],
			as_dict=True,
		)
		self.assertEqual(bd.record_status, "Checked Out")
		self.assertEqual(bd.status, "Closed")
		self.assertEqual(bd.boarding_outcome, "Death")
		self.assertEqual(bd.death_during_boarding, 1)
		self.assertEqual(bd.death_record, record)

	def test_boarding_death_settles_billing_up_to_death(self):
		boarding = self._make_checked_in_boarding(with_billable=True)
		reason = self._make_death_reason("Natural Death")

		result = mortality.report_pet_death(
			pet=boarding.pet,
			death_reason=reason.name,
			death_datetime=frappe.utils.now_datetime(),
			source_doctype="Pet Boarding",
			source_name=boarding.name,
		)
		self.assertTrue(result["ok"])

		bd = frappe.db.get_value(
			"Pet Boarding", boarding.name, ["sales_invoice", "billing_status", "check_out"], as_dict=True
		)
		self.assertTrue(bd.sales_invoice)
		self.assertEqual(bd.billing_status, "Invoiced")
		self.assertTrue(bd.check_out)
		self.assertTrue(frappe.db.exists("Sales Invoice", bd.sales_invoice))

	def test_boarding_death_closes_open_clinical_docs(self):
		# Build a complete in-progress visit (with case sheet + doctor) for a pet,
		# then a boarding for the same pet, and an open Lab order on the visit.
		visit = self._make_visit()
		pet = frappe.get_doc("Pet", visit.animal_patient)
		guardian = frappe.get_doc("Guardian", visit.guardian)
		boarding = self._make_checked_in_boarding(guardian=guardian, pet=pet)
		service = self._make_care_service_standard("Cascade Lab")
		lab = frappe.get_doc(
			{"doctype": "Lab", "visit": visit.name, "pet": pet.name, "care_service": service.name, "status": "Ordered"}
		).insert(ignore_permissions=True)
		reason = self._make_death_reason("Natural Death")

		result = mortality.report_pet_death(
			pet=pet.name,
			death_reason=reason.name,
			source_doctype="Pet Boarding",
			source_name=boarding.name,
		)
		self.assertTrue(result["ok"])

		self.assertEqual(frappe.db.get_value("Lab", lab.name, "status"), "Cancelled")
		self.assertEqual(frappe.db.get_value("Vet Visit", visit.name, "status"), "Cancelled")
		self.assertEqual(frappe.db.get_value("Vet Case Sheet", visit.case_sheet, "status"), "Closed")

	def test_boarding_death_is_idempotent(self):
		boarding = self._make_checked_in_boarding(with_billable=True)
		reason = self._make_death_reason("Natural Death")

		first = mortality.report_pet_death(
			pet=boarding.pet, death_reason=reason.name, source_doctype="Pet Boarding", source_name=boarding.name
		)
		second = mortality.report_pet_death(
			pet=boarding.pet, death_reason=reason.name, source_doctype="Pet Boarding", source_name=boarding.name
		)

		self.assertTrue(first["ok"])
		self.assertTrue(second["ok"])
		# Same record returned, no duplicate created.
		self.assertEqual(first["data"]["death_record"]["name"], second["data"]["death_record"]["name"])
		self.assertEqual(frappe.db.count("Pet Death Record", {"pet": boarding.pet, "status": ["!=", "Cancelled"]}), 1)
		# Billing settled exactly once (not double-invoiced).
		invoice = frappe.db.get_value("Pet Boarding", boarding.name, "sales_invoice")
		self.assertTrue(invoice)
		self.assertEqual(frappe.db.count("Pet Boarding", {"name": boarding.name, "sales_invoice": invoice}), 1)

	def test_boarding_death_tolerates_already_closed_boarding(self):
		# A boarding still only Reserved (never checked in) -> no invoice, but still
		# closed and linked without erroring.
		boarding = self._make_boarding()
		reason = self._make_death_reason("Natural Death")

		result = mortality.report_pet_death(
			pet=boarding.pet, death_reason=reason.name, source_doctype="Pet Boarding", source_name=boarding.name
		)
		self.assertTrue(result["ok"])
		record = result["data"]["death_record"]["name"]
		bd = frappe.db.get_value(
			"Pet Boarding", boarding.name, ["record_status", "death_record", "sales_invoice"], as_dict=True
		)
		self.assertEqual(bd.record_status, "Checked Out")
		self.assertEqual(bd.death_record, record)
		self.assertFalse(bd.sales_invoice)

	def test_report_pet_death_external_without_source(self):
		guardian, pet = self._make_guardian_pet()
		reason = self._make_death_reason("External / Reported By Guardian")

		result = mortality.report_pet_death(pet=pet.name, guardian=guardian.name, death_reason=reason.name)

		self.assertTrue(result["ok"])
		self.assertFalse(result["data"]["death_record"].get("source_doctype"))

	def test_death_record_dynamic_link_validation(self):
		visit = self._make_visit()
		other = self._make_visit()
		reason = self._make_death_reason("Disease / Illness")

		result = mortality.report_pet_death(pet=visit.animal_patient, death_reason=reason.name, source_doctype="Vet Visit", source_name=other.name)

		self.assertFalse(result["ok"])

	def test_only_one_active_death_record_per_pet(self):
		guardian, pet = self._make_guardian_pet()
		reason = self._make_death_reason("Disease / Illness")
		first = mortality.report_pet_death(pet=pet.name, guardian=guardian.name, death_reason=reason.name)
		second = mortality.report_pet_death(pet=pet.name, guardian=guardian.name, death_reason=reason.name)

		self.assertTrue(first["ok"])
		self.assertFalse(second["ok"])

	def test_confirm_death_requires_doctor_or_manager(self):
		guardian, pet = self._make_guardian_pet()
		reason = self._make_death_reason("Disease / Illness")
		record = mortality.report_pet_death(pet=pet.name, guardian=guardian.name, death_reason=reason.name)["data"]["death_record"]["name"]
		user = self._make_user("p2.reception", ["Visit"])

		frappe.set_user(user.name)
		result = mortality.confirm_pet_death(death_record=record)
		self.assertFalse(result["ok"])

	def test_finalize_death_sets_pet_deceased_and_cancels_future_appointments(self):
		visit = self._make_visit()
		appointment = self._make_appointment(frappe.get_doc("Guardian", visit.guardian), frappe.get_doc("Pet", visit.animal_patient))
		reason = self._make_death_reason("Disease / Illness")
		record = mortality.report_pet_death(pet=visit.animal_patient, death_reason=reason.name, source_doctype="Vet Visit", source_name=visit.name)["data"]["death_record"]["name"]

		result = mortality.finalize_pet_death(death_record=record)

		self.assertTrue(result["ok"])
		pet = frappe.db.get_value("Pet", visit.animal_patient, ["is_deceased", "death_record", "pet_status"], as_dict=True)
		self.assertEqual(pet.death_record, record)
		self.assertEqual(pet.pet_status, "Deceased")
		self.assertEqual(frappe.db.get_value("Appointment", appointment.name, "status"), "Cancelled")

	def test_finalize_death_blocks_new_case_sheet_and_boarding(self):
		guardian, pet = self._make_guardian_pet()
		reason = self._make_death_reason("Disease / Illness")
		record = mortality.report_pet_death(pet=pet.name, guardian=guardian.name, death_reason=reason.name)["data"]["death_record"]["name"]
		self.assertTrue(mortality.finalize_pet_death(death_record=record)["ok"])

		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({"doctype": "Vet Case Sheet", "guardian": guardian.name, "animal_patient": pet.name, "chief_complaint": "Checkup"}).insert(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			self._make_boarding(guardian=guardian, pet=pet)

	def test_complication_and_boarding_death_require_manager_review(self):
		procedure = self._make_procedure()
		procedure_reason = self._make_death_reason("Procedure Complication")
		procedure_record = mortality.report_pet_death(
			pet=procedure.pet,
			death_reason=procedure_reason.name,
			source_doctype="Pet Procedure",
			source_name=procedure.name,
		)["data"]["death_record"]["name"]
		self.assertFalse(mortality.finalize_pet_death(death_record=procedure_record)["ok"])

		boarding = self._make_boarding()
		boarding_reason = self._make_death_reason("Boarding Incident")
		boarding_record = mortality.report_pet_death(
			pet=boarding.pet,
			death_reason=boarding_reason.name,
			source_doctype="Pet Boarding",
			source_name=boarding.name,
		)["data"]["death_record"]["name"]
		self.assertFalse(mortality.finalize_pet_death(death_record=boarding_record)["ok"])

	def test_guardian_cannot_see_internal_death_notes_and_certificate_issue(self):
		guardian, pet = self._make_guardian_pet()
		user = self._make_user("p2.death.guardian", ["Guardians"])
		frappe.db.set_value("Guardian", guardian.name, "user_id", user.name)
		reason = self._make_death_reason("Disease / Illness")
		record = mortality.report_pet_death(
			pet=pet.name,
			guardian=guardian.name,
			death_reason=reason.name,
			guardian_visible_summary="Owner safe",
			internal_note="Internal only",
		)["data"]["death_record"]["name"]
		self.assertTrue(mortality.confirm_pet_death(death_record=record)["ok"])
		certificate = mortality.issue_death_certificate(death_record=record, certificate_file="/files/death-certificate.pdf")
		self.assertTrue(certificate["ok"])

		frappe.set_user(user.name)
		result = mortality.get_pet_death_record(death_record=record)
		self.assertTrue(result["ok"])
		self.assertNotIn("internal_note", result["data"]["death_record"])
		self.assertEqual(result["data"]["death_record"]["certificate_file"], "/files/death-certificate.pdf")

	def test_cancel_death_record_requires_manager(self):
		guardian, pet = self._make_guardian_pet()
		reason = self._make_death_reason("Disease / Illness")
		record = mortality.report_pet_death(pet=pet.name, guardian=guardian.name, death_reason=reason.name)["data"]["death_record"]["name"]
		user = self._make_user("p2.cancel.reception", ["Visit"])

		frappe.set_user(user.name)
		blocked = mortality.cancel_pet_death_record(death_record=record, reason="Mistake")
		frappe.set_user("Administrator")
		cancelled = mortality.cancel_pet_death_record(death_record=record, reason="Mistake")

		self.assertFalse(blocked["ok"])
		self.assertTrue(cancelled["ok"])

	def test_medical_timeline_includes_death_event_and_audit_log(self):
		guardian, pet = self._make_guardian_pet()
		reason = self._make_death_reason("Disease / Illness")
		record = mortality.report_pet_death(pet=pet.name, guardian=guardian.name, death_reason=reason.name)["data"]["death_record"]["name"]

		from pet_app.api import medical_file

		timeline = medical_file.get_pet_medical_timeline(pet=pet.name)
		self.assertTrue(timeline["ok"])
		self.assertIn("death", {event["type"] for event in timeline["data"]["events"]})
		self.assertTrue(frappe.db.exists("Pet App Audit Log", {"event_type": "pet_death.reported", "reference_name": record}))

	def _make_guardian_pet(self):
		suffix = frappe.generate_hash(length=8)
		phone_digits = self._digits(suffix, 9)
		guardian = frappe.get_doc(
			{
				"doctype": "Guardian",
				"phone": f"07{phone_digits}",
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
		frappe.get_doc({"doctype": "PetGuardian", "pet_id": pet.name, "guardian_id": guardian.name, "role": "primary_owner"}).insert(ignore_permissions=True)
		return guardian, pet

	def _make_visit(self):
		guardian, pet = self._make_guardian_pet()
		customer = get_or_create_customer_from_guardian(guardian.name)
		doctor = self._make_doctor()
		case_sheet = frappe.get_doc(
			{
				"doctype": "Vet Case Sheet",
				"status": "Waiting Practitioner",
				"guardian": guardian.name,
				"customer": customer,
				"animal_patient": pet.name,
				"chief_complaint": "Checkup",
			}
		).insert(ignore_permissions=True)
		return frappe.get_doc(
			{
				"doctype": "Vet Visit",
				"case_sheet": case_sheet.name,
				"guardian": guardian.name,
				"customer": customer,
				"animal_patient": pet.name,
				"doctor": doctor.name,
				"priority": "Normal",
				"status": "In Progress",
				"visit_type": "Consultation",
			}
		).insert(ignore_permissions=True)

	def _make_procedure(self):
		visit = self._make_visit()
		service = self._make_care_service("P2 Procedure")
		template = frappe.get_doc(
			{
				"doctype": "Procedure Template",
				"procedure_name": f"P2 Procedure {frappe.generate_hash(length=8)}",
				"price": 25,
				"active": 1,
			}
		).insert(ignore_permissions=True)
		return frappe.get_doc(
			{
				"doctype": "Pet Procedure",
				"visit": visit.name,
				"procedure_template": template.name,
				"care_service": service.name,
			}
		).insert(ignore_permissions=True)

	def _make_boarding(self, guardian=None, pet=None):
		if not guardian or not pet:
			guardian, pet = self._make_guardian_pet()
		room = frappe.get_doc(
			{
				"doctype": "Service Room",
				"room_code": f"P2-{frappe.generate_hash(length=6)}",
				"room_name": f"P2 Room {frappe.generate_hash(length=6)}",
				"room_type": "Boarding",
				"status": "Active",
			}
		).insert(ignore_permissions=True)
		return frappe.get_doc(
			{
				"doctype": "Pet Boarding",
				"service_room": room.name,
				"pet": pet.name,
				"guardian": guardian.name,
				"record_status": "Reserved",
			}
		).insert(ignore_permissions=True)

	def _make_checked_in_boarding(self, guardian=None, pet=None, with_billable=False):
		boarding = self._make_boarding(guardian=guardian, pet=pet)
		boarding.record_status = "Checked In"
		boarding.check_in = frappe.utils.add_to_date(frappe.utils.now_datetime(), days=-2)
		boarding.customer = get_or_create_customer_from_guardian(boarding.guardian)
		if with_billable:
			item = self._make_item("Boarding Cascade")
			boarding.append(
				"billable_items",
				{
					"item_name": item.item_name,
					"item_code": item.name,
					"item_type": "Service",
					"qty": 2,
					"rate": 30,
					"status": "Billable",
				},
			)
		boarding.save(ignore_permissions=True)
		return boarding

	def _make_appointment(self, guardian, pet):
		customer = get_or_create_customer_from_guardian(guardian.name)
		return frappe.get_doc(
			{
				"doctype": "Appointment",
				"status": "Open",
				"customer_name": guardian.full_name,
				"customer_phone_number": guardian.phone,
				"customer_email": guardian.email_id,
				"scheduled_time": datetime.now() + timedelta(days=1),
				"custom_appointment_type": "visit",
				"custom_pet": pet.name,
				"custom_guardian": guardian.name,
				"custom_customer": customer,
			}
		).insert(ignore_permissions=True)

	def _make_death_reason(self, category):
		suffix = frappe.generate_hash(length=8)
		return frappe.get_doc(
			{
				"doctype": "Pet Death Reason",
				"reason_name": f"{category} {suffix}",
				"reason_code": f"P2-{suffix}",
				"category": category,
				"active": 1,
				"requires_manager_review": 1 if category in {"Procedure Complication", "Anesthesia Complication", "Boarding Incident", "Unknown / Found Dead"} else 0,
				"guardian_visible_label": category,
			}
		).insert(ignore_permissions=True)

	def _make_doctor(self):
		suffix = frappe.generate_hash(length=8)
		phone_digits = self._digits(suffix, 9)
		return frappe.get_doc({"doctype": "Healthcare Practitioner", "practitioner_name": f"Practitioner {suffix}", "practitioner_type": "Doctor", "phone": f"07{phone_digits}"}).insert(ignore_permissions=True)

	def _make_care_service(self, label):
		suffix = frappe.generate_hash(length=8)
		item = self._make_item(f"{label} Item")
		category = frappe.get_doc({"doctype": "CategoryCareServices", "category_name": f"{label} Category {suffix}"}).insert(ignore_permissions=True)
		return frappe.get_doc(
			{
				"doctype": "CareService template",
				"service_name": f"{label} {suffix}",
				"animal_species": "Mammal",
				"frequency": "onetime",
				"category_id": category.name,
				"item_code": item.name,
				"default_price": 25,
				"price_list": "Standard Selling",
			}
		).insert(ignore_permissions=True)

	def _make_care_service_standard(self, label):
		# Like _make_care_service but uses the always-present "Standard Selling"
		# price list, so the test does not depend on extra seeded price lists.
		suffix = frappe.generate_hash(length=8)
		item = self._make_item(f"{label} Item")
		category = frappe.get_doc({"doctype": "CategoryCareServices", "category_name": f"{label} Category {suffix}"}).insert(ignore_permissions=True)
		return frappe.get_doc(
			{
				"doctype": "CareService template",
				"service_name": f"{label} {suffix}",
				"animal_species": "Mammal",
				"frequency": "onetime",
				"category_id": category.name,
				"item_code": item.name,
				"default_price": 25,
				"price_list": "Standard Selling",
			}
		).insert(ignore_permissions=True)

	def _make_item(self, label):
		suffix = frappe.generate_hash(length=8)
		return frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"{label} {suffix}",
				"item_name": f"{label} {suffix}",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}
		).insert(ignore_permissions=True, ignore_mandatory=True)

	def _make_user(self, prefix, roles):
		suffix = frappe.generate_hash(length=8)
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"{prefix}.{suffix}@example.com",
				"first_name": prefix,
				"enabled": 1,
				"user_type": "System User",
				"send_welcome_email": 0,
				"roles": [{"role": role} for role in roles],
			}
		)
		user.flags.no_welcome_mail = True
		return user.insert(ignore_permissions=True)

	def _digits(self, value, length):
		digits = "".join(str(ord(char) % 10) for char in value)
		while len(digits) < length:
			digits += digits
		return digits[:length]
