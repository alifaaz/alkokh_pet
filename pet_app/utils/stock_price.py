"""Apply, guard and undo the Item Price writes behind a submitted Stock Price document.

The controller keeps three-line hooks and the work lives here, which is how the rest of
this app splits submit-time side effects. The hooks are `on_submit` / `on_cancel` rather
than a whitelisted method called afterwards, because the price writes must be ATOMIC with
the docstatus transition: a submitted document whose prices were never applied would then
be "undone" on cancel by restoring prices that were never set.

CANCEL RESTORES A SNAPSHOT, NOT HISTORY. Each row records the rate it replaced and the
rate it wrote, and cancel puts the first back. That is only safe while nothing else has
moved the price since, so `guard_stock_price_cancel` refuses when it has and names the
item. The consequence, stated rather than discovered: once a later document changes the
same item, the earlier one can no longer be cancelled. Silently stomping the later price
would be worse.

Nothing here commits. A submit arrives as a POST and Frappe commits it on the way out;
committing early would make a later failure in the same request unrollbackable.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

from pet_app.api.permissions import require_doctype_permission
from pet_app.utils.item_price import (
	ACTION_CREATE,
	RATE_TOLERANCE,
	WRITING_ACTIONS,
	load_general_price_rows,
	write_price,
)

SIDES = ("selling", "buying")
SIDE_LABELS = {"selling": "Selling", "buying": "Buying"}


def planned_writes(doc) -> list[tuple]:
	"""``[(row, side), ...]`` for every side of every row that will write a price."""
	return [
		(row, side)
		for row in (doc.items or [])
		for side in SIDES
		if row.get(f"{side}_action") in WRITING_ACTIONS
	]


def price_list_for(doc, side: str) -> str:
	return doc.selling_price_list if side == "selling" else doc.buying_price_list


def apply_stock_price(doc) -> None:
	"""Write every planned price and record what was replaced. Called from `on_submit`."""
	plans = planned_writes(doc)
	if not plans:
		# validate() refuses this during submit; nothing to do if it is ever reached.
		return

	# Gate on the doctype actually mutated. Permission to submit a Stock Price is not
	# permission to rewrite the price list.
	require_doctype_permission("Item Price", "write")
	if any(row.get(f"{side}_action") == ACTION_CREATE for row, side in plans):
		require_doctype_permission("Item Price", "create")

	existing = load_general_price_rows(
		[row.item_code for row in doc.items],
		[doc.selling_price_list, doc.buying_price_list],
		on_date=doc.posting_date,
	)

	counts = {"selling": 0, "buying": 0}
	for row, side in plans:
		price_list = price_list_for(doc, side)
		rate = flt(row.get(f"new_{side}_rate"))
		try:
			name, previous = write_price(
				item_code=row.item_code,
				stock_uom=row.stock_uom,
				price_list=price_list,
				rate=rate,
				existing_row=existing.get((row.item_code, price_list)),
			)
		except Exception as exc:
			# No savepoint: a submit is all-or-nothing. Letting this propagate means the
			# document is not submitted and not one price moved. Swallowing it per row is
			# the one outcome that must not be possible.
			frappe.throw(
				_("Row {0}: could not set the {1} price for {2}. {3}").format(
					row.idx,
					SIDE_LABELS[side].lower(),
					frappe.bold(row.item_code),
					frappe.utils.strip_html(str(exc)),
				)
			)

		# db_set because db_update() has already run by the time on_submit fires.
		row.db_set(
			{
				f"{side}_item_price": name,
				f"previous_{side}_rate": previous,
				f"applied_{side}_rate": rate,
			},
			update_modified=False,
		)
		counts[side] += 1

	doc.db_set(
		{
			"applied_on": now_datetime(),
			"selling_changes": counts["selling"],
			"buying_changes": counts["buying"],
		},
		update_modified=False,
	)


def guard_stock_price_cancel(doc) -> None:
	"""Refuse the cancel when restoring would overwrite someone else's change.

	Runs from `before_cancel`, so a refusal leaves the document exactly as it was.
	"""
	drifted: list[str] = []
	linked: list[str] = []
	first_item = None

	for row, side in planned_writes(doc):
		name = row.get(f"{side}_item_price")
		if not name:
			continue

		current = frappe.db.get_value("Item Price", name, "price_list_rate")
		if current is None:
			# The row was deleted. There is nothing left to restore and nothing to get
			# wrong, and blocking here would leave a document that can never be cancelled.
			continue

		applied = flt(row.get(f"applied_{side}_rate"))
		if abs(flt(current) - applied) >= RATE_TOLERANCE:
			first_item = first_item or row.item_code
			drifted.append(
				_("Row {0} {1} - {2}: this document set {3}, the price list now holds {4}").format(
					row.idx,
					SIDE_LABELS[side],
					frappe.bold(row.item_code),
					frappe.bold(frappe.format_value(applied, {"fieldtype": "Currency"})),
					frappe.bold(frappe.format_value(flt(current), {"fieldtype": "Currency"})),
				)
			)
			continue

		if row.get(f"{side}_action") == ACTION_CREATE:
			product = frappe.db.get_value("Product", {"item_price": name}, "name")
			if product:
				first_item = first_item or row.item_code
				linked.append(
					_("Row {0} {1} - {2}: Product {3} points at the price row this document created").format(
						row.idx, SIDE_LABELS[side], frappe.bold(row.item_code), frappe.bold(product)
					)
				)

	if not drifted and not linked:
		return

	lines = [
		_("Cannot cancel: the price of {0} has changed since this document set it.").format(
			frappe.bold(first_item)
		)
		if drifted
		else _("Cannot cancel: a price row created by this document is still in use."),
		"",
	]
	lines.extend(drifted)
	lines.extend(linked)
	lines.append("")
	lines.append(
		_(
			"Cancelling would overwrite those values. Set the differing Item Price rows back "
			"by hand first, or leave this document submitted and correct the prices with a new one."
		)
	)
	frappe.throw("<br>".join(lines), title=_("Cancel refused"))


def restore_stock_price(doc) -> None:
	"""Put the replaced prices back. Called from `on_cancel`, after the guard has passed."""
	plans = planned_writes(doc)
	if not plans:
		return

	require_doctype_permission("Item Price", "write")
	if any(row.get(f"{side}_action") == ACTION_CREATE for row, side in plans):
		# Undoing our own insert is gated on `create`, the same permission that authorised
		# it - not on `delete`. Stock Manager holds create but not delete on Item Price, and
		# this is not an arbitrary delete: it is one named row, recorded in a submitted
		# document. Whoever may create the row may un-create it.
		require_doctype_permission("Item Price", "create")

	for row, side in plans:
		name = row.get(f"{side}_item_price")
		if not name or not frappe.db.exists("Item Price", name):
			continue

		if row.get(f"{side}_action") == ACTION_CREATE:
			# Back to no price on this list. Writing 0 instead would leave the item
			# sellable for nothing.
			frappe.delete_doc("Item Price", name, ignore_permissions=True)
			continue

		item_price = frappe.get_doc("Item Price", name)
		item_price.price_list_rate = flt(row.get(f"previous_{side}_rate"))
		item_price.flags.ignore_permissions = True
		item_price.save()
