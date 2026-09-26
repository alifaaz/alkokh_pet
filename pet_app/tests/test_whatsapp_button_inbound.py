"""A template quick-reply tap arrives as its own inbound type, and must be recorded.

Meta sends a tap on a template's quick-reply button as ``type: "button"``, not as an
``interactive`` message. Nothing parsed it, so it landed as ``Unsupported`` with a blank
body: a row that recorded something had arrived and nothing about what.

The tap already opened the 24-hour session window before this change and already
triggered the pending-interactive retry - ``record_inbound_message`` is type-agnostic.
So this is about the record, not the function, and the tests that matter most are the
ones proving the function did not regress while the record improved.

No send path is touched here. The parser is pure and the inbound path only writes.
"""

from __future__ import annotations

import json

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import get_datetime, now_datetime

from pet_app.notifications import inbox, webhook


def _tap(phone="9647700000001", text="قيّم الخدمة", **overrides):
	message = {
		"id": f"wamid.TEST-{frappe.generate_hash(length=8)}",
		"from": phone,
		"timestamp": str(int(now_datetime().timestamp())),
		"type": "button",
		"button": {"payload": text, "text": text},
	}
	message.update(overrides)
	return message


class TestWhatsAppButtonInbound(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")

	# ------------------------------------------------------------- the parse

	def test_documented_tap_populates_body_and_title(self):
		event = webhook._message_event(_tap(), "PHONE_ID")

		self.assertEqual(event["message_type"], "Interactive")
		self.assertNotEqual(event["message_type"], "Unsupported")
		self.assertEqual(event["message"], "قيّم الخدمة")
		self.assertEqual(event["interactive_title"], "قيّم الخدمة")
		self.assertIsNone(event["interactive_id"])

	def test_interactive_id_is_never_written_for_a_tap(self):
		"""It means "a payload this app minted and can parse", and this is not one.

		match_response returns no-match without falling back to the text path when
		interactive_id is set but does not parse as wa:<request>:<key>, and comment
		capture is gated on its absence. A constant we did not mint belongs in neither.
		"""
		for text in ("قيّم الخدمة", "RATE_SERVICE", "wa:SOMETHING:1"):
			event = webhook._message_event(_tap(text=text), "PHONE_ID")
			self.assertIsNone(event["interactive_id"], f"interactive_id set for {text!r}")

	def test_payload_is_used_when_text_is_absent(self):
		message = _tap()
		message["button"] = {"payload": "RATE_SERVICE"}
		event = webhook._message_event(message, "PHONE_ID")

		self.assertEqual(event["message"], "RATE_SERVICE")
		self.assertEqual(event["interactive_title"], "RATE_SERVICE")

	def test_malformed_taps_record_cleanly_instead_of_raising(self):
		"""The shape is unverified - no tap has ever been observed on this site.

		Anything unexpected must still yield a row, because the session window opens
		downstream of here and losing that costs the whole rating flow.
		"""
		for label, button in (
			("empty dict", {}),
			("missing key", None),
			("explicit null", None),
			("a bare string", "unexpected"),
			("whitespace text", {"text": "   "}),
			("wrong inner types", {"text": {"nested": 1}, "payload": None}),
		):
			message = _tap()
			if label == "missing key":
				message.pop("button")
			else:
				message["button"] = button
			with self.subTest(shape=label):
				event = webhook._message_event(message, "PHONE_ID")
				self.assertEqual(event["message_type"], "Interactive")
				self.assertIsNone(event["interactive_id"])
				self.assertIsInstance(event["message"], str)

	def test_raw_message_is_the_payload_verbatim(self):
		message = _tap()
		event = webhook._message_event(message, "PHONE_ID")
		self.assertIs(event["raw_message"], message)

	# -------------------------------------------------- the load-bearing part

	def test_a_tap_opens_the_session_window(self):
		"""The behaviour this whole design rests on. It must not regress.

		record_inbound_message sets last_inbound_at and session_expires_at as
		straight-line assignments with no type guard, which is why a tap opened the
		window even while it was landing as Unsupported.
		"""
		event = webhook._message_event(_tap(), "PHONE_ID")
		before = now_datetime()

		message, conversation = inbox.record_inbound_message(event)

		self.assertIsNotNone(conversation.last_inbound_at)
		self.assertIsNotNone(conversation.session_expires_at)
		self.assertGreater(get_datetime(conversation.session_expires_at), before)
		self.assertTrue(inbox.session_is_open(conversation))

	def test_the_stored_row_carries_the_text_and_no_interactive_id(self):
		event = webhook._message_event(_tap(), "PHONE_ID")

		message, _conversation = inbox.record_inbound_message(event)

		self.assertEqual(message.message_type, "Interactive")
		self.assertEqual(message.body, "قيّم الخدمة")
		self.assertEqual(message.interactive_title, "قيّم الخدمة")
		self.assertFalse(message.interactive_id)
		# raw_json stays the record of truth: the real inbound type is preserved even
		# though message_type had to borrow an existing Select option.
		self.assertEqual(json.loads(message.raw_json)["type"], "button")

	def test_a_tap_is_distinguishable_from_a_list_pick(self):
		"""Interactive is shared, so the distinction has to live somewhere else."""
		tap = webhook._message_event(_tap(), "PHONE_ID")
		pick = webhook._message_event(
			{
				"id": "wamid.PICK",
				"from": "9647700000001",
				"type": "interactive",
				"interactive": {"type": "list_reply", "list_reply": {"id": "wa:REQ:4", "title": "4 / 5"}},
			},
			"PHONE_ID",
		)

		self.assertEqual(tap["message_type"], pick["message_type"])
		self.assertIsNone(tap["interactive_id"])
		self.assertEqual(pick["interactive_id"], "wa:REQ:4")
		self.assertEqual(tap["raw_message"]["type"], "button")
		self.assertEqual(pick["raw_message"]["type"], "interactive")

	def test_button_text_does_not_trip_the_opt_out_check(self):
		"""webhook.py applies the opt-out test to the body it just populated.

		Before this change a tap had an empty body and could not match; now it carries
		the button label, so the label has to be checked against the opt-out words.
		"""
		event = webhook._message_event(_tap(), "PHONE_ID")
		body = (event.get("message") or "").strip().upper()

		self.assertNotIn(body, webhook.OPT_OUT_WORDS)
		for word in webhook.OPT_OUT_WORDS:
			self.assertNotEqual(body, word)
