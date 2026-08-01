from __future__ import annotations

import hashlib
import hmac
from datetime import timedelta
from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from pet_app.api import notifications as notifications_api
from pet_app.api import push as push_api
from pet_app.api.mobile import config as mobile_config
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
		if frappe.db.exists("DocType", "Pet App Notification Settings") and frappe.get_meta("Pet App Notification Settings").has_field("onesignal_enabled"):
			frappe.db.set_single_value("Pet App Notification Settings", "onesignal_enabled", 0)
			frappe.db.set_single_value("Pet App Notification Settings", "onesignal_mirror_frappe_notifications", 0)
			if frappe.get_meta("Pet App Notification Settings").has_field("push_frontend_base_url"):
				frappe.db.set_single_value("Pet App Notification Settings", "push_frontend_base_url", "")

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

	def test_push_queue_uses_onesignal_dry_run_without_template_or_phone(self):
		self._enable_onesignal(dry_run=1)
		user = self._make_user()
		key = f"push-{frappe.generate_hash(length=10)}"

		queued = engine.queue_push_notification(
			user=user.name,
			title="New assignment",
			body="Please review the visit.",
			url="/app/user/" + user.name,
			data={"kind": "assignment"},
			idempotency_key=key,
		)
		duplicate = engine.queue_push_notification(
			user=user.name,
			title="New assignment",
			body="Please review the visit.",
			idempotency_key=key,
		)
		sent = engine.process_notification_queue(queued["data"]["queue"]["name"])

		self.assertTrue(queued["ok"])
		self.assertTrue(duplicate["meta"]["duplicate"])
		self.assertEqual(sent["data"]["queue"]["channel"], "Push")
		self.assertEqual(sent["data"]["queue"]["provider"], "OneSignal")
		self.assertEqual(sent["data"]["queue"]["status"], "Sent")
		self.assertTrue(sent["data"]["queue"]["provider_message_id"].startswith("dry-run-"))
		log = frappe.db.get_value("Pet App Notification Log", {"queue": sent["data"]["queue"]["name"]}, ["message", "push_user"], as_dict=True)
		self.assertEqual(log.message, "Please review the visit.")
		self.assertEqual(log.push_user, user.name)

	def test_onesignal_provider_posts_external_user_alias(self):
		self._enable_onesignal(dry_run=0)
		user = self._make_user()
		settings = frappe.get_single("Pet App Notification Settings")
		settings.onesignal_rest_api_key = "rest-api-key"
		settings.save(ignore_permissions=True)
		response = Mock(ok=True)
		response.json.return_value = {"id": "onesignal-message-id"}

		with patch("pet_app.notifications.channels.onesignal.requests.post", return_value=response) as mocked:
			queued = engine.queue_push_notification(
				user=user.name,
				title="New mention",
				body="You were mentioned.",
				url="/app/user/" + user.name,
				idempotency_key=f"onesignal-post-{frappe.generate_hash(length=10)}",
			)
			sent = engine.process_notification_queue(queued["data"]["queue"]["name"])

		self.assertEqual(sent["data"]["queue"]["provider_message_id"], "onesignal-message-id")
		payload = mocked.call_args.kwargs["data"]
		self.assertIn('"target_channel": "push"', payload)
		self.assertIn(user.name, payload)
		self.assertEqual(mocked.call_args.kwargs["headers"]["Authorization"], "Key rest-api-key")

	def test_onesignal_provider_surfaces_invalid_alias_errors(self):
		self._enable_onesignal(dry_run=0)
		user = self._make_user()
		settings = frappe.get_single("Pet App Notification Settings")
		settings.onesignal_rest_api_key = "rest-api-key"
		settings.save(ignore_permissions=True)
		response = Mock(ok=True)
		response.json.return_value = {"id": "", "errors": {"invalid_aliases": {"external_id": [user.name]}}}

		with patch("pet_app.notifications.channels.onesignal.requests.post", return_value=response):
			queued = engine.queue_push_notification(
				user=user.name,
				title="New mention",
				body="You were mentioned.",
				idempotency_key=f"onesignal-invalid-alias-{frappe.generate_hash(length=10)}",
			)
			sent = engine.process_notification_queue(queued["data"]["queue"]["name"])

		self.assertFalse(sent["ok"])
		queue = frappe.get_doc("Pet App Notification Queue", queued["data"]["queue"]["name"])
		self.assertEqual(queue.status, "Failed")
		self.assertIn("invalid_aliases", queue.provider_error_message)
		self.assertIn(user.name, queue.provider_error_message)

	def test_frappe_notification_log_is_mirrored_to_push(self):
		self._enable_onesignal(dry_run=1, mirror=1)
		user = self._make_user()
		log = frappe.get_doc(
			{
				"doctype": "Notification Log",
				"for_user": user.name,
				"from_user": "Administrator",
				"type": "Alert",
				"subject": "<b>Vet Visit assigned</b>",
				"document_type": "User",
				"document_name": user.name,
			}
		).insert(ignore_permissions=True)

		queue_name = frappe.db.get_value(
			"Pet App Notification Queue",
			{"idempotency_key": f"frappe-notification-log:{log.name}:push"},
			"name",
		)
		self.assertTrue(queue_name)
		queue = frappe.get_doc("Pet App Notification Queue", queue_name)
		self.assertEqual(queue.channel, "Push")
		self.assertEqual(queue.push_user, user.name)
		self.assertEqual(queue.push_title, "Vet Visit assigned")
		self.assertEqual(queue.status, "Sent")
		admin_queue_name = frappe.db.get_value(
			"Pet App Notification Queue",
			{"idempotency_key": f"frappe-notification-log:{log.name}:push:Administrator"},
			"name",
		)
		self.assertTrue(admin_queue_name)
		admin_queue = frappe.get_doc("Pet App Notification Queue", admin_queue_name)
		self.assertEqual(admin_queue.channel, "Push")
		self.assertEqual(admin_queue.push_user, "Administrator")
		self.assertEqual(admin_queue.push_title, "Vet Visit assigned")
		self.assertEqual(admin_queue.status, "Sent")
		self.assertEqual(frappe.db.count("Pet App Notification Queue", {"source_doctype": "Notification Log", "source_name": log.name, "channel": "Push"}), 2)

	def test_frappe_notification_log_push_uses_frontend_base_url(self):
		self._enable_onesignal(dry_run=1, mirror=1)
		frappe.db.set_single_value("Pet App Notification Settings", "push_frontend_base_url", "clinic.example.com")
		user = self._make_user()
		log = frappe.get_doc(
			{
				"doctype": "Notification Log",
				"for_user": user.name,
				"from_user": "Administrator",
				"type": "Alert",
				"subject": "Service assigned",
				"document_type": "PetCareService",
				"document_name": "PetCareService-02209",
			}
		).insert(ignore_permissions=True)

		queue_name = frappe.db.get_value(
			"Pet App Notification Queue",
			{"idempotency_key": f"frappe-notification-log:{log.name}:push"},
			"name",
		)
		admin_queue_name = frappe.db.get_value(
			"Pet App Notification Queue",
			{"idempotency_key": f"frappe-notification-log:{log.name}:push:Administrator"},
			"name",
		)

		self.assertEqual(
			frappe.db.get_value("Pet App Notification Queue", queue_name, "push_url"),
			"https://clinic.example.com/healthcare/services/PetCareService-02209",
		)
		self.assertEqual(
			frappe.db.get_value("Pet App Notification Queue", admin_queue_name, "push_url"),
			"https://clinic.example.com/healthcare/services/PetCareService-02209",
		)

	def test_push_subscription_api_upserts_and_unregisters(self):
		self._enable_onesignal(dry_run=1)
		user = self._make_user()
		frappe.set_user(user.name)

		config = push_api.get_config()
		self.assertTrue(config["ok"], config)
		self.assertTrue(config["data"]["enabled"])
		self.assertEqual(config["data"]["external_id"], user.name)
		self.assertNotIn("rest_api_key", frappe.as_json(config["data"]))
		self.assertTrue(mobile_config.get_config()["data"]["feature_flags"]["push_notifications"])

		first = push_api.register_subscription(subscription_id="push-subscription", platform="web", onesignal_id="onesignal-id")
		second = push_api.register_subscription(subscription_id="push-subscription", platform="android", onesignal_id="onesignal-id", token="push-token", opted_in=1)
		third = push_api.register_subscription(subscription_id="push-subscription-2", platform="web", onesignal_id="onesignal-id", token="push-token-2", opted_in=1)
		self.assertTrue(first["ok"], first)
		self.assertTrue(second["ok"], second)
		self.assertTrue(third["ok"], third)
		self.assertEqual(first["data"]["subscription"]["id"], second["data"]["subscription"]["id"])
		self.assertNotEqual(second["data"]["subscription"]["id"], third["data"]["subscription"]["id"])
		self.assertEqual(frappe.db.count("Pet App Push Subscription", {"user": user.name}), 2)
		self.assertTrue(second["data"]["subscription"]["token_present"])
		self.assertTrue(second["data"]["subscription"]["opted_in"])
		self.assertTrue(third["data"]["subscription"]["token_present"])
		self.assertEqual(second["data"]["subscription"]["onesignal_app_id"], "onesignal-app-id")
		self.assertEqual(third["data"]["subscription"]["onesignal_app_id"], "onesignal-app-id")
		listed = push_api.list_subscriptions()
		self.assertTrue(listed["ok"], listed)
		self.assertEqual(listed["data"]["active_count"], 2)
		self.assertEqual(
			{row["subscription_id"] for row in listed["data"]["subscriptions"]},
			{"push-subscription", "push-subscription-2"},
		)

		deleted = push_api.unregister_subscription(subscription_id="push-subscription")
		self.assertTrue(deleted["ok"], deleted)
		self.assertEqual(
			frappe.db.get_value("Pet App Push Subscription", {"subscription_id": "push-subscription"}, "disabled"),
			1,
		)
		self.assertEqual(push_api.list_subscriptions()["data"]["active_count"], 1)

	def test_manual_push_registration_and_test_send_api(self):
		self._enable_onesignal(dry_run=1)
		frappe.db.set_single_value("Pet App Notification Settings", "push_frontend_base_url", "https://clinic.example.com")
		user = self._make_user()
		frappe.set_user("Administrator")
		record = self._onesignal_user_record(user.name, subscription_id="manual-subscription")

		with patch("pet_app.notifications.channels.onesignal.OneSignalPushChannel.get_user_by_alias", return_value=record):
			registered = push_api.manual_register_subscription(
				user=user.name,
				subscription_id="manual-subscription",
				onesignal_id="manual-onesignal-id",
				token="manual-push-token",
				opted_in=1,
				permission="granted",
			)
			sent = push_api.send_test_push(user=user.name, subscription_id="manual-subscription", title="Manual test", body="Hello from test")

		self.assertTrue(registered["ok"], registered)
		self.assertEqual(registered["data"]["subscription"]["user"], user.name)
		self.assertTrue(registered["data"]["subscription"]["token_present"])
		self.assertTrue(registered["data"]["subscription"]["opted_in"])
		self.assertTrue(sent["ok"], sent)
		self.assertTrue(sent["data"]["accepted"])
		self.assertTrue(sent["data"]["dry_run"])
		self.assertEqual(sent["data"]["click_url"], "https://clinic.example.com")
		self.assertTrue(sent["data"]["provider_message_id"].startswith("dry-run-"))

	def test_onesignal_status_api_sanitizes_tokens(self):
		self._enable_onesignal(dry_run=0)
		user = self._make_user()
		frappe.set_user(user.name)
		push_api.register_subscription(subscription_id="subscription-id", platform="web", onesignal_id="onesignal-id", token="local-token", opted_in=1)
		push_api.register_subscription(subscription_id="subscription-id-2", platform="web", onesignal_id="onesignal-id", token="local-token-2", opted_in=1)
		settings = frappe.get_single("Pet App Notification Settings")
		settings.onesignal_rest_api_key = "rest-api-key"
		settings.save(ignore_permissions=True)
		frappe.set_user("Administrator")
		response = Mock(ok=True, status_code=200)
		response.json.return_value = {
			"identity": {"external_id": user.name, "onesignal_id": "onesignal-id"},
			"properties": {"country": "IQ", "ip": "127.0.0.1"},
			"subscriptions": [
				{
					"id": "subscription-id",
					"app_id": "onesignal-app-id",
					"type": "ChromePush",
					"enabled": True,
					"notification_types": 1,
					"token": "secret-token",
				},
				{
					"id": "subscription-id-2",
					"app_id": "onesignal-app-id",
					"type": "ChromePush",
					"enabled": True,
					"notification_types": 1,
					"token": "secret-token-2",
				},
				{
					"id": "subscription-id-disabled",
					"app_id": "onesignal-app-id",
					"type": "ChromePush",
					"enabled": False,
					"notification_types": -2,
					"token": None,
				}
			],
		}

		with patch("pet_app.notifications.channels.onesignal.requests.get", return_value=response) as mocked:
			status = push_api.get_onesignal_status(user=user.name)
			exact = push_api.get_onesignal_status(user=user.name, subscription_id="subscription-id-2")
			missing = push_api.get_onesignal_status(user=user.name, subscription_id="missing-subscription-id")

		self.assertTrue(status["ok"], status)
		self.assertEqual(status["data"]["user"], user.name)
		self.assertEqual(status["data"]["external_id"], user.name)
		self.assertEqual(status["data"]["onesignal_id"], "onesignal-id")
		self.assertEqual(status["data"]["subscription_id"], "subscription-id")
		self.assertTrue(status["data"]["enabled"])
		self.assertTrue(status["data"]["token_present"])
		self.assertEqual(status["data"]["sendable_subscription_count"], 2)
		self.assertEqual(set(status["data"]["sendable_subscription_ids"]), {"subscription-id", "subscription-id-2"})
		self.assertEqual(status["data"]["local_active_count"], 2)
		self.assertEqual(len(status["data"]["local_subscriptions"]), 2)
		self.assertNotIn("secret-token", frappe.as_json(status["data"]))
		self.assertNotIn("secret-token-2", frappe.as_json(status["data"]))
		self.assertEqual(mocked.call_args.kwargs["headers"]["Authorization"], "Key rest-api-key")

		self.assertTrue(exact["ok"], exact)
		self.assertEqual(exact["data"]["subscription_id"], "subscription-id-2")
		self.assertTrue(exact["data"]["subscription_found"])
		self.assertTrue(exact["data"]["enabled"])
		self.assertEqual(exact["data"]["sendable_subscription_count"], 1)
		self.assertEqual(exact["data"]["sendable_subscription_ids"], ["subscription-id-2"])
		self.assertEqual(len(exact["data"]["subscriptions"]), 1)
		self.assertEqual(len(exact["data"]["local_subscriptions"]), 1)

		self.assertTrue(missing["ok"], missing)
		self.assertEqual(missing["data"]["subscription_id"], "missing-subscription-id")
		self.assertFalse(missing["data"]["subscription_found"])
		self.assertFalse(missing["data"]["enabled"])
		self.assertFalse(missing["data"]["token_present"])
		self.assertEqual(missing["data"]["sendable_subscription_count"], 0)
		self.assertEqual(missing["data"]["subscriptions"], [])
		self.assertEqual(missing["data"]["local_subscriptions"], [])

	def test_onesignal_status_no_record_is_not_server_error(self):
		self._enable_onesignal(dry_run=0)
		user = self._make_user()
		frappe.set_user(user.name)
		response = Mock(ok=False, status_code=404)
		response.json.return_value = {"errors": ["not found"]}

		with patch("pet_app.notifications.channels.onesignal.requests.get", return_value=response):
			status = push_api.get_onesignal_status()

		self.assertTrue(status["ok"], status)
		self.assertEqual(status["data"]["user"], user.name)
		self.assertIsNone(status["data"]["onesignal_id"])
		self.assertFalse(status["data"]["enabled"])
		self.assertFalse(status["data"]["token_present"])

	def test_push_debug_api_blocks_cross_user_for_normal_user(self):
		self._enable_onesignal(dry_run=0)
		current = self._make_user()
		other = self._make_user()
		frappe.set_user(current.name)

		status = push_api.get_onesignal_status(user=other.name)

		self.assertFalse(status["ok"])
		self.assertEqual(status["meta"]["code"], "PUSH_USER_FORBIDDEN")

	def test_notification_settings_api_sanitizes_onesignal_rest_key(self):
		self._enable_onesignal(dry_run=0)
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Pet App Notification Settings", "onesignal_rest_api_key", "server-secret-key")

		read = notifications_api.get_notification_settings()
		blank_update = notifications_api.update_notification_settings(onesignal_rest_api_key="", push_frontend_base_url="https://clinic.example.com")
		updated = notifications_api.get_notification_settings()

		self.assertTrue(read["ok"], read)
		self.assertTrue(read["data"]["settings"]["onesignal_rest_api_key_configured"])
		self.assertNotIn("onesignal_rest_api_key", read["data"]["settings"])
		self.assertTrue(blank_update["ok"], blank_update)
		self.assertNotIn("server-secret-key", frappe.as_json(blank_update["data"]["settings"]))
		self.assertEqual(updated["data"]["settings"]["push_frontend_base_url"], "https://clinic.example.com")
		self.assertTrue(updated["data"]["settings"]["onesignal_rest_api_key_configured"])
		self.assertEqual(
			frappe.get_single("Pet App Notification Settings").get_password("onesignal_rest_api_key"),
			"server-secret-key",
		)

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
					"birth_date": "2024-01-01",
					"pet_status": "Approved",
					"weight": 12,
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

	def _make_user(self):
		suffix = frappe.generate_hash(length=8)
		return frappe.get_doc(
			{
				"doctype": "User",
				"email": f"push.{suffix}@example.com",
				"first_name": "Push",
				"enabled": 1,
				"send_welcome_email": 0,
			}
		).insert(ignore_permissions=True)

	def _enable_onesignal(self, dry_run=1, mirror=0):
		frappe.db.set_single_value("Pet App Notification Settings", "enabled", 1)
		frappe.db.set_single_value("Pet App Notification Settings", "dry_run", dry_run)
		frappe.db.set_single_value("Pet App Notification Settings", "onesignal_enabled", 1)
		frappe.db.set_single_value("Pet App Notification Settings", "onesignal_web_enabled", 1)
		frappe.db.set_single_value("Pet App Notification Settings", "onesignal_mobile_enabled", 1)
		frappe.db.set_single_value("Pet App Notification Settings", "onesignal_mirror_frappe_notifications", mirror)
		frappe.db.set_single_value("Pet App Notification Settings", "onesignal_app_id", "onesignal-app-id")
		frappe.db.set_single_value("Pet App Notification Settings", "onesignal_rest_api_key", "rest-api-key")

	def _onesignal_user_record(self, user, subscription_id="subscription-id", *, enabled=True, token="secret-token"):
		return {
			"identity": {"external_id": user, "onesignal_id": "manual-onesignal-id"},
			"properties": {"country": "IQ"},
			"subscriptions": [
				{
					"id": subscription_id,
					"app_id": "onesignal-app-id",
					"type": "ChromePush",
					"enabled": enabled,
					"notification_types": 1,
					"token": token,
				}
			],
		}

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
