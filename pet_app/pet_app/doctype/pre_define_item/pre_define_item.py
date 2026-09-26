# Copyright (c) 2026, solvers and contributors
# For license information, please see license.txt

"""Pre Define Item - staging row for an Item that does not exist yet.

Holds the five columns of the intake sheet (Arabic name, English name, code,
Arabic group, English category) plus the handful of defaults an Item needs.
``migrate()`` turns one row into a real Item Group (if missing) and a real Item,
then locks the row. Bulk migration goes through
``pet_app.utils.staging_migration.migrate_each`` so one bad row never blocks the rest.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr, flt

from pet_app.api.permissions import require_doctype_permission
from pet_app.pet_app.doctype.medication.medication import _ensure_uom
from pet_app.utils.item_taxonomy import ensure_leaf_chain, is_scrap_row, leaf_stock_uom, resolve_leaf
from pet_app.utils.staging_migration import migrate_each

DEFAULT_PARENT_ITEM_GROUP = "Pet Supplies"
DEFAULT_STOCK_UOM = "Unit"
MAX_ITEM_CODE_LENGTH = 140

TEXT_FIELDS = ("arabic_name", "english_name", "item_code", "group_arabic", "category", "barcode", "description")


class PreDefineItem(Document):
	def validate(self):
		self._strip_text_fields()
		self._refuse_edit_when_migrated()
		self._validate_unique_item_code()
		self._validate_effective_code()
		self._reset_error_status()

	# --- validation -------------------------------------------------------

	def _strip_text_fields(self):
		for fieldname in TEXT_FIELDS:
			value = self.get(fieldname)
			if value is not None:
				self.set(fieldname, cstr(value).strip() or None)

	def _refuse_edit_when_migrated(self):
		"""A migrated row is the audit trail of what became the Item - it stays frozen."""
		if self.is_new() or self.flags.from_migration:
			return
		before = self.get_doc_before_save()
		if before and before.status == "Migrated":
			frappe.throw(
				_("Pre Define Item {0} is already migrated to Item {1} and can no longer be edited.").format(
					frappe.bold(self.name), frappe.bold(before.item or "")
				)
			)

	def _validate_unique_item_code(self):
		if not self.item_code:
			return
		other = frappe.db.get_value(
			"Pre Define Item", {"item_code": self.item_code, "name": ("!=", self.name)}, "name"
		)
		if other:
			frappe.throw(
				_("Item Code {0} is already used by Pre Define Item {1}.").format(
					frappe.bold(self.item_code), frappe.bold(other)
				),
				frappe.DuplicateEntryError,
			)

	def effective_code(self) -> str:
		"""The name the real Item will get: the sheet code, else the English name, else the Arabic name."""
		return cstr(self.item_code or self.english_name or self.arabic_name).strip()

	def scan_code(self) -> str:
		"""The value that goes into Item.custom_barcode.

		The intake sheet's `barcode` column arrived empty on all 1710 rows - the printed
		code is in `item_code`. `barcode` stays the explicit override so an operator can
		record a scan key that differs from the sheet code.
		"""
		return cstr(self.barcode or self.item_code).strip()

	def _validate_effective_code(self):
		code = self.effective_code()
		if not code:
			frappe.throw(_("Enter at least one of Item Code, English Name or Arabic Name."))
		if len(code) > MAX_ITEM_CODE_LENGTH:
			frappe.throw(
				_("Item Code {0} is longer than {1} characters and cannot become an Item name.").format(
					frappe.bold(code), MAX_ITEM_CODE_LENGTH
				)
			)

	def _reset_error_status(self):
		"""Any manual save of an Error row is a retry request: put it back to Draft."""
		if self.is_new() or self.flags.from_migration:
			return
		before = self.get_doc_before_save()
		if before and before.status == "Error":
			self.status = "Draft"
			self.migration_error = None

	# --- migration --------------------------------------------------------

	def migrate(self) -> str:
		"""Create (or link) the real Item Group and Item for this row. Returns the Item name.

		Idempotent: a row already migrated to an existing Item is a no-op; an Item whose
		name equals this row's effective code is linked rather than duplicated.
		"""
		require_doctype_permission("Item", "create")
		require_doctype_permission("Item Group", "create")

		if self.status == "Migrated" and self.item and frappe.db.exists("Item", self.item):
			return self.item

		item_group = self._ensure_item_group()
		item_code = self._ensure_item(item_group)

		self.flags.from_migration = True
		self.item_group = item_group
		self.item = item_code
		self.status = "Migrated"
		self.migration_error = None
		self.save()
		return item_code

	def _ensure_item_group(self) -> str:
		"""The taxonomy leaf for a mapped sheet category, else the legacy free-text group.

		The mapped path builds Store > <family> > <leaf> and puts the accounting on the
		leaf, because get_item_group_defaults reads the item's own group and never its
		ancestors. An unmapped category falls through to the original behaviour so a new
		sheet value fails on its own row instead of landing somewhere plausible and wrong.
		"""
		leaf = resolve_leaf(self.category, self.arabic_name)
		if leaf:
			return ensure_leaf_chain(leaf)
		return self._ensure_freetext_item_group()

	def _ensure_freetext_item_group(self) -> str:
		group_name = cstr(self.category or self.group_arabic).strip()
		if not group_name:
			frappe.throw(_("Pre Define Item {0} has no Category or Group, so no Item Group can be resolved.").format(frappe.bold(self.name)))

		existing = frappe.db.get_value("Item Group", group_name, ["name", "is_group", "arabic_name"], as_dict=True)
		if existing:
			if cint(existing.is_group):
				frappe.throw(
					_("Item Group {0} is a group node and cannot hold items. Use a leaf category name or change Parent Item Group.").format(
						frappe.bold(existing.name)
					)
				)
			if self.group_arabic and not existing.arabic_name:
				frappe.db.set_value("Item Group", existing.name, "arabic_name", self.group_arabic)
			return existing.name

		parent = self.parent_item_group or DEFAULT_PARENT_ITEM_GROUP
		parent_row = frappe.db.get_value("Item Group", parent, ["name", "is_group"], as_dict=True)
		if not parent_row:
			frappe.throw(_("Parent Item Group {0} does not exist.").format(frappe.bold(parent)))
		if not cint(parent_row.is_group):
			frappe.throw(_("Parent Item Group {0} is not a group node.").format(frappe.bold(parent)))

		item_group = frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": group_name,
				"parent_item_group": parent_row.name,
				"is_group": 0,
				"arabic_name": self.group_arabic,
			}
		)
		item_group.flags.ignore_permissions = True  # checked above via require_doctype_permission
		item_group.insert()
		return item_group.name

	def _ensure_item(self, item_group: str) -> str:
		code = self.effective_code()

		scan = self.scan_code()

		existing = frappe.db.get_value("Item", code, ["name", "arabic_name", "custom_barcode"], as_dict=True)
		if existing:
			if self.arabic_name and not existing.arabic_name:
				frappe.db.set_value("Item", existing.name, "arabic_name", self.arabic_name)
			# db.set_value bypasses before_validate_item_barcode, so the value must already be
			# stripped - scan_code() strips it. The exists() check keeps the UNIQUE index from
			# raising an opaque DuplicateEntryError onto the staging row.
			if scan and not existing.custom_barcode and not frappe.db.exists("Item", {"custom_barcode": scan}):
				frappe.db.set_value("Item", existing.name, "custom_barcode", scan)
			return existing.name

		# Retail stock is counted in Unit; a few leaves are sold by weight instead.
		stock_uom = leaf_stock_uom(item_group) or self.stock_uom or DEFAULT_STOCK_UOM
		_ensure_uom(stock_uom)

		item = frappe.new_doc("Item")
		item.item_code = code
		item.item_name = self.english_name or self.arabic_name
		item.arabic_name = self.arabic_name
		item.item_group = item_group
		item.stock_uom = stock_uom
		item.is_stock_item = 1
		item.is_sales_item = 1
		item.is_purchase_item = 1
		item.description = self.description or item.item_name
		item.standard_rate = flt(self.standard_rate)
		item.valuation_rate = flt(self.valuation_rate)
		# تلف rows are damage/waste bookkeeping, not sellable stock. They still get an Item so
		# a scanned code resolves, but disabled keeps them off invoices.
		item.disabled = 1 if is_scrap_row(self.arabic_name) else cint(self.disabled)
		if scan:
			# custom_barcode carries a unique index; never write an empty string into it.
			item.custom_barcode = scan
		item.flags.ignore_permissions = True  # checked above via require_doctype_permission
		item.insert()
		return item.name



# POST-only: the framework default of GET/POST/PUT/DELETE lets a GET reach the body,
# insert real Items, and return ok - then Frappe rolls the request back on the way out
# because it only commits for unsafe methods (frappe/app.py: UNSAFE_HTTP_METHODS). The
# caller would be told Items were created that no longer exist anywhere. See the same
# fix and its rationale at pet_app/api/item_barcode.py:92-97.
@frappe.whitelist(methods=["POST"])
def migrate_pre_define_items(names):
	"""Migrate the given Pre Define Items (list or JSON list of names) to real Items."""
	frappe.has_permission("Pre Define Item", "write", throw=True)
	return migrate_each("Pre Define Item", names)
