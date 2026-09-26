"""Lab and Imaging as template sources: the test name must never arrive as a code.

``care_service`` holds a docname - CareService template is autonamed
``CareService-.#####`` - so the readable name is one hop away, on the linked row. That is
the same problem Service Room posed for boarding, and this is deliberately the same
solution: an allowlisted pair fetched in _safe_values, blanks when the link does not
resolve, and never the raw code.

The tests that matter are the negative ones. A resolver that returns the right name for a
good record but leaks "CareService-00006" for a deleted one has failed at exactly the
moment it mattered, and that failure reaches a customer.

No send path is touched. build_document_context reads; nothing here queues or sends.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from pet_app.notifications.context import (
	SOURCE_NAMESPACES,
	VARIABLE_FIELDS,
	_care_service_values,
	build_document_context,
	list_template_variables,
)

NAME_KEYS = ("test_name", "test_arabic_name")


class TestLabImagingVariables(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")

	def _record_with_service(self, doctype):
		name = frappe.db.get_value(doctype, {"care_service": ["!=", ""]}, "name")
		if not name:
			self.skipTest(f"no {doctype} row with a care_service on this site")
		return name

	# --- registration ---------------------------------------------------------

	def test_both_doctypes_are_registered_as_template_sources(self):
		self.assertEqual(SOURCE_NAMESPACES.get("Lab"), "lab")
		self.assertEqual(SOURCE_NAMESPACES.get("Imaging"), "imaging")
		self.assertIn("lab", VARIABLE_FIELDS)
		self.assertIn("imaging", VARIABLE_FIELDS)

	def test_a_mirror_row_may_now_declare_them(self):
		from pet_app.notifications.meta_templates import validate_source_doctype

		validate_source_doctype("Lab")
		validate_source_doctype("Imaging")

	def test_imaging_exposes_body_part_and_never_modality(self):
		"""modality is null on every row; offering it would be offering a blank."""
		self.assertIn("body_part", VARIABLE_FIELDS["imaging"])
		self.assertNotIn("modality", VARIABLE_FIELDS["imaging"])
		self.assertNotIn("body_part", VARIABLE_FIELDS["lab"])

	def test_the_variable_picker_offers_the_pair_scoped_to_each_source(self):
		for doctype, namespace in (("Lab", "lab"), ("Imaging", "imaging")):
			keys = {v["key"] for v in list_template_variables(doctype)}
			for field in NAME_KEYS:
				self.assertIn(f"{namespace}.{field}", keys)
			# Scoping still holds: a Lab template is not offered imaging variables.
			other = "imaging" if namespace == "lab" else "lab"
			self.assertFalse({k for k in keys if k.startswith(f"{other}.")})

	# --- resolution -----------------------------------------------------------

	def test_a_real_record_resolves_the_linked_name_not_the_link(self):
		for doctype, namespace in (("Lab", "lab"), ("Imaging", "imaging")):
			name = self._record_with_service(doctype)
			values = build_document_context(doctype, name)[namespace]
			code = frappe.db.get_value(doctype, name, "care_service")
			expected = frappe.db.get_value(
				"CareService template", code, ["service_name", "arabic_name"], as_dict=True
			)
			self.assertEqual(values["test_name"], expected.service_name)
			self.assertEqual(values["test_arabic_name"], expected.arabic_name)
			self.assertNotEqual(values["test_name"], code)

	def test_an_unresolvable_link_yields_blanks_and_never_the_code(self):
		"""The failure that would reach a customer. Blank is the only acceptable answer."""
		for missing in (None, "", "CareService-DOES-NOT-EXIST"):
			values = _care_service_values(missing)
			self.assertEqual(set(values), set(NAME_KEYS))
			for key in NAME_KEYS:
				self.assertEqual(values[key], "")
				self.assertNotIn("CareService-", values[key])

	def test_every_resolved_value_is_a_string(self):
		"""cstr runs after format_variable_value, so an unset column is "" and not None.

		resolve_slots calls .strip() on the value, so a None here would raise instead of
		using the slot's fallback.
		"""
		for source in (None, "CareService-DOES-NOT-EXIST", self._any_service()):
			for value in _care_service_values(source).values():
				self.assertIsInstance(value, str)

	def _any_service(self):
		name = frappe.db.get_value("CareService template", {}, "name")
		if not name:
			self.skipTest("no CareService template rows on this site")
		return name

	def test_a_blank_arabic_name_is_left_blank_not_filled_with_english(self):
		"""An Arabic message printing "CBC" mid-sentence is the failure this avoids.

		The slot's fallback decides instead - that is the operator's call, made per
		template, not one made here for every template at once.
		"""
		service = self._any_service()
		frappe.db.set_value("CareService template", service, "arabic_name", "")
		values = _care_service_values(service)
		self.assertEqual(values["test_arabic_name"], "")
		self.assertNotEqual(values["test_name"], values["test_arabic_name"])

	def test_the_pet_and_guardian_namespaces_still_resolve_from_a_lab(self):
		"""{{1}} is the pet name, so the lab record has to carry the pet through."""
		name = self._record_with_service("Lab")
		context = build_document_context("Lab", name)
		self.assertIn("lab", context)
		self.assertIn("clinic", context)
		if frappe.db.get_value("Lab", name, "pet"):
			self.assertTrue(context.get("pet", {}).get("display_name"))
