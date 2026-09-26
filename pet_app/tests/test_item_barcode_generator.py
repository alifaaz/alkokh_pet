"""Tests for the internal Item barcode generator.

Layers:

- ``TestBarcodeFormatting`` needs no database and always runs.
- ``TestItemBarcodeGenerator`` and ``TestBarcodePeek`` need ``Item.custom_barcode`` to
  exist, which happens when ``bench migrate`` syncs the Custom Field fixture. On a site
  that has not migrated yet the whole class skips with a message saying so, rather than
  failing on a column that is expected to be absent.
- ``TestGenerateOnSave`` additionally needs ``Item.custom_generate_barcode`` and skips
  on the same basis.

Nothing here commits. ``IntegrationTestCase`` rolls the class back, so the series
counter and every barcode written below vanish with it; a test run does not consume
live ``ALK-`` numbers.

Run:

    bench --site <site> run-tests --app pet_app --module pet_app.tests.test_item_barcode_generator
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from pet_app.api import item_barcode as api
from pet_app.utils import item_barcode_generator as generator
from pet_app.utils.item_barcode import FIELDNAME, GENERATE_FLAG_FIELDNAME
from pet_app.utils.item_barcode_generator import (
	PREFIX,
	REASON_ALREADY_HAS_BARCODE,
	REASON_NOT_FOUND,
	REASON_NOT_PERMITTED,
	SERIES_KEY,
	STATUS_GENERATED,
	STATUS_SKIPPED,
	format_barcode,
)


class TestBarcodeFormatting(UnitTestCase):
	def test_format_is_prefix_plus_six_zero_padded_digits(self):
		self.assertEqual(format_barcode(1), "ALK-000001")
		self.assertEqual(format_barcode(123), "ALK-000123")
		self.assertEqual(format_barcode(999999), "ALK-999999")

	def test_format_widens_past_six_digits_instead_of_wrapping(self):
		self.assertEqual(format_barcode(1000000), "ALK-1000000")

	def test_series_key_is_private_to_the_barcode(self):
		"""Not the bare prefix: a naming series ``ALK-.######`` must not share the counter."""
		self.assertNotEqual(SERIES_KEY, PREFIX)
		self.assertTrue(SERIES_KEY.startswith(PREFIX))

	def test_parse_items_accepts_json_list_python_list_and_single_name(self):
		self.assertEqual(api.parse_items('["A", "B"]'), ["A", "B"])
		self.assertEqual(api.parse_items(["A", "B"]), ["A", "B"])
		self.assertEqual(api.parse_items("Dog 1-5 kg, large"), ["Dog 1-5 kg, large"])

	def test_parse_items_dedupes_and_trims_preserving_first_order(self):
		self.assertEqual(api.parse_items([" B ", "A", "B", "", None, "A"]), ["B", "A"])

	def test_parse_items_rejects_non_list_json(self):
		with self.assertRaises(ValueError):
			api.parse_items('{"item": "A"}')
		with self.assertRaises(ValueError):
			api.parse_items("[not json")

	def test_the_two_writing_endpoints_are_post_only(self):
		"""A GET reached the body, generated, and was then rolled back by the framework,
		which commits only for unsafe methods. The caller was handed a barcode that did
		not exist. The declaration is what makes that impossible."""
		for fn in (api.generate_item_barcodes, api.generate_missing_item_barcodes):
			self.assertEqual(
				frappe.allowed_http_methods_for_whitelisted_func[fn],
				["POST"],
				f"{fn.__name__} must be POST-only: it writes",
			)

	def test_the_preview_endpoint_allows_get(self):
		"""It writes nothing, so a GET is honest and cacheable-shaped."""
		self.assertIn("GET", frappe.allowed_http_methods_for_whitelisted_func[api.peek_next_item_barcode])


