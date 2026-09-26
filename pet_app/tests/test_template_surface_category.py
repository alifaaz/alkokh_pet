"""The picker can be narrowed to one screen, and narrowing never hides unsorted work.

``surface_category`` is a second, independent axis on the unified catalogue: Meta's
``category`` says MARKETING or UTILITY, this says which screen offers the template. The
tests that matter are the ones proving the axis is optional and forgiving:

* no parameter returns exactly what it returned before - the frontend passes none today,
  and every send surface reads this one endpoint;
* a filtered call returns the matches PLUS everything still uncategorised, because the
  field starts empty on all 36 templates and a screen that shows nothing on the day the
  feature ships is worse than no feature;
* a bound pair is one template, so a category set on either row describes both.

Nothing here touches ``category``, and no send path is involved: this listing is
read-only.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from pet_app.notifications.meta_templates import (
	LOCAL_TEMPLATE_DOCTYPE,
	MIRROR_DOCTYPE,
	unified_template_options,
)
from pet_app.patches.p1_28_template_surface_category import CATEGORIES, CATEGORY_DOCTYPE

SEEDED = [row[0] for row in CATEGORIES]


class TestTemplateSurfaceCategory(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.local = frappe.get_all(LOCAL_TEMPLATE_DOCTYPE, pluck="name", order_by="name asc")
		self.mirror = frappe.get_all(MIRROR_DOCTYPE, pluck="name", order_by="name asc")
		self.assertTrue(self.local and self.mirror, "needs at least one template of each kind")

	# --- helpers -------------------------------------------------------------

	def _ids(self, payload):
		"""The identity a picker selects by: the mirror docname, or the local key."""
		return {entry["meta_template"] if entry["source"] == "meta" else entry["template_key"] for entry in payload["templates"]}

	def _entry(self, payload, *, meta=None, local=None):
		for entry in payload["templates"]:
			if meta and entry["source"] == "meta" and entry["meta_template"] == meta:
				return entry
			if local and entry["source"] == "local" and entry["template_key"] == local:
				return entry
		return None

	def _set(self, doctype, name, value):
		frappe.db.set_value(doctype, name, "surface_category", value)

	def _uncategorised(self, payload):
		return {
			entry["meta_template"] if entry["source"] == "meta" else entry["template_key"]
			for entry in payload["templates"]
			if not entry["surface_category"]
		}

	# --- the unfiltered contract --------------------------------------------

	def test_seed_created_exactly_the_nine_screens(self):
		self.assertEqual(len(SEEDED), 9)
		self.assertEqual(len(set(SEEDED)), 9)
		for key in SEEDED:
			self.assertTrue(frappe.db.exists(CATEGORY_DOCTYPE, key), f"{key} not seeded")
			row = frappe.db.get_value(CATEGORY_DOCTYPE, key, ["label_en", "label_ar", "enabled"], as_dict=True)
			self.assertTrue(row.label_en)
			self.assertFalse(row.label_ar, "Arabic labels are the owner's to fill in")
			self.assertTrue(row.enabled)

	def test_no_parameter_returns_the_whole_catalogue(self):
		payload = unified_template_options()
		self.assertEqual(payload["counts"]["total"], len(self.local) + len(self.mirror))
		self.assertEqual(payload["counts"]["meta"], len(self.mirror))
		self.assertEqual(payload["counts"]["local"], len(self.local))
		for entry in payload["templates"]:
			self.assertIn("surface_category", entry)

	def test_categorising_a_template_does_not_change_the_unfiltered_list(self):
		before = unified_template_options()
		self._set(LOCAL_TEMPLATE_DOCTYPE, self.local[0], "lab")
		self._set(MIRROR_DOCTYPE, self.mirror[0], "boarding")
		after = unified_template_options()
		self.assertEqual(self._ids(before), self._ids(after))
		self.assertEqual(before["counts"], after["counts"])

	def test_empty_values_are_not_a_filter(self):
		full = unified_template_options()["counts"]["total"]
		for empty in (None, "", "   ", [], "[]"):
			self.assertEqual(
				unified_template_options(surface_category=empty)["counts"]["total"],
				full,
				f"{empty!r} should mean 'no filter'",
			)

	# --- filtering -----------------------------------------------------------

	def test_each_seeded_category_returns_its_matches_plus_the_uncategorised(self):
		wanted_local, other_local = self.local[0], self.local[1]
		wanted_meta, other_meta = self.mirror[0], self.mirror[1]
		for key in SEEDED:
			other = "boarding" if key != "boarding" else "lab"
			self._set(LOCAL_TEMPLATE_DOCTYPE, wanted_local, key)
			self._set(MIRROR_DOCTYPE, wanted_meta, key)
			self._set(LOCAL_TEMPLATE_DOCTYPE, other_local, other)
			self._set(MIRROR_DOCTYPE, other_meta, other)

			payload = unified_template_options(surface_category=key)
			ids = self._ids(payload)
			full = unified_template_options()
			self.assertIn(wanted_meta, ids, key)
			self.assertIn(frappe.db.get_value(LOCAL_TEMPLATE_DOCTYPE, wanted_local, "template_key"), ids, key)
			self.assertNotIn(other_meta, ids, key)
			self.assertNotIn(frappe.db.get_value(LOCAL_TEMPLATE_DOCTYPE, other_local, "template_key"), ids, key)
			self.assertTrue(self._uncategorised(full) <= ids, f"{key} dropped an uncategorised template")
			for entry in payload["templates"]:
				self.assertIn(entry["surface_category"], (None, key))

	def test_unknown_category_returns_only_the_uncategorised(self):
		self._set(LOCAL_TEMPLATE_DOCTYPE, self.local[0], "lab")
		self._set(MIRROR_DOCTYPE, self.mirror[0], "lab")
		payload = unified_template_options(surface_category="no_such_screen")
		self.assertTrue(payload["templates"], "an unknown key must not empty the catalogue")
		for entry in payload["templates"]:
			self.assertIsNone(entry["surface_category"])
		self.assertEqual(self._ids(payload), self._uncategorised(unified_template_options()))

	def test_several_categories_as_a_list_or_a_json_array(self):
		self._set(LOCAL_TEMPLATE_DOCTYPE, self.local[0], "lab")
		self._set(MIRROR_DOCTYPE, self.mirror[0], "radiology")
		self._set(MIRROR_DOCTYPE, self.mirror[1], "boarding")
		expected = self._ids(unified_template_options(surface_category=["lab", "radiology"]))
		self.assertEqual(expected, self._ids(unified_template_options(surface_category='["lab", "radiology"]')))
		self.assertIn(self.mirror[0], expected)
		self.assertNotIn(self.mirror[1], expected)

	def test_counts_describe_what_is_returned(self):
		self._set(MIRROR_DOCTYPE, self.mirror[0], "lab")
		self._set(MIRROR_DOCTYPE, self.mirror[1], "boarding")
		payload = unified_template_options(surface_category="lab")
		self.assertEqual(payload["counts"]["total"], len(payload["templates"]))
		self.assertEqual(
			payload["counts"]["meta"] + payload["counts"]["local"], payload["counts"]["total"]
		)
		self.assertEqual(
			payload["counts"]["sendable"] + payload["counts"]["unsendable"], payload["counts"]["total"]
		)

	# --- a bound pair is one template ---------------------------------------

	def test_a_bound_pair_is_categorised_from_either_side(self):
		mirror = frappe.get_all(MIRROR_DOCTYPE, filters={"local_template": ["is", "set"]}, pluck="name")
		if not mirror:
			self.skipTest("no bound mirror row on this site")
		mirror = mirror[0]
		local = frappe.db.get_value(MIRROR_DOCTYPE, mirror, "local_template")
		key = frappe.db.get_value(LOCAL_TEMPLATE_DOCTYPE, local, "template_key")

		self._set(LOCAL_TEMPLATE_DOCTYPE, local, "lab")
		payload = unified_template_options(surface_category="lab")
		self.assertEqual(self._entry(payload, meta=mirror)["surface_category"], "lab")
		self.assertEqual(self._entry(payload, local=key)["surface_category"], "lab")

		self._set(LOCAL_TEMPLATE_DOCTYPE, local, None)
		self._set(MIRROR_DOCTYPE, mirror, "radiology")
		payload = unified_template_options(surface_category="radiology")
		self.assertEqual(self._entry(payload, meta=mirror)["surface_category"], "radiology")
		self.assertEqual(self._entry(payload, local=key)["surface_category"], "radiology")

	# --- Meta's own category is a different axis -----------------------------

	def test_meta_category_is_untouched(self):
		before = {
			(entry["source"], entry["meta_template"], entry["template_key"]): entry["category"]
			for entry in unified_template_options()["templates"]
		}
		self._set(LOCAL_TEMPLATE_DOCTYPE, self.local[0], "invoice")
		self._set(MIRROR_DOCTYPE, self.mirror[0], "invoice")
		after = {
			(entry["source"], entry["meta_template"], entry["template_key"]): entry["category"]
			for entry in unified_template_options()["templates"]
		}
		self.assertEqual(before, after)

	def test_every_documented_key_survives_a_filtered_call(self):
		self._set(MIRROR_DOCTYPE, self.mirror[0], "lab")
		unfiltered = unified_template_options()["templates"][0]
		filtered = unified_template_options(surface_category="lab")["templates"][0]
		self.assertEqual(set(unfiltered) | {"surface_category"}, set(filtered))


class TestTemplateSurfaceCategoryApi(IntegrationTestCase):
	"""The web editor's path: save_template, list_templates and the dropdown source.

	The save rule that matters is the three-way one - absent keeps, empty clears, a key
	must be an enabled category - because an editor that predates the field sends saves
	without it and must not wipe what someone set in desk.
	"""

	KEY = "zz_surface_api_probe"

	def setUp(self):
		frappe.set_user("Administrator")

	def _save(self, **data):
		from pet_app.api.notifications import save_template

		return save_template(data={"template_key": self.KEY, **data})

	def _stored(self):
		return frappe.db.get_value(LOCAL_TEMPLATE_DOCTYPE, self.KEY, ["surface_category", "category"], as_dict=True)

	def _create(self, **extra):
		result = self._save(
			template_name=self.KEY, language="en", category="Utility", enabled=1,
			delivery_mode="App Styled", body_preview="hi", **extra,
		)
		self.assertTrue(result["ok"], result)
		return result

	def _code(self, result):
		return (result.get("meta") or {}).get("code") or (result.get("error") or {}).get("code")

	def test_categories_are_the_enabled_ones_sorted(self):
		from pet_app.api.notifications import list_template_categories

		frappe.db.set_value(CATEGORY_DOCTYPE, "radiology", "enabled", 0)
		rows = list_template_categories()["data"]["categories"]
		self.assertNotIn("radiology", [row["name"] for row in rows])
		# The database sorts case-insensitively (utf8mb4 collation), so compare the same way:
		# a live category such as "Appointment" or "Other" must not look out of order.
		names = [row["category_name"] for row in rows]
		self.assertEqual([name.lower() for name in names], sorted(name.lower() for name in names))
		for row in rows:
			self.assertEqual(set(row), {"name", "category_name", "label_en", "label_ar", "page_key", "surface_key", "page_keys", "surface_keys"})

	def test_absent_keeps_empty_clears(self):
		self._create(surface_category="lab")
		self.assertEqual(self._stored().surface_category, "lab")
		self.assertTrue(self._save(body_preview="edited")["ok"])
		self.assertEqual(self._stored().surface_category, "lab")
		self.assertTrue(self._save(surface_category="")["ok"])
		self.assertFalse(self._stored().surface_category)
		self.assertEqual(self._stored().category, "Utility")

	def test_unknown_and_disabled_are_refused_and_change_nothing(self):
		self._create(surface_category="lab")
		result = self._save(surface_category="no_such_screen")
		self.assertFalse(result["ok"])
		self.assertEqual(self._code(result), "SURFACE_CATEGORY_NOT_FOUND")
		frappe.db.set_value(CATEGORY_DOCTYPE, "radiology", "enabled", 0)
		result = self._save(surface_category="radiology")
		self.assertFalse(result["ok"])
		self.assertEqual(self._code(result), "SURFACE_CATEGORY_DISABLED")
		self.assertEqual(self._stored().surface_category, "lab")

	def test_list_templates_returns_the_field(self):
		from pet_app.api.notifications import list_templates

		self._create(surface_category="invoice")
		rows = list_templates()["data"]["templates"]
		self.assertTrue(all("surface_category" in row for row in rows))
		self.assertEqual(next(row for row in rows if row["template_key"] == self.KEY)["surface_category"], "invoice")

	def test_a_local_save_reaches_the_bound_mirror_entry_without_writing_it(self):
		mirror = frappe.get_all(MIRROR_DOCTYPE, filters={"local_template": ["is", "set"]}, fields=["name", "local_template", "surface_category"])
		if not mirror:
			self.skipTest("no bound mirror row on this site")
		mirror = mirror[0]
		key = frappe.db.get_value(LOCAL_TEMPLATE_DOCTYPE, mirror.local_template, "template_key")
		from pet_app.api.notifications import save_template

		self.assertTrue(save_template(data={"template_key": key, "surface_category": "general_guardian"})["ok"])
		entry = next(
			e for e in unified_template_options(surface_category="general_guardian")["templates"]
			if e["source"] == "meta" and e["meta_template"] == mirror.name
		)
		self.assertEqual(entry["surface_category"], "general_guardian")
		self.assertEqual(frappe.db.get_value(MIRROR_DOCTYPE, mirror.name, "surface_category"), mirror.surface_category)


class TestMetaTemplateSurfaceCategory(IntegrationTestCase):
	"""set_meta_template_surface_category: the local-only write for a Meta mirror row.

	The point of the endpoint is what it does NOT do - no Graph call, no change to
	anything Meta owns - so those are asserted directly, and every outbound HTTP verb is
	made to fail for the duration of each test.
	"""

	WATCHED = ("status", "components_json", "raw_json", "last_synced_at", "missing_on_meta", "slot_map_stale", "local_template")

	def setUp(self):
		import requests

		frappe.set_user("Administrator")
		self.unbound = frappe.get_all(MIRROR_DOCTYPE, filters={"local_template": ["is", "not set"]}, pluck="name", limit=1)
		if not self.unbound:
			self.skipTest("no unbound mirror row on this site")
		self.unbound = self.unbound[0]

		def refuse(*args, **kwargs):
			raise AssertionError("outbound HTTP call attempted")

		for verb in ("get", "post", "put", "patch", "delete", "request"):
			patcher = patch.object(requests, verb, refuse)
			patcher.start()
			self.addCleanup(patcher.stop)
		patcher = patch.object(requests.Session, "request", refuse)
		patcher.start()
		self.addCleanup(patcher.stop)

	def _set(self, name, **kwargs):
		from pet_app.api.notifications import set_meta_template_surface_category

		return set_meta_template_surface_category(meta_template=name, **kwargs)

	def _code(self, result):
		return (result.get("meta") or {}).get("code")

	def _watched(self, name):
		return frappe.db.get_value(MIRROR_DOCTYPE, name, self.WATCHED, as_dict=True)

	def test_set_change_clear_and_nothing_meta_owned_moves(self):
		before = self._watched(self.unbound)
		result = self._set(self.unbound, surface_category="lab")
		self.assertTrue(result["ok"], result)
		self.assertEqual(result["data"]["surface_category"], "lab")
		result = self._set(self.unbound, surface_category="invoice")
		self.assertTrue(result["ok"], result)
		self.assertEqual(result["data"]["surface_category_source"], "mirror")
		self.assertTrue(result["data"]["surface_category_editable"])
		self.assertEqual(frappe.db.get_value(MIRROR_DOCTYPE, self.unbound, "surface_category"), "invoice")
		self.assertTrue(self._set(self.unbound, surface_category="")["ok"])
		self.assertFalse(frappe.db.get_value(MIRROR_DOCTYPE, self.unbound, "surface_category"))
		self.assertTrue(self._set(self.unbound, surface_category=None)["ok"])
		self.assertEqual(self._watched(self.unbound), before)

	def test_absent_unknown_and_disabled_are_refused(self):
		self._set(self.unbound, surface_category="lab")
		self.assertEqual(self._code(self._set(self.unbound)), "SURFACE_CATEGORY_REQUIRED")
		self.assertEqual(self._code(self._set(self.unbound, surface_category="no_such_screen")), "SURFACE_CATEGORY_NOT_FOUND")
		# IntegrationTestCase rolls back per class, not per test, so undo it here.
		self.addCleanup(frappe.db.set_value, CATEGORY_DOCTYPE, "radiology", "enabled", 1)
		frappe.db.set_value(CATEGORY_DOCTYPE, "radiology", "enabled", 0)
		self.assertEqual(self._code(self._set(self.unbound, surface_category="radiology")), "SURFACE_CATEGORY_DISABLED")
		self.assertEqual(frappe.db.get_value(MIRROR_DOCTYPE, self.unbound, "surface_category"), "lab")

	def test_any_meta_status_is_accepted(self):
		original = frappe.db.get_value(MIRROR_DOCTYPE, self.unbound, "status")
		self.addCleanup(frappe.db.set_value, MIRROR_DOCTYPE, self.unbound, "status", original, update_modified=False)
		for status in ("PENDING", "REJECTED", "DISABLED"):
			frappe.db.set_value(MIRROR_DOCTYPE, self.unbound, "status", status, update_modified=False)
			result = self._set(self.unbound, surface_category="boarding")
			self.assertTrue(result["ok"], result)
			self.assertEqual(result["data"]["status"], status)

	def test_a_bound_row_is_refused_and_inherits_from_the_local_side(self):
		from pet_app.api.notifications import get_meta_template, list_meta_templates

		bound = frappe.get_all(MIRROR_DOCTYPE, filters={"local_template": ["is", "set"]}, fields=["name", "local_template"], limit=1)
		if not bound:
			self.skipTest("no bound mirror row on this site")
		bound = bound[0]
		frappe.db.set_value(LOCAL_TEMPLATE_DOCTYPE, bound.local_template, "surface_category", None)
		frappe.db.set_value(MIRROR_DOCTYPE, bound.name, "surface_category", None)

		self.assertEqual(self._code(self._set(bound.name, surface_category="lab")), "SURFACE_CATEGORY_BOUND_TO_LOCAL")
		self.assertFalse(frappe.db.get_value(MIRROR_DOCTYPE, bound.name, "surface_category"))

		frappe.db.set_value(LOCAL_TEMPLATE_DOCTYPE, bound.local_template, "surface_category", "lab")
		row = get_meta_template(meta_template_id=bound.name)["data"]
		self.assertEqual((row["surface_category"], row["surface_category_source"], row["surface_category_editable"]), ("lab", "local", False))
		listed = {r["meta_template_id"]: r for r in list_meta_templates(limit=100)["data"]["templates"]}
		self.assertTrue(all("surface_category" in r for r in listed.values()))

		# Both sides set - reachable through desk, or a sync binding an already-categorised
		# row: the local value wins on both picker entries, so the pair never splits.
		frappe.db.set_value(MIRROR_DOCTYPE, bound.name, "surface_category", "radiology")
		key = frappe.db.get_value(LOCAL_TEMPLATE_DOCTYPE, bound.local_template, "template_key")
		payload = unified_template_options()
		meta_entry = next(e for e in payload["templates"] if e["source"] == "meta" and e["meta_template"] == bound.name)
		local_entry = next(e for e in payload["templates"] if e["source"] == "local" and e["template_key"] == key)
		self.assertEqual((meta_entry["surface_category"], local_entry["surface_category"]), ("lab", "lab"))


class TestTemplateCategoryPageKey(IntegrationTestCase):
	"""page_key: which screen a category belongs to, set in desk, resolved without a deploy.

	Skipped where patch p1_29 has not added the column, so the suite still runs on a site
	that has not migrated.
	"""

	def setUp(self):
		from pet_app.notifications.template_categories import has_page_key

		frappe.set_user("Administrator")
		if not has_page_key():
			self.skipTest("page_key not on this site yet - run patch p1_29")
		# Owned by this test: each run starts from no screen on these two.
		for name in ("lab", "radiology"):
			original = frappe.db.get_value(CATEGORY_DOCTYPE, name, "page_key")
			self.addCleanup(frappe.db.set_value, CATEGORY_DOCTYPE, name, "page_key", original, update_modified=False)
			frappe.db.set_value(CATEGORY_DOCTYPE, name, "page_key", None, update_modified=False)

	def _save(self, name, page_key):
		doc = frappe.get_doc(CATEGORY_DOCTYPE, name)
		doc.page_key = page_key
		doc.save(ignore_permissions=True)
		return frappe.db.get_value(CATEGORY_DOCTYPE, name, "page_key")

	def test_stores_trims_and_clears(self):
		self.assertEqual(self._save("lab", "  page.zz.probe_lab "), "page.zz.probe_lab")
		self.assertIsNone(self._save("lab", ""))

	def test_a_screen_belongs_to_one_category(self):
		from pet_app.notifications.template_categories import PageKeyTakenError

		self._save("lab", "page.zz.probe_shared")
		with self.assertRaises(PageKeyTakenError) as caught:
			self._save("radiology", "PAGE.ZZ.PROBE_SHARED")
		self.assertEqual(caught.exception.details["category"], "lab")
		self.assertIsNone(frappe.db.get_value(CATEGORY_DOCTYPE, "radiology", "page_key"))
		# Several categories with no screen is the normal case, not a collision.
		self.assertIsNone(self._save("lab", None))
		self.assertIsNone(self._save("radiology", None))

	def test_lookup_returns_the_enabled_category_or_none(self):
		from pet_app.notifications.template_categories import has_screens

		if has_screens():
			self.skipTest("since p1_31 screens bind through Pet App Template Screen - see TestTemplateScreens")
		from pet_app.api.notifications import get_surface_category_for_page, list_template_categories

		self._save("lab", "page.zz.probe_lookup")
		found = get_surface_category_for_page(page_key="page.zz.probe_lookup")["data"]
		self.assertEqual(found["category"]["name"], "lab")
		self.assertIsNone(get_surface_category_for_page(page_key="page.zz.nothing_here")["data"]["category"])
		self.assertIsNone(get_surface_category_for_page(page_key="")["data"]["category"])
		rows = {row["name"]: row for row in list_template_categories()["data"]["categories"]}
		self.assertEqual(rows["lab"]["page_key"], "page.zz.probe_lookup")

		self.addCleanup(frappe.db.set_value, CATEGORY_DOCTYPE, "lab", "enabled", 1)
		frappe.db.set_value(CATEGORY_DOCTYPE, "lab", "enabled", 0)
		self.assertIsNone(get_surface_category_for_page(page_key="page.zz.probe_lookup")["data"]["category"])

	def test_the_catalogue_does_not_read_page_key(self):
		before = unified_template_options()
		self._save("lab", "page.zz.probe_catalogue")
		self.assertEqual(unified_template_options(), before)


class TestTemplateCategorySurfaceKey(IntegrationTestCase):
	"""surface_key: a category bound to one send dialog, resolved before its page.

	Skipped where patch p1_30 has not added the column.
	"""

	OWNED = ("lab", "radiology", "death_certificate")

	def setUp(self):
		from pet_app.notifications.template_categories import has_page_key, has_surface_key

		frappe.set_user("Administrator")
		if not (has_page_key() and has_surface_key()):
			self.skipTest("page_key / surface_key not on this site yet - run patches p1_29 and p1_30")
		# IntegrationTestCase rolls back per class, not per test: restore what each test owns.
		for name in self.OWNED:
			original = frappe.db.get_value(CATEGORY_DOCTYPE, name, ["page_key", "surface_key", "enabled"], as_dict=True)
			self.addCleanup(frappe.db.set_value, CATEGORY_DOCTYPE, name, dict(original), update_modified=False)
			frappe.db.set_value(
				CATEGORY_DOCTYPE, name, {"page_key": None, "surface_key": None, "enabled": 1}, update_modified=False
			)

	def _save(self, name, **values):
		doc = frappe.get_doc(CATEGORY_DOCTYPE, name)
		doc.update(values)
		doc.save(ignore_permissions=True)
		return frappe.db.get_value(CATEGORY_DOCTYPE, name, list(values))

	def _resolve(self, **keys):
		from pet_app.api.notifications import get_surface_category

		return get_surface_category(**keys)["data"]

	def test_stores_trims_and_clears(self):
		self.assertEqual(self._save("death_certificate", surface_key="  zz.probe.death "), "zz.probe.death")
		self.assertIsNone(self._save("death_certificate", surface_key=""))

	def test_a_surface_belongs_to_one_category(self):
		from pet_app.notifications.template_categories import SurfaceKeyTakenError

		self._save("death_certificate", surface_key="zz.probe.shared")
		with self.assertRaises(SurfaceKeyTakenError) as caught:
			self._save("lab", surface_key="ZZ.PROBE.SHARED")
		self.assertEqual(caught.exception.exc_type, "SURFACE_KEY_TAKEN")
		self.assertEqual(caught.exception.details["category"], "death_certificate")
		self.assertIsNone(frappe.db.get_value(CATEGORY_DOCTYPE, "lab", "surface_key"))
		# Different namespaces: the same text as a page_key elsewhere is not a collision.
		self.assertEqual(self._save("lab", page_key="zz.probe.shared"), "zz.probe.shared")

	def test_resolution_order(self):
		from pet_app.notifications.template_categories import has_screens

		if has_screens():
			self.skipTest("since p1_31 screens bind through Pet App Template Screen - see TestTemplateScreens")
		self._save("lab", page_key="page.zz.probe_profile")
		self._save("death_certificate", surface_key="zz.probe.death_dialog")
		self._save("radiology", surface_key="zz.probe.lab_dialog", page_key="page.zz.probe_radiology")

		surface_only = self._resolve(surface_key="zz.probe.death_dialog")
		self.assertEqual((surface_only["category"]["name"], surface_only["resolved_by"]), ("death_certificate", "surface"))

		page_only = self._resolve(page_key="page.zz.probe_profile")
		self.assertEqual((page_only["category"]["name"], page_only["resolved_by"]), ("lab", "page"))

		# An unknown surface on a known page falls through to the page.
		fallthrough = self._resolve(surface_key="zz.probe.unbound_dialog", page_key="page.zz.probe_profile")
		self.assertEqual((fallthrough["category"]["name"], fallthrough["resolved_by"]), ("lab", "page"))

		agreeing = self._resolve(surface_key="zz.probe.lab_dialog", page_key="page.zz.probe_radiology")
		self.assertEqual((agreeing["category"]["name"], agreeing["resolved_by"], agreeing["page_category"]), ("radiology", "surface", None))

		conflicting = self._resolve(surface_key="zz.probe.death_dialog", page_key="page.zz.probe_profile")
		self.assertEqual(
			(conflicting["category"]["name"], conflicting["resolved_by"], conflicting["page_category"]),
			("death_certificate", "surface", "lab"),
		)

		nothing = self._resolve(surface_key="zz.probe.nothing", page_key="page.zz.nothing")
		self.assertEqual((nothing["category"], nothing["resolved_by"]), (None, None))
		self.assertIsNone(self._resolve()["category"])

	def test_a_disabled_category_does_not_match(self):
		from pet_app.notifications.template_categories import has_screens

		if has_screens():
			self.skipTest("since p1_31 screens bind through Pet App Template Screen - see TestTemplateScreens")
		self._save("death_certificate", surface_key="zz.probe.death_dialog")
		self._save("lab", page_key="page.zz.probe_profile")
		frappe.db.set_value(CATEGORY_DOCTYPE, "death_certificate", "enabled", 0)
		self.assertIsNone(self._resolve(surface_key="zz.probe.death_dialog")["category"])
		# Skipped as if unbound, so the page rule answers.
		fallback = self._resolve(surface_key="zz.probe.death_dialog", page_key="page.zz.probe_profile")
		self.assertEqual((fallback["category"]["name"], fallback["resolved_by"]), ("lab", "page"))

	def test_the_list_and_the_page_lookup(self):
		from pet_app.notifications.template_categories import has_screens

		if has_screens():
			self.skipTest("since p1_31 screens bind through Pet App Template Screen - see TestTemplateScreens")
		from pet_app.api.notifications import get_surface_category_for_page, list_template_categories

		self._save("radiology", surface_key="zz.probe.list", page_key="page.zz.probe_list")
		rows = {row["name"]: row for row in list_template_categories()["data"]["categories"]}
		self.assertEqual((rows["radiology"]["surface_key"], rows["radiology"]["page_key"]), ("zz.probe.list", "page.zz.probe_list"))
		self.assertTrue(all({"page_key", "surface_key"} <= set(row) for row in rows.values()))
		# The page-only lookup is unchanged: it ignores surface_key entirely.
		found = get_surface_category_for_page(page_key="page.zz.probe_list")["data"]
		self.assertEqual(found["category"]["name"], "radiology")

	def test_the_catalogue_does_not_read_either_key(self):
		before = unified_template_options()
		self._save("radiology", surface_key="zz.probe.catalogue", page_key="page.zz.probe_catalogue")
		self.assertEqual(unified_template_options(), before)


class TestSurfaceKeyPicker(IntegrationTestCase):
	"""surface_key picked from send_targets.SEND_SURFACES in the category form's dialog.

	The options come from the registry at runtime; a value outside it stays valid and
	visible. Uniqueness, the empty value and the resolver are exactly as before.
	"""

	def setUp(self):
		from pet_app.notifications.template_categories import has_surface_key

		frappe.set_user("Administrator")
		if not has_surface_key():
			self.skipTest("surface_key not on this site yet - run patch p1_30")
		for name in ("death_certificate", "general_guardian"):
			original = frappe.db.get_value(CATEGORY_DOCTYPE, name, ["surface_key", "enabled"], as_dict=True)
			self.addCleanup(frappe.db.set_value, CATEGORY_DOCTYPE, name, dict(original), update_modified=False)
			frappe.db.set_value(CATEGORY_DOCTYPE, name, {"surface_key": None, "enabled": 1}, update_modified=False)

	def _save(self, name, value):
		doc = frappe.get_doc(CATEGORY_DOCTYPE, name)
		doc.surface_key = value
		doc.save(ignore_permissions=True)
		return frappe.db.get_value(CATEGORY_DOCTYPE, name, "surface_key")

	def _options(self):
		from pet_app.api.notifications import list_surface_key_options

		return list_surface_key_options()["data"]["options"]

	def test_the_field_stays_unique_data(self):
		# The picker is the form script's dialog, not a field type: Frappe allows `unique`
		# only on Data, Link, Read Only and Int, and the index is the duplicate backstop.
		field = frappe.get_meta(CATEGORY_DOCTYPE).get_field("surface_key")
		self.assertEqual((field.fieldtype, bool(field.unique)), ("Data", True))

	def test_the_form_script_is_wired(self):
		# An app include, not doctype_js: FormMeta.add_code() skips custom doctypes entirely.
		import os

		path = "/assets/pet_app/js/template_category.js"
		# The hook carries a ?v= cache-buster; the served file is the path without it.
		self.assertTrue(any(hook.split("?")[0] == path for hook in frappe.get_hooks("app_include_js")))
		served = os.path.join(frappe.local.sites_path, path.lstrip("/"))
		with open(served) as handle:
			script = handle.read()
		self.assertIn('frappe.ui.form.on("Pet App Template Category"', script)
		self.assertIn("pet_app.api.notifications.list_surface_key_options", script)

	def test_the_picker_offers_the_whole_registry_with_labels(self):
		from pet_app.notifications.send_targets import SEND_SURFACES

		registered = {o["value"]: o["label"] for o in self._options() if o["registered"]}
		self.assertEqual(registered, SEND_SURFACES)

	def test_empty_saves_and_a_registered_key_saves(self):
		self.assertEqual(self._save("death_certificate", "pet.death"), "pet.death")
		self.assertIsNone(self._save("death_certificate", ""))
		self.assertIsNone(self._save("death_certificate", None))

	def test_a_duplicate_is_still_refused(self):
		from pet_app.notifications.template_categories import SurfaceKeyTakenError

		self._save("death_certificate", "pet.death")
		with self.assertRaises(SurfaceKeyTakenError) as caught:
			self._save("general_guardian", "pet.death")
		self.assertEqual(caught.exception.exc_type, "SURFACE_KEY_TAKEN")

	def test_an_unregistered_value_stays_valid_and_visible(self):
		frappe.message_log = []
		self.assertEqual(self._save("general_guardian", "zz.retired.surface"), "zz.retired.surface")
		self.assertTrue(any("zz.retired.surface" in str(m) for m in frappe.message_log))
		# Other stored values outside the registry (a live category's own) may be listed too.
		unknown = [o for o in self._options() if not o["registered"]]
		self.assertIn("zz.retired.surface", [o["value"] for o in unknown])
		# Saving the category again, unchanged, neither refuses nor warns.
		frappe.message_log = []
		doc = frappe.get_doc(CATEGORY_DOCTYPE, "general_guardian")
		doc.description = (doc.description or "") + " "
		doc.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value(CATEGORY_DOCTYPE, "general_guardian", "surface_key"), "zz.retired.surface")
		self.assertFalse(any("zz.retired.surface" in str(m) for m in frappe.message_log))

	def test_the_resolver_is_unchanged(self):
		from pet_app.notifications.template_categories import has_screens

		if has_screens():
			self.skipTest("since p1_31 screens bind through Pet App Template Screen - see TestTemplateScreens")
		from pet_app.api.notifications import get_surface_category

		self._save("death_certificate", "pet.death")
		result = get_surface_category(surface_key="pet.death", page_key="page.healthcare.boarding")["data"]
		self.assertEqual((result["category"]["name"], result["resolved_by"]), ("death_certificate", "surface"))


class TestPageKeyPicker(IntegrationTestCase):
	"""page_key picked from Pet App Access Settings -> Page Access in the category form's dialog.

	Same rules as the surface_key picker: runtime options, a stored value outside the
	registry stays valid and visible, and an unknown key saves with a warning.
	"""

	SEEDED = {
		"boarding": "page.healthcare.boarding",
		"lab": "page.healthcare.labs",
		"radiology": "page.healthcare.radiology",
		"preventive": "page.healthcare.preventive-care",
	}

	def setUp(self):
		from pet_app.notifications.template_categories import has_page_key

		frappe.set_user("Administrator")
		if not has_page_key():
			self.skipTest("page_key not on this site yet - run patch p1_29")
		for name in ("death_certificate", "general_guardian"):
			original = frappe.db.get_value(CATEGORY_DOCTYPE, name, "page_key")
			self.addCleanup(frappe.db.set_value, CATEGORY_DOCTYPE, name, "page_key", original, update_modified=False)
			frappe.db.set_value(CATEGORY_DOCTYPE, name, "page_key", None, update_modified=False)

	def _save(self, name, value):
		doc = frappe.get_doc(CATEGORY_DOCTYPE, name)
		doc.page_key = value
		doc.save(ignore_permissions=True)
		return frappe.db.get_value(CATEGORY_DOCTYPE, name, "page_key")

	def _options(self):
		from pet_app.api.notifications import list_page_key_options

		return list_page_key_options()["data"]["options"]

	def _free_page(self):
		"""A real page key no category holds, so saving it cannot collide."""
		taken = set(frappe.get_all(CATEGORY_DOCTYPE, filters={"page_key": ["is", "set"]}, pluck="page_key"))
		return next(o["value"] for o in self._options() if o["registered"] and o["value"] not in taken)

	def test_the_picker_lists_the_real_page_keys(self):
		from pet_app.notifications.template_categories import registered_pages

		pages = {row.page_key for row in registered_pages()}
		listed = {o["value"] for o in self._options() if o["registered"]}
		self.assertTrue(pages)
		self.assertEqual(listed, pages)
		self.assertTrue(all(o["label"] and o["description"] == o["value"] for o in self._options()))

	def test_empty_saves_and_a_real_page_saves_without_a_warning(self):
		page = self._free_page()
		frappe.message_log = []
		self.assertEqual(self._save("death_certificate", page), page)
		self.assertFalse(any("Page Access" in str(m) for m in frappe.message_log))
		self.assertIsNone(self._save("death_certificate", ""))
		self.assertIsNone(self._save("death_certificate", None))

	def test_a_duplicate_is_still_refused(self):
		from pet_app.notifications.template_categories import PageKeyTakenError

		page = self._free_page()
		self._save("death_certificate", page)
		with self.assertRaises(PageKeyTakenError) as caught:
			self._save("general_guardian", page.upper())
		self.assertEqual(caught.exception.exc_type, "PAGE_KEY_TAKEN")
		self.assertEqual(caught.exception.details["category"], "death_certificate")

	def test_an_unknown_key_saves_with_a_warning_and_stays_listed(self):
		frappe.message_log = []
		self.assertEqual(self._save("general_guardian", "page.zz.not_yet_created"), "page.zz.not_yet_created")
		self.assertTrue(any("page.zz.not_yet_created" in str(m) for m in frappe.message_log))
		unknown = [(o["value"], o["registered"]) for o in self._options() if not o["registered"]]
		self.assertIn(("page.zz.not_yet_created", False), unknown)
		# Saving the category again, unchanged, does not warn again.
		frappe.message_log = []
		doc = frappe.get_doc(CATEGORY_DOCTYPE, "general_guardian")
		doc.description = (doc.description or "") + " "
		doc.save(ignore_permissions=True)
		self.assertFalse(any("page.zz.not_yet_created" in str(m) for m in frappe.message_log))

	def test_every_stored_key_is_listed_and_flagged_by_the_registry(self):
		# Always listed, never blanked; `registered` says whether a Page Access row exists.
		# Data-driven on purpose: a site whose registry lacks one of the seeded pages (the
		# test clone has 68 rows, production 80) must list that key as "(not in registry)".
		from pet_app.notifications.template_categories import registered_pages

		pages = {row.page_key for row in registered_pages()}
		listed = {o["value"]: o["registered"] for o in self._options()}
		for name in self.SEEDED:
			key = frappe.db.get_value(CATEGORY_DOCTYPE, name, "page_key")
			if key:
				self.assertIn(key, listed, name)
				self.assertEqual(listed[key], key in pages, name)


class TestTemplateScreens(IntegrationTestCase):
	"""A screen shows several categories (p1_31): one Pet App Template Screen record per screen.

	Probe keys (page.zz.*, zz.*) never collide with a real screen. Skipped where p1_31 has not
	run.
	"""

	def setUp(self):
		from pet_app.notifications.template_categories import has_screens

		frappe.set_user("Administrator")
		if not has_screens():
			self.skipTest("Pet App Template Screen not on this site yet - run patch p1_31")
		for name in ("lab", "boarding", "general_guardian"):
			original = frappe.db.get_value(CATEGORY_DOCTYPE, name, "enabled")
			self.addCleanup(frappe.db.set_value, CATEGORY_DOCTYPE, name, "enabled", original, update_modified=False)
		self.addCleanup(self._drop_probes)

	def _drop_probes(self):
		from pet_app.notifications.template_categories import SCREEN_DOCTYPE

		for name in frappe.get_all(
			SCREEN_DOCTYPE,
			or_filters=[["page_key", "like", "page.zz.%"], ["surface_key", "like", "zz.%"]],
			pluck="name",
		):
			frappe.delete_doc(SCREEN_DOCTYPE, name, force=True, ignore_permissions=True)

	def _screen(self, *, page=None, surface=None, categories=()):
		from pet_app.notifications.template_categories import SCREEN_DOCTYPE

		return frappe.get_doc(
			{
				"doctype": SCREEN_DOCTYPE,
				"page_key": page,
				"surface_key": surface,
				"categories": [{"category": name} for name in categories],
			}
		).insert(ignore_permissions=True)

	def _resolve(self, **keys):
		from pet_app.api.notifications import get_surface_category

		return get_surface_category(**keys)["data"]

	def _names(self, resolved):
		return [category["name"] for category in resolved["categories"]]

	def test_every_category_held_key_has_its_screen(self):
		from pet_app.notifications.template_categories import SCREEN_CATEGORY_DOCTYPE, SCREEN_DOCTYPE

		for fieldname in ("page_key", "surface_key"):
			for row in frappe.get_all(CATEGORY_DOCTYPE, filters={fieldname: ["is", "set"]}, fields=["name", fieldname]):
				screen = frappe.db.get_value(SCREEN_DOCTYPE, {fieldname: row[fieldname]}, "name")
				self.assertTrue(screen, f"{row.name}: no screen for {row[fieldname]}")
				listed = frappe.get_all(SCREEN_CATEGORY_DOCTYPE, filters={"parent": screen}, pluck="category")
				self.assertIn(row.name, listed, row[fieldname])

	def test_a_page_bound_to_three_categories_returns_all_three(self):
		from pet_app.api.notifications import get_surface_category_for_page

		self._screen(page="page.zz.live", categories=("lab", "boarding", "general_guardian"))
		resolved = self._resolve(page_key="page.zz.live")
		self.assertEqual(self._names(resolved), ["lab", "boarding", "general_guardian"])
		self.assertEqual(resolved["resolved_by"], "page")
		self.assertIsNone(resolved["category"])
		page_only = get_surface_category_for_page(page_key="page.zz.live")["data"]
		self.assertEqual([c["name"] for c in page_only["categories"]], ["lab", "boarding", "general_guardian"])
		self.assertIsNone(page_only["category"])

	def test_the_list_filter_returns_those_categories_plus_uncategorised_only(self):
		import json

		self._screen(page="page.zz.live", categories=("lab", "boarding", "general_guardian"))
		wanted = set(self._names(self._resolve(page_key="page.zz.live")))
		everything = unified_template_options()["templates"]
		expected = [e for e in everything if not e["surface_category"] or e["surface_category"] in wanted]
		for value in (json.dumps(sorted(wanted)), sorted(wanted)):
			got = unified_template_options(surface_category=value)["templates"]
			self.assertEqual(got, expected)
		self.assertTrue(all(not e["surface_category"] or e["surface_category"] in wanted for e in got))

	def test_a_screen_bound_to_nothing_gets_the_full_catalogue(self):
		resolved = self._resolve(page_key="page.zz.unbound", surface_key="zz.unbound")
		self.assertEqual((resolved["categories"], resolved["category"], resolved["resolved_by"]), ([], None, None))
		self.assertEqual(unified_template_options(surface_category=None), unified_template_options())

	def test_the_surface_replaces_the_page(self):
		self._screen(page="page.zz.profile", categories=("lab", "boarding"))
		self._screen(surface="zz.death_dialog", categories=("general_guardian",))
		both = self._resolve(surface_key="zz.death_dialog", page_key="page.zz.profile")
		self.assertEqual(self._names(both), ["general_guardian"])
		self.assertEqual(both["resolved_by"], "surface")
		self.assertEqual(both["page_categories"], ["lab", "boarding"])
		self.assertIsNone(both["page_category"])
		# A surface with no record, or only disabled categories, falls through to the page.
		self.assertEqual(self._names(self._resolve(surface_key="zz.other", page_key="page.zz.profile")), ["lab", "boarding"])
		frappe.db.set_value(CATEGORY_DOCTYPE, "general_guardian", "enabled", 0)
		fallback = self._resolve(surface_key="zz.death_dialog", page_key="page.zz.profile")
		self.assertEqual((self._names(fallback), fallback["resolved_by"]), (["lab", "boarding"], "page"))

	def test_single_value_fields_for_older_callers(self):
		self._screen(page="page.zz.one", categories=("lab",))
		one = self._resolve(page_key="page.zz.one")
		self.assertEqual(one["category"]["name"], "lab")
		self._screen(page="page.zz.many", categories=("lab", "boarding"))
		self.assertIsNone(self._resolve(page_key="page.zz.many")["category"])
		self._screen(surface="zz.one_dialog", categories=("general_guardian",))
		conflict = self._resolve(surface_key="zz.one_dialog", page_key="page.zz.one")
		self.assertEqual((conflict["category"]["name"], conflict["page_category"]), ("general_guardian", "lab"))

	def test_a_disabled_category_is_skipped(self):
		self._screen(page="page.zz.mixed", categories=("lab", "boarding"))
		frappe.db.set_value(CATEGORY_DOCTYPE, "lab", "enabled", 0)
		self.assertEqual(self._names(self._resolve(page_key="page.zz.mixed")), ["boarding"])

	def test_a_screen_is_claimed_once(self):
		from pet_app.notifications.template_categories import PageKeyTakenError, SurfaceKeyTakenError

		first = self._screen(page="page.zz.once", categories=("lab",))
		with self.assertRaises(PageKeyTakenError) as caught:
			self._screen(page="PAGE.ZZ.ONCE", categories=("boarding",))
		self.assertEqual((caught.exception.exc_type, caught.exception.details["holder"]), ("PAGE_KEY_TAKEN", first.name))
		self._screen(surface="zz.once", categories=("lab",))
		with self.assertRaises(SurfaceKeyTakenError):
			self._screen(surface="zz.once", categories=("boarding",))
		# One category on many screens is the point, not a collision.
		self._screen(page="page.zz.twice", categories=("lab",))

	def test_a_record_names_exactly_one_screen(self):
		from pet_app.notifications.template_categories import ScreenKeyError

		with self.assertRaises(ScreenKeyError):
			self._screen(categories=("lab",))
		with self.assertRaises(ScreenKeyError):
			self._screen(page="page.zz.both", surface="zz.both", categories=("lab",))

	def test_a_category_listed_twice_is_kept_once_and_label_defaults(self):
		screen = self._screen(page="page.zz.dupes", categories=("lab", "boarding", "lab"))
		self.assertEqual([row.category for row in screen.categories], ["lab", "boarding"])
		self.assertEqual(screen.label, "page.zz.dupes")

	def test_the_category_list_reports_every_screen(self):
		from pet_app.api.notifications import list_template_categories

		self._screen(page="page.zz.a", categories=("general_guardian",))
		self._screen(page="page.zz.b", categories=("general_guardian",))
		row = next(r for r in list_template_categories()["data"]["categories"] if r["name"] == "general_guardian")
		self.assertTrue({"page.zz.a", "page.zz.b"} <= set(row["page_keys"]))
		self.assertIsNone(row["page_key"])

	def test_the_catalogue_does_not_read_screens(self):
		before = unified_template_options()
		self._screen(page="page.zz.catalogue", categories=("lab", "boarding"))
		self.assertEqual(unified_template_options(), before)
