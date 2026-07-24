from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from pet_app.notifications.renderer import render_message


class _Template:
	"""Minimal stand-in for a WhatsApp template doc."""

	def __init__(self, body):
		self._body = body

	def get(self, key):
		return {"body_preview": self._body}.get(key)


class TestNotificationRenderer(FrappeTestCase):
	def test_missing_namespace_does_not_raise(self):
		# `pet` and `guardian` are entirely absent from the context; the shared
		# Frappe (DebugUndefined) env would raise UndefinedError here.
		body = "hi dear {{ guardian.full_name }}, your pet {{ pet.pet_name }} passed away"
		self.assertEqual(
			render_message(body, {}),
			"hi dear , your pet  passed away",
		)

	def test_populated_names_render(self):
		body = "hi dear {{ guardian.full_name }}, your pet {{ pet.pet_name }} passed away"
		out = render_message(
			body,
			{"guardian": {"full_name": "Sara"}, "pet": {"pet_name": "Rex"}},
		)
		self.assertEqual(out, "hi dear Sara, your pet Rex passed away")

	def test_display_name_alias_renders(self):
		body = "hi {{ guardian.display_name }} about {{ pet.display_name }}"
		out = render_message(
			body,
			{"guardian": {"display_name": "Sara"}, "pet": {"display_name": "Rex"}},
		)
		self.assertEqual(out, "hi Sara about Rex")

	def test_string_namespace_item_access_does_not_raise(self):
		# Reproduces the `str object['name']` crash: guardian is a bare string,
		# not a dict, and the template does item access on it.
		body = "hi dear {{ guardian['name'] }}"
		self.assertEqual(render_message("hi dear {{ guardian }}", {"guardian": "GUARDIAN-0001"}), "hi dear GUARDIAN-0001")
		# Undefined item access blanks out rather than crashing.
		self.assertEqual(render_message(body, {}), "hi dear ")

	def test_illegal_template_is_blocked(self):
		with self.assertRaises(frappe.exceptions.ValidationError):
			render_message("{{ foo.__class__ }}", {})

	def test_broken_template_falls_back_to_stripped_body(self):
		# A genuinely broken expression should not crash the send; unresolved
		# tokens are dropped and the literal text is preserved.
		out = render_message("hello {{ 1 / 0 }} world", {})
		self.assertEqual(out, "hello  world")
