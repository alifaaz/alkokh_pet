# Copyright (c) 2026, solvers and contributors
# For license information, please see license.txt

"""Pre Define Stock Entry - a Stock Entry captured before its Items exist.

Mirrors the real Stock Entry (same purposes, same warehouse rules, same row
fieldnames) but rows point at Pre Define Item. ``migrate()`` first migrates any
row's Pre Define Item that is not yet an Item, then inserts a **draft** Stock
Entry copying fields by name. Submitting the real entry stays a human decision.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr, flt

from pet_app.api.permissions import require_doctype_permission
from pet_app.utils.staging_migration import migrate_each

REAL_NAMING_SERIES = "MAT-STE-.YYYY.-"

PARENT_FIELDS = (
	"stock_entry_type",
	"purpose",
	"company",
	"posting_date",
	"posting_time",
	"from_warehouse",
	"to_warehouse",
	"remarks",
)
ROW_FIELDS = ("item_code", "qty", "uom", "conversion_factor", "s_warehouse", "t_warehouse")

SOURCE_ONLY_PURPOSES = {
	"Material Issue",
	"Material Consumption for Manufacture",
	"Send to Subcontractor",
	"Subcontracting Delivery",
}
TARGET_ONLY_PURPOSES = {"Material Receipt"}
BOTH_WAREHOUSE_PURPOSES = {"Material Transfer", "Material Transfer for Manufacture"}


class PreDefineStockEntry(Document):
	def validate(self):
		self._refuse_edit_when_migrated()
		self._set_purpose()
		self._set_company()
		self._validate_rows()
		self._reset_error_status()

	# --- validation -------------------------------------------------------

	def _refuse_edit_when_migrated(self):
		if self.is_new() or self.flags.from_migration:
			return
		before = self.get_doc_before_save()
		if before and before.status == "Migrated":
			frappe.throw(
				_("Pre Define Stock Entry {0} is already migrated to Stock Entry {1} and can no longer be edited.").format(
					frappe.bold(self.name), frappe.bold(before.stock_entry or "")
				)
			)

	def _set_purpose(self):
		purpose = frappe.db.get_value("Stock Entry Type", self.stock_entry_type, "purpose")
		if not purpose:
			frappe.throw(_("Stock Entry Type {0} has no Purpose.").format(frappe.bold(self.stock_entry_type)))
		self.purpose = purpose

	def _set_company(self):
		if not self.company:
			self.company = frappe.defaults.get_global_default("company")
		if not self.company:
			frappe.throw(_("Company is required."))

	def _validate_rows(self):
		if not self.items:
			frappe.throw(_("Add at least one item row."))

		warehouse_cache: dict[str, dict] = {}
		for row in self.items:
			label = _("Row {0}").format(row.idx)

			if flt(row.qty) <= 0:
				frappe.throw(_("{0}: Quantity must be greater than zero.").format(label))
			if not flt(row.conversion_factor):
				row.conversion_factor = 1
			if flt(row.conversion_factor) <= 0:
				frappe.throw(_("{0}: Conversion Factor must be greater than zero.").format(label))
			if not row.uom:
				row.uom = frappe.db.get_value("Pre Define Item", row.pre_item, "stock_uom")
			if not row.uom:
				frappe.throw(_("{0}: UOM is required.").format(label))

			row.s_warehouse = row.s_warehouse or self.from_warehouse
			row.t_warehouse = row.t_warehouse or self.to_warehouse
			self._apply_purpose_rule(row, label)

			for warehouse in (row.s_warehouse, row.t_warehouse):
				if warehouse:
					self._validate_warehouse(warehouse, warehouse_cache)

	def _apply_purpose_rule(self, row, label: str):
		purpose = self.purpose
		if purpose in TARGET_ONLY_PURPOSES:
			if not row.t_warehouse:
				frappe.throw(_("{0}: Target Warehouse is required for {1}.").format(label, purpose))
			row.s_warehouse = None
		elif purpose in SOURCE_ONLY_PURPOSES:
			if not row.s_warehouse:
				frappe.throw(_("{0}: Source Warehouse is required for {1}.").format(label, purpose))
			row.t_warehouse = None
		elif purpose in BOTH_WAREHOUSE_PURPOSES:
			if not (row.s_warehouse and row.t_warehouse):
				frappe.throw(_("{0}: Source and Target Warehouse are both required for {1}.").format(label, purpose))
			if row.s_warehouse == row.t_warehouse:
				frappe.throw(_("{0}: Source and Target Warehouse cannot be the same.").format(label))
		elif not (row.s_warehouse or row.t_warehouse):
			frappe.throw(_("{0}: Enter a Source or Target Warehouse.").format(label))

	def _validate_warehouse(self, warehouse: str, cache: dict[str, dict]):
		# Same guard as pet_app.utils.medication_stock._post_stock_entry: only a leaf
		# warehouse of this company can hold stock.
		if warehouse not in cache:
			cache[warehouse] = (
				frappe.db.get_value("Warehouse", warehouse, ["is_group", "company"], as_dict=True) or {}
			)
		row = cache[warehouse]
		if not row:
			frappe.throw(_("Warehouse {0} does not exist.").format(frappe.bold(warehouse)))
		if cint(row.get("is_group")):
			frappe.throw(
				_("Warehouse {0} is a group warehouse and cannot hold stock. Choose a leaf warehouse.").format(
					frappe.bold(warehouse)
				)
			)
		if row.get("company") and row.get("company") != self.company:
			frappe.throw(
				_("Warehouse {0} belongs to {1}, not {2}.").format(
					frappe.bold(warehouse), frappe.bold(row.get("company")), frappe.bold(self.company)
				)
			)

	def _reset_error_status(self):
		if self.is_new() or self.flags.from_migration:
			return
		before = self.get_doc_before_save()
		if before and before.status == "Error":
			self.status = "Draft"
			self.migration_error = None

	# --- migration --------------------------------------------------------

	def migrate(self) -> str:
		"""Create the real (draft) Stock Entry for this document. Returns its name.

		Rows whose Pre Define Item is not yet migrated are migrated first, inside the
		caller's savepoint, so a failing item fails this entry as a whole.
		"""
		require_doctype_permission("Stock Entry", "create")

		if self.status == "Migrated" and self.stock_entry and frappe.db.exists("Stock Entry", self.stock_entry):
			return self.stock_entry

		pre_items = self._migrate_pre_items()
		entry = self._build_stock_entry(pre_items)
		entry.flags.ignore_permissions = True  # checked above via require_doctype_permission
		entry.insert()

		self.flags.from_migration = True
		self.stock_entry = entry.name
		self.status = "Migrated"
		self.migration_error = None
		self.save()
		return entry.name

	def _migrate_pre_items(self) -> dict:
		pre_items: dict[str, Document] = {}
		for row in self.items:
			pre_item = pre_items.get(row.pre_item)
			if pre_item is None:
				pre_item = frappe.get_doc("Pre Define Item", row.pre_item)
				pre_items[row.pre_item] = pre_item
			if pre_item.status != "Migrated" or not pre_item.item or not frappe.db.exists("Item", pre_item.item):
				pre_item.migrate()
			row.item_code = pre_item.item
		return pre_items

	def _build_stock_entry(self, pre_items: dict):
		entry = frappe.new_doc("Stock Entry")
		entry.naming_series = REAL_NAMING_SERIES
		for fieldname in PARENT_FIELDS:
			entry.set(fieldname, self.get(fieldname))
		# Without this ERPNext overwrites posting_date/time with "now" on validate.
		entry.set_posting_time = 1

		batch_notes = []
		for row in self.items:
			pre_item = pre_items[row.pre_item]
			stock_uom = frappe.db.get_value("Item", row.item_code, "stock_uom")
			rate = flt(row.basic_rate) or flt(pre_item.valuation_rate)
			se_row = {fieldname: row.get(fieldname) for fieldname in ROW_FIELDS}
			se_row.update(
				{
					"stock_uom": stock_uom,
					"basic_rate": rate,
					# A draft can exist without a rate; submitting would otherwise fail on
					# zero valuation, so let the reviewer decide.
					"allow_zero_valuation_rate": 0 if rate else 1,
					# Keeps the captured rate; otherwise ERPNext recomputes incoming rows.
					"set_basic_rate_manually": 1 if rate else 0,
				}
			)
			entry.append("items", se_row)

			if row.batch_no or row.expiry_date:
				batch_notes.append(
					_("Row {0} {1}: batch {2}, expiry {3}").format(
						row.idx, row.item_code, row.batch_no or "-", cstr(row.expiry_date) or "-"
					)
				)

		if batch_notes:
			header = _("Batch details from Pre Define Stock Entry {0}:").format(self.name)
			entry.remarks = "\n".join(part for part in [cstr(entry.remarks).strip(), header, *batch_notes] if part)

		return entry



# POST-only: the framework default of GET/POST/PUT/DELETE lets a GET reach the body,
# insert draft Stock Entries, and return ok - then Frappe rolls the request back on the
# way out because it only commits for unsafe methods (frappe/app.py: UNSAFE_HTTP_METHODS).
# The caller would be told entries were created that no longer exist anywhere. See the
# same fix and its rationale at pet_app/api/item_barcode.py:92-97.
@frappe.whitelist(methods=["POST"])
def migrate_pre_define_stock_entries(names):
	"""Migrate the given Pre Define Stock Entries (list or JSON list of names) to draft Stock Entries."""
	frappe.has_permission("Pre Define Stock Entry", "write", throw=True)
	return migrate_each("Pre Define Stock Entry", names)
