from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from pet_app.api import appointment as appointment_api
from pet_app.api import care_service
from pet_app.api import care_plan
from pet_app.api import queue as queue_api
from pet_app.api import visit_workbench
from pet_app.api import vitals
from pet_app.api import workspace
from pet_app.pet_app.doctype.medication.medication import Medication
from pet_app.utils.guardian_customer import get_or_create_customer_from_guardian
from pet_app.utils.visit_billing import upsert_visit_billable_item


class TestClinicalHardening(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_care_service_api_uses_careservice_template(self):
		calls = []

		def fake_get_all(doctype, *args, **kwargs):
			calls.append(doctype)
			if doctype == "PetCareService":
				return []
			if doctype == "CareService template":
				return []
			if doctype == "CategoryCareServices":
				return []
			self.fail(f"Unexpected DocType queried: {doctype}")

		with (
			patch.object(care_service, "_require_care_service_access"),
			patch.object(care_service.frappe, "get_value", return_value=frappe._dict({"animal_species": "Mammal"})),
			patch.object(care_service.frappe, "get_all", side_effect=fake_get_all),
		):
			care_service.get_pet_and_services("PET-TEST")

		self.assertIn("CareService template", calls)
		self.assertNotIn("CareService", calls)

	def test_case_sheet_validates_petguardian(self):
		guardian, pet = self._make_guardian_pet(link=False)
		case_sheet = frappe.get_doc(
			{
				"doctype": "Vet Case Sheet",
				"guardian": guardian.name,
				"animal_patient": pet.name,
				"chief_complaint": "Checkup",
			}
		)
		with self.assertRaises(frappe.ValidationError):
			case_sheet.insert(ignore_permissions=True)

	def test_appointment_check_in_creates_queue_ticket_once(self):
		guardian, pet = self._make_guardian_pet()
		appointment = self._make_appointment(guardian, pet)

		first = appointment_api.check_in_appointment(appointment.name)
		second = appointment_api.check_in_appointment(appointment.name)

		self.assertTrue(first["ok"])
		self.assertTrue(second["ok"])
		self.assertEqual(first["data"]["queue_ticket"]["name"], second["data"]["queue_ticket"]["name"])
		self.assertEqual(frappe.db.count("Pet Queue Ticket", {"appointment": appointment.name}), 1)
		self.assertEqual(frappe.db.count("Vet Case Sheet", {"appointment": appointment.name}), 1)

	def test_queue_call_no_show_and_complete(self):
		guardian, pet = self._make_guardian_pet()
		appointment = self._make_appointment(guardian, pet)
		check_in = appointment_api.check_in_appointment(appointment.name)
		self.assertTrue(check_in["ok"])

		called = queue_api.call_next()
		self.assertTrue(called["ok"])
		self.assertEqual(called["data"]["status"], "Called")

		no_show = queue_api.mark_no_show(called["data"]["name"])
		self.assertTrue(no_show["ok"])
		self.assertEqual(no_show["data"]["status"], "No Show")

		second_appointment = self._make_appointment(guardian, pet)
		second_check_in = appointment_api.check_in_appointment(second_appointment.name)
		completed = queue_api.complete_queue_ticket(second_check_in["data"]["queue_ticket"]["name"])
		self.assertTrue(completed["ok"])
		self.assertEqual(completed["data"]["status"], "Completed")

	def test_case_sheet_converts_to_visit_once(self):
		visit = self._make_visit()
		case_sheet = frappe.get_doc("Vet Case Sheet", visit.case_sheet)
		doctor = visit.doctor
		frappe.delete_doc("Vet Visit", visit.name, force=True)
		frappe.db.set_value("Vet Case Sheet", case_sheet.name, {"vet_visit": None, "status": "Waiting Practitioner"})

		first = workspace._convert_to_visit("Vet Case Sheet", case_sheet.name, {"doctor": doctor, "doctor_case_choice": "wellness"})
		second = workspace._convert_to_visit("Vet Case Sheet", case_sheet.name, {"doctor": doctor, "doctor_case_choice": "wellness"})

		self.assertEqual(first, second)
		self.assertEqual(frappe.db.count("Vet Visit", {"case_sheet": case_sheet.name}), 1)
		converted = frappe.get_doc("Vet Visit", first)
		self.assertEqual(converted.get("doctor_case_choice"), "wellness")
		self.assertFalse(converted.get("care_episode"))

	def test_follow_up_conversion_is_idempotent(self):
		original = self._make_visit()
		appointment = self._make_appointment(
			frappe.get_doc("Guardian", original.guardian),
			frappe.get_doc("Pet", original.animal_patient),
			appointment_type="follow_up",
			extra={"custom_follow_up_of_visit_id": original.name},
		)

		first = workspace._convert_follow_up_to_visit("Appointment", appointment.name, {"doctor": original.doctor})
		second = workspace._convert_follow_up_to_visit("Appointment", appointment.name, {"doctor": original.doctor})

		self.assertEqual(first, second)
		self.assertEqual(frappe.db.count("Vet Visit", {"follow_up_of_visit_id": original.name}), 1)

	def test_visit_case_choice_controls_episode_and_workbench(self):
		visit = self._make_visit()
		visit.reload()

		self.assertFalse(visit.get("care_episode"))
		profile = frappe.get_doc("Pet Medical Profile", {"pet": visit.animal_patient})
		self.assertFalse(profile.active_care_episode)
		self.assertEqual(profile.current_visit, visit.name)

		workbench = visit_workbench.get_visit_workbench(visit.name)
		self.assertTrue(workbench["ok"])
		self.assertEqual(workbench["data"]["visit"]["name"], visit.name)
		self.assertTrue(workbench["data"]["case_context"]["case_choice_required"])
		self.assertIsNone(workbench["data"]["case_context"]["visit_care_episode"])

		aggregate = workspace.perform_action("Visit", visit.name, "set_case_choice", {"doctor_case_choice": "new_case"})
		visit.reload()
		profile.reload()

		self.assertTrue(visit.care_episode)
		self.assertEqual(visit.get("doctor_case_choice"), "new_case")
		self.assertEqual(profile.active_care_episode, visit.care_episode)
		self.assertEqual(aggregate["case_context"]["visit_care_episode"], visit.care_episode)
		self.assertFalse(aggregate["case_context"]["case_choice_required"])

		workbench = visit_workbench.get_visit_workbench(visit.name)
		self.assertTrue(workbench["ok"])
		self.assertEqual(workbench["data"]["active_episode"]["name"], visit.care_episode)

	def test_wellness_orders_do_not_create_episode(self):
		visit = self._make_visit()
		aggregate = workspace.perform_action("Visit", visit.name, "set_case_choice", {"doctor_case_choice": "wellness"})
		self.assertIsNone(aggregate["case_context"]["visit_care_episode"])

		service = self._make_care_service("Clinical Hardening Wellness Lab")
		result = workspace.perform_action(
			"Visit",
			visit.name,
			"create_orders",
			{"orders": [{"kind": "lab", "template_id": service.name, "title": "Wellness CBC"}]},
		)
		visit.reload()
		profile = frappe.get_doc("Pet Medical Profile", {"pet": visit.animal_patient})
		lab_order = next(row for row in result["orders"] if row["kind"] == "lab")

		self.assertFalse(visit.get("care_episode"))
		self.assertFalse(profile.active_care_episode)
		self.assertEqual(lab_order["linked_doctype"], "Lab")
		self.assertTrue(lab_order["linked_name"])

	def test_new_case_is_blocked_when_active_episode_exists(self):
		first_visit = self._make_visit()
		workspace.perform_action("Visit", first_visit.name, "set_case_choice", {"doctor_case_choice": "new_case"})
		first_visit.reload()

		second_visit = self._make_visit(frappe.get_doc("Guardian", first_visit.guardian), frappe.get_doc("Pet", first_visit.animal_patient))
		with self.assertRaises(frappe.ValidationError):
			workspace.perform_action("Visit", second_visit.name, "set_case_choice", {"doctor_case_choice": "new_case"})

		continued = workspace.perform_action("Visit", second_visit.name, "set_case_choice", {"doctor_case_choice": "continue_case"})
		second_visit.reload()
		self.assertEqual(second_visit.care_episode, first_visit.care_episode)
		self.assertEqual(continued["case_context"]["profile_active_episode"], first_visit.care_episode)

	def test_add_plan_item_schedule_and_convert_idempotent(self):
		visit = self._make_visit()
		workspace.perform_action("Visit", visit.name, "set_case_choice", {"doctor_case_choice": "new_case"})
		visit.reload()
		due_date = (datetime.now() + timedelta(days=7)).date()
		created = care_plan.add_plan_item_from_visit(
			visit.name,
			data={
				"plan_type": "Follow-up Visit",
				"title": "Recheck appetite",
				"due_date": str(due_date),
				"priority": "Important",
			},
		)
		self.assertTrue(created["ok"])
		plan_name = created["data"]["created_plan_item"]["name"]

		scheduled_time = datetime.now() + timedelta(days=7, hours=2)
		first_schedule = care_plan.schedule_plan_item_appointment(
			plan_name,
			appointment_data={"scheduled_time": scheduled_time},
		)
		second_schedule = care_plan.schedule_plan_item_appointment(
			plan_name,
			appointment_data={"scheduled_time": scheduled_time},
		)
		self.assertTrue(first_schedule["ok"])
		self.assertTrue(second_schedule["ok"])
		self.assertEqual(first_schedule["data"]["appointment"]["name"], second_schedule["data"]["appointment"]["name"])

		first_convert = care_plan.convert_plan_item_to_visit(plan_name)
		second_convert = care_plan.convert_plan_item_to_visit(plan_name)
		self.assertTrue(first_convert["ok"])
		self.assertTrue(second_convert["ok"])
		self.assertEqual(first_convert["data"]["visit"], second_convert["data"]["visit"])
		self.assertEqual(frappe.db.get_value("Pet Care Plan Item", plan_name, "status"), "Converted To Visit")
		self.assertEqual(frappe.db.get_value("Vet Visit", first_convert["data"]["visit"], "care_episode"), visit.care_episode)

	def test_create_orders_is_idempotent(self):
		visit = self._make_visit()
		service = self._make_care_service("Clinical Hardening Lab")
		payload = {"orders": [{"kind": "lab", "template_id": service.name, "title": "CBC"}]}

		workspace._create_orders(visit.name, payload)
		workspace._create_orders(visit.name, payload)
		visit.reload()

		self.assertEqual(len([row for row in visit.orders if row.kind == "lab"]), 1)
		order_id = visit.orders[0].order_id
		self.assertEqual(frappe.db.count("Lab", {"visit": visit.name, "order_id": order_id}), 1)

	def test_clinical_billable_sync_does_not_duplicate(self):
		visit = self._make_visit()
		service = self._make_care_service("Clinical Hardening Billable")

		lab = frappe.get_doc(
			{
				"doctype": "Lab",
				"visit": visit.name,
				"order_id": "ORD-LAB",
				"care_service": service.name,
			}
		).insert(ignore_permissions=True)
		lab.save(ignore_permissions=True)

		imaging = frappe.get_doc(
			{
				"doctype": "Imaging",
				"visit": visit.name,
				"order_id": "ORD-IMG",
				"care_service": service.name,
			}
		).insert(ignore_permissions=True)
		imaging.save(ignore_permissions=True)

		template = frappe.get_doc(
			{
				"doctype": "Procedure Template",
				"procedure_name": f"Procedure {frappe.generate_hash(length=6)}",
				"billing_care_service": service.name,
				"active": 1,
			}
		).insert(ignore_permissions=True)
		procedure = frappe.get_doc(
			{
				"doctype": "Pet Procedure",
				"visit": visit.name,
				"order_id": "ORD-PROC",
				"procedure_template": template.name,
				"care_service": service.name,
			}
		).insert(ignore_permissions=True)
		procedure.save(ignore_permissions=True)

		visit.reload()
		active_rows = [row for row in visit.billable_items if row.status != "Cancelled"]
		self.assertEqual(len(active_rows), 3)

	def test_cancel_service_cancels_order_and_linked_billable(self):
		visit = self._make_visit()
		care_service = self._make_care_service("Clinical Hardening Service Cancel")
		result = workspace.perform_action(
			"Visit",
			visit.name,
			"create_orders",
			{"orders": [{"kind": "service", "care_service_id": care_service.name, "title": "Nail Trim"}]},
		)
		order = next(row for row in result["orders"] if row["kind"] == "service")
		service = frappe.get_doc("PetCareService", order["linked_name"])

		visit.reload()
		upsert_visit_billable_item(
			visit,
			linked_service_id=f"PetCareService::{service.name}",
			linked_doctype="PetCareService",
			linked_name=service.name,
			order_id=service.order_id,
			item_code=care_service.item_code,
			item_type="Service",
			qty=1,
			rate=care_service.default_price,
			item_name=care_service.service_name,
		)
		visit.save(ignore_permissions=True)

		workspace.perform_action("Service", service.name, "cancel_service", {"reason": "Owner declined"})
		service.reload()
		visit.reload()

		self.assertEqual(service.status, "cancelled")
		order_row = next(row for row in visit.orders if row.order_id == service.order_id)
		self.assertEqual(order_row.status, "Cancelled")
		billable = next(row for row in visit.billable_items if row.get("linked_name") == service.name)
		self.assertEqual(billable.status, "Cancelled")
		self._assert_cancelled_billable_hidden_from_active_payloads(
			visit.name, lambda row: row.get("linked_name") == service.name
		)

	def test_cancel_completed_service_fails(self):
		visit = self._make_visit()
		care_service = self._make_care_service("Clinical Hardening Completed Service")
		result = workspace.perform_action(
			"Visit",
			visit.name,
			"create_orders",
			{"orders": [{"kind": "service", "care_service_id": care_service.name, "title": "Completed Service"}]},
		)
		order = next(row for row in result["orders"] if row["kind"] == "service")
		service_name = order["linked_name"]

		workspace.perform_action("Service", service_name, "finish_service", {})
		with self.assertRaises(frappe.ValidationError):
			workspace.perform_action("Service", service_name, "cancel_service", {"reason": "Too late"})

	def test_cancel_service_on_billed_visit_fails(self):
		visit = self._make_visit()
		care_service = self._make_care_service("Clinical Hardening Billed Service")
		result = workspace.perform_action(
			"Visit",
			visit.name,
			"create_orders",
			{"orders": [{"kind": "service", "care_service_id": care_service.name, "title": "Billed Service"}]},
		)
		order = next(row for row in result["orders"] if row["kind"] == "service")
		frappe.db.set_value("Vet Visit", visit.name, "billed", 1)

		with self.assertRaises(frappe.ValidationError):
			workspace.perform_action("Service", order["linked_name"], "cancel_service", {"reason": "Billed"})

	def test_cancel_medication_cancels_billable_and_does_not_recreate(self):
		visit = self._make_visit()
		item = self._make_item("Clinical Hardening Medication")
		visit.append(
			"prescribed_medications",
			{
				"medication_item": item.name,
				"qty": 2,
				"rate": 10,
				"dispense_status": "Prescribed",
			},
		)
		visit.save(ignore_permissions=True)
		row_name = visit.prescribed_medications[0].name
		linked_service_id = f"medication::{row_name}"

		workspace.perform_action("Visit", visit.name, "cancel_medication", {"row_name": row_name, "reason": "Owner declined"})
		visit.reload()

		medication_row = next(row for row in visit.prescribed_medications if row.name == row_name)
		self.assertEqual(medication_row.dispense_status, "Cancelled")
		billable = next(row for row in visit.billable_items if row.linked_service_id == linked_service_id)
		self.assertEqual(billable.status, "Cancelled")
		self._assert_cancelled_billable_hidden_from_active_payloads(
			visit.name, lambda row: row.get("linked_service_id") == linked_service_id
		)

		visit.save(ignore_permissions=True)
		visit.reload()
		active_medication_billables = [
			row
			for row in visit.billable_items
			if row.linked_service_id == linked_service_id and row.status != "Cancelled"
		]
		self.assertEqual(active_medication_billables, [])

	def test_cancel_dispensed_medication_fails(self):
		visit = self._make_visit()
		item = self._make_item("Clinical Hardening Dispensed Medication")
		visit.append(
			"prescribed_medications",
			{
				"medication_item": item.name,
				"qty": 2,
				"rate": 10,
				"dispense_status": "Partially Dispensed",
				"dispensed_qty": 1,
			},
		)
		visit.save(ignore_permissions=True)
		row_name = visit.prescribed_medications[0].name

		with self.assertRaises(frappe.ValidationError):
			workspace.perform_action("Visit", visit.name, "cancel_medication", {"row_name": row_name})

	def test_cancelled_visit_care_service_row_cancels_billable(self):
		visit = self._make_visit()
		care_service = self._make_care_service("Clinical Hardening Visit Service")
		visit.append("care_services", {"care_service_id": care_service.name})
		visit.save(ignore_permissions=True)
		row_name = visit.care_services[0].name
		linked_service_id = f"visit-care-service::{row_name}"

		visit.care_services[0].status = "Cancelled"
		visit.save(ignore_permissions=True)
		visit.reload()

		billable = next(row for row in visit.billable_items if row.linked_service_id == linked_service_id)
		self.assertEqual(billable.status, "Cancelled")

	def test_removed_medication_and_care_service_rows_cancel_billables(self):
		visit = self._make_visit()
		item = self._make_item("Clinical Hardening Removed Medication")
		care_service = self._make_care_service("Clinical Hardening Removed Visit Service")
		visit.append("prescribed_medications", {"medication_item": item.name, "qty": 1, "rate": 10})
		visit.append("care_services", {"care_service_id": care_service.name})
		visit.save(ignore_permissions=True)
		medication_link = f"medication::{visit.prescribed_medications[0].name}"
		service_link = f"visit-care-service::{visit.care_services[0].name}"

		visit.set("prescribed_medications", [])
		visit.set("care_services", [])
		visit.save(ignore_permissions=True)
		visit.reload()

		medication_billable = next(row for row in visit.billable_items if row.linked_service_id == medication_link)
		service_billable = next(row for row in visit.billable_items if row.linked_service_id == service_link)
		self.assertEqual(medication_billable.status, "Cancelled")
		self.assertEqual(service_billable.status, "Cancelled")

	def test_cancel_procedure_still_cancels_order_and_billable(self):
		visit = self._make_visit()
		care_service = self._make_care_service("Clinical Hardening Procedure Cancel")
		template = frappe.get_doc(
			{
				"doctype": "Procedure Template",
				"procedure_name": f"Procedure Cancel {frappe.generate_hash(length=6)}",
				"billing_care_service": care_service.name,
				"price": 25,
				"active": 1,
			}
		).insert(ignore_permissions=True)
		result = workspace.perform_action(
			"Visit",
			visit.name,
			"create_orders",
			{
				"orders": [
					{
						"kind": "procedure",
						"procedure_template": template.name,
						"care_service": care_service.name,
						"title": template.procedure_name,
					}
				]
			},
		)
		order = next(row for row in result["orders"] if row["kind"] == "procedure")
		procedure = frappe.get_doc("Pet Procedure", order["linked_name"])

		workspace.perform_action("Procedure", procedure.name, "cancel_procedure", {"reason": "Owner declined"})
		procedure.reload()
		visit.reload()

		self.assertEqual(procedure.status, "Cancelled")
		order_row = next(row for row in visit.orders if row.order_id == procedure.order_id)
		self.assertEqual(order_row.status, "Cancelled")
		billable = next(row for row in visit.billable_items if row.get("linked_name") == procedure.name)
		self.assertEqual(billable.status, "Cancelled")
		self._assert_cancelled_billable_hidden_from_active_payloads(
			visit.name, lambda row: row.get("linked_name") == procedure.name
		)

	def test_billed_visit_cannot_be_edited_and_accepts_addendum(self):
		visit = self._make_visit()
		frappe.db.set_value("Vet Visit", visit.name, "billed", 1)
		visit.reload()
		visit.diagnosis = "Edited after billing"
		with self.assertRaises(frappe.ValidationError):
			visit.save(ignore_permissions=True)

		addendum = frappe.get_doc(
			{
				"doctype": "Vet Visit Addendum",
				"visit": visit.name,
				"note": "Post-billing clarification",
			}
		).insert(ignore_permissions=True)
		self.assertTrue(addendum.name)

	def test_unauthorized_guardian_cannot_read_another_pet_visit(self):
		visit = self._make_visit()
		other_guardian, _pet = self._make_guardian_pet()
		user = self._make_user("guardian", roles=["Guardians"])
		frappe.db.set_value("Guardian", other_guardian.name, "user_id", user.name)

		frappe.set_user(user.name)
		with self.assertRaises(frappe.PermissionError):
			workspace.get_record("Visit", visit.name)

	def test_add_visit_vital_syncs_latest_and_keeps_history(self):
		visit = self._make_visit()

		first = vitals.add_visit_vital(visit.name, data={"weight": 12.4, "temperature": 38.1})
		second = vitals.add_visit_vital(visit.name, data={"weight": 12.8, "temperature": 38.4, "heart_rate": 95})
		self.assertTrue(first["ok"])
		self.assertTrue(second["ok"])

		visit.reload()
		self.assertEqual(len(visit.vital_signs), 2)
		self.assertEqual(visit.weight, 12.8)
		self.assertEqual(visit.temperature, 38.4)
		self.assertEqual(visit.heart_rate, 95)

		profile = frappe.get_doc("Pet Medical Profile", {"pet": visit.animal_patient})
		self.assertEqual(profile.last_weight, 12.8)
		self.assertEqual(profile.last_temperature, 38.4)
		self.assertEqual(profile.last_heart_rate, 95)

	def test_doctor_restriction_blocks_other_doctors_visits(self):
		visit = self._make_visit()
		allowed_doctor = self._make_doctor()
		user = self._make_user("restricted.doctor", roles=["Physician"])
		frappe.db.set_value("Healthcare Practitioner", allowed_doctor.name, "user_id", user.name)
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": user.name,
				"allow": "Healthcare Practitioner",
				"for_value": allowed_doctor.name,
				"apply_to_all_doctypes": 1,
			}
		).insert(ignore_permissions=True)

		frappe.set_user(user.name)
		with self.assertRaises(frappe.PermissionError):
			workspace.get_record("Visit", visit.name)

	def test_warehouse_restriction_blocks_medication_wrong_warehouse(self):
		user = self._make_user("warehouse.restricted", roles=["Stock Manager", "Item Manager"])
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": user.name,
				"allow": "Warehouse",
				"for_value": "Allowed Warehouse - TEST",
				"apply_to_all_doctypes": 1,
			}
		).insert(ignore_permissions=True)

		frappe.set_user(user.name)
		medication = frappe.get_doc(
			{
				"doctype": "Medication",
				"medication_name": "Restricted Medication",
				"default_warehouse": "Other Warehouse - TEST",
			}
		)
		with self.assertRaises(frappe.PermissionError):
			Medication._validate_default_warehouse(medication)

	def _make_guardian_pet(self, link=True):
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
		if link:
			frappe.get_doc(
				{
					"doctype": "PetGuardian",
					"pet_id": pet.name,
					"guardian_id": guardian.name,
					"role": "primary_owner",
				}
			).insert(ignore_permissions=True)
		return guardian, pet

	def _make_appointment(self, guardian, pet, appointment_type="visit", extra=None):
		customer = get_or_create_customer_from_guardian(guardian.name)
		data = {
			"doctype": "Appointment",
			"status": "Open",
			"customer_name": guardian.full_name,
			"customer_phone_number": guardian.phone,
			"customer_email": guardian.email_id,
			"scheduled_time": datetime.now() + timedelta(hours=1),
			"custom_appointment_type": appointment_type,
			"custom_pet": pet.name,
			"custom_guardian": guardian.name,
			"custom_customer": customer,
		}
		data.update(extra or {})
		return frappe.get_doc(data).insert(ignore_permissions=True)

	def _make_visit(self, guardian=None, pet=None):
		if not guardian or not pet:
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
		return frappe.get_doc(
			{
				"doctype": "Healthcare Practitioner",
				"practitioner_name": f"Practitioner {suffix}",
				"practitioner_type": "Doctor",
				"phone": f"07{phone_digits}",
			}
		).insert(ignore_permissions=True)

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

	def _ensure_clinic_price_list(self):
		if frappe.db.exists("Price List", "Clinic"):
			return "Clinic"
		return frappe.get_doc(
			{
				"doctype": "Price List",
				"price_list_name": "Clinic",
				"enabled": 1,
				"selling": 1,
				"currency": "USD",
			}
		).insert(ignore_permissions=True, ignore_mandatory=True).name

	def _make_care_service(self, label):
		suffix = frappe.generate_hash(length=8)
		item = self._make_item(label)
		category = frappe.get_doc(
			{
				"doctype": "CategoryCareServices",
				"category_name": f"{label} Category {suffix}",
			}
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

	def _assert_cancelled_billable_hidden_from_active_payloads(self, visit_name, matches):
		aggregate = workspace.get_record("Visit", visit_name)
		self.assertFalse(any(matches(row) for row in aggregate["billing"]["billable_items"]))
		self.assertTrue(any(matches(row) for row in aggregate["billing"]["cancelled_billable_items"]))

		workbench = visit_workbench.get_visit_workbench(visit=visit_name)
		data = workbench["data"]
		self.assertFalse(any(matches(row) for row in data["billables"]))
		self.assertTrue(any(matches(row) for row in data["cancelled_billables"]))
		self.assertFalse(any(matches(row) for row in data["billing"]["billable_items"]))
		self.assertTrue(any(matches(row) for row in data["billing"]["cancelled_billable_items"]))

	def _digits(self, value, length):
		digits = "".join(str(ord(char) % 10) for char in value)
		while len(digits) < length:
			digits += digits
		return digits[:length]
