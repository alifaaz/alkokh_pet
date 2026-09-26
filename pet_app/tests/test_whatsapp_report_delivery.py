"""A template that promises a report must be able to keep the promise, and then keep it.

On 2026-09-03 `lab_result_ready` went to a guardian against LAB-00538. Its body ends
"press the button below and we will send you the full report". The guardian pressed it 19
seconds later. Nothing happened - no reviewed PDF was attached to the lab, and there is no
renderer here that could have made one.

Two halves, and both are needed. Refusing the send (Phase 3) stops a promise nobody can
keep from being made. Answering the tap (Phase 4) keeps the ones that are. Either alone
leaves the guardian waiting.

Nothing in these tests names a template in production code, and that is the property being
tested: `test_a_second_template_needs_no_code` uses a mirror row invented here, promising a
report on a doctype the delivery code has never heard of, and it works. The clinic will add
a radiology template and more, and none of them may require a diff.

Nothing reaches the network. dry_run is forced on in setUp and every send is recorded.
"""

from __future__ import annotations

import json
from io import BytesIO

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from pet_app.notifications import engine, inbox, reports


def _pdf(tag: str) -> bytes:
	from pypdf import PdfWriter

	writer = PdfWriter()
	writer.add_blank_page(width=200, height=200)
	writer.add_metadata({"/Title": tag})
	buffer = BytesIO()
	writer.write(buffer)
	return buffer.getvalue()


