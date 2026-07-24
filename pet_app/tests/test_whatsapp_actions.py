from __future__ import annotations

import json

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime, nowdate

from pet_app.api import notifications as notifications_api
from pet_app.notifications import actions, engine
from pet_app.notifications.context import build_document_context, list_template_variables
from pet_app.notifications.webhook import handle_whatsapp_webhook


class TestWhatsAppActions(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Pet App Notification Settings", "dry_run", 1)
		frappe.db.set_single_value("Pet App Notification Settings", "enabled", 1)
		if frappe.db.exists("Pet App WhatsApp Action Rule", "PetCareService Completed Rating"):
			frappe.db.set_value("Pet App WhatsApp Action Rule", "PetCareService Completed Rating", "enabled", 1)

	def test_structured_queue_error_is_preserved_on_action_failure(self):
		result = {"ok": False, "errors": [{"message": "Not permitted", "details": {}}]}

		self.assertEqual(actions._result_error(result), "Not permitted")

	def test_template_context_is_dynamic_and_death_record_internal_note_is_not_exposed(self):
		variables = {row["key"] for row in list_template_variables()}
		self.assertIn("guardian.full_name", variables)
		self.assertIn("pet.pet_name", variables)
		self.assertIn("invoice.grand_total", variables)
		self.assertIn("death_record.guardian_visible_summary", variables)
		self.assertIn("pet_service.pet_service_name", variables)
		self.assertNotIn("death_record.internal_note", variables)

		guardian, pet = self._make_guardian_pet()
		service = self._make_service(guardian, pet)
		context = build_document_context(service.doctype, service.name)
		self.assertEqual(context["guardian"]["full_name"], guardian.full_name)
		self.assertEqual(context["pet"]["pet_name"], pet.pet_name)
		self.assertEqual(context["pet_service"]["pet_service_name"], service.pet_service_name)

	def test_completed_service_creates_one_request_and_typed_rating(self):
		guardian, pet = self._make_guardian_pet()
		service = self._make_service(guardian, pet)
		service.status = "completed"
		service.save(ignore_permissions=True)
		service.save(ignore_permissions=True)

		requests = frappe.get_all(
			"Pet App WhatsApp Action Request",
			filters={"rule": "PetCareService Completed Rating", "source_name": service.name},
			pluck="name",
			ignore_permissions=True,
		)
		self.assertEqual(len(requests), 1)
		request = frappe.get_doc("Pet App WhatsApp Action Request", requests[0])
		self.assertEqual(request.recipient_phone, self._normalized(guardian.phone))

		result = handle_whatsapp_webhook(self._text_payload(guardian.phone, "5"))
		self.assertTrue(result["ok"])
		request.reload()
		self.assertEqual(request.status, "Completed")
		rating = frappe.get_doc("Rating", request.result_name)
		self.assertEqual(rating.reference_doctype, "PetCareService")
		self.assertEqual(rating.reference_name, service.name)
		self.assertEqual(rating.overall_rating, 5)
		conversation = notifications_api.get_whatsapp_conversation(request.conversation)
		action_payload = next(row for row in conversation["data"]["actions"] if row["name"] == request.name)
		self.assertEqual(action_payload["source_doctype"], "PetCareService")
		self.assertEqual(action_payload["source_name"], service.name)

		result = handle_whatsapp_webhook(self._text_payload(guardian.phone, "5"))
		self.assertTrue(result["ok"])
		self.assertEqual(
			frappe.db.count("Rating", {"reference_doctype": "PetCareService", "reference_name": service.name}),
			1,
		)

	def test_interactive_list_reply_matches_request_id(self):
		guardian, pet = self._make_guardian_pet()
		service = self._make_service(guardian, pet)
		service.status = "Completed"
		service.save(ignore_permissions=True)
		request_name = frappe.db.get_value(
			"Pet App WhatsApp Action Request",
			{"rule": "PetCareService Completed Rating", "source_name": service.name},
			"name",
		)
		result = handle_whatsapp_webhook(self._interactive_payload(guardian.phone, f"wa:{request_name}:4", "4 / 5"))
		self.assertTrue(result["ok"])
		request = frappe.get_doc("Pet App WhatsApp Action Request", request_name)
		self.assertEqual(request.status, "Completed")
		self.assertEqual(frappe.db.get_value("Rating", request.result_name, "overall_rating"), 4)

	def test_rating_executor_is_reusable_for_another_doctype(self):
		guardian, pet = self._make_guardian_pet()
		rule = self._make_rule(
			source_doctype="Pet",
			recipient_field=None,
			response_config_json=json.dumps(
				{"options": [{"key": "4", "label": "Four", "value": 4, "aliases": ["4", "good"]}]}
			),
		)
		request = actions.create_action_request(rule, pet, recipient=guardian.phone)
		result = handle_whatsapp_webhook(self._text_payload(guardian.phone, "GOOD"))
		self.assertTrue(result["ok"])
		request.reload()
		self.assertEqual(request.status, "Completed")
		self.assertEqual(frappe.db.get_value("Rating", request.result_name, "reference_doctype"), "Pet")

	def test_typed_action_prompt_sent_in_open_session_waits_for_reply(self):
		guardian, pet = self._make_guardian_pet()
		rule = self._make_rule(source_doctype="Pet")
		request = actions.create_action_request(rule, pet, recipient=guardian.phone)
		thread = notifications_api.get_whatsapp_conversation(request.conversation)
		self.assertIn(request.name, {row["name"] for row in thread["data"]["actions"]})
		conversation = frappe.get_doc("Pet App WhatsApp Conversation", request.conversation)
		conversation.last_inbound_at = now_datetime()
		conversation.session_expires_at = add_to_date(now_datetime(), hours=24)
		conversation.status = "Open"
		conversation.save(ignore_permissions=True)

		result = engine.process_notification_queue(request.notification_queue)
		self.assertTrue(result["ok"])
		request.reload()
		self.assertEqual(request.status, "Waiting Reply")
		self.assertEqual(request.delivery_stage, "Interactive Sent")

		queue = frappe.get_doc("Pet App Notification Queue", request.notification_queue)
		failure = handle_whatsapp_webhook(
			{
				"entry": [
					{
						"changes": [
							{
								"value": {
									"metadata": {"phone_number_id": self._account().phone_number_id},
									"statuses": [
										{
											"id": queue.provider_message_id,
											"status": "failed",
											"timestamp": str(int(now_datetime().timestamp())),
											"recipient_id": request.recipient_phone,
											"errors": [{"code": 131047, "message": "Test delivery failure"}],
										}
									],
								}
							}
						]
					}
				]
			}
		)
		self.assertTrue(failure["ok"])
		request.reload()
		self.assertEqual(request.status, "Failed")
		self.assertEqual(request.error_message, "Test delivery failure")
		self.assertEqual(
			frappe.db.count("Pet App WhatsApp Action Event", {"action_request": request.name, "event_type": "Failed"}),
			1,
		)

	def test_ambiguous_alias_needs_review_and_does_not_execute(self):
		guardian, pet = self._make_guardian_pet()
		rule = self._make_rule(
			source_doctype="Pet",
			response_config_json=json.dumps(
				{
					"options": [
						{"key": "yes", "label": "Yes", "value": 5, "aliases": ["ok"]},
						{"key": "confirm", "label": "Confirm", "value": 4, "aliases": ["OK"]},
					]
				}
			),
		)
		request = actions.create_action_request(rule, pet, recipient=guardian.phone)
		handle_whatsapp_webhook(self._text_payload(guardian.phone, "ok"))
		request.reload()
		self.assertEqual(request.status, "Needs Review")
		self.assertFalse(request.result_name)

	def test_expired_request_does_not_execute(self):
		guardian, pet = self._make_guardian_pet()
		rule = self._make_rule(source_doctype="Pet")
		request = actions.create_action_request(rule, pet, recipient=guardian.phone)
		frappe.db.set_value("Pet App WhatsApp Action Request", request.name, "expires_at", add_to_date(now_datetime(), hours=-1))
		handle_whatsapp_webhook(self._text_payload(guardian.phone, "5"))
		request.reload()
		self.assertEqual(request.status, "Expired")
		self.assertFalse(request.result_name)

	def test_unallowlisted_field_update_is_rejected(self):
		guardian, pet = self._make_guardian_pet()
		with self.assertRaises(frappe.ValidationError):
			self._make_rule(
				source_doctype="Pet",
				executor="Update Allowed Field",
				executor_config_json=json.dumps({"field": "pet_name", "values": {"yes": "Changed"}}),
			)

	def test_stop_is_stored_in_inbox_and_blocks_future_utility_messages(self):
		guardian, pet = self._make_guardian_pet()
		result = handle_whatsapp_webhook(self._text_payload(guardian.phone, "STOP"))
		self.assertTrue(result["ok"])
		phone = self._normalized(guardian.phone)
		conversation = frappe.get_doc(
			"Pet App WhatsApp Conversation",
			frappe.db.get_value("Pet App WhatsApp Conversation", {"normalized_phone": phone}, "name"),
		)
		self.assertEqual(conversation.unread_count, 1)
		self.assertEqual(
			frappe.db.count("Pet App WhatsApp Message", {"conversation": conversation.name, "direction": "Inbound", "body": "STOP"}),
			1,
		)
		self.assertEqual(conversation.status, "Blocked")
		self.assertEqual(frappe.db.get_value("Pet App Communication Consent", {"phone": phone}, "opt_in"), 0)

		template = self._make_template("Pet")
		blocked = engine.queue_notification(
			event_key=template.event_key,
			recipient_type="Guardian",
			recipient_name=guardian.name,
			context={"pet": {"pet_name": pet.pet_name}},
			template_key=template.name,
			idempotency_key=f"stopped-{frappe.generate_hash(length=8)}",
		)
		self.assertFalse(blocked["ok"])

	def _make_service(self, guardian, pet):
		category = frappe.get_doc(
			{
				"doctype": "CategoryCareServices",
				"category_name": f"WhatsApp Category {frappe.generate_hash(length=8)}",
			}
		).insert(ignore_permissions=True)
		return frappe.get_doc(
			{
				"doctype": "PetCareService",
				"pet_service_name": f"WhatsApp Service {frappe.generate_hash(length=8)}",
				"pet_id": pet.name,
				"guardian_id": guardian.name,
				"category": category.name,
				"due_date": nowdate(),
				"status": "pending",
			}
		).insert(ignore_permissions=True)

	def _make_guardian_pet(self):
		suffix = frappe.generate_hash(length=8)
		digits = "".join(str(ord(char) % 10) for char in suffix).ljust(9, "0")[:9]
		guardian = frappe.get_doc(
			{
				"doctype": "Guardian",
				"phone": f"07{digits}",
				"full_name": f"WhatsApp Guardian {suffix}",
				"email_id": f"whatsapp.{suffix}@example.com",
			}
		).insert(ignore_permissions=True)
		pet = frappe.get_doc(
			{
				"doctype": "Pet",
				"pet_name": f"WhatsApp Pet {suffix}",
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

	def _make_rule(self, **overrides):
		suffix = frappe.generate_hash(length=8)
		template = self._make_template(overrides.get("source_doctype") or "Pet")
		data = {
			"doctype": "Pet App WhatsApp Action Rule",
			"rule_name": f"WhatsApp Test Rule {suffix}",
			"enabled": 1,
			"source_doctype": "Pet",
			"trigger_event": "Manual",
			"condition_json": "{}",
			"recipient_type": "Manual",
			"template_key": template.name,
			"response_type": "Typed Reply",
			"response_config_json": json.dumps(
				{"options": [{"key": "5", "label": "Five", "value": 5, "aliases": ["5"]}]}
			),
			"expiry_hours": 24,
			"duplicate_policy": "Allow Repeats",
			"executor": "Create Rating",
			"executor_config_json": json.dumps({"rating_scale": 5}),
			"risk_level": "Low",
			"requires_approval": 0,
		}
		data.update(overrides)
		return frappe.get_doc(data).insert(ignore_permissions=True)

	def _make_template(self, source_doctype):
		suffix = frappe.generate_hash(length=8)
		return frappe.get_doc(
			{
				"doctype": "Pet App WhatsApp Template",
				"template_key": f"whatsapp_action_test_{suffix}",
				"enabled": 1,
				"template_name": f"whatsapp_action_test_{suffix}",
				"language": "en",
				"category": "Service",
				"provider_account": frappe.db.get_single_value("Pet App Notification Settings", "default_whatsapp_account"),
				"event_key": f"whatsapp.action.test.{suffix}",
				"source_doctype": source_doctype,
				"recipient_type": "Manual",
				"delivery_mode": "App Styled",
				"body_preview": "Please reply with your choice for {{ pet.pet_name }}.",
			}
		).insert(ignore_permissions=True)

	def _text_payload(self, local_phone, body):
		return self._message_payload(
			local_phone,
			{"id": f"wamid-test-{frappe.generate_hash(length=10)}", "timestamp": str(int(now_datetime().timestamp())), "type": "text", "text": {"body": body}},
		)

	def _interactive_payload(self, local_phone, reply_id, title):
		return self._message_payload(
			local_phone,
			{
				"id": f"wamid-test-{frappe.generate_hash(length=10)}",
				"timestamp": str(int(now_datetime().timestamp())),
				"type": "interactive",
				"interactive": {"type": "list_reply", "list_reply": {"id": reply_id, "title": title}},
			},
		)

	def _message_payload(self, local_phone, message):
		account = self._account()
		message["from"] = self._normalized(local_phone)
		return {
			"entry": [
				{
					"changes": [
						{"value": {"metadata": {"phone_number_id": account.phone_number_id}, "messages": [message]}}
					]
				}
			]
		}

	def _normalized(self, local_phone):
		return "964" + local_phone[1:] if local_phone.startswith("0") else local_phone.lstrip("+")

	def _account(self):
		return frappe.get_doc(
			"Pet App WhatsApp Account",
			frappe.db.get_single_value("Pet App Notification Settings", "default_whatsapp_account"),
		)
