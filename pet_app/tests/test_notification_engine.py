from __future__ import annotations

from datetime import timedelta

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from pet_app.notifications import engine
from pet_app.notifications.scheduler import enqueue_due_reminders
from pet_app.notifications.webhook import handle_whatsapp_webhook


class TestNotificationEngine(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")

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

	def test_whatsapp_account_secrets_are_password_fields(self):
		meta = frappe.get_meta("Pet App WhatsApp Account")
		self.assertEqual(meta.get_field("access_token").fieldtype, "Password")
		self.assertEqual(meta.get_field("app_secret").fieldtype, "Password")
		self.assertEqual(meta.get_field("verify_token").fieldtype, "Password")

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

	def _make_template(self, category="Utility"):
		suffix = frappe.generate_hash(length=8)
		account = frappe.db.get_value("Pet App WhatsApp Account", {"is_default": 1}, "name")
		return frappe.get_doc(
			{
				"doctype": "Pet App WhatsApp Template",
				"template_key": f"test_{category.lower()}_{suffix}",
				"enabled": 1,
				"template_name": f"test_{suffix}",
				"language": "en",
				"category": category,
				"provider_account": account,
				"event_key": f"test.{suffix}",
				"recipient_type": "Guardian",
				"body_preview": "Hello {{ name }}",
			}
		).insert(ignore_permissions=True)

	def _digits(self, value, length):
		digits = "".join(str(ord(char) % 10) for char in value)
		while len(digits) < length:
			digits += digits
		return digits[:length]
