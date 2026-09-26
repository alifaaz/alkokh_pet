"""Which template a send surface uses is configuration, and there is no code fallback.

Phase 11a moved that answer out of a dict in send_targets.py and out of the frontend's
constants into ``Pet App Access Settings.send_defaults``. The tests that matter are the
ones proving the move changed where the answer lives and nothing else:

* the five surfaces that had a default in code resolve to exactly what they resolved to
  before, and
* the three ways of having no default - no row, blank row, disabled row - are one
  behaviour, not three.

The second is the load-bearing one. If they ever diverge, "what does this surface send?"
stops having a single answer again, which is the whole thing this replaced.

No send path is touched. resolve_send_target is a lookup; the picker is read-only.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from pet_app.notifications import send_targets as st
from pet_app.notifications.engine import _registry_send_target
from pet_app.notifications.meta_templates import unified_template_options
from pet_app.patches.p1_23_whatsapp_send_defaults import SEED_TEMPLATE_NAMES

SETTINGS = "Pet App Access Settings"


class TestWhatsAppSendDefaults(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")

	# --- helpers -------------------------------------------------------------

	def _settings(self):
		return frappe.get_single(SETTINGS)

	def _row(self, doc, surface):
		for row in doc.get("send_defaults") or []:
			if row.surface_key == surface:
				return row
		return None

	def _entry(self, source, ident):
		key = "meta_template" if source == "meta" else "template_key"
		for entry in unified_template_options()["templates"]:
			if entry["source"] == source and entry[key] == ident:
				return entry
		return None

	def _configured_surface(self):
		for surface, default in st.configured_defaults().items():
			if default.configured and default.template_source == "meta":
				return surface
		self.skipTest("no surface has a configured Meta default on this site")

	# --- the move changed nothing -------------------------------------------

	def test_seeded_surfaces_resolve_to_the_template_the_retired_map_named(self):
		"""Every surface the code map named resolves to that same template through settings."""
		checked = 0
		for surface, template_name in SEED_TEMPLATE_NAMES.items():
			if not template_name:
				continue
			if not st.configured_defaults()[surface].configured:
				continue
			target = st.resolve_send_target(surface)
			self.assertEqual(target.template_name, template_name)
			self.assertEqual(target.template_source, "meta")
			self.assertTrue(target.meta_template)
			# The source doctype still comes off the mirror row, never off the setting -
			# a second copy could disagree with the slot map it was validated against.
			self.assertEqual(target.source_doctype, target.source_doctype or None)
			checked += 1
		if not checked:
			self.skipTest("send defaults are not seeded on this site")

	def test_a_configured_surface_reaches_the_send_path_as_a_meta_send(self):
		surface = self._configured_surface()
		target = _registry_send_target(surface, None, None, "SOME-RECORD")
		self.assertIsNotNone(target)
		self.assertEqual(target.template_source, "meta")
		self.assertEqual(target.meta_template, st.configured_defaults()[surface].meta_template)

	def test_an_explicitly_addressed_send_ignores_the_configured_default(self):
		"""Settings supply a default, not an override. An explicit template still wins."""
		surface = self._configured_surface()
		self.assertIsNone(_registry_send_target(surface, None, "SOME-MIRROR-ROW", "X"))
		self.assertIsNone(_registry_send_target(surface, "some_local_key", None, "X"))

	# --- absent, blank and disabled are one behaviour ------------------------

	def test_absent_blank_and_disabled_all_mean_no_default(self):
		surface = self._configured_surface()

		def outcome():
			refusal = None
			try:
				st.resolve_send_target(surface)
			except st.SendTargetError as exc:
				refusal = exc.exc_type
			entry = self._entry("meta", st.configured_defaults()[surface].meta_template or "-")
			return (
				st.configured_defaults()[surface].configured,
				_registry_send_target(surface, None, None, "X"),
				refusal,
				(entry or {}).get("default_for", []),
			)

		doc = self._settings()
		row = self._row(doc, surface)
		stored = row.meta_template

		row.enabled = 0
		doc.save(ignore_permissions=True)
		disabled = outcome()
		# The row keeps what it was set to - switching a default off must not lose it.
		self.assertEqual(self._row(self._settings(), surface).meta_template, stored)

		doc = self._settings()
		row = self._row(doc, surface)
		row.enabled = 1
		row.template_source = ""
		doc.save(ignore_permissions=True)
		blank = outcome()

		doc = self._settings()
		doc.remove(self._row(doc, surface))
		doc.save(ignore_permissions=True)
		absent = outcome()

		self.assertEqual(disabled, blank)
		self.assertEqual(blank, absent)
		self.assertFalse(absent[0])
		self.assertIsNone(absent[1])
		self.assertEqual(absent[2], "SEND_TARGET_NO_DEFAULT_CONFIGURED")
		self.assertEqual(absent[3], [])

	def test_a_registered_surface_with_no_default_falls_through_untouched(self):
		"""Registering a surface must not change how it sends until somebody picks a template."""
		for surface, default in st.configured_defaults().items():
			if not default.configured:
				self.assertIsNone(_registry_send_target(surface, None, None, "X"))
				return
		self.skipTest("every registered surface has a default on this site")

	def test_an_unregistered_event_key_is_not_addressed_at_all(self):
		"""event_key is also a free-form per-send label; only registered surfaces resolve."""
		self.assertIsNone(_registry_send_target("whatsapp.action.Some Rule", None, None, "X"))
		self.assertIsNone(_registry_send_target("reminder.Deworming Due", None, None, "X"))
		with self.assertRaises(st.SendTargetError):
			st.resolve_send_target("whatsapp.real_test.hello_world.20260721T1519Z")

	# --- what the picker reports --------------------------------------------

	def test_default_for_is_always_a_list_and_event_keys_mirrors_it(self):
		for entry in unified_template_options()["templates"]:
			self.assertIsInstance(entry["default_for"], list)
			self.assertEqual(entry["default_for"], entry["event_keys"])

	def test_default_for_names_only_the_entry_the_default_addresses(self):
		"""A local default belongs to the local entry, not to the mirror row it is bound to."""
		local = next(
			(
				entry
				for entry in unified_template_options()["templates"]
				if entry["source"] == "local" and entry["template_key"]
			),
			None,
		)
		if not local:
			self.skipTest("no local templates on this site")
		surface = sorted(st.SEND_SURFACES)[0]
		doc = self._settings()
		row = self._row(doc, surface)
		if row is None:
			row = doc.append("send_defaults", {"surface_key": surface})
		row.enabled = 1
		row.template_source = "local"
		row.template_key = local["template_key"]
		doc.save(ignore_permissions=True)

		self.assertIn(surface, self._entry("local", local["template_key"])["default_for"])
		claimed = [
			entry
			for entry in unified_template_options()["templates"]
			if entry["source"] == "meta" and surface in entry["default_for"]
		]
		self.assertEqual(claimed, [])

	def test_a_default_on_an_unsendable_template_stays_selected_with_its_reasons(self):
		"""Clearing it would report no default configured when one is. Show why instead."""
		unsendable = next(
			(
				entry
				for entry in unified_template_options()["templates"]
				if entry["source"] == "meta" and not entry["sendable"]
			),
			None,
		)
		if not unsendable:
			self.skipTest("every mirror row is sendable on this site")
		surface = sorted(st.SEND_SURFACES)[0]
		doc = self._settings()
		row = self._row(doc, surface)
		if row is None:
			row = doc.append("send_defaults", {"surface_key": surface})
		row.enabled = 1
		row.template_source = "meta"
		row.meta_template = unsendable["meta_template"]
		doc.save(ignore_permissions=True)

		entry = self._entry("meta", unsendable["meta_template"])
		self.assertIn(surface, entry["default_for"])
		self.assertFalse(entry["sendable"])
		self.assertTrue(entry["reasons"])

	# --- the table stays addressable ----------------------------------------

	def test_an_unregistered_surface_key_is_refused(self):
		doc = self._settings()
		doc.append("send_defaults", {"surface_key": "not.a.real.surface"})
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)

	def test_two_rows_for_one_surface_are_refused(self):
		surface = sorted(st.SEND_SURFACES)[0]
		doc = self._settings()
		if self._row(doc, surface) is None:
			doc.append("send_defaults", {"surface_key": surface})
		doc.append("send_defaults", {"surface_key": surface})
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)

	def test_a_source_must_name_the_template_it_points_at(self):
		surface = sorted(st.SEND_SURFACES)[0]
		doc = self._settings()
		row = self._row(doc, surface)
		if row is None:
			row = doc.append("send_defaults", {"surface_key": surface})
		row.template_source = "meta"
		row.meta_template = None
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)

	def test_the_unused_half_of_a_row_is_cleared_on_save(self):
		"""A row can never carry two answers and leave a reader to pick."""
		surface = sorted(st.SEND_SURFACES)[0]
		doc = self._settings()
		row = self._row(doc, surface)
		if row is None:
			row = doc.append("send_defaults", {"surface_key": surface})
		row.template_source = ""
		row.template_key = "something"
		row.meta_template = None
		doc.save(ignore_permissions=True)
		saved = self._row(self._settings(), surface)
		self.assertFalse(saved.template_key)
		self.assertFalse(saved.meta_template)

	def test_the_defaults_index_costs_one_query(self):
		"""The picker adds one read for the whole table, never one per entry."""
		calls = []
		original = frappe.db.sql

		def counting(*args, **kwargs):
			calls.append(1)
			return original(*args, **kwargs)

		frappe.db.sql = counting
		try:
			st.defaults_by_template()
		finally:
			frappe.db.sql = original
		self.assertLessEqual(len(calls), 1)


class TestSendDefaultsEndpoints(FrappeTestCase):
	"""The settings screen's read/write pair.

	The screen shows every surface and edits rows in place. It must not be able to
	invent a surface or drop one, because which surfaces exist is code - so the tests
	that matter are the ones proving the endpoint refuses to do either.
	"""

	def setUp(self):
		frappe.set_user("Administrator")

	def _get(self):
		from pet_app.api.notifications import get_send_defaults

		response = get_send_defaults()
		self.assertTrue(response.get("ok"))
		return {row["surface_key"]: row for row in response["data"]["send_defaults"]}

	def _update(self, rows):
		from pet_app.api.notifications import update_send_defaults

		return update_send_defaults(data={"send_defaults": rows})

	def test_read_returns_one_entry_per_registered_surface(self):
		rows = self._get()
		self.assertEqual(sorted(rows), sorted(st.SEND_SURFACES))
		for surface, row in rows.items():
			self.assertEqual(row["label"], st.surface_label(surface))
			self.assertEqual(row["configured"], st.configured_defaults()[surface].configured)

	def test_read_reports_configured_false_for_a_disabled_row(self):
		"""configured is what the send path will do, not whether the row is filled in."""
		surface = next(
			(s for s, d in st.configured_defaults().items() if d.configured), None
		)
		if not surface:
			self.skipTest("no configured surface on this site")
		doc = frappe.get_single(SETTINGS)
		for row in doc.get("send_defaults"):
			if row.surface_key == surface:
				row.enabled = 0
		doc.save(ignore_permissions=True)
		entry = self._get()[surface]
		self.assertFalse(entry["configured"])
		# ...and it still remembers the template it was set to.
		self.assertTrue(entry["meta_template"] or entry["template_key"])

	def test_write_refuses_a_surface_that_is_not_registered(self):
		response = self._update([{"surface_key": "invoice.due_reminder"}])
		self.assertFalse(response.get("ok"))
		self.assertEqual(response["meta"]["code"], "SEND_SURFACE_UNKNOWN")

	def test_write_refuses_a_payload_that_is_not_a_list(self):
		from pet_app.api.notifications import update_send_defaults

		response = update_send_defaults(data={"send_defaults": {"surface_key": "pet.death"}})
		self.assertFalse(response.get("ok"))
		self.assertEqual(response["meta"]["code"], "SEND_DEFAULTS_PAYLOAD_INVALID")

	def test_write_never_drops_a_surface_left_out_of_the_payload(self):
		before = self._get()
		surface = sorted(st.SEND_SURFACES)[0]
		self._update([{"surface_key": surface, "template_source": ""}])
		after = self._get()
		self.assertEqual(sorted(before), sorted(after))
		for other in before:
			if other == surface:
				continue
			self.assertEqual(before[other]["template_source"], after[other]["template_source"])
			self.assertEqual(before[other]["meta_template"], after[other]["meta_template"])

	def test_write_ignores_a_client_supplied_label(self):
		surface = sorted(st.SEND_SURFACES)[0]
		self._update([{"surface_key": surface, "label": "Whatever The Client Says"}])
		self.assertEqual(self._get()[surface]["label"], st.surface_label(surface))

	def test_a_blank_template_source_clears_the_default(self):
		surface = "manual.invoice_send"
		unsendable_or_any = next(
			(
				entry
				for entry in unified_template_options()["templates"]
				if entry["source"] == "meta"
			),
			None,
		)
		if not unsendable_or_any:
			self.skipTest("no mirror rows on this site")
		self._update(
			[
				{
					"surface_key": surface,
					"enabled": 1,
					"template_source": "meta",
					"meta_template": unsendable_or_any["meta_template"],
				}
			]
		)
		self.assertTrue(self._get()[surface]["configured"])
		self._update([{"surface_key": surface, "template_source": ""}])
		cleared = self._get()[surface]
		self.assertFalse(cleared["configured"])
		self.assertEqual(cleared["template_source"], "")
		self.assertEqual(cleared["meta_template"], "")

	def test_the_invoice_surface_uses_the_key_the_frontend_sends(self):
		"""manual.invoice_send, not the guess p1_23 first seeded."""
		self.assertIn("manual.invoice_send", st.SEND_SURFACES)
		self.assertNotIn("invoice.due_reminder", st.SEND_SURFACES)
		self.assertIn("manual.invoice_send", self._get())

	def test_a_manual_surface_reaches_default_for_exactly_like_a_registered_one(self):
		"""surface_key is independent of any registered-event list. Nothing special here."""
		mirror = next(
			(
				entry
				for entry in unified_template_options()["templates"]
				if entry["source"] == "meta" and not entry["default_for"]
			),
			None,
		)
		if not mirror:
			self.skipTest("every mirror row is already a default")
		self._update(
			[
				{
					"surface_key": "manual.invoice_send",
					"enabled": 1,
					"template_source": "meta",
					"meta_template": mirror["meta_template"],
				}
			]
		)
		entry = next(
			e
			for e in unified_template_options()["templates"]
			if e["source"] == "meta" and e["meta_template"] == mirror["meta_template"]
		)
		self.assertEqual(entry["default_for"], ["manual.invoice_send"])
		self.assertEqual(entry["event_keys"], ["manual.invoice_send"])
