"""The one selector and the one writer for a general ``Item Price`` row.

An item can carry several rows on the same price list - dated, per UOM, per supplier,
per batch. Which one is "the price" is a choice, and the only way that choice stays
correct is for the code that READS it and the code that WRITES it to make the same one.
`api/medication.py` says this in its own docstring; this module is that rule extracted
so a second caller cannot quietly disagree with the first.

A row is a GENERAL row for ``(item_code, price_list)`` when it carries no ``customer``,
``supplier`` or ``batch_no`` and is valid on the date asked about. Among the matches the
winner is the latest ``valid_from``, then a row with no ``packing_unit`` over one with,
then the latest ``modified``, then the highest ``name``. Those last tiebreaks are not
decoration: `_buying_price_row` orders by ``valid_from``/``modified`` only, and on this
site 14 items carry two rows on one list, so a tie is reachable - and a tie means the
answer changes between two identical calls. The ordering here is total, so it cannot.

``packing_unit`` is a preference, NOT a filter, because ERPNext does not treat it as one:
`get_item_price` selects without looking at it at all and only afterwards checks that the
desired qty is a multiple of it. Excluding those rows made two items on this site read as
unpriced when they are priced, which would have had this tool create a second row
competing with the real one. A row that applies at any quantity is simply the more general
answer when both exist on the same date.

AN ITEM CAN HOLD MORE THAN ONE LEGITIMATE PRICE on one list - the 14 two-row items here
are mostly per-UOM (a bag price and a kilo price). Every reader in this app already
collapses those to one, so this does too, but the resolved row carries ``candidate_count``
so a caller can SAY it is choosing, rather than choose silently.

WRITES UPDATE THE RESOLVED ROW IN PLACE and change ``price_list_rate`` and nothing else.
Not the uom, not the dates, not ``selling``/``buying``/``currency`` (read-only - ERPNext
overwrites them from the Price List in ``update_price_list_details``). Two reasons:

  * ``ItemPrice.check_duplicates`` matches the exact tuple
	``(item_code, price_list, uom, valid_from, valid_upto, customer, supplier, batch_no,
	packing_unit)`` and excludes the row itself, so leaving the tuple alone makes a
	duplicate error impossible. None of the uom-blanking workarounds elsewhere in this
	app are needed - and they cannot work here anyway, because ``Item Price.uom`` is
	``reqd: 1`` on this site and a blank-uom row will not save at all.
  * ERPNext applies NO date-overlap validation, so a second row dated today is perfectly
	legal alongside an undated one - and every selling reader in this app resolves with
	an UNORDERED ``frappe.db.get_value``, which would then return an arbitrary one of the
	two. Updating in place keeps the count at one, so ordered and unordered readers agree.

A row is created only when no general row exists at all, always in the item's
``stock_uom`` - the one UOM ERPNext guarantees is in the item's UOM Conversion Detail,
and the shape the other 4,277 rows on this site already have.

This module never commits and never opens a savepoint: the caller owns the transaction,
so it is equally safe from a web request, a test, or ``bench execute``.
"""

from __future__ import annotations

from datetime import date, datetime

import frappe
from frappe.utils import cint, cstr, flt, getdate, get_datetime, nowdate

# Two rates closer than this are the same rate. Currency fields round on the way in and
# out, so an exact `==` reports phantom changes.
RATE_TOLERANCE = 0.001

ACTION_SKIPPED = "Skipped"
ACTION_UNCHANGED = "Unchanged"
ACTION_UPDATE = "Update"
ACTION_CREATE = "Create"

WRITING_ACTIONS = (ACTION_UPDATE, ACTION_CREATE)

_EARLIEST = date(1900, 1, 1)

_ROW_FIELDS = (
	"name",
	"item_code",
	"price_list",
	"price_list_rate",
	"uom",
	"valid_from",
	"valid_upto",
	"customer",
	"supplier",
	"batch_no",
	"packing_unit",
	"modified",
)


