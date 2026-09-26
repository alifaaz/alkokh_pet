"""Every medication's Item is allowed to go negative; nothing else is.

A dispense or a counter sale for a drug the ledger believes is at zero used to fail
outright. That refusal is correct for goods - a bag of food that is not on the shelf
cannot be sold - but wrong for medicine. Clinical stock drifts by its nature: a vial is
opened before its receipt is posted, a box is miscounted, a dose option starts deducting
against a bin nobody opening-balanced. None of those are a reason to stop treating an
animal, and a blocked dispense does not make the count correct - it just moves the error
somewhere nobody records.

SCOPED TO MEDICATIONS ON PURPOSE. `Stock Settings.allow_negative_stock` would do this in
one checkbox and is the wrong instrument: it lifts the floor for all ~2160 items, food and
accessories included, and ERPNext forbids it alongside Stock Reservation
(stock_settings.py:210-229). ERPNext's per-Item `allow_negative_stock` is resolved by the
same authority - `is_negative_stock_allowed()` in stock_ledger.py - so the narrow flag
costs nothing in fidelity. Do not "simplify" this into the global setting.

Written with `frappe.db.set_value`, not `item.save()`. This is a plain Check with no
save-time side effects, and a full Item validate across a couple hundred legacy rows can
trip on unrelated stale data and abort the whole migrate for a checkbox.

Backfill only. `Medication._sync_item_defaults` asserts the same flag on every save, so
medications created after this patch never need it, and this never needs running twice.
Medications whose `linked_item` points at an Item that no longer exists (disabled TEST_*
rows) fall out of the Item filter on their own - there is nothing to set.
"""

from __future__ import annotations

import frappe


FIELDNAME = "allow_negative_stock"


def execute():
	if not frappe.db.exists("DocType", "Medication"):
		return
	if not frappe.db.has_column("Item", FIELDNAME):
		frappe.log_error(
			title="MEDICATION_ITEM_ALLOW_NEGATIVE_STOCK_MISSING",
			message=f"Item.{FIELDNAME} does not exist; medication items were left unchanged.",
		)
		return

	linked_items = {code for code in frappe.get_all("Medication", pluck="linked_item") if code}
	if not linked_items:
		return

	# Existing Items only, and only the ones not already set - the flag has been ticked by
	# hand on part of the catalogue, and rewriting those would churn `modified` for nothing.
	pending = frappe.get_all(
		"Item",
		filters={"name": ["in", sorted(linked_items)], FIELDNAME: 0},
		pluck="name",
	)
	for item_code in pending:
		frappe.db.set_value("Item", item_code, FIELDNAME, 1)
