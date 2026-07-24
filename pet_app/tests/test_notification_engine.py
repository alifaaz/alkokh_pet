from __future__ import annotations

import hashlib
import hmac
from datetime import timedelta
from unittest.mock import Mock

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from pet_app.api.whatsapp import _verify_signature, _verify_token, webhook
from pet_app.notifications import engine
from pet_app.notifications.channels.whatsapp_meta import WhatsAppMetaAPIError, WhatsAppMetaChannel, media_mime_type
from pet_app.notifications.scheduler import enqueue_due_reminders, process_due_notifications
from pet_app.notifications.webhook import handle_whatsapp_webhook


class TestNotificationEngine(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.test_account = self._make_whatsapp_account(verify_token=f"test-{frappe.generate_hash(length=10)}")
		frappe.db.set_single_value("Pet App Notification Settings", "default_whatsapp_account", self.test_account.name)
		frappe.db.set_single_value("Pet App Notification Settings", "dry_run", 1)
		frappe.db.set_single_value("Pet App Notification Settings", "enabled", 1)
		self._ensure_auth_template()

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_queue_notification_idempotency_and_dummy_send_masks_otp(self):
		guardian = self._make_guardian()
		key = f"notif-{frappe.generate_hash(length=10)}"

		first = engine.queue_notification(
			event_key="auth.otp",
			recipient_type="Guardian",
			recipient_name=guardian.name,
			context={"otp": "123456"},
			template_key="auth_otp",
			idempotency_key=key,
		)
		second = engine.queue_notification(
			event_key="auth.otp",
			recipient_type="Guardian",
			recipient_name=guardian.name,
			context={"otp": "123456"},
			template_key="auth_otp",
			idempotency_key=key,
		)

		self.assertTrue(first["ok"])
		self.assertTrue(second["ok"])
		self.assertEqual(first["data"]["queue"]["name"], second["data"]["queue"]["name"])
		self.assertTrue(second["meta"]["duplicate"])

		sent = engine.process_notification_queue(first["data"]["queue"]["name"])
		self.assertTrue(sent["ok"])
		self.assertEqual(sent["data"]["queue"]["status"], "Sent")
		self.assertTrue(sent["data"]["queue"]["provider_message_id"].startswith("dummy-"))
		self.assertNotIn("123456", sent["data"]["queue"]["rendered_preview"])
		log = frappe.db.get_value("Pet App Notification Log", {"queue": sent["data"]["queue"]["name"]}, ["message", "to_phone_masked"], as_dict=True)
		self.assertNotIn("123456", log.message)
		self.assertTrue(log.to_phone_masked.endswith(guardian.phone[-4:]))

	def test_manual_send_is_not_swallowed_by_idempotency(self):
		guardian = self._make_guardian()
		template = self._make_template()

		def _manual_send():
			return engine.queue_notification(
				event_key=template.event_key,
				recipient_type="Guardian",
				recipient_name=guardian.name,
				context={"name": guardian.full_name},
				template_key=template.template_key,
				manual=True,
			)

		first = _manual_send()
		second = _manual_send()

		self.assertTrue(first["ok"])
		self.assertTrue(second["ok"])
		# A deliberate second manual click must create a distinct queue row and
		# not be reported as a swallowed duplicate.
		self.assertNotEqual(first["data"]["queue"]["name"], second["data"]["queue"]["name"])
		self.assertFalse(second.get("meta", {}).get("duplicate"))

		# Both actually dispatch.
		for result in (first, second):
			sent = engine.process_notification_queue(result["data"]["queue"]["name"])
			self.assertEqual(sent["data"]["queue"]["status"], "Sent")

	def test_automated_send_still_dedupes(self):
		guardian = self._make_guardian()
		template = self._make_template()
		kwargs = dict(
			event_key=template.event_key,
			recipient_type="Guardian",
			recipient_name=guardian.name,
			context={"name": guardian.full_name},
			template_key=template.template_key,
			source_doctype="Guardian",
			source_name=guardian.name,
		)

		first = engine.queue_notification(**kwargs)
		second = engine.queue_notification(**kwargs)

		self.assertEqual(first["data"]["queue"]["name"], second["data"]["queue"]["name"])
		self.assertTrue(second["meta"]["duplicate"])

	def test_source_context_populates_nested_pet_and_guardian_names(self):
		guardian, pet = self._make_guardian_pet()
		template = self._make_template()
		template.source_doctype = "Pet"
		template.body_preview = "hi {{ guardian.display_name }} about {{ pet.display_name }}"
		template.save(ignore_permissions=True)

		result = engine.queue_notification(
			event_key=template.event_key,
			recipient_type="Guardian",
			recipient_name=guardian.name,
			context={"guardian": guardian.full_name, "pet_name": pet.pet_name},
			template_key=template.template_key,
			source_doctype="Pet",
			source_name=pet.name,
			idempotency_key=f"source-context-{frappe.generate_hash(length=10)}",
		)

		self.assertTrue(result["ok"])
		self.assertEqual(
			result["data"]["queue"]["rendered_preview"],
			f"hi {guardian.full_name} about {pet.pet_name}",
		)

	def test_failed_outbound_message_is_visible_in_conversation(self):
		guardian = self._make_guardian()
		result = engine.queue_notification(
			event_key="auth.otp",
			recipient_type="Guardian",
			recipient_name=guardian.name,
			context={"otp": "123456"},
			template_key="auth_otp",
			idempotency_key=f"failed-inbox-{frappe.generate_hash(length=10)}",
		)
		queue = frappe.get_doc("Pet App Notification Queue", result["data"]["queue"]["name"])

		failure = engine._mark_failed(queue.name, RuntimeError("Test provider failure"))
		self.assertFalse(failure["ok"])
		message = frappe.get_doc(
			"Pet App WhatsApp Message",
			frappe.db.get_value("Pet App WhatsApp Message", {"notification_queue": queue.name}, "name"),
		)
		self.assertEqual(message.conversation, queue.conversation)
		self.assertEqual(message.status, "Failed")

	def test_send_whatsapp_otp_masks_stored_payload(self):
		guardian = self._make_guardian()
		result = engine.send_whatsapp_otp(guardian=guardian.name, otp="654321", idempotency_key=f"otp-{frappe.generate_hash(length=10)}")

		self.assertTrue(result["ok"])
		self.assertEqual(result["data"]["queue"]["status"], "Sent")
		queue = frappe.get_doc("Pet App Notification Queue", result["data"]["queue"]["name"])
		self.assertNotIn("654321", queue.context_json)
		self.assertNotIn("654321", queue.rendered_preview)
		self.assertFalse(frappe.db.sql(
			"select name from `tabPet App Notification Log` where queue=%s and message like %s",
			(queue.name, "%654321%"),
		))

	def test_webhook_delivered_updates_queue_and_is_idempotent(self):
		guardian = self._make_guardian()
		result = engine.send_whatsapp_otp(guardian=guardian.name, otp="111222", idempotency_key=f"hook-{frappe.generate_hash(length=10)}")
		queue = result["data"]["queue"]
		payload = {
			"entry": [
				{
					"changes": [
						{
							"value": {
								"metadata": {"phone_number_id": "phone-id"},
								"statuses": [{"id": queue["provider_message_id"], "status": "delivered", "timestamp": "1", "recipient_id": queue["to_phone"]}],
							}
						}
					]
				}
			]
		}

		first = handle_whatsapp_webhook(payload)
		second = handle_whatsapp_webhook(payload)

		self.assertTrue(first["ok"])
		self.assertTrue(second["ok"])
		self.assertEqual(frappe.db.get_value("Pet App Notification Queue", queue["name"], "status"), "Delivered")
		self.assertEqual(frappe.db.count("Pet App WhatsApp Webhook Event", {"provider_message_id": queue["provider_message_id"], "processed": 1}), 1)

	def test_direct_inbox_status_does_not_regress_on_late_sent_webhook(self):
		provider_message_id = f"wamid-{frappe.generate_hash(length=12)}"
		conversation = frappe.get_doc(
			{
				"doctype": "Pet App WhatsApp Conversation",
				"conversation_key": frappe.generate_hash(length=40),
				"provider_account": self.test_account.name,
				"normalized_phone": "9647712345678",
				"status": "Open",
			}
		).insert(ignore_permissions=True)
		message = frappe.get_doc(
			{
				"doctype": "Pet App WhatsApp Message",
				"conversation": conversation.name,
				"direction": "Outbound",
				"message_type": "Document",
				"body": "Media test",
				"provider_message_id": provider_message_id,
				"status": "Sent",
				"message_at": now_datetime(),
			}
		).insert(ignore_permissions=True)

		def status_payload(status, timestamp):
			return {
				"entry": [{"changes": [{"value": {"statuses": [{"id": provider_message_id, "status": status, "timestamp": timestamp}]}}]}]
			}

		handle_whatsapp_webhook(status_payload("delivered", "2"))
		handle_whatsapp_webhook(status_payload("sent", "1"))

		self.assertEqual(frappe.db.get_value(message.doctype, message.name, "status"), "Delivered")

	def test_opt_out_blocks_utility_but_not_authentication(self):
		guardian = self._make_guardian()
		phone = engine.queue_notification(
			event_key="auth.otp",
			recipient_type="Guardian",
			recipient_name=guardian.name,
			context={"otp": "222333"},
			template_key="auth_otp",
			idempotency_key=f"phone-{frappe.generate_hash(length=10)}",
		)["data"]["queue"]["to_phone"]
		frappe.get_doc(
			{
				"doctype": "Pet App Communication Consent",
				"party_type": "Guardian",
				"party": guardian.name,
				"phone": phone,
				"channel": "WhatsApp",
				"opt_in": 0,
			}
		).insert(ignore_permissions=True)
		template = self._make_template(category="Utility")

		blocked = engine.queue_notification(
			event_key=template.event_key,
			recipient_type="Guardian",
			recipient_name=guardian.name,
			context={"name": guardian.full_name},
			template_key=template.template_key,
			idempotency_key=f"blocked-{frappe.generate_hash(length=10)}",
		)
		auth = engine.queue_notification(
			event_key="auth.otp",
			recipient_type="Guardian",
			recipient_name=guardian.name,
			context={"otp": "333444"},
			template_key="auth_otp",
			idempotency_key=f"auth-{frappe.generate_hash(length=10)}",
		)

		self.assertFalse(blocked["ok"])
		self.assertEqual(blocked["meta"]["code"], "PERMISSION_ERROR")
		self.assertTrue(auth["ok"])

	def test_quiet_hours_delays_non_urgent_message(self):
		frappe.db.set_single_value("Pet App Notification Settings", "respect_quiet_hours", 1)
		frappe.db.set_single_value("Pet App Notification Settings", "quiet_hours_start", "00:00:00")
		frappe.db.set_single_value("Pet App Notification Settings", "quiet_hours_end", "23:59:59")
		guardian = self._make_guardian()
		template = self._make_template(category="Utility")

		result = engine.queue_notification(
			event_key=template.event_key,
			recipient_type="Guardian",
			recipient_name=guardian.name,
			context={"name": guardian.full_name},
			template_key=template.template_key,
			idempotency_key=f"quiet-{frappe.generate_hash(length=10)}",
		)

		self.assertTrue(result["ok"])
		self.assertGreater(frappe.utils.get_datetime(result["data"]["queue"]["scheduled_at"]), now_datetime())

	def test_due_reminder_queues_once(self):
		guardian = self._make_guardian()
		template = self._make_template(category="Utility")
		key = f"reminder-{frappe.generate_hash(length=10)}"
		reminder = frappe.get_doc(
			{
				"doctype": "Pet App Reminder",
				"reminder_type": "Follow-up Due",
				"status": "Scheduled",
				"channel": "WhatsApp",
				"template_key": template.template_key,
				"guardian": guardian.name,
				"due_datetime": now_datetime(),
				"send_at": now_datetime(),
				"title": "Follow-up",
				"context_json": "{}",
				"dedupe_key": key,
			}
		).insert(ignore_permissions=True)

		first = enqueue_due_reminders()
		second = enqueue_due_reminders()

		self.assertTrue(first["ok"])
		self.assertTrue(second["ok"])
		self.assertEqual(frappe.db.count("Pet App Notification Queue", {"idempotency_key": key}), 1)
		self.assertEqual(frappe.db.get_value("Pet App Reminder", reminder.name, "status"), "Queued")

	def test_due_notification_queue_is_processed_by_scheduler(self):
		guardian = self._make_guardian()
		template = self._make_template(category="Utility")
		key = f"scheduled-send-{frappe.generate_hash(length=10)}"
		reminder = frappe.get_doc(
			{
				"doctype": "Pet App Reminder",
				"reminder_type": "Follow-up Due",
				"status": "Scheduled",
				"channel": "WhatsApp",
				"template_key": template.template_key,
				"guardian": guardian.name,
				"due_datetime": now_datetime(),
				"send_at": now_datetime(),
				"title": "Follow-up",
				"context_json": "{}",
				"dedupe_key": key,
			}
		).insert(ignore_permissions=True)

		queued = enqueue_due_reminders()
		processed = process_due_notifications()

		self.assertTrue(queued["ok"])
		self.assertTrue(processed["ok"])
		queue_name = frappe.db.get_value("Pet App Reminder", reminder.name, "notification_queue")
		self.assertEqual(frappe.db.get_value("Pet App Notification Queue", queue_name, "status"), "Sent")

	def test_whatsapp_account_secrets_are_password_fields(self):
		meta = frappe.get_meta("Pet App WhatsApp Account")
		self.assertEqual(meta.get_field("access_token").fieldtype, "Password")
		self.assertGreaterEqual(meta.get_field("access_token").length, 4096)
		self.assertEqual(meta.get_field("app_secret").fieldtype, "Password")
		self.assertGreaterEqual(meta.get_field("app_secret").length, 512)
		self.assertEqual(meta.get_field("verify_token").fieldtype, "Password")
		self.assertGreaterEqual(meta.get_field("verify_token").length, 512)

	def test_meta_template_without_variables_omits_empty_components(self):
		channel = WhatsAppMetaChannel(account=frappe._dict(default_language="en"))
		template = frappe._dict(template_name="hello_world", language="en_US", components_json=None)

		payload = channel.build_template_payload(to_phone="9647716940612", template=template)

		self.assertNotIn("components", payload["template"])

	def test_meta_api_error_preserves_safe_provider_code(self):
		response = Mock(ok=False, status_code=400)
		response.json.return_value = {
			"error": {
				"message": "(#133010) Account not registered",
				"type": "OAuthException",
				"code": 133010,
				"fbtrace_id": "safe-trace-id",
			}
		}

		with self.assertRaises(WhatsAppMetaAPIError) as raised:
			WhatsAppMetaChannel()._response_json(response)

		self.assertEqual(raised.exception.exc_type, "META_133010")
		self.assertEqual(raised.exception.details["http_status"], 400)
		self.assertEqual(raised.exception.details["trace_id"], "safe-trace-id")

	def test_meta_media_upload_uses_supported_mime_type(self):
		self.assertEqual(media_mime_type("alkokh-whatsapp-live-test.pdf"), "application/pdf")

	def test_whatsapp_webhook_verification_returns_raw_challenge(self):
		token = f"verify-{frappe.generate_hash(length=10)}"
		challenge = f"challenge-{frappe.generate_hash(length=10)}"
		self._make_whatsapp_account(verify_token=token)

		frappe.local.form_dict = frappe._dict(
			{
				"hub.mode": "subscribe",
				"hub.verify_token": token,
				"hub.challenge": challenge,
			}
		)
		response = webhook()

		self.assertTrue(_verify_token(token))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.get_data(as_text=True), challenge)
		self.assertIn("text/plain", response.content_type)

	def test_whatsapp_webhook_signature_uses_configured_app_secret(self):
		secret = f"secret-{frappe.generate_hash(length=12)}"
		body = b'{"object":"whatsapp_business_account"}'
		self.test_account.app_secret = secret
		self.test_account.save(ignore_permissions=True)
		signature = f"sha256={hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()}"

		self.assertTrue(_verify_signature(body, signature))
		self.assertFalse(_verify_signature(body + b" ", signature))
		self.assertFalse(_verify_signature(body, None))

	def _make_guardian(self):
		suffix = frappe.generate_hash(length=8)
		phone_digits = self._digits(suffix, 9)
		return frappe.get_doc(
			{
				"doctype": "Guardian",
				"phone": f"07{phone_digits}",
				"full_name": f"Notification Guardian {suffix}",
				"email_id": f"notification.{suffix}@example.com",
			}
		).insert(ignore_permissions=True)

	def _make_guardian_pet(self):
		guardian = self._make_guardian()
		suffix = frappe.generate_hash(length=8)
		pet = frappe.get_doc(
			{
				"doctype": "Pet",
				"pet_name": f"Notification Pet {suffix}",
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

	def _make_template(self, category="Utility"):
		suffix = frappe.generate_hash(length=8)
		return frappe.get_doc(
			{
				"doctype": "Pet App WhatsApp Template",
				"template_key": f"test_{category.lower()}_{suffix}",
				"enabled": 1,
				"template_name": f"test_{suffix}",
				"language": "en",
				"category": category,
				"provider_account": self.test_account.name,
				"event_key": f"test.{suffix}",
				"recipient_type": "Guardian",
				"body_preview": "Hello {{ name }}",
			}
		).insert(ignore_permissions=True)

	def _make_whatsapp_account(self, verify_token):
		suffix = frappe.generate_hash(length=8)
		return frappe.get_doc(
			{
				"doctype": "Pet App WhatsApp Account",
				"account_name": f"Test WhatsApp {suffix}",
				"enabled": 1,
				"is_default": 0,
				"provider": "Dummy / Dev",
				"verify_token": verify_token,
				"default_language": "en",
			}
		).insert(ignore_permissions=True)

	def _ensure_auth_template(self):
		if frappe.db.exists("Pet App WhatsApp Template", "auth_otp"):
			return
		frappe.get_doc(
			{
				"doctype": "Pet App WhatsApp Template",
				"template_key": "auth_otp",
				"enabled": 1,
				"template_name": "auth_otp",
				"language": "en",
				"category": "Authentication",
				"provider_account": self.test_account.name,
				"event_key": "auth.otp",
				"recipient_type": "Guardian",
				"body_preview": "Your verification code is {{ otp }}.",
				"delivery_mode": "Meta Template",
				"allow_during_quiet_hours": 1,
				"priority": "urgent",
			}
		).insert(ignore_permissions=True)

	def _digits(self, value, length):
		digits = "".join(str(ord(char) % 10) for char in value)
		while len(digits) < length:
			digits += digits
		return digits[:length]