class TestItemBarcodeGenerator(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.has_column("Item", FIELDNAME):
			raise unittest.SkipTest(
				f"Item.{FIELDNAME} is not on this site yet; run bench migrate to sync the fixture first."
			)
		frappe.set_user("Administrator")
		cls.group = _item_group()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	# --- format and sequence -----------------------------------------------------

	def test_generates_sequential_barcodes_in_the_requested_order(self):
		a, b = self._make_item(), self._make_item()
		start = _series_current()

		results = generator.generate_for_items([a, b])

		self.assertEqual([r["status"] for r in results], [STATUS_GENERATED, STATUS_GENERATED])
		self.assertEqual(results[0]["barcode"], format_barcode(start + 1))
		self.assertEqual(results[1]["barcode"], format_barcode(start + 2))
		self.assertEqual(frappe.db.get_value("Item", a, FIELDNAME), format_barcode(start + 1))
		self.assertEqual(frappe.db.get_value("Item", b, FIELDNAME), format_barcode(start + 2))

	def test_generated_value_is_stored_exactly_with_no_whitespace(self):
		a = self._make_item()
		barcode = generator.generate_for_items([a])[0]["barcode"]
		self.assertEqual(barcode, barcode.strip())
		self.assertEqual(frappe.db.get_value("Item", a, FIELDNAME), barcode)

	def test_write_bumps_modified_so_a_stale_desk_save_is_refused(self):
		a = self._make_item()
		before = frappe.db.get_value("Item", a, "modified")
		generator.generate_for_items([a])
		self.assertNotEqual(frappe.db.get_value("Item", a, "modified"), before)

	# --- never overwrite ----------------------------------------------------------

	def test_existing_barcode_is_returned_unchanged_and_reported(self):
		a = self._make_item()
		frappe.db.set_value("Item", a, FIELDNAME, "5941234")
		start = _series_current()

		(row,) = generator.generate_for_items([a])

		self.assertEqual(row["status"], STATUS_SKIPPED)
		self.assertEqual(row["reason_code"], REASON_ALREADY_HAS_BARCODE)
		self.assertEqual(row["barcode"], "5941234")
		self.assertFalse(row["generated"])
		self.assertIn("5941234", row["reason"])
		self.assertEqual(frappe.db.get_value("Item", a, FIELDNAME), "5941234")
		self.assertEqual(_series_current(), start, "a skip must not consume a series number")

	def test_running_twice_is_idempotent(self):
		a = self._make_item()
		first = generator.generate_for_items([a])[0]["barcode"]
		second = generator.generate_for_items([a])[0]
		self.assertEqual(second["status"], STATUS_SKIPPED)
		self.assertEqual(second["reason_code"], REASON_ALREADY_HAS_BARCODE)
		self.assertEqual(second["barcode"], first)

	# --- per-item reasons ---------------------------------------------------------

	def test_unknown_item_is_a_skipped_row_not_an_exception(self):
		(row,) = generator.generate_for_items(["no-such-item-" + frappe.generate_hash(length=6)])
		self.assertEqual(row["status"], STATUS_SKIPPED)
		self.assertEqual(row["reason_code"], REASON_NOT_FOUND)
		self.assertIsNone(row["barcode"])

	def test_one_bad_name_does_not_stop_the_others(self):
		a = self._make_item()
		results = generator.generate_for_items(["no-such-item-" + frappe.generate_hash(length=6), a])
		self.assertEqual(results[0]["reason_code"], REASON_NOT_FOUND)
		self.assertEqual(results[1]["status"], STATUS_GENERATED)

	def test_item_the_user_may_not_write_is_skipped_with_reason(self):
		a = self._make_item()
		with patch.object(frappe, "has_permission", return_value=False):
			(row,) = generator.generate_for_items([a])
		self.assertEqual(row["status"], STATUS_SKIPPED)
		self.assertEqual(row["reason_code"], REASON_NOT_PERMITTED)
		self.assertIsNone(frappe.db.get_value("Item", a, FIELDNAME))

	def test_duplicate_names_in_one_call_are_collapsed(self):
		a = self._make_item()
		results = generator.generate_for_items([a, a, a])
		self.assertEqual(len(results), 1)
		self.assertEqual(results[0]["status"], STATUS_GENERATED)

	# --- collision handling -------------------------------------------------------

	def test_number_already_typed_by_hand_is_passed_over(self):
		"""An operator entered the very next ALK- number manually. Generation skips it."""
		holder, a = self._make_item(), self._make_item()
		taken = format_barcode(_series_current() + 1)
		frappe.db.set_value("Item", holder, FIELDNAME, taken)

		(row,) = generator.generate_for_items([a])

		self.assertEqual(row["status"], STATUS_GENERATED)
		self.assertEqual(row["barcode"], format_barcode(int(taken[len(PREFIX) :]) + 1))
		self.assertEqual(row["numbers_skipped"], 1)
		self.assertEqual(frappe.db.get_value("Item", holder, FIELDNAME), taken, "the holder keeps its code")

	def test_unique_index_race_falls_through_to_the_next_number(self):
		"""The pre-check says free, the index says taken: retry, do not reuse.

		Simulated by blinding ``is_taken`` so the write itself hits the UNIQUE index.
		The savepoint must undo the failed write only; the counter must keep moving.
		"""
		holder, a = self._make_item(), self._make_item()
		taken = format_barcode(_series_current() + 1)
		frappe.db.set_value("Item", holder, FIELDNAME, taken)

		with patch.object(generator, "is_taken", return_value=False):
			(row,) = generator.generate_for_items([a])

		self.assertEqual(row["status"], STATUS_GENERATED)
		self.assertEqual(row["barcode"], format_barcode(int(taken[len(PREFIX) :]) + 1))
		self.assertEqual(row["numbers_skipped"], 1)
		self.assertEqual(frappe.db.get_value("Item", a, FIELDNAME), row["barcode"])
		self.assertEqual(frappe.db.get_value("Item", holder, FIELDNAME), taken)

	def test_second_operator_waits_on_the_counter_lock(self):
		"""Two connections. The first has advanced the counter and not committed; the
		second cannot read it until the first finishes. With a one-second lock wait the
		second call times out instead of returning a number - which is the proof that it
		could not have returned the SAME number."""
		generator.next_candidate()  # primary connection now holds the tabSeries row lock

		with self.secondary_connection():
			frappe.db.sql("SET SESSION innodb_lock_wait_timeout = 1")
			with self.assertRaises(Exception) as caught:
				generator.next_candidate()
		self.assertTrue(
			frappe.db.is_timedout(caught.exception) or "lock wait timeout" in str(caught.exception).lower(),
			f"expected a lock wait timeout, got: {caught.exception!r}",
		)

	def test_series_row_is_seeded_once_and_reused(self):
		generator.ensure_series_row()
		current = _series_current()
		generator.ensure_series_row()
		self.assertEqual(_series_current(), current)
		self.assertEqual(frappe.db.count("Series", {"name": SERIES_KEY}), 1)

	# --- batch --------------------------------------------------------------------

	def test_missing_list_contains_only_items_without_a_barcode(self):
		a, b = self._make_item(), self._make_item()
		frappe.db.set_value("Item", b, FIELDNAME, "5941235")
		missing = generator.items_missing_barcode()
		self.assertIn(a, missing)
		self.assertNotIn(b, missing)

	def test_batch_endpoint_dry_run_writes_nothing(self):
		a = self._make_item()
		start = _series_current()
		response = api.generate_missing_item_barcodes(dry_run=1)
		self.assertTrue(response["ok"])
		self.assertTrue(response["meta"]["dry_run"])
		self.assertIn(a, response["data"]["items"])
		self.assertEqual(response["meta"]["missing"], len(response["data"]["items"]))
		self.assertIsNone(frappe.db.get_value("Item", a, FIELDNAME))
		self.assertEqual(_series_current(), start)

	def test_batch_endpoint_assigns_every_missing_item_and_then_finds_none(self):
		a, b = self._make_item(), self._make_item()
		before = len(generator.items_missing_barcode())

		response = api.generate_missing_item_barcodes()

		self.assertTrue(response["ok"], response)
		self.assertEqual(response["meta"]["requested"], before)
		self.assertEqual(response["meta"]["generated"], before)
		self.assertEqual(response["meta"]["skipped"], 0)
		self.assertEqual(generator.items_missing_barcode(), [])
		codes = {r["item"]: r["barcode"] for r in response["data"]["results"]}
		self.assertTrue(codes[a].startswith(PREFIX))
		self.assertTrue(codes[b].startswith(PREFIX))
		self.assertEqual(len(set(codes.values())), len(codes), "every barcode in the batch is distinct")

	# --- endpoint envelope ---------------------------------------------------------

	def test_named_endpoint_returns_envelope_with_per_item_rows_and_counts(self):
		a = self._make_item()
		frappe.db.set_value("Item", a, FIELDNAME, "5941236")
		b = self._make_item()

		response = api.generate_item_barcodes(items=[a, b, "no-such-item-" + frappe.generate_hash(length=6)])

		self.assertTrue(response["ok"])
		rows = {r["item"]: r for r in response["data"]["results"]}
		self.assertEqual(rows[a]["reason_code"], REASON_ALREADY_HAS_BARCODE)
		self.assertEqual(rows[b]["status"], STATUS_GENERATED)
		meta = response["meta"]
		self.assertEqual(meta["requested"], 3)
		self.assertEqual(meta["generated"], 1)
		self.assertEqual(meta["skipped"], 2)
		self.assertEqual(meta["skipped_by_reason"][REASON_ALREADY_HAS_BARCODE], 1)
		self.assertEqual(meta["skipped_by_reason"][REASON_NOT_FOUND], 1)
		self.assertEqual(meta["series_key"], SERIES_KEY)

	def test_named_endpoint_requires_a_name(self):
		response = api.generate_item_barcodes(items=[])
		self.assertFalse(response["ok"])
		self.assertEqual(response["meta"]["code"], "ITEMS_REQUIRED")

	def test_endpoints_raise_permission_error_without_item_write(self):
		with patch("pet_app.api.item_barcode.require_doctype_permission", side_effect=frappe.PermissionError):
			with self.assertRaises(frappe.PermissionError):
				api.generate_item_barcodes(items=["anything"])
			with self.assertRaises(frappe.PermissionError):
				api.generate_missing_item_barcodes()

	def test_unexpected_failure_rolls_back_everything_written_in_the_request(self):
		a, b = self._make_item(), self._make_item()
		real_assign = generator.assign_barcode
		calls = {"n": 0}

		def assign_then_explode(name, **kwargs):
			calls["n"] += 1
			if calls["n"] == 2:
				raise RuntimeError("simulated failure on the second item")
			return real_assign(name, **kwargs)

		with patch.object(generator, "assign_barcode", side_effect=assign_then_explode):
			response = api.generate_item_barcodes(items=[a, b])

		self.assertFalse(response["ok"])
		self.assertEqual(response["meta"]["code"], "BARCODE_GENERATION_FAILED")
		# The endpoint rolled the whole transaction back. The two Items were inserted in
		# that same transaction, so the proof is that they are gone too - not just that
		# the first one's barcode is empty, which would also be true of a missing row.
		self.assertFalse(frappe.db.exists("Item", a), "the request's transaction was rolled back")
		self.assertFalse(frappe.db.exists("Item", b))

	# --- helpers ------------------------------------------------------------------

	def _make_item(self) -> str:
		return _insert_item(self.group).name


class TestBarcodePeek(IntegrationTestCase):
	"""The preview must forecast without reserving. Every test here asserts the counter."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_skip_unless_column(FIELDNAME)
		frappe.set_user("Administrator")
		cls.group = _item_group()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")

	def test_peek_does_not_advance_the_counter(self):
		generator.ensure_series_row()
		start = _series_current()
		for _ in range(3):
			generator.peek_next_barcode()
		self.assertEqual(_series_current(), start, "a preview must reserve nothing")

	def test_peek_returns_what_the_next_generation_actually_produces(self):
		item = _insert_item(self.group).name
		forecast = generator.peek_next_barcode()
		(row,) = generator.generate_for_items([item])
		self.assertEqual(row["barcode"], forecast)

	def test_two_peeks_in_a_row_return_the_same_number(self):
		self.assertEqual(generator.peek_next_barcode(), generator.peek_next_barcode())

	def test_peek_skips_a_number_already_typed_onto_an_item(self):
		generator.ensure_series_row()
		holder = _insert_item(self.group).name
		taken = format_barcode(_series_current() + 1)
		frappe.db.set_value("Item", holder, FIELDNAME, taken)

		self.assertEqual(generator.peek_next_barcode(), format_barcode(_series_current() + 2))

	def test_endpoint_returns_the_preview_labelled_as_one(self):
		response = api.peek_next_item_barcode()
		self.assertTrue(response["ok"])
		self.assertTrue(response["data"]["barcode"].startswith(PREFIX))
		self.assertTrue(response["data"]["is_preview"])
		self.assertTrue(response["meta"]["preview"])
		self.assertFalse(response["meta"]["reserved"])

	def test_endpoint_names_the_field_the_client_must_send_on_save(self):
		"""So a frontend reads the field name off the API instead of hardcoding it."""
		response = api.peek_next_item_barcode()
		self.assertEqual(response["meta"]["generate_flag_field"], GENERATE_FLAG_FIELDNAME)

	def test_endpoint_requires_item_write(self):
		with patch("pet_app.api.item_barcode.require_doctype_permission", side_effect=frappe.PermissionError):
			with self.assertRaises(frappe.PermissionError):
				api.peek_next_item_barcode()


class TestGenerateOnSave(IntegrationTestCase):
	"""The save is what persists a barcode. The button click must not."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_skip_unless_column(FIELDNAME)
		_skip_unless_column(GENERATE_FLAG_FIELDNAME)
		frappe.set_user("Administrator")
		cls.group = _item_group()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")

	def test_flag_on_a_new_item_mints_a_barcode_and_clears_itself(self):
		doc = _insert_item(self.group, **{GENERATE_FLAG_FIELDNAME: 1})
		self.assertTrue(doc.get(FIELDNAME).startswith(PREFIX))
		self.assertEqual(frappe.db.get_value("Item", doc.name, FIELDNAME), doc.get(FIELDNAME))
		self.assertFalse(
			frappe.db.get_value("Item", doc.name, GENERATE_FLAG_FIELDNAME),
			"the request must never persist as stored state",
		)

	def test_flag_on_an_existing_item_without_a_barcode_mints_one(self):
		doc = _insert_item(self.group)
		self.assertIsNone(frappe.db.get_value("Item", doc.name, FIELDNAME))

		doc.set(GENERATE_FLAG_FIELDNAME, 1)
		doc.save()

		self.assertTrue(frappe.db.get_value("Item", doc.name, FIELDNAME).startswith(PREFIX))
		self.assertFalse(frappe.db.get_value("Item", doc.name, GENERATE_FLAG_FIELDNAME))

	def test_a_typed_barcode_survives_the_flag_and_no_number_is_consumed(self):
		generator.ensure_series_row()
		start = _series_current()

		doc = _insert_item(self.group, **{FIELDNAME: "5941237", GENERATE_FLAG_FIELDNAME: 1})

		self.assertEqual(doc.get(FIELDNAME), "5941237")
		self.assertEqual(frappe.db.get_value("Item", doc.name, FIELDNAME), "5941237")
		self.assertFalse(frappe.db.get_value("Item", doc.name, GENERATE_FLAG_FIELDNAME))
		self.assertEqual(_series_current(), start, "keeping a typed code must not burn a number")

	def test_no_flag_means_no_barcode(self):
		generator.ensure_series_row()
		start = _series_current()
		doc = _insert_item(self.group)
		self.assertIsNone(frappe.db.get_value("Item", doc.name, FIELDNAME))
		self.assertEqual(_series_current(), start)

	def test_saving_again_does_not_mint_a_second_barcode(self):
		doc = _insert_item(self.group, **{GENERATE_FLAG_FIELDNAME: 1})
		first = doc.get(FIELDNAME)
		doc.item_name = doc.item_name + " edited"
		doc.save()
		self.assertEqual(frappe.db.get_value("Item", doc.name, FIELDNAME), first)

	def test_the_generated_value_is_trimmed_and_unique_shaped(self):
		a = _insert_item(self.group, **{GENERATE_FLAG_FIELDNAME: 1})
		b = _insert_item(self.group, **{GENERATE_FLAG_FIELDNAME: 1})
		self.assertNotEqual(a.get(FIELDNAME), b.get(FIELDNAME))
		self.assertEqual(a.get(FIELDNAME), a.get(FIELDNAME).strip())

	def test_an_abandoned_save_consumes_no_number(self):
		"""The whole point of the split: the counter moves inside the save's transaction,
		so a save that never lands leaves the sequence untouched and gapless."""
		generator.ensure_series_row()
		start = _series_current()

		savepoint = "pet_app_test_abandoned_save"
		frappe.db.savepoint(savepoint)
		doc = _insert_item(self.group, **{GENERATE_FLAG_FIELDNAME: 1})
		self.assertEqual(_series_current(), start + 1, "the save did advance the counter")
		frappe.db.rollback(save_point=savepoint)

		self.assertEqual(_series_current(), start, "rolling the save back returns the number")
		self.assertFalse(frappe.db.exists("Item", doc.name))

	def test_peek_then_save_gives_the_operator_the_number_they_were_shown(self):
		"""The end-to-end flow: click Generate, then save."""
		forecast = generator.peek_next_barcode()
		doc = _insert_item(self.group, **{GENERATE_FLAG_FIELDNAME: 1})
		self.assertEqual(doc.get(FIELDNAME), forecast)


def _skip_unless_column(fieldname: str):
	if not frappe.db.has_column("Item", fieldname):
		raise unittest.SkipTest(
			f"Item.{fieldname} is not on this site yet; run bench migrate to sync the fixture first."
		)


def _insert_item(group: str, **extra):
	code = "_Test ALK Barcode " + frappe.generate_hash(length=8)
	doc = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": code,
			"item_name": code,
			"item_group": group,
			"stock_uom": "Nos",
			"is_stock_item": 0,
			**extra,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc


def _series_current() -> int:
	# Through the module's own reader: tabSeries has no `creation` column, so the
	# obvious frappe.db.get_value("Series", ...) fails on its default ORDER BY.
	return generator.series_current()


def _item_group() -> str:
	for candidate in ("All Item Groups", "Services", "Products"):
		if frappe.db.exists("Item Group", candidate):
			return candidate
	return frappe.get_all("Item Group", filters={"is_group": 0}, pluck="name", limit=1)[0]
