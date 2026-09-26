"""Assign an internal barcode to an Item that has none.

Format is ``ALK-`` followed by a six-digit zero-padded sequence number, e.g.
``ALK-000123``. Internal only - deliberately not EAN-13 or any registered scheme. That
decision is settled; do not revisit it here.

Three properties this module exists to guarantee, in order of importance:

1. **An existing barcode is never overwritten.** The field is read under a row lock and
   the write is skipped when anything is already there. A printed label is a promise
   that a code resolves to one item forever; regenerating over it breaks every label
   on the shelf.

2. **Two operators generating at the same moment cannot receive the same number.** The
   sequence lives in one row of ``tabSeries`` - the table Frappe's own naming series
   use - and is advanced through ``frappe.model.naming.getseries``, which reads that row
   with ``SELECT ... FOR UPDATE`` before incrementing it. InnoDB holds that lock until
   the transaction commits, so the second caller blocks on the row and reads the value
   the first one left behind. The ``UNIQUE`` index on ``Item.custom_barcode`` is the
   final arbiter on top of that: a number that is somehow already in use (an operator
   typed ``ALK-000005`` by hand, say) is detected before the write and skipped, and if
   the index still refuses the write the statement is rolled back to a savepoint and
   the next number is tried. The series never moves backwards.

3. **One generator for every Item.** No branching on item group, type or business.

Why the series key is ``ALK-BARCODE-`` and not ``ALK-``: ``tabSeries`` is keyed by
prefix and shared by every naming series that uses that prefix. A DocType configured
tomorrow with ``ALK-.######`` would share - and, on delete, revert - an ``ALK-``
counter. Frappe's ``revert_series_if_last`` decrements a series when the document
holding the LAST number is deleted, and that is the reuse the task warned about. It is
keyed on the deleted document's own name. Item is named ``field:item_code`` and this
barcode is a field value, not a name, so deleting an Item never touches this counter:
the number is retired and the sequence simply gets a gap. A private key keeps it that
way regardless of what naming series are added later.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.naming import getseries
from frappe.query_builder import DocType
from frappe.utils import cint, cstr

from pet_app.utils.item_barcode import FIELDNAME

PREFIX = "ALK-"
DIGITS = 6
SERIES_KEY = "ALK-BARCODE-"

# How many consecutive numbers may turn out to be already in use before generation
# gives up for the whole request. In practice one or two, ever; a thousand in a row
# means someone bulk-imported hand-typed ALK- codes and a human needs to look.
MAX_TAKEN_NUMBERS_IN_A_ROW = 1000

_SAVEPOINT = "pet_app_item_barcode"

STATUS_GENERATED = "generated"
STATUS_SKIPPED = "skipped"

REASON_ALREADY_HAS_BARCODE = "ALREADY_HAS_BARCODE"
REASON_NOT_FOUND = "NOT_FOUND"
REASON_NOT_PERMITTED = "NOT_PERMITTED"


class BarcodeFieldMissing(frappe.ValidationError):
	"""``Item.custom_barcode`` is not on this site yet.

	The field ships as a Custom Field fixture and appears when ``bench migrate`` runs
	``sync_fixtures``. Until then there is nothing to write to.
	"""


class SeriesExhausted(frappe.ValidationError):
	"""``MAX_TAKEN_NUMBERS_IN_A_ROW`` consecutive candidates were already in use."""


def field_is_installed() -> bool:
	"""Whether the barcode column exists in the database.

	The column, not the meta: ``sync_fixtures`` creates the Custom Field row and the
	column together, but ``has_column`` is the fact the write below depends on.
	"""
	return bool(frappe.db.has_column("Item", FIELDNAME))


def format_barcode(number: int) -> str:
	"""``ALK-000123``. Widens past six digits rather than wrapping."""
	return f"{PREFIX}{int(number):0{DIGITS}d}"


def ensure_series_row() -> None:
	"""Make sure the counter row exists, so the lock in ``getseries`` has a row to take.

	``getseries`` handles a missing row by inserting it, but two first-ever callers can
	both find it missing and both insert - the loser gets a primary-key error on
	``tabSeries``. Seeding here with ``ON DUPLICATE KEY UPDATE`` is safe under
	concurrency: the loser blocks on the winner's insert, then no-ops. The row is
	seeded at 0 so the first number handed out is 1.
	"""
	frappe.db.sql(
		"INSERT INTO `tabSeries` (`name`, `current`) VALUES (%s, 0) ON DUPLICATE KEY UPDATE `name` = `name`",
		(SERIES_KEY,),
	)


def next_candidate() -> str:
	"""Advance the counter under lock and return the barcode for the new value."""
	ensure_series_row()
	return PREFIX + getseries(SERIES_KEY, DIGITS)


def is_taken(barcode: str) -> bool:
	"""Whether any Item already carries this exact code."""
	return bool(frappe.db.exists("Item", {FIELDNAME: barcode}))


def next_free_barcode() -> tuple[str, int]:
	"""The next candidate no Item carries, and how many taken ones were passed over.

	Every candidate consumes a series number whether or not it is used: a number found
	to be in use is left behind rather than handed out later, because handing it out
	later is exactly the reuse this module refuses to allow.
	"""
	skipped = 0
	while skipped <= MAX_TAKEN_NUMBERS_IN_A_ROW:
		candidate = next_candidate()
		if not is_taken(candidate):
			return candidate, skipped
		skipped += 1
	raise SeriesExhausted(
		_("{0} consecutive {1} numbers are already in use by other items. Generation stopped.").format(
			MAX_TAKEN_NUMBERS_IN_A_ROW, PREFIX
		)
	)


def series_current() -> int:
	"""The counter's value right now, read without taking a lock.

	Uses the query builder rather than ``frappe.db.get_value`` for a concrete reason:
	``tabSeries`` has exactly two columns, ``name`` and ``current``, and ``get_value``
	appends its default ``ORDER BY creation``, so it fails on this table with
	``Unknown column 'creation'``. Frappe's own ``getseries`` sidesteps it the same way.
	"""
	series = DocType("Series")
	row = (frappe.qb.from_(series).where(series.name == SERIES_KEY).select("current")).run()
	return cint(row[0][0]) if row and row[0][0] is not None else 0


def peek_next_barcode() -> str:
	"""The value the next generation would most likely produce. Writes nothing.

	Strictly read-only, and that is the whole point: it backs the Generate button in an
	edit dialog, where the operator may fill the field, change their mind and close
	without saving. A button that advanced the counter would retire a number on every
	such click, and a button that wrote the Item would make the click - not the save -
	the act that persists a barcode. So this reads the counter WITHOUT the
	``SELECT ... FOR UPDATE`` that ``getseries`` takes, leaves it exactly where it was,
	and hands back a string the caller must label as provisional.

	It is a forecast, not a reservation. Two things can make the saved value differ:

	- Another operator saves first and takes this number. Theirs is the real one; the
	  next save here moves on to the following number.
	- A number in the window is already carried by an Item (someone typed ``ALK-000005``
	  by hand). This skips those the same way the write path does, so the forecast
	  matches in the ordinary case rather than being predictably wrong.

	The taken codes are fetched in ONE query, not one existence check per candidate as
	the write path does: the write path checks a single number it has already committed
	to, whereas this may scan forward over a run of them, and a preview must not turn
	into a thousand round trips. Only ``PREFIX``-shaped values can collide with a
	generated candidate, so the LIKE filter is complete for this purpose.

	Raises ``SeriesExhausted`` on the same condition the write path does, so a caller
	that cannot show a number learns why for the same reason in both places.
	"""
	current = series_current()

	taken = {
		cstr(code).strip()
		for code in frappe.get_all("Item", filters={FIELDNAME: ("like", f"{PREFIX}%")}, pluck=FIELDNAME)
		if code
	}

	for offset in range(MAX_TAKEN_NUMBERS_IN_A_ROW + 1):
		candidate = format_barcode(current + 1 + offset)
		if candidate not in taken:
			return candidate

	raise SeriesExhausted(
		_("{0} consecutive {1} numbers are already in use by other items. Generation stopped.").format(
			MAX_TAKEN_NUMBERS_IN_A_ROW, PREFIX
		)
	)


def assign_barcode(item_name: str, *, user: str | None = None) -> dict:
	"""Give one Item a barcode if it has none. Never raises for a per-item condition.

	Returns a result row (see ``_result``) with ``status`` ``generated`` or ``skipped``.
	A skipped row carries a stable ``reason_code`` and a human ``reason``.

	The write is ``frappe.db.set_value``, not ``doc.save()``, on purpose:

	- ``save()`` rewrites every column from an in-memory copy, which is the classic
	  lost-update shape when a desk user has the same Item open. ``set_value`` touches
	  one column plus ``modified``/``modified_by``, so a stale desk save is refused by
	  Frappe's timestamp check instead of quietly clearing the code.
	- ``save()`` runs ERPNext's full Item validation and every ``on_update`` hook,
	  including the wildcard WhatsApp rule evaluation this app registers. A batch of a
	  few hundred items must not fan out into hundreds of rule evaluations.
	- The trim hook in ``pet_app.utils.item_barcode`` is bypassed by ``set_value``, and
	  that is fine here: the value is built by ``format_barcode`` and has no whitespace
	  to trim. Any OTHER ``set_value`` on this column would have to normalise itself.
	"""
	user = user or frappe.session.user
	item_name = cstr(item_name).strip()

	row = frappe.db.get_value("Item", item_name, ["name", "item_name", FIELDNAME], as_dict=True)
	if not row:
		return _result(item_name, None, None, REASON_NOT_FOUND, _("No Item named {0}.").format(item_name))

	# Document-level, so a User Permission on Item is honoured, not just the DocPerm.
	if not frappe.has_permission("Item", ptype="write", doc=row.name, user=user):
		return _result(
			row.name,
			row.item_name,
			None,
			REASON_NOT_PERMITTED,
			_("You do not have write permission on Item {0}.").format(row.name),
		)

	# Locked re-read. The first read above was for existence and permission; this one
	# holds the row until commit, so nothing can put a barcode on it between the check
	# and the write. That is what makes "never overwrite" a guarantee and not a race.
	locked = frappe.db.get_value("Item", row.name, ["item_name", FIELDNAME], as_dict=True, for_update=True)
	existing = cstr(locked.get(FIELDNAME)).strip()
	if existing:
		return _result(
			row.name,
			locked.item_name,
			existing,
			REASON_ALREADY_HAS_BARCODE,
			_("Item already has barcode {0}; left unchanged.").format(existing),
		)

	numbers_skipped = 0
	attempts = 0
	while attempts <= MAX_TAKEN_NUMBERS_IN_A_ROW:
		attempts += 1
		barcode, skipped = next_free_barcode()
		numbers_skipped += skipped

		# The savepoint sits AFTER the series advance: rolling back to it undoes only the
		# failed item write, never the counter increment. Undoing the increment would
		# hand the same number to the next attempt and loop on it forever.
		frappe.db.savepoint(_SAVEPOINT)
		try:
			frappe.db.set_value("Item", row.name, FIELDNAME, barcode)
		except Exception as exc:
			if not frappe.db.is_unique_key_violation(exc):
				raise
			# The index refused a number is_taken() had just cleared: a hand-typed write
			# landed in between. Drop this attempt, keep the counter, take the next.
			frappe.db.rollback(save_point=_SAVEPOINT)
			numbers_skipped += 1
			continue
		frappe.db.release_savepoint(_SAVEPOINT)
		result = _result(row.name, locked.item_name, barcode, None, None)
		result["numbers_skipped"] = numbers_skipped
		return result

	raise SeriesExhausted(
		_("Could not find a free {0} number for Item {1} after {2} attempts.").format(
			PREFIX, row.name, attempts
		)
	)


def generate_for_items(item_names: list[str], *, user: str | None = None) -> list[dict]:
	"""One result row per distinct name, in the order first given.

	Raises ``BarcodeFieldMissing`` before touching anything if the column is absent,
	and ``SeriesExhausted`` mid-way if the sequence is unusable. Per-item conditions
	never raise; they come back as skipped rows. The caller owns the transaction: on a
	raise, roll back, because earlier rows in the same call were written already.
	"""
	if not field_is_installed():
		raise BarcodeFieldMissing(_field_missing_message())
	return [assign_barcode(name, user=user) for name in dedupe(item_names)]


def items_missing_barcode() -> list[str]:
	"""Names of every Item the current user can read that has no barcode, by name.

	``get_list`` rather than ``get_all`` so read permission and any User Permission on
	Item shape the list. Each write is then permission-checked again at document level.
	``"is", "not set"`` covers both NULL and '' - the field patch stores blanks as NULL,
	but a column added by hand before it may hold ''.
	"""
	if not field_is_installed():
		raise BarcodeFieldMissing(_field_missing_message())
	return frappe.get_list(
		"Item",
		filters=[[FIELDNAME, "is", "not set"]],
		pluck="name",
		order_by="name asc",
		limit_page_length=0,
	)


def dedupe(names) -> list[str]:
	"""Trimmed, non-empty, first occurrence wins, order preserved."""
	seen = set()
	out = []
	for name in names or []:
		clean = cstr(name).strip()
		if clean and clean not in seen:
			seen.add(clean)
			out.append(clean)
	return out


def summarize(results: list[dict]) -> dict:
	by_reason: dict[str, int] = {}
	for row in results:
		if row["status"] == STATUS_SKIPPED:
			by_reason[row["reason_code"]] = by_reason.get(row["reason_code"], 0) + 1
	return {
		"requested": len(results),
		"generated": sum(1 for r in results if r["status"] == STATUS_GENERATED),
		"skipped": sum(1 for r in results if r["status"] == STATUS_SKIPPED),
		"skipped_by_reason": by_reason,
		"numbers_skipped_as_taken": sum(r.get("numbers_skipped", 0) for r in results),
		"series_key": SERIES_KEY,
	}


def _result(item, item_name, barcode, reason_code, reason) -> dict:
	generated = reason_code is None
	return {
		"item": item,
		"item_name": item_name,
		"barcode": barcode,
		"generated": generated,
		"status": STATUS_GENERATED if generated else STATUS_SKIPPED,
		"reason_code": reason_code,
		"reason": reason,
	}


def _field_missing_message() -> str:
	return _(
		"Item.{0} does not exist on this site yet. Run bench migrate to sync the Custom Field fixture, then retry."
	).format(FIELDNAME)
