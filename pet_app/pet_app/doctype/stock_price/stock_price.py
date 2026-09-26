"""Stock Price: review and apply many selling and buying prices in one submitted document.

Each row shows what an item costs and sells for now, next to what you want it to be. Save
fills an Action column per side - Skipped / Unchanged / Update / Create - so what submit
will do is visible before it does it. Submit writes the prices and records what it
replaced; cancel puts them back.

BLANK AND 0 MEAN THE SAME THING HERE: leave that price alone. A Currency field cannot tell
the two apart, and the app already reads a 0 in a price sheet as "not priced yet" rather
than "worth nothing" (`utils/item_price_import`), because writing it would put the item on
sale for free. Rather than add a checkbox per side, the Action column says out loud which
prices will move. Setting a price to literally zero stays a deliberate one-off on the Item
Price form.

This document writes ``Item Price`` rows and nothing else. It does not touch
``Item.standard_rate``, ``Item.valuation_rate``, Stock Ledger Entries or Bins, so it
cannot change what stock is worth - only what it is bought and sold for on the price list.
See `docs/STOCK_PRICE_CONTRACT.md`.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr, flt

from pet_app.api.permissions import require_doctype_permission
from pet_app.api.response import ok
from pet_app.utils.item_price import (
	ACTION_CREATE,
	WRITING_ACTIONS,
	classify,
	has_uom_conversion,
	load_general_price_rows,
)
from pet_app.utils.price_list import get_buying_price_list, get_veterinary_selling_price_list
from pet_app.utils.stock_price import (
	SIDES,
	apply_stock_price,
	guard_stock_price_cancel,
	price_list_for,
	restore_stock_price,
)

DEFAULT_LIMIT = 500
MAX_LIMIT = 2000


class StockPrice(Document):
	def validate(self):
		self._set_price_lists()
		self._validate_rows()
		self._resolve_current_prices()
		self._plan_changes()
		self._require_at_least_one_change()
		self._set_status()

	def before_submit(self):
		self.status = "Submitted"

	def on_submit(self):
		apply_stock_price(self)

	def before_cancel(self):
		# Guard before db_update, so a refused cancel leaves the document untouched.
		guard_stock_price_cancel(self)
		self.status = "Cancelled"

	def on_cancel(self):
		restore_stock_price(self)

	def _set_price_lists(self):
		# Stored read-only on the document so an old record still says which lists it wrote
		# to, even if the site configuration later points somewhere else.
		self.selling_price_list = get_veterinary_selling_price_list()
		self.buying_price_list = get_buying_price_list()

		currencies = {
			cstr(frappe.db.get_value("Price List", price_list, "currency"))
			for price_list in (self.selling_price_list, self.buying_price_list)
		}
		if len(currencies) > 1:
			# One set of Currency fields cannot honestly show two currencies at once.
			frappe.throw(
				_("Price List {0} and {1} use different currencies.").format(
					frappe.bold(self.selling_price_list), frappe.bold(self.buying_price_list)
				)
			)

	def _validate_rows(self):
		if not self.items:
			frappe.throw(_("Add at least one item."))

		seen: dict = {}
		for row in self.items:
			item_code = cstr(row.item_code).strip()
			if not item_code:
				frappe.throw(_("Row {0}: select an item.").format(row.idx))

			if item_code in seen:
				# The second row would overwrite the first, and the first row's snapshot
				# would then record a price that was never live.
				frappe.throw(
					_("Item {0} is in rows {1} and {2}. Keep one row per item.").format(
						frappe.bold(item_code), seen[item_code], row.idx
					)
				)
			seen[item_code] = row.idx

			item = frappe.db.get_value(
				"Item",
				item_code,
				["name", "item_name", "stock_uom", "disabled", "has_variants"],
				as_dict=True,
			)
			if not item:
				frappe.throw(_("Row {0}: item {1} does not exist.").format(row.idx, frappe.bold(item_code)))
			if cint(item.has_variants):
				# ERPNext refuses an Item Price on a template. It would refuse at submit,
				# after the earlier rows had already been written.
				frappe.throw(
					_("Row {0}: {1} is a template item and cannot be priced. Use its variants.").format(
						row.idx, frappe.bold(item_code)
					)
				)

			# Set rather than trusted: a document built server-side never ran fetch_from,
			# and write_price needs stock_uom to be right.
			row.item_name = item.item_name
			row.stock_uom = item.stock_uom
			row.disabled = cint(item.disabled)

			for side in SIDES:
				if flt(row.get(f"new_{side}_rate")) < 0:
					frappe.throw(
						_("Row {0}: the new {1} price cannot be negative.").format(row.idx, side)
					)

	def _resolve_current_prices(self):
		# One query for the whole table, whatever its size.
		self._general_rows = load_general_price_rows(
			[row.item_code for row in self.items],
			[self.selling_price_list, self.buying_price_list],
			on_date=self.posting_date,
		)
		for row in self.items:
			for side in SIDES:
				existing = self._general_rows.get((row.item_code, price_list_for(self, side)))
				row.set(f"current_{side}_rate", flt(existing.price_list_rate) if existing else 0)
				# Empty means no price row exists at all - which a Currency 0 cannot say.
				row.set(f"{side}_item_price", existing.name if existing else None)
				row.set(
					f"{side}_multi_row",
					1 if existing and cint(getattr(existing, "candidate_count", 1)) > 1 else 0,
				)

		self._warn_about_multiple_price_rows()

	def _warn_about_multiple_price_rows(self):
		"""Say out loud when a row is one of several live prices for that item.

		Mostly per-UOM prices - a bag price and a kilo price. Every reader in this app
		collapses those to one, so this document does too, but a price changed without the
		user knowing a second one existed is how the other one silently goes stale.
		"""
		affected = [
			_("Row {0} {1}: {2} - editing {3}").format(
				row.idx,
				side.capitalize(),
				frappe.bold(row.item_code),
				row.get(f"{side}_item_price"),
			)
			for row in self.items
			for side in SIDES
			if cint(row.get(f"{side}_multi_row"))
		]
		if not affected:
			return

		shown = affected[:10]
		if len(affected) > len(shown):
			shown.append(_("...and {0} more.").format(len(affected) - len(shown)))
		frappe.msgprint(
			_("These items hold more than one price on the same list, usually one price per UOM. Only the linked row is read and written; the others are left alone.")
			+ "<br><br>"
			+ "<br>".join(shown),
			title=_("More than one price row"),
			indicator="orange",
		)

	def _plan_changes(self):
		for row in self.items:
			for side in SIDES:
				existing = self._general_rows.get((row.item_code, price_list_for(self, side)))
				action = classify(existing, row.get(f"new_{side}_rate"))
				row.set(f"{side}_action", action)

				if action == ACTION_CREATE and not has_uom_conversion(row.item_code, row.stock_uom):
					# ERPNext's validate_item would throw on this at submit. Throwing on the
					# draft means it is a correction, not a half-applied document.
					frappe.throw(
						_("Row {0}: UOM {1} is missing from the UOM Conversion table of {2}, so a new price row cannot be created.").format(
							row.idx, frappe.bold(row.stock_uom), frappe.bold(row.item_code)
						)
					)

	def _require_at_least_one_change(self):
		# Only while submitting. `_submit()` sets docstatus before saving, so this is the
		# submit pass - a draft full of unchanged rows must stay saveable.
		if self.docstatus != 1:
			return
		if any(
			row.get(f"{side}_action") in WRITING_ACTIONS for row in self.items for side in SIDES
		):
			return
		frappe.throw(
			_("Nothing to apply: every row would leave the current price unchanged. Enter at least one new price.")
		)

	def _set_status(self):
		if self.docstatus == 1:
			self.status = "Submitted"
		elif self.docstatus == 2:
			self.status = "Cancelled"
		else:
			self.status = "Draft"


@frappe.whitelist()
def get_items(
	item_group=None,
	brand=None,
	include_child_groups=1,
	search=None,
	include_disabled=0,
	unpriced_only=0,
	limit=DEFAULT_LIMIT,
):
	"""Items matching the filters, each with its current selling and buying price.

	Read-only. Prices for the whole batch are resolved in one query - the medication list
	endpoint resolves them one item at a time, which is exactly what not to copy here.
	"""
	require_doctype_permission("Item Price", "read")

	filters: dict = {"has_variants": 0}
	if not cint(include_disabled):
		filters["disabled"] = 0
	if brand:
		filters["brand"] = brand
	if item_group:
		filters["item_group"] = _item_group_filter(item_group, cint(include_child_groups))

	or_filters = None
	search = cstr(search).strip()
	if search:
		or_filters = [
			["Item", "item_code", "like", f"%{search}%"],
			["Item", "item_name", "like", f"%{search}%"],
		]

	limit = min(max(cint(limit) or DEFAULT_LIMIT, 1), MAX_LIMIT)
	rows = frappe.get_all(
		"Item",
		filters=filters,
		or_filters=or_filters,
		fields=["name as item_code", "item_name", "stock_uom", "disabled"],
		order_by="item_name asc",
		# One more than asked for, so "there are more" is a fact rather than a guess.
		limit_page_length=limit + 1,
	)
	truncated = len(rows) > limit
	rows = rows[:limit]

	selling_price_list = get_veterinary_selling_price_list()
	buying_price_list = get_buying_price_list()
	prices = load_general_price_rows(
		[row.item_code for row in rows], [selling_price_list, buying_price_list]
	)

	items = []
	for row in rows:
		selling = prices.get((row.item_code, selling_price_list))
		buying = prices.get((row.item_code, buying_price_list))
		if cint(unpriced_only) and selling:
			continue
		items.append(
			{
				"item_code": row.item_code,
				"item_name": row.item_name,
				"stock_uom": row.stock_uom,
				"disabled": cint(row.disabled),
				# None, never 0: an item with no price row is not an item priced at nothing.
				"current_selling_rate": flt(selling.price_list_rate) if selling else None,
				"current_buying_rate": flt(buying.price_list_rate) if buying else None,
				"selling_item_price": selling.name if selling else None,
				"buying_item_price": buying.name if buying else None,
			}
		)

	return ok(
		{"items": items},
		meta={
			"count": len(items),
			"truncated": truncated,
			"limit": limit,
			"selling_price_list": selling_price_list,
			"buying_price_list": buying_price_list,
		},
	)


def _item_group_filter(item_group: str, include_child_groups: int):
	if not include_child_groups:
		return item_group

	group = frappe.db.get_value("Item Group", item_group, ["lft", "rgt"], as_dict=True)
	if not group:
		frappe.throw(_("Item Group {0} does not exist.").format(frappe.bold(item_group)))

	descendants = frappe.get_all(
		"Item Group", filters={"lft": [">=", group.lft], "rgt": ["<=", group.rgt]}, pluck="name"
	)
	return ["in", descendants or [item_group]]
