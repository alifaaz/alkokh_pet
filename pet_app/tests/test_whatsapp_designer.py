from __future__ import annotations

import json

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime, nowdate

from pet_app.api import notifications as notifications_api
from pet_app.notifications import actions, designer, engine


class TestWhatsAppRuleDesigner(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")

	def test_schema_contains_only_registered_sources(self):
		response = notifications_api.get_whatsapp_designer_schema()
		self.assertTrue(response["ok"])
		sources = {row["value"] for row in response["data"]["schema"]["source_tables"]}
		self.assertEqual(sources, set(designer.SOURCE_REGISTRY))
		self.assertNotIn("User", sources)

		with self.assertRaises(designer.DesignerError) as caught:
			designer.get_source_schema("User")
		self.assertEqual(caught.exception.code, "SOURCE_NOT_ALLOWED")

	def test_appointment_source_resolves_guardian_pet_and_safe_status_updates(self):
		guardian, pet = self._make_guardian_pet()
		appointment = frappe.get_doc(
			{
				"doctype": "Appointment",
				"scheduled_time": now_datetime(),
				"status": "Open",
				"customer_name": guardian.full_name,
				"customer_phone_number": guardian.phone,
				"custom_guardian": guardian.name,
				"custom_pet": pet.name,
				"custom_appointment_type": "visit",
			}
		).insert(ignore_permissions=True)

		schema = designer.get_source_schema("Appointment")
		status = next(row for row in schema["fields"] if row["fieldname"] == "status")
		self.assertEqual(set(status["allowed_values"]), {"Open", "Unverified", "Closed", "Cancelled"})
		self.assertIn("Update Allowed Field", {row["value"] for row in schema["executors"]})

		context = designer.build_document_context(appointment.doctype, appointment.name)
		self.assertEqual(context["guardian"]["name"], guardian.name)
		self.assertEqual(context["pet"]["name"], pet.name)
		self.assertEqual(context["appointment"]["status"], "Open")

	def test_legacy_parser_accepts_objects_and_double_encoding(self):
		value = {"all": [{"field": "status", "operator": "equals", "value": "Completed"}]}
		for encoded in (value, json.dumps(value), json.dumps(json.dumps(value))):
			parsed, error = designer.parse_config_section(encoded, {"all": []}, "condition")
			self.assertIsNone(error)
			self.assertEqual(parsed, value)

		parsed, error = designer.parse_config_section("{broken", {"all": []}, "condition")
		self.assertIsNone(parsed)
		self.assertTrue(error)

	def test_validation_rejects_unsafe_values_and_forces_approval(self):
		rule = self._base_rule()
		rule.update(
			{
				"executor": "Update Allowed Field",
				"executor_config": {"field": "status", "values": {"confirm": "completed"}},
				"response_type": "Buttons",
				"response_config": {
					"options": [{"key": "confirm", "label": "Confirm", "value": "confirm", "aliases": ["yes"]}]
				},
				"risk_level": "Low",
				"requires_approval": 0,
			}
		)
		validation = designer.validate_rule(rule)
		self.assertTrue(validation["valid"], validation["issues"])
		self.assertEqual(validation["normalized_rule"]["risk_level"], "Medium")
		self.assertEqual(validation["normalized_rule"]["requires_approval"], 1)

		rule["executor_config"] = {"field": "pet_service_name", "values": {"confirm": "Changed"}}
		validation = designer.validate_rule(rule)
		self.assertFalse(validation["valid"])
		self.assertIn("FIELD_NOT_ALLOWED", {issue["code"] for issue in validation["issues"]})

	def test_canonical_save_and_list(self):
		rule = self._base_rule()
		rule["rule_name"] = f"Designer Save {frappe.generate_hash(length=8)}"
		response = notifications_api.save_whatsapp_action_rule(rule=rule)
		self.assertTrue(response["ok"], response)
		saved = response["data"]["rule"]
		self.assertIsInstance(saved["condition"], dict)
		self.assertIsInstance(saved["response_config"], dict)
		self.assertNotIn("condition_json", saved)

		listed = notifications_api.list_whatsapp_action_rules(source_doctype="PetCareService")
		row = next(item for item in listed["data"]["rules"] if item["name"] == saved["name"])
		self.assertEqual(row["response_config"]["options"][4]["value"], 5)

	def test_approved_allowlisted_update_executes_once(self):
		guardian, pet = self._make_guardian_pet()
		service = self._make_service(guardian, pet)
		rule = self._base_rule()
		rule.update(
			{
				"rule_name": f"Designer Approval {frappe.generate_hash(length=8)}",
				"response_type": "Buttons",
				"response_config": {
					"options": [{"key": "confirm", "label": "Confirm", "value": "confirm", "aliases": ["yes"]}]
				},
				"duplicate_policy": "Allow Repeats",
				"executor": "Update Allowed Field",
				"executor_config": {"field": "status", "values": {"confirm": "completed"}},
				"risk_level": "Low",
				"requires_approval": 0,
			}
		)
		saved = notifications_api.save_whatsapp_action_rule(rule=rule)
		self.assertTrue(saved["ok"], saved)
		self.assertEqual(saved["data"]["rule"]["requires_approval"], 1)
		rule_doc = frappe.get_doc("Pet App WhatsApp Action Rule", saved["data"]["rule"]["name"])
		request = actions.create_action_request(rule_doc, service)
		request.status = "Pending Review"
		request.response_key = "confirm"
		request.response_value = "confirm"
		request.save(ignore_permissions=True)

		approved = notifications_api.approve_whatsapp_action(action_request=request.name, note="Reviewed")
		self.assertTrue(approved["ok"], approved)
		self.assertEqual(frappe.db.get_value("PetCareService", service.name, "status"), "completed")
		request.reload()
		self.assertEqual(request.status, "Completed")
		self.assertTrue(request.rule_snapshot_json)
		self.assertTrue(request.validation_json)
		self.assertTrue(request.resolved_recipient_json)

		second = notifications_api.approve_whatsapp_action(action_request=request.name)
		self.assertFalse(second["ok"])

	def test_malformed_legacy_section_is_listed_and_save_is_rejected(self):
		name = "PetCareService Completed Rating"
		original = frappe.db.get_value("Pet App WhatsApp Action Rule", name, "response_config_json")
		try:
			frappe.db.set_value(
				"Pet App WhatsApp Action Rule",
				name,
				"response_config_json",
				"{malformed",
				update_modified=False,
			)
			listed = notifications_api.list_whatsapp_action_rules(source_doctype="PetCareService")
			row = next(item for item in listed["data"]["rules"] if item["name"] == name)
			self.assertIn("response_config", row["parse_errors"])

			for payload in ({"name": name, "rule_name": name}, row):
				response = notifications_api.save_whatsapp_action_rule(data=payload)
				self.assertFalse(response["ok"])
				self.assertEqual(response["meta"]["code"], "LEGACY_SECTION_MALFORMED")
		finally:
			frappe.db.set_value(
				"Pet App WhatsApp Action Rule",
				name,
				"response_config_json",
				original,
				update_modified=False,
			)

	def test_template_rejects_unallowlisted_variables_and_html(self):
		with self.assertRaises(designer.DesignerError) as caught:
			designer.validate_template_content(
				{"body_preview": "<b>{{ guardian.otp_code }}</b>"},
				"PetCareService",
			)
		self.assertEqual(caught.exception.code, "TEMPLATE_NOT_COMPATIBLE")
		with self.assertRaises(designer.DesignerError):
			designer.validate_template_content({"body_preview": "{{ arbitrary.method }}"}, None)
		designer.validate_template_content({"body_preview": "Your code is {{ otp }}"}, None)

	def test_simulation_is_read_only(self):
		guardian, pet = self._make_guardian_pet()
		service = self._make_service(guardian, pet)
		frappe.db.set_value("PetCareService", service.name, "status", "Completed", update_modified=False)
		rule = self._base_rule()
		tracked = (
			"Pet App Notification Queue",
			"Pet App WhatsApp Action Request",
			"Pet App WhatsApp Conversation",
			"Pet App WhatsApp Message",
			"Rating",
			"ToDo",
		)
		before = {doctype: frappe.db.count(doctype) for doctype in tracked}
		response = notifications_api.simulate_whatsapp_action_rule(rule=rule, source_name=service.name)
		after = {doctype: frappe.db.count(doctype) for doctype in tracked}

		self.assertTrue(response["ok"], response)
		self.assertTrue(response["data"]["simulation"]["matched"])
		self.assertEqual(response["data"]["simulation"]["recipient"]["name"], guardian.name)
		self.assertIn(pet.pet_name, response["data"]["simulation"]["rendered_message"])
		self.assertEqual(before, after)

	def test_simulation_context_blocks_notification_queueing(self):
		before = frappe.db.count("Pet App Notification Queue")
		with designer.simulation_context():
			result = engine.queue_notification(
				event_key="designer.simulation.block",
				recipient_type="Manual",
				to_phone="9647710000000",
				template_key="pet_service_rating_request",
			)
		self.assertFalse(result["ok"])
		self.assertEqual(result["meta"]["code"], "SIMULATION_SIDE_EFFECT_BLOCKED")
		self.assertEqual(frappe.db.count("Pet App Notification Queue"), before)

	def _base_rule(self):
		rule = designer.canonical_rule(
			frappe.get_doc("Pet App WhatsApp Action Rule", "PetCareService Completed Rating")
		)
		rule["name"] = None
		return rule

	def _make_guardian_pet(self):
		suffix = frappe.generate_hash(length=8)
		digits = "".join(str(ord(char) % 10) for char in suffix).ljust(9, "0")[:9]
		guardian = frappe.get_doc(
			{
				"doctype": "Guardian",
				"phone": f"07{digits}",
				"full_name": f"Designer Guardian {suffix}",
				"email_id": f"designer.{suffix}@example.com",
			}
		).insert(ignore_permissions=True)
		pet = frappe.get_doc(
			{
				"doctype": "Pet",
				"pet_name": f"Designer Pet {suffix}",
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

	def _make_service(self, guardian, pet):
		category = frappe.get_doc(
			{
				"doctype": "CategoryCareServices",
				"category_name": f"Designer Category {frappe.generate_hash(length=8)}",
			}
		).insert(ignore_permissions=True)
		return frappe.get_doc(
			{
				"doctype": "PetCareService",
				"pet_service_name": f"Designer Service {frappe.generate_hash(length=8)}",
				"pet_id": pet.name,
				"guardian_id": guardian.name,
				"category": category.name,
				"due_date": nowdate(),
				"status": "pending",
			}
		).insert(ignore_permissions=True)
