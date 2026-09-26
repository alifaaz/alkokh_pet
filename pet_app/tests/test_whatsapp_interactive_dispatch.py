"""One queue row, two possible messages - and the sequence between them.

A WhatsApp message carries exactly one top-level Graph ``type``, so a queue row holding
both a template and an interactive payload can only ever become one of them. Before this
was fixed, the template won and the interactive was dropped without a word, while the
Action Request was stamped ``Interactive Sent`` - a record of a message that never left,
which also killed the retry that would have sent it later.

These tests pin the sequence: template first, ``Awaiting Session`` while the interactive
is still owed, interactive once the customer's reply has opened the window.

Nothing here reaches the network. ``dry_run`` is forced on in setUp and restored in
tearDown, and every send goes to a recorder rather than a channel.
"""

from __future__ import annotations

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from pet_app.notifications import actions, engine, inbox


# An APPROVED mirror row that declares no variables, so no slot map is needed for it to
# be a legal rule target. Its identity is irrelevant to what is being tested; only its
# presence on the queue row matters.
META_TEMPLATE_ID = "882733814412589"


class Recorder:
	"""Stands in for a channel. Records the call instead of making it."""

	def __init__(self):
		self.calls = []

	def __getattr__(self, item):
		if not item.startswith("send_"):
			raise AttributeError(item)

		def _call(**kwargs):
			kwargs.pop("queue", None)
			self.calls.append((item, kwargs))
			return {"provider_message_id": "recorded", "status": "sent"}

		return _call

	@property
	def sent(self):
		return [name for name, _ in self.calls]


class TestWhatsAppInteractiveDispatch(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self._dry_run = frappe.db.get_single_value("Pet App Notification Settings", "dry_run")
		frappe.db.set_single_value("Pet App Notification Settings", "dry_run", 1)
		self._session_is_open = inbox.session_is_open

	def tearDown(self):
		inbox.session_is_open = self._session_is_open
		engine.session_is_open = self._session_is_open
		frappe.db.set_single_value("Pet App Notification Settings", "dry_run", self._dry_run)

	def _window(self, is_open):
		inbox.session_is_open = lambda conversation: is_open
		engine.session_is_open = lambda conversation: is_open

	def _queue_row(self, **overrides):
		"""An unsaved queue row. _send_queue_message only reads fields."""
		values = {
			"doctype": "Pet App Notification Queue",
			"channel": "WhatsApp",
			"event_key": "test.interactive.dispatch",
			"to_phone": "9640000000000",
			"conversation": "TEST-CONV",
			"rendered_preview": "Rate this from 1 to 5.",
			"meta_template": META_TEMPLATE_ID,
			"interactive_json": json.dumps(
				{
					"type": "Buttons",
					"options": [
						{"id": "wa:REQ:yes", "label": "Yes", "description": ""},
						{"id": "wa:REQ:no", "label": "No", "description": ""},
					],
				}
			),
		}
		values.update(overrides)
		return frappe.get_doc(values)

	# ---------------------------------------------------------------- claim 3

	def test_meta_opener_with_pending_interactive_lands_on_awaiting_session(self):
		"""The template goes out; the stage says the interactive is still owed.

		This is the whole defect in one assertion. The old code returned
		"Interactive Sent" here, claiming a message that was never sent and leaving
		process_inbound_action with no reason to send it later.
		"""
		self._window(False)
		recorder = Recorder()
		doc = self._queue_row()

		response, kind, stage = engine._send_queue_message(doc, None, {}, recorder)

		self.assertEqual(recorder.sent, ["send_meta_template"])
		self.assertEqual(kind, "Text")
		self.assertEqual(stage, "Awaiting Session")
		self.assertIsNotNone(response)

	def test_meta_row_sends_the_interactive_once_the_window_is_open(self):
		"""Same row, open window: the interactive wins and the template is not resent."""
		self._window(True)
		recorder = Recorder()
		doc = self._queue_row()

		_response, kind, stage = engine._send_queue_message(doc, None, {}, recorder)

		self.assertEqual(recorder.sent, ["send_interactive"])
		self.assertNotIn("send_meta_template", recorder.sent)
		self.assertEqual(kind, "Interactive")
		self.assertEqual(stage, "Interactive Sent")

	def test_meta_row_without_an_interactive_is_unchanged(self):
		"""A plain mirror-direct send still reports Interactive Sent, as it always did."""
		self._window(False)
		recorder = Recorder()
		doc = self._queue_row(interactive_json=None)

		_response, kind, stage = engine._send_queue_message(doc, None, {}, recorder)

		self.assertEqual(recorder.sent, ["send_meta_template"])
		self.assertEqual((kind, stage), ("Text", "Interactive Sent"))

	def test_local_meta_template_delivery_mode_gets_the_same_correction(self):
		"""The path Appointment Guardian Response takes. It is enabled today."""
		self._window(False)
		recorder = Recorder()
		template = frappe.get_doc(
			{"doctype": "Pet App WhatsApp Template", "delivery_mode": "Meta Template"}
		)
		doc = self._queue_row(meta_template=None, delivery_mode="Meta Template")

		_response, kind, stage = engine._send_queue_message(doc, template, {}, recorder)

		self.assertEqual(recorder.sent, ["send_template"])
		self.assertEqual(stage, "Awaiting Session")

	def test_awaiting_session_is_the_value_the_retry_reads(self):
		"""actions.py gates the retry on this exact string, unmodified by this fix.

		Asserting the contract rather than mocking a webhook: the stage the send path
		now writes has to be the one the reply path looks for, or the two halves of the
		sequence never meet.
		"""
		import inspect

		source = inspect.getsource(actions.process_inbound_action)
		self.assertIn('request.delivery_stage == "Awaiting Session"', source)
		self.assertIn("send_interactive_for_request(request)", source)

		self._window(False)
		_response, _kind, stage = engine._send_queue_message(
			self._queue_row(), None, {}, Recorder()
		)
		self.assertEqual(stage, "Awaiting Session")

	# ---------------------------------------------------------------- claim 4

	def test_guard_refuses_a_template_send_carrying_buttons(self):
		"""The impossible combination is loud, not silent.

		No Action Request means no second queue row, so nothing would ever send the
		interactive - it would be accepted here and dropped at dispatch.
		"""
		result = engine.queue_notification(
			event_key="test.guard.template_plus_interactive",
			recipient_type="Guardian",
			to_phone="9640000000000",
			template_source="meta",
			meta_template=META_TEMPLATE_ID,
			interactive={"type": "Buttons", "options": [{"id": "a", "label": "A"}]},
			channel="WhatsApp",
		)

		self.assertFalse(result.get("ok"))
		# api_error carries the code on the envelope's meta, not at the top level.
		self.assertEqual((result.get("meta") or {}).get("code"), "TEMPLATE_INTERACTIVE_CONFLICT")
		self.assertIn("two separate", result["errors"][0]["message"])

	def test_guard_allows_the_same_combination_when_a_request_drives_it(self):
		"""With an Action Request the sequence exists, so the pairing is legal.

		The guard must not break the opener - create_action_request deliberately
		attaches the interactive so an already-open window is served by one message.
		"""
		import inspect

		source = inspect.getsource(engine.queue_notification)
		self.assertIn("TEMPLATE_INTERACTIVE_CONFLICT", source)
		self.assertIn("and not action_request", source)