def load_general_price_rows(item_codes, price_lists, *, on_date=None) -> dict:
	"""Resolve the general row for every ``(item_code, price_list)`` pair, in one query.

	Returns ``{(item_code, price_list): row}``; a pair with no general row is absent,
	which the caller must read as "no price", not as zero.
	"""
	codes = _unique(item_codes)
	lists = _unique(price_lists)
	if not codes or not lists:
		return {}

	on = getdate(on_date) if on_date else getdate(nowdate())

	# One query for the whole batch. The general/date filtering happens in Python
	# because an `in ('', None)` filter never matches NULL in SQL - pushing these
	# checks down silently drops rows rather than failing.
	rows = frappe.get_all(
		"Item Price",
		filters={"item_code": ["in", codes], "price_list": ["in", lists]},
		fields=list(_ROW_FIELDS),
	)

	candidates: dict = {}
	for row in rows:
		if not _is_general(row) or not _is_valid_on(row, on):
			continue
		candidates.setdefault((row.item_code, row.price_list), []).append(row)

	resolved: dict = {}
	for key, group in candidates.items():
		winner = max(group, key=_rank)
		# How many rows this one was chosen from. > 1 means the item carries more than one
		# live price on this list and the caller is picking; it should say so on screen.
		winner.candidate_count = len(group)
		resolved[key] = winner
	return resolved


def find_general_price_row(item_code: str, price_list: str, *, on_date=None):
	"""The general row for one pair, or ``None``. A wrapper - there is one implementation."""
	if not item_code or not price_list:
		return None
	return load_general_price_rows([item_code], [price_list], on_date=on_date).get(
		(item_code, price_list)
	)


def classify(existing_row, rate) -> str:
	"""What writing ``rate`` would do: Skipped / Unchanged / Update / Create.

	A rate of 0 or less means "not entered", never "worth nothing" - the same reading
	`item_price_import` applies to a blank cell in the price sheet. Writing it would make
	the item sell for free, so it leaves any existing price alone.
	"""
	rate = flt(rate)
	if rate <= 0:
		return ACTION_SKIPPED
	if not existing_row:
		return ACTION_CREATE
	if abs(flt(existing_row.price_list_rate) - rate) < RATE_TOLERANCE:
		return ACTION_UNCHANGED
	return ACTION_UPDATE


def write_price(*, item_code: str, stock_uom: str, price_list: str, rate, existing_row) -> tuple[str, float | None]:
	"""Write ``rate``. Returns ``(item_price_name, previous_rate)``; previous is None on create."""
	rate = flt(rate)
	if existing_row:
		doc = frappe.get_doc("Item Price", existing_row.name)
		previous = flt(doc.price_list_rate)
		# Only the rate. Everything else keeps the tuple check_duplicates matches on.
		doc.price_list_rate = rate
	else:
		previous = None
		doc = frappe.new_doc("Item Price")
		doc.item_code = item_code
		doc.price_list = price_list
		# reqd:1 on this site, and validated against the item's UOM Conversion Detail.
		# stock_uom is the only value ERPNext guarantees is in that table.
		doc.uom = stock_uom
		doc.price_list_rate = rate
	# selling / buying / currency are read-only and are set from the Price List by
	# ItemPrice.update_price_list_details. Assigning them here would be a guess.
	doc.flags.ignore_permissions = True
	doc.save()
	return doc.name, previous


def has_uom_conversion(item_code: str, uom: str) -> bool:
	"""True when ``uom`` is in the item's UOM Conversion Detail - what validate_item checks."""
	if not item_code or not uom:
		return False
	return bool(
		frappe.db.exists(
			"UOM Conversion Detail", {"parenttype": "Item", "parent": item_code, "uom": uom}
		)
	)


def _unique(values) -> list:
	if isinstance(values, str):
		values = [values]
	seen: dict = {}
	for value in values or []:
		value = cstr(value).strip()
		if value:
			seen[value] = True
	return list(seen)


def _is_general(row) -> bool:
	# A party or batch price is for that party or batch, not for the item.
	return not (cstr(row.customer).strip() or cstr(row.supplier).strip() or cstr(row.batch_no).strip())


def _is_valid_on(row, on: date) -> bool:
	if row.valid_from and getdate(row.valid_from) > on:
		return False
	if row.valid_upto and getdate(row.valid_upto) < on:
		return False
	return True


def _rank(row) -> tuple:
	return (
		getdate(row.valid_from) if row.valid_from else _EARLIEST,
		# Prefer the row that applies at any quantity, but only to break a date tie.
		0 if cint(row.packing_unit) else 1,
		get_datetime(row.modified) if row.modified else datetime.min,
		cstr(row.name),
	)