class TestWhatsAppReportDelivery(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self._dry_run = frappe.db.get_single_value("Pet App Notification Settings", "dry_run")
		frappe.db.set_single_value("Pet App Notification Settings", "dry_run", 1)

		# The record whose report is being promised. A ToDo, because the delivery code
		# addresses records as (doctype, name) and must not need to recognise one.
		self.record = frappe.get_doc(
			{"doctype": "ToDo", "description": f"report delivery {frappe.generate_hash(length=8)}"}
		).insert(ignore_permissions=True)

		self.promising = self._mirror_row(promises=True)
		self.silent = self._mirror_row(promises=False)
		self.conversation = self._conversation()

	def tearDown(self):
		frappe.db.set_single_value("Pet App Notification Settings", "dry_run", self._dry_run)

	# ------------------------------------------------------------------- fixtures

	def _mirror_row(self, *, promises: bool):
		return frappe.get_doc(
			{
				"doctype": "Pet App WhatsApp Meta Template",
				"meta_template_id": frappe.generate_hash(length=12),
				"template_name": f"test_template_{frappe.generate_hash(length=6)}",
				"language": "ar",
				"status": "APPROVED",
				"category": "UTILITY",
				"components_json": json.dumps([{"type": "BODY", "text": "your report is ready"}]),
				"delivers_reviewed_report": 1 if promises else 0,
			}
		).insert(ignore_permissions=True)

	def _conversation(self):
		account = frappe.db.get_value("Pet App WhatsApp Account", {"enabled": 1}, "name")
		return frappe.get_doc(
			{
				"doctype": "Pet App WhatsApp Conversation",
				"conversation_key": frappe.generate_hash(length=20),
				"provider_account": account,
				"normalized_phone": f"96470{frappe.utils.random_string(8).lower()[:8]}",
				"display_name": "Report delivery test",
				"status": "Open",
				"last_inbound_at": now_datetime(),
				"session_expires_at": add_to_date(now_datetime(), hours=24),
			}
		).insert(ignore_permissions=True)

	def _attach_report(self, tag="reviewed"):
		return reports.save_reviewed_report(
			self.record.doctype, self.record.name, content=_pdf(tag), file_name="report.pdf"
		)

	def _promised_exchange(self, mirror=None, *, source=True, with_queue=True):
		"""An outbound template send and the tap that answered it, as the webhook stores them."""
		mirror = mirror or self.promising
		queue = None
		if with_queue:
			queue = frappe.get_doc(
				{
					"doctype": "Pet App Notification Queue",
					"channel": "WhatsApp",
					"event_key": "test.report",
					"status": "Sent",
					"to_phone": self.conversation.normalized_phone,
					"conversation": self.conversation.name,
					"meta_template": mirror.name,
					"template_source": "meta",
					"source_doctype": self.record.doctype if source else None,
					"source_name": self.record.name if source else None,
				}
			).insert(ignore_permissions=True)
		outbound = frappe.get_doc(
			{
				"doctype": "Pet App WhatsApp Message",
				"conversation": self.conversation.name,
				"direction": "Outbound",
				"message_type": "Text",
				"body": "your report is ready",
				"provider_message_id": f"wamid.OUT-{frappe.generate_hash(length=10)}",
				"status": "Sent",
				"message_at": now_datetime(),
				"notification_queue": queue.name if queue else None,
				"source_doctype": self.record.doctype if source else None,
				"source_name": self.record.name if source else None,
			}
		).insert(ignore_permissions=True)
		inbound = frappe.get_doc(
			{
				"doctype": "Pet App WhatsApp Message",
				"conversation": self.conversation.name,
				"direction": "Inbound",
				"message_type": "Interactive",
				"body": "أرسل لي النتيجة",
				"provider_message_id": f"wamid.IN-{frappe.generate_hash(length=10)}",
				"replied_to_message": outbound.name,
				"status": "Received",
				"message_at": now_datetime(),
			}
		).insert(ignore_permissions=True)
		return outbound, inbound

	# ============================================ Phase 3: refusing an unkeepable send

	def test_a_template_that_promises_nothing_is_unaffected(self):
		"""Every mirror row on the live site is in this state. Nothing may change for them."""
		refusal = engine._reviewed_report_refusal(self.silent, None, None)

		self.assertIsNone(refusal)

	def test_a_promised_send_with_no_report_is_refused(self):
		refusal = engine._reviewed_report_refusal(self.promising, self.record.doctype, self.record.name)

		self.assertIsNotNone(refusal)
		self.assertFalse(refusal["ok"])
		self.assertEqual(refusal["meta"]["code"], "REVIEWED_REPORT_MISSING")

	def test_the_refusal_names_the_record_and_says_what_to_do(self):
		"""A reason nobody can act on is the silent failure again, with extra words."""
		refusal = engine._reviewed_report_refusal(self.promising, self.record.doctype, self.record.name)
		message = refusal["errors"][0]["message"]

		self.assertIn(self.record.name, message)
		self.assertIn(self.promising.template_name, message)
		self.assertIn("Attach the reviewed PDF", message)

	def test_a_promised_send_naming_no_record_is_refused_differently(self):
		"""Nowhere a report could ever be, versus waiting on someone to attach one."""
		refusal = engine._reviewed_report_refusal(self.promising, None, None)

		self.assertEqual(refusal["meta"]["code"], "REPORT_SOURCE_MISSING")

	def test_attaching_the_report_lets_the_same_send_through(self):
		self.assertIsNotNone(
			engine._reviewed_report_refusal(self.promising, self.record.doctype, self.record.name)
		)

		self._attach_report()

		self.assertIsNone(
			engine._reviewed_report_refusal(self.promising, self.record.doctype, self.record.name)
		)

	def test_another_record_s_report_does_not_satisfy_this_send(self):
		other = frappe.get_doc({"doctype": "ToDo", "description": "other"}).insert(ignore_permissions=True)
		reports.save_reviewed_report("ToDo", other.name, content=_pdf("theirs"), file_name="r.pdf")

		refusal = engine._reviewed_report_refusal(self.promising, self.record.doctype, self.record.name)

		self.assertEqual(refusal["meta"]["code"], "REVIEWED_REPORT_MISSING")

	# ================================================== Phase 4: answering the tap

	def test_a_tap_on_a_promised_template_resolves_to_the_report(self):
		report = self._attach_report()
		_outbound, inbound = self._promised_exchange()

		target = reports.resolve_reply_report(inbound)

		self.assertIsNotNone(target)
		self.assertEqual(target["file"]["name"], report.name)
		self.assertEqual(target["source_doctype"], self.record.doctype)
		self.assertEqual(target["source_name"], self.record.name)

	def test_the_newest_report_is_the_one_sent(self):
		self._attach_report("first")
		corrected = self._attach_report("corrected")
		_outbound, inbound = self._promised_exchange()

		self.assertEqual(reports.resolve_reply_report(inbound)["file"]["name"], corrected.name)

	def test_a_tap_on_a_template_that_promised_nothing_sends_nothing(self):
		"""The rating templates. They carry quick-reply buttons too, and mean something else."""
		self._attach_report()
		_outbound, inbound = self._promised_exchange(mirror=self.silent)

		self.assertIsNone(reports.resolve_reply_report(inbound))

	def test_a_reply_with_no_context_sends_nothing(self):
		self._attach_report()
		_outbound, inbound = self._promised_exchange()
		inbound.replied_to_message = None

		self.assertIsNone(reports.resolve_reply_report(inbound))

	def test_a_reply_quoting_another_inbound_message_sends_nothing(self):
		"""A guardian can quote themselves. That is not a promise we made."""
		self._attach_report()
		_outbound, inbound = self._promised_exchange()
		other_inbound = frappe.get_doc(
			{
				"doctype": "Pet App WhatsApp Message",
				"conversation": self.conversation.name,
				"direction": "Inbound",
				"message_type": "Text",
				"body": "hello",
				"status": "Received",
				"message_at": now_datetime(),
			}
		).insert(ignore_permissions=True)
		inbound.replied_to_message = other_inbound.name

		self.assertIsNone(reports.resolve_reply_report(inbound))

	def test_a_reply_to_a_message_that_came_from_no_queue_sends_nothing(self):
		"""An inbox reply typed by staff carries no template and promises nothing."""
		self._attach_report()
		_outbound, inbound = self._promised_exchange(with_queue=False)

		self.assertIsNone(reports.resolve_reply_report(inbound))

	def test_a_promised_tap_with_the_report_since_removed_sends_nothing(self):
		"""Phase 3 makes this rare, not impossible. It must not raise, and must not guess."""
		_outbound, inbound = self._promised_exchange()

		self.assertIsNone(reports.resolve_reply_report(inbound))

	def test_a_send_naming_no_record_cannot_be_answered(self):
		self._attach_report()
		_outbound, inbound = self._promised_exchange(source=False)

		self.assertIsNone(reports.resolve_reply_report(inbound))

	# -------------------------------------------------------------- the send itself

	def test_the_job_sends_the_stored_file_as_a_document(self):
		report = self._attach_report()

		sent = reports.send_reply_report(self.conversation.name, report.name)

		self.assertEqual(sent.file, report.name)
		self.assertEqual(sent.message_type, "Document")
		self.assertEqual(sent.direction, "Outbound")

	def test_the_job_works_from_the_guest_session_the_webhook_leaves_behind(self):
		"""The whole tap dies silently without this.

		The webhook is allow_guest, frappe.enqueue carries the calling session's user with
		no way to override it, and send_conversation_message read-checks the file - which
		is private. Guest fails that check.
		"""
		report = self._attach_report()
		with self.assertRaises(frappe.PermissionError):
			frappe.set_user("Guest")
			frappe.get_doc("File", report.name).check_permission("read")

		frappe.set_user("Guest")
		sent = reports.send_reply_report(self.conversation.name, report.name)

		self.assertEqual(sent.file, report.name)
		self.assertEqual(frappe.session.user, "Administrator")

	# ------------------------------------------------------------ repeat taps

	def test_the_same_file_twice_in_one_minute_is_sent_once(self):
		report = self._attach_report()

		first = reports.send_reply_report(self.conversation.name, report.name)
		second = reports.send_reply_report(self.conversation.name, report.name)

		self.assertIsNotNone(first)
		self.assertIsNone(second)
		self.assertEqual(
			frappe.db.count(
				"Pet App WhatsApp Message",
				{"conversation": self.conversation.name, "file": report.name, "direction": "Outbound"},
			),
			1,
		)

	def test_a_tap_after_the_window_re_sends(self):
		"""Agreed behaviour: they pressed a button that promised a report, so they get one."""
		report = self._attach_report()
		reports.send_reply_report(self.conversation.name, report.name)
		frappe.db.set_value(
			"Pet App WhatsApp Message",
			{"conversation": self.conversation.name, "file": report.name},
			"message_at",
			add_to_date(now_datetime(), seconds=-(reports.DOUBLE_FIRE_SECONDS + 30)),
		)

		self.assertIsNotNone(reports.send_reply_report(self.conversation.name, report.name))

	def test_a_corrected_report_is_not_suppressed_by_the_one_before_it(self):
		"""The guard is scoped to the file, not the record, exactly so this works."""
		first = self._attach_report("first")
		reports.send_reply_report(self.conversation.name, first.name)
		corrected = self._attach_report("corrected")

		self.assertIsNotNone(reports.send_reply_report(self.conversation.name, corrected.name))

	# ---------------------------------------------------------------- generality

	def test_a_second_template_needs_no_code(self):
		"""The requirement, as an assertion.

		A mirror row that did not exist when this code was written, promising a report on
		a record type it has never been told about. Configuration only - one ticked box.
		"""
		radiology = self._mirror_row(promises=True)
		record = frappe.get_doc({"doctype": "ToDo", "description": "imaging stand-in"}).insert(
			ignore_permissions=True
		)
		report = reports.save_reviewed_report(
			"ToDo", record.name, content=_pdf("radiology"), file_name="radiology.pdf"
		)
		self.record = record
		_outbound, inbound = self._promised_exchange(mirror=radiology)

		self.assertIsNone(engine._reviewed_report_refusal(radiology, "ToDo", record.name))
		self.assertEqual(reports.resolve_reply_report(inbound)["file"]["name"], report.name)
