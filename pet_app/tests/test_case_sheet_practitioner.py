from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from pet_app.api import appointment as appointment_api
from pet_app.api import workspace
from pet_app.pet_app.doctype.vet_case_sheet.vet_case_sheet import start_visit
from pet_app.utils.guardian_customer import get_or_create_customer_from_guardian


class TestCaseSheetPractitioner(FrappeTestCase):
	"""Explicit-practitioner resolution on Vet Case Sheet -> Vet Visit conversion."""

	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")

	# -- start_visit -----------------------------------------------------------

	def test_coordinator_with_explicit_practitioner_assigns_that_practitioner(self):
		case_sheet = self._make_case_sheet()
		hcp = self._make_doctor()
		coordinator = self._make_user("coordinator", roles=["Desk User", "Coordinator", "Reception"])

		frappe.set_user(coordinator.name)
		result = start_visit(case_sheet.name, practitioner=hcp.name)

		self.assertTrue(result["ok"], msg=result)
		self.assertEqual(result["data"]["doctype"], "Vet Visit")
		visit_name = result["data"]["name"]
		self.assertEqual(frappe.db.get_value("Vet Visit", visit_name, "doctor"), hcp.name)

	def test_start_visit_falls_back_to_session_practitioner(self):
		case_sheet = self._make_case_sheet()
		hcp = self._make_doctor()
		vet = self._make_user("vet", roles=["Desk User", "Doctor"])
		frappe.db.set_value("Healthcare Practitioner", hcp.name, "user_id", vet.name)

		frappe.set_user(vet.name)
		result = start_visit(case_sheet.name)

		self.assertTrue(result["ok"], msg=result)
		self.assertEqual(frappe.db.get_value("Vet Visit", result["data"]["name"], "doctor"), hcp.name)

	def test_start_visit_uses_case_sheet_field_when_arg_omitted(self):
		hcp = self._make_doctor()
		case_sheet = self._make_case_sheet(practitioner=hcp.name)
		coordinator = self._make_user("coordinator", roles=["Desk User", "Coordinator", "Reception"])

		frappe.set_user(coordinator.name)
		result = start_visit(case_sheet.name)

		self.assertTrue(result["ok"], msg=result)
		self.assertEqual(frappe.db.get_value("Vet Visit", result["data"]["name"], "doctor"), hcp.name)

	def test_start_visit_explicit_arg_overrides_case_sheet_field(self):
		hcp_a = self._make_doctor()
		hcp_b = self._make_doctor()
		case_sheet = self._make_case_sheet(practitioner=hcp_a.name)
		coordinator = self._make_user("coordinator", roles=["Desk User", "Coordinator", "Reception"])

		frappe.set_user(coordinator.name)
		result = start_visit(case_sheet.name, practitioner=hcp_b.name)

		self.assertTrue(result["ok"], msg=result)
		self.assertEqual(frappe.db.get_value("Vet Visit", result["data"]["name"], "doctor"), hcp_b.name)

	def test_start_visit_rejects_unknown_practitioner(self):
		case_sheet = self._make_case_sheet()
		coordinator = self._make_user("coordinator", roles=["Desk User", "Coordinator", "Reception"])

		frappe.set_user(coordinator.name)
		result = start_visit(case_sheet.name, practitioner="HCP-DOES-NOT-EXIST")

		self.assertFalse(result["ok"])
		self.assertIn("not found or is disabled", _error_message(result))

	def test_start_visit_rejects_disabled_practitioner(self):
		case_sheet = self._make_case_sheet()
		hcp = self._make_doctor()
		frappe.db.set_value("Healthcare Practitioner", hcp.name, "disabled", 1)
		coordinator = self._make_user("coordinator", roles=["Desk User", "Coordinator", "Reception"])

		frappe.set_user(coordinator.name)
		result = start_visit(case_sheet.name, practitioner=hcp.name)

		self.assertFalse(result["ok"])
		self.assertIn("not found or is disabled", _error_message(result))

	def test_start_visit_requires_resolvable_practitioner(self):
		case_sheet = self._make_case_sheet()
		coordinator = self._make_user("coordinator", roles=["Desk User", "Coordinator", "Reception"])

		frappe.set_user(coordinator.name)
		result = start_visit(case_sheet.name)

		self.assertFalse(result["ok"])
		self.assertIn("Healthcare Practitioner is required to convert to a visit", _error_message(result))

	def test_start_visit_is_idempotent(self):
		case_sheet = self._make_case_sheet()
		hcp = self._make_doctor()
		coordinator = self._make_user("coordinator", roles=["Desk User", "Coordinator", "Reception"])

		frappe.set_user(coordinator.name)
		first = start_visit(case_sheet.name, practitioner=hcp.name)
		second = start_visit(case_sheet.name, practitioner=hcp.name)

		self.assertTrue(first["ok"])
		self.assertTrue(second["ok"])
		self.assertEqual(first["data"]["name"], second["data"]["name"])
		self.assertTrue(second["data"]["idempotent"])
		self.assertEqual(frappe.db.count("Vet Visit", {"case_sheet": case_sheet.name}), 1)

	# -- _convert_to_visit (workspace perform_action path) ---------------------

	def test_convert_to_visit_honours_payload_practitioner(self):
		case_sheet = self._make_case_sheet()
		hcp = self._make_doctor()

		visit_name = workspace._convert_to_visit("Vet Case Sheet", case_sheet.name, {"practitioner": hcp.name})

		self.assertEqual(frappe.db.get_value("Vet Visit", visit_name, "doctor"), hcp.name)

	def test_convert_to_visit_uses_case_sheet_field_when_payload_empty(self):
		hcp = self._make_doctor()
		case_sheet = self._make_case_sheet(practitioner=hcp.name)

		visit_name = workspace._convert_to_visit("Vet Case Sheet", case_sheet.name, {})

		self.assertEqual(frappe.db.get_value("Vet Visit", visit_name, "doctor"), hcp.name)

	def test_convert_to_visit_rejects_disabled_practitioner(self):
		case_sheet = self._make_case_sheet()
		hcp = self._make_doctor()
		frappe.db.set_value("Healthcare Practitioner", hcp.name, "disabled", 1)

		with self.assertRaises(frappe.ValidationError):
			workspace._convert_to_visit("Vet Case Sheet", case_sheet.name, {"practitioner": hcp.name})

	# -- create_walkin_case_sheet ----------------------------------------------

	def test_walkin_persists_full_intake_and_creates_queue_ticket(self):
		guardian, pet = self._make_guardian_pet()
		hcp = self._make_doctor()

		result = appointment_api.create_walkin_case_sheet(
			payload={
				"pet": pet.name,
				"guardian": guardian.name,
				"practitioner": hcp.name,
				"chief_complaint": "Vomiting",
				"has_vomiting": 1,
				"has_diarrhea": 1,
				"symptom_duration": "1-3 days",
				"appetite_status": "Reduced",
				"feeding_type": "Dry Food",
				"intake_notes": "Walk-in full intake",
			}
		)

		self.assertTrue(result["ok"], msg=result)
		case_sheet_name = result["data"]["case_sheet"]
		self.assertTrue(result["data"]["queue_ticket"]["name"])

		case_sheet = frappe.get_doc("Vet Case Sheet", case_sheet_name)
		self.assertEqual(case_sheet.practitioner, hcp.name)
		self.assertEqual(case_sheet.chief_complaint, "Vomiting")
		self.assertTrue(case_sheet.has_vomiting)
		self.assertTrue(case_sheet.has_diarrhea)
		self.assertEqual(case_sheet.symptom_duration, "1-3 days")
		self.assertEqual(case_sheet.appetite_status, "Reduced")
		self.assertEqual(case_sheet.feeding_type, "Dry Food")
		self.assertEqual(case_sheet.intake_notes, "Walk-in full intake")
		self.assertEqual(frappe.db.count("Pet Queue Ticket", {"case_sheet": case_sheet_name}), 1)

	# -- helpers ---------------------------------------------------------------

	def _make_case_sheet(self, practitioner=None):
		guardian, pet = self._make_guardian_pet()
		customer = get_or_create_customer_from_guardian(guardian.name)
		data = {
			"doctype": "Vet Case Sheet",
			"status": "Waiting Practitioner",
			"guardian": guardian.name,
			"customer": customer,
			"animal_patient": pet.name,
			"chief_complaint": "Checkup",
		}
		if practitioner:
			data["practitioner"] = practitioner
		return frappe.get_doc(data).insert(ignore_permissions=True)

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

	def _digits(self, value, length):
		digits = "".join(str(ord(char) % 10) for char in value)
		while len(digits) < length:
			digits += digits
		return digits[:length]


def _error_message(result: dict) -> str:
	errors = result.get("errors") or []
	if errors:
		return " ".join(str(err.get("message", "")) for err in errors)
	meta = result.get("meta") or {}
	return str(meta.get("message") or result.get("message") or "")
