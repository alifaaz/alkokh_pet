"""Whitelisted entry points for the internal Item barcode generator.

Three endpoints over one generator (``pet_app.utils.item_barcode_generator``), split by
who persists the value:

- ``peek_next_item_barcode()`` - GET. Forecasts the next value and writes NOTHING. This
  is what an edit dialog's Generate button calls: the operator then saves, and the SAVE
  mints the real barcode through the ``custom_generate_barcode`` Check and the
  before_validate hook in ``pet_app.utils.item_barcode``. A dialog closed without saving
  costs nothing and consumes no number.
- ``generate_item_barcodes(items)`` - POST. A named list, written immediately.
- ``generate_missing_item_barcodes(dry_run=0)`` - POST. Every Item currently without a
  barcode, written immediately. ``dry_run=1`` lists what would be touched and writes
  nothing, consuming no series numbers.

The last two are the bulk admin tools, where writing at call time is the point: there is
no form open and no save to wait for. They are POST-only, and that is load-bearing
rather than tidiness - see the note on the decorators below.

Generation only. Label printing is a separate, later change.

Transaction shape: one request is one transaction. Per-item conditions (already has
one, not found, not permitted) come back as skipped rows and do not abort the batch.
Anything else - the field missing, the series unusable, an unexpected failure - rolls
back everything written so far in the request and returns ``ok: false``, so the caller
never gets a response that says "generated" for a row that was then rolled back.

``@standardize_response`` is deliberately not used: it turns an exception into an
``ok: false`` envelope but lets the request go on to commit, which would persist a
half-finished batch. Permission denial is the one exception left to raise, so it
reaches the client as Frappe's normal PermissionError, as every endpoint here does.
"""

from __future__ import annotations

import time

import frappe
from frappe import _
from frappe.utils import cint, cstr

from pet_app.api.permissions import require_doctype_permission
from pet_app.api.response import fail, ok
from pet_app.utils.item_barcode import GENERATE_FLAG_FIELDNAME
from pet_app.utils.item_barcode_generator import (
	SERIES_KEY,
	BarcodeFieldMissing,
	SeriesExhausted,
	dedupe,
	field_is_installed,
	generate_for_items,
	items_missing_barcode,
	peek_next_barcode,
	summarize,
)


@frappe.whitelist(methods=["GET", "POST"])
def peek_next_item_barcode():
	"""Forecast the next barcode without reserving it. Nothing is written.

	For the Generate button in an edit dialog. The returned value is provisional: it is
	what a save right now would most likely produce, and the caller must present it as
	such and let the save's own response be the authority. Two operators peeking at the
	same moment see the same number, and the first to save gets it.

	To actually mint one, set ``custom_generate_barcode`` on the Item and save - see
	``meta.generate_flag_field`` in this response, so a client reads the field name off
	the API rather than hardcoding it.
	"""
	require_doctype_permission("Item", "write")

	if not field_is_installed():
		return _field_missing()

	try:
		barcode = peek_next_barcode()
	except SeriesExhausted as exc:
		return fail(cstr(exc), code="SERIES_EXHAUSTED")

	return ok(
		{"barcode": barcode, "is_preview": True},
		meta={
			"preview": True,
			"reserved": False,
			"generate_flag_field": GENERATE_FLAG_FIELDNAME,
			"series_key": SERIES_KEY,
		},
	)


# POST-only, and not merely for correctness: with the framework default of
# GET/POST/PUT/DELETE, a GET reached the body, generated, returned ok:true with a real
# barcode - and then Frappe rolled the whole request back on the way out, because it
# only commits for unsafe methods (frappe/app.py: UNSAFE_HTTP_METHODS). The caller was
# told a number had been assigned that no longer existed anywhere. Declaring the methods
# makes the framework refuse the GET before the body runs, so the lie is impossible.
@frappe.whitelist(methods=["POST"])
def generate_item_barcodes(items=None):
	"""Assign a barcode to each named Item that has none. Existing codes are returned as-is.

	Writes at call time. For a form where the operator has yet to save, use
	``peek_next_item_barcode`` plus the ``custom_generate_barcode`` Check instead.

	``items``: a JSON array of Item names, a Python list, or a single Item name. Names
	are deduplicated; the first occurrence sets the order. Not split on commas - an Item
	name may contain one.
	"""
	require_doctype_permission("Item", "write")

	try:
		names = parse_items(items)
	except ValueError as exc:
		return fail(cstr(exc), code="ITEMS_INVALID")
	if not names:
		return fail(_("Name at least one Item."), code="ITEMS_REQUIRED")

	return _generate(names)


@frappe.whitelist(methods=["POST"])
def generate_missing_item_barcodes(dry_run=0):
	"""Assign a barcode to every Item currently without one.

	``dry_run=1`` returns the Item names that would be touched and their count, and
	writes nothing. Use it to see the blast radius before the real run.
	"""
	require_doctype_permission("Item", "write")

	if not field_is_installed():
		return _field_missing()

	names = items_missing_barcode()
	if cint(dry_run):
		return ok(
			{"items": names, "results": []},
			meta={
				"dry_run": True,
				"missing": len(names),
				"would_generate": len(names),
				"series_key": SERIES_KEY,
			},
		)

	if not names:
		return ok({"results": []}, meta={**summarize([]), "missing": 0})

	return _generate(names)


def parse_items(items) -> list[str]:
	"""Accept the shapes a whitelisted call can arrive in and return clean names."""
	if items is None:
		return []
	if isinstance(items, str):
		text = items.strip()
		if not text:
			return []
		# Anything that looks like JSON is parsed as JSON, so a mis-nested {"items": [...]}
		# is refused below rather than being taken literally as one Item's name.
		if text[0] in "[{":
			try:
				items = frappe.parse_json(text)
			except Exception:
				raise ValueError(_("items must be a JSON array of Item names."))
		else:
			items = [text]
	if not isinstance(items, (list, tuple)):
		raise ValueError(_("items must be a list of Item names."))
	return dedupe(items)


def _generate(names: list[str]):
	if not field_is_installed():
		return _field_missing()

	started = time.monotonic()
	try:
		results = generate_for_items(names)
	except BarcodeFieldMissing as exc:
		frappe.db.rollback()
		return fail(cstr(exc), code="BARCODE_FIELD_MISSING")
	except SeriesExhausted as exc:
		frappe.db.rollback()
		return fail(cstr(exc), code="SERIES_EXHAUSTED")
	except Exception:
		# Roll back FIRST, then log: the Error Log row must survive the rollback, and it
		# does because it is written after it and committed with the request.
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "ITEM_BARCODE_GENERATION_FAILED")
		return fail(
			_("Barcode generation failed and nothing was written. The error has been logged."),
			code="BARCODE_GENERATION_FAILED",
		)

	meta = summarize(results)
	meta["elapsed_ms"] = int((time.monotonic() - started) * 1000)
	return ok({"results": results}, meta=meta)


def _field_missing():
	from pet_app.utils.item_barcode import FIELDNAME

	return fail(
		_(
			"Item.{0} does not exist on this site yet. Run bench migrate to sync the Custom Field fixture, then retry."
		).format(FIELDNAME),
		code="BARCODE_FIELD_MISSING",
	)
