from __future__ import annotations

from datetime import datetime, timedelta

import frappe
from frappe.tests.utils import FrappeTestCase

from pet_app.api import case_sheet_templates, clinical_decision_support, diagnostics, follow_up, medical_file, pharmacy, printing, workspace
from pet_app.pet_app.doctype.pet_medical_profile.pet_medical_profile import ensure_pet_medical_profile
from pet_app.utils.guardian_customer import get_or_create_customer_from_guardian


class TestClinicalP1Flows(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_template_application_and_response_save_are_idempotent(self):
		visit = self._make_visit()
		template = self._make_case_sheet_template()

		first = case_sheet_templates.apply_template_to_case_sheet(visit.case_sheet, template.name)
		second = case_sheet_templates.apply_template_to_case_sheet(visit.case_sheet, template.name)
		self.assertTrue(first["ok"])
		self.assertTrue(second["ok"])
		self.assertEqual(frappe.db.count("Vet Case Sheet Response", {"case_sheet": visit.case_sheet, "template": template.name}), 2)

		saved = case_sheet_templates.save_case_sheet_responses(
			case_sheet=visit.case_sheet,
			template=template.name,
			responses=[
				{"field_key": "triage_note", "label": "Triage Note", "value": "Bright and alert"},
				{"field_key": "dehydrated", "label": "Dehydrated", "value": "0"},
			],
		)
		self.assertTrue(saved["ok"])
		self.assertEqual(frappe.db.count("Vet Case Sheet Response", {"case_sheet": visit.case_sheet, "field_key": "triage_note"}), 1)

	def test_medication_dispense_return_and_warehouse_restriction(self):
		visit = self._make_visit_with_medication()
		row = visit.prescribed_medications[0]
		warehouse = self._make_warehouse("Allowed")

		dispensed = pharmacy.dispense_visit_medication(visit=visit.name, row_name=row.name, qty=1, warehouse=warehouse)
		self.assertTrue(dispensed["ok"])
		self.assertEqual(dispensed["data"]["medication"]["dispense_status"], "Partially Dispensed")

		returned = pharmacy.return_dispensed_medication(visit=visit.name, row_name=row.name, qty=1)
		self.assertTrue(returned["ok"])
		self.assertEqual(returned["data"]["medication"]["dispense_status"], "Returned")

		user = self._make_user("p1.warehouse", roles=["Desk User"])
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": user.name,
				"allow": "Warehouse",
				"for_value": warehouse,
				"apply_to_all_doctypes": 1,
			}
		).insert(ignore_permissions=True)
		visit = self._make_visit_with_medication()
		frappe.set_user(user.name)
		blocked = pharmacy.dispense_visit_medication(visit=visit.name, row_name=visit.prescribed_medications[0].name, qty=1, warehouse="Wrong Warehouse - P1")
		self.assertFalse(blocked["ok"])
		self.assertEqual(blocked["meta"]["code"], "PERMISSION_ERROR")

	def test_lab_and_imaging_release_visibility(self):
		visit = self._make_visit()
		service = self._make_care_service("P1 Diagnostics")
		lab = frappe.get_doc({"doctype": "Lab", "visit": visit.name, "care_service": service.name}).insert(ignore_permissions=True)
		imaging = frappe.get_doc({"doctype": "Imaging", "visit": visit.name, "care_service": service.name}).insert(ignore_permissions=True)

		self.assertTrue(diagnostics.collect_sample(lab=lab.name)["ok"])
		self.assertTrue(diagnostics.save_lab_result(lab=lab.name, result="Normal CBC")["ok"])
		released_lab = diagnostics.release_lab_result(lab=lab.name, result_visibility="Guardian Visible")
		self.assertTrue(released_lab["ok"])
		self.assertEqual(released_lab["data"]["lab"]["status"], "Released")
		self.assertEqual(released_lab["data"]["lab"]["result_visibility"], "Guardian Visible")

		self.assertTrue(diagnostics.save_imaging_report(imaging=imaging.name, report="No fracture")["ok"])
		released_imaging = diagnostics.release_imaging_report(imaging=imaging.name, result_visibility="Guardian Visible")
		self.assertTrue(released_imaging["ok"])
		self.assertEqual(released_imaging["data"]["imaging"]["status"], "Released")
		self.assertEqual(released_imaging["data"]["imaging"]["result_visibility"], "Guardian Visible")

	def test_follow_up_contacted_missed_and_rescheduled(self):
		visit = self._make_visit()
		visit.follow_up_required = 1
		visit.follow_up_date = frappe.utils.nowdate()
		visit.follow_up_status = "Scheduled"
		visit.save(ignore_permissions=True)

		due = follow_up.list_due_follow_ups(date_to=frappe.utils.nowdate())
		self.assertTrue(due["ok"])
		self.assertIn(visit.name, {row["name"] for row in due["data"]["follow_ups"]})

		contacted = follow_up.mark_follow_up_contacted(visit=visit.name, note="Left message")
		self.assertTrue(contacted["ok"])
		self.assertEqual(contacted["data"]["follow_up"]["status"], "Contacted")

		missed = follow_up.mark_follow_up_missed(visit=visit.name, missed_reason="No answer")
		self.assertTrue(missed["ok"])
		self.assertEqual(missed["data"]["follow_up"]["status"], "Missed")

		next_date = frappe.utils.add_days(frappe.utils.nowdate(), 5)
		rescheduled = follow_up.reschedule_follow_up(visit=visit.name, follow_up_date=next_date)
		self.assertTrue(rescheduled["ok"])
		self.assertEqual(str(rescheduled["data"]["follow_up"]["date"]), str(next_date))

	def test_print_payloads(self):
		visit = self._make_visit_with_medication()
		service = self._make_care_service("P1 Print")
		lab = frappe.get_doc({"doctype": "Lab", "visit": visit.name, "care_service": service.name, "result": "Clear"}).insert(ignore_permissions=True)
		procedure_template = self._make_procedure_template(service)
		procedure = frappe.get_doc(
			{"doctype": "Pet Procedure", "visit": visit.name, "procedure_template": procedure_template.name, "care_service": service.name}
		).insert(ignore_permissions=True)

		self.assertTrue(printing.get_visit_summary(visit=visit.name)["ok"])
		self.assertTrue(printing.get_prescription(visit=visit.name)["ok"])
		self.assertTrue(printing.get_lab_result(lab=lab.name)["ok"])
		self.assertTrue(printing.get_procedure_report(procedure=procedure.name)["ok"])

	def test_vaccination_deworming_next_due_and_timeline(self):
		visit = self._make_visit()
		next_due = frappe.utils.add_days(frappe.utils.nowdate(), 30)
		vaccine = frappe.get_doc(
			{
				"doctype": "Pet Vaccination Record",
				"visit": visit.name,
				"vaccine_name": "Rabies",
				"next_due_date": next_due,
			}
		).insert(ignore_permissions=True)
		deworming = frappe.get_doc(
			{
				"doctype": "Pet Deworming Record",
				"visit": visit.name,
				"medication_name": "Fenbendazole",
				"next_due_date": next_due,
			}
		).insert(ignore_permissions=True)

		self.assertEqual(str(vaccine.next_due_date), str(next_due))
		self.assertEqual(str(deworming.next_due_date), str(next_due))
		timeline = medical_file.get_pet_medical_timeline(pet=visit.animal_patient)
		self.assertTrue(timeline["ok"])
		event_types = {event["type"] for event in timeline["data"]["events"]}
		self.assertIn("vaccination", event_types)
		self.assertIn("deworming", event_types)

	def test_consent_required_before_procedure_completion(self):
		visit = self._make_visit()
		service = self._make_care_service("P1 Consent")
		template = self._make_procedure_template(service, consent_required=1)
		procedure = frappe.get_doc(
			{"doctype": "Pet Procedure", "visit": visit.name, "procedure_template": template.name, "care_service": service.name}
		).insert(ignore_permissions=True)

		procedure.status = "Completed"
		with self.assertRaises(frappe.ValidationError):
			procedure.save(ignore_permissions=True)

		frappe.get_doc(
			{
				"doctype": "Pet Consent Form",
				"procedure": procedure.name,
				"status": "Signed",
				"signed_by": "Guardian",
			}
		).insert(ignore_permissions=True)
		procedure.reload()
		procedure.status = "Completed"
		procedure.save(ignore_permissions=True)
		self.assertEqual(procedure.status, "Completed")

	def test_workspace_filters(self):
		visit = self._make_visit()
		result = workspace.get_my_workspace(source_type="Visit", doctor=visit.doctor, pet=visit.animal_patient, visit_type=visit.visit_type)
		self.assertTrue(result["items"])
		self.assertTrue(all(item["source_type"] == "Visit" for item in result["items"]))
		self.assertTrue(all(item["pet"]["id"] == visit.animal_patient for item in result["items"]))

	def test_pet_add_request_approval_ensures_profile_without_patient_bridge(self):
		guardian, pet = self._make_pending_guardian_pet()
		patient_count_before = frappe.db.count("Patient") if frappe.db.exists("DocType", "Patient") else 0
		request = frappe.get_doc(
			{
				"doctype": "PetAddRequest",
				"pet_id": pet.name,
				"guardian_id": guardian.name,
				"status": "Pending",
			}
		).insert(ignore_permissions=True)

		request.status = "Approved"
		request.save(ignore_permissions=True)

		self.assertEqual(frappe.db.get_value("Pet", pet.name, "pet_status"), "Approved")
		self.assertTrue(frappe.db.exists("PetGuardian", {"pet_id": pet.name, "guardian_id": guardian.name}))
		profile = frappe.db.get_value("Pet Medical Profile", {"pet": pet.name}, ["pet", "primary_guardian", "healthcare_patient"], as_dict=True)
		self.assertEqual(profile.pet, pet.name)
		self.assertEqual(profile.primary_guardian, guardian.name)
		self.assertFalse(profile.healthcare_patient)
		if frappe.db.exists("DocType", "Patient"):
			self.assertEqual(frappe.db.count("Patient"), patient_count_before)

	def test_pet_medical_profile_summary_update_and_guardian_write_block(self):
		guardian, pet = self._make_guardian_pet()
		summary = medical_file.get_pet_medical_summary(pet=pet.name)
		self.assertTrue(summary["ok"])
		self.assertEqual(summary["data"]["profile"]["pet"], pet.name)
		self.assertNotIn("healthcare_patient", summary["data"]["profile"])

		updated = medical_file.update_pet_medical_profile(
			pet=pet.name,
			allergies="Chicken",
			chronic_conditions="Mild asthma",
			special_alerts="Handle gently",
		)
		self.assertTrue(updated["ok"])
		self.assertEqual(updated["data"]["profile"]["allergies"], "Chicken")
		self.assertEqual(updated["data"]["profile"]["chronic_conditions"], "Mild asthma")

		user = self._make_user("p1.profile.guardian", roles=["Guardians"])
		frappe.db.set_value("Guardian", guardian.name, "user_id", user.name)
		frappe.set_user(user.name)
		blocked = medical_file.update_pet_medical_profile(pet=pet.name, allergies="Beef")
		self.assertFalse(blocked["ok"])
		self.assertEqual(blocked["meta"]["code"], "PERMISSION_DENIED")

	def test_visit_save_updates_pet_medical_profile_snapshot(self):
		visit = self._make_visit()
		profile_name = ensure_pet_medical_profile(visit.animal_patient, visit.guardian)
		visit.weight = 12.5
		visit.save(ignore_permissions=True)
		profile = frappe.db.get_value("Pet Medical Profile", profile_name, ["last_visit", "last_weight"], as_dict=True)
		self.assertEqual(profile.last_visit, visit.name)
		self.assertEqual(profile.last_weight, 12.5)

	def test_clinical_decision_support_reads_pet_profile_allergies(self):
		visit = self._make_visit_with_medication()
		medication = visit.prescribed_medications[0].medication or visit.prescribed_medications[0].medication_item
		profile = frappe.get_doc("Pet Medical Profile", ensure_pet_medical_profile(visit.animal_patient, visit.guardian))
		profile.allergies = medication
		profile.save(ignore_permissions=True)

		result = clinical_decision_support.evaluate_visit(visit.name)
		self.assertTrue(result["ok"])
		self.assertIn("Allergy", {alert["alert_type"] for alert in result["data"]["alerts"]})

	def _make_case_sheet_template(self):
		suffix = frappe.generate_hash(length=8)
		return frappe.get_doc(
			{
				"doctype": "Vet Case Sheet Template",
				"template_name": f"P1 Template {suffix}",
				"visit_type": "General Consultation",
				"active": 1,
				"items": [
					{"section": "Triage", "label": "Triage Note", "field_key": "triage_note", "fieldtype": "Small Text", "required": 1, "default_value": "Stable"},
					{"section": "Triage", "label": "Dehydrated", "field_key": "dehydrated", "fieldtype": "Check"},
				],
			}
		).insert(ignore_permissions=True)

	def _make_visit_with_medication(self):
		visit = self._make_visit()
		item = self._make_item("P1 Medication Item")
		visit.append(
			"prescribed_medications",
			{
				"medication_item": item.name,
				"qty": 2,
				"rate": 10,
				"dosage": "1 tab",
				"frequency": "BID",
				"duration_days": 3,
			},
		)
		visit.save(ignore_permissions=True)
		visit.reload()
		return visit

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

	def _make_pending_guardian_pet(self):
		suffix = frappe.generate_hash(length=8)
		phone_digits = self._digits(suffix, 9)
		guardian = frappe.get_doc(
			{
				"doctype": "Guardian",
				"phone": f"07{phone_digits}",
				"full_name": f"Pending Guardian {suffix}",
				"email_id": f"pending.guardian.{suffix}@example.com",
			}
		).insert(ignore_permissions=True)
		pet = frappe.get_doc(
			{
				"doctype": "Pet",
				"pet_name": f"Pending Pet {suffix}",
				"animal_species": "Mammal",
				"animal_type": "Dog",
				"pet_status": "Pending",
				"requested_by": guardian.name,
			}
		).insert(ignore_permissions=True)
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
				"price_list": "Clinic",
			}
		).insert(ignore_permissions=True)

	def _make_procedure_template(self, service, consent_required=0):
		return frappe.get_doc(
			{
				"doctype": "Procedure Template",
				"procedure_name": f"P1 Procedure {frappe.generate_hash(length=8)}",
				"billing_care_service": service.name,
				"consent_required": consent_required,
				"active": 1,
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

	def _make_warehouse(self, label):
		name = f"{label} Warehouse {frappe.generate_hash(length=6)} - P1"
		return frappe.get_doc(
			{
				"doctype": "Warehouse",
				"warehouse_name": name,
				"is_group": 0,
			}
		).insert(ignore_permissions=True, ignore_mandatory=True).name

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
