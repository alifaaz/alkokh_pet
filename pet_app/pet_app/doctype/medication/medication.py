# Copyright (c) 2026, solvers and contributors
# For license information, please see license.txt

from __future__ import annotations

from typing import Iterable

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr, flt

from pet_app.api.permissions import require_doctype_permission, require_restriction_value
from pet_app.utils.price_list import get_veterinary_selling_price_list


DOSE_OPTION_PLACEHOLDER_UOM = "Nos"
DOSE_OPTION_PLACEHOLDER_UOM_PREFERENCES = ("Nos", "ml", "Unit", "Bottle", "Tablet")


class Medication(Document):
	def validate(self):
		self._normalize_fields()
		self._validate_code_uniqueness()
		self._validate_name_uniqueness()
		self._validate_default_duration_days()
		self._resolve_linked_item()
		self._validate_linked_item()
		self._hydrate_item_fields_from_linked_item()
		self._validate_item_group()
		self._validate_default_warehouse()
		self._sync_item_defaults()
		self._refresh_usage_counters()

	def _normalize_fields(self):
		self.medication_name = cstr(self.medication_name).strip()
		self.code = cstr(self.code).strip() or None
		self.dosage_form_or_unit = cstr(self.dosage_form_or_unit).strip()
		self.default_dispense_uom = cstr(self.default_dispense_uom).strip()
		self.item_group = cstr(self.item_group).strip()
		self.strength = cstr(self.strength).strip()
		self.default_dosage = cstr(self.default_dosage).strip()
		self.default_frequency = cstr(self.default_frequency).strip()
		self.default_instructions = cstr(self.default_instructions).strip()
		self.default_price = flt(self.default_price) if self.default_price not in (None, "") else None
		self.default_conversion_factor = flt(self.default_conversion_factor) if self.default_conversion_factor not in (None, "") else None
		self.total_dispensed_amount = flt(self.total_dispensed_amount)
		for row in self.get("dose_options") or []:
			row.label = cstr(row.get("label")).strip()
			row.qty = flt(row.get("qty"))

		if not self.medication_name:
			frappe.throw(_("Medication Name is required."))

	def _validate_code_uniqueness(self):
		if not self.code:
			return

		filters = [["Medication", "code", "=", self.code]]
		if not self.is_new():
			filters.append(["Medication", "name", "!=", self.name])

		if frappe.get_all("Medication", filters=filters, pluck="name", limit=1):
			frappe.throw(_("Medication Code {0} is already in use.").format(frappe.bold(self.code)))

	def _validate_name_uniqueness(self):
		if frappe.db.exists("Medication", {"medication_name": self.medication_name, "name": ["!=", self.name]}):
			frappe.throw(_("Medication Name {0} is already in use.").format(frappe.bold(self.medication_name)))

	def _validate_default_duration_days(self):
		if self.default_duration_days in (None, ""):
			return

		self.default_duration_days = cint(self.default_duration_days)
		if self.default_duration_days < 0:
			frappe.throw(_("Default Duration Days must be zero or greater."))

	def _resolve_linked_item(self):
		if self.linked_item:
			return

		match = find_matching_item(self.medication_name, code=self.code)
		self.linked_item = match or create_item_for_medication(self)

	def _validate_linked_item(self):
		if not self.linked_item:
			return

		item = frappe.db.get_value(
			"Item",
			self.linked_item,
			["name", "disabled"],
			as_dict=True,
		)
		if not item:
			frappe.throw(_("Linked Item {0} does not exist.").format(frappe.bold(self.linked_item)))
		if cint(item.disabled):
			frappe.throw(_("Linked Item {0} is disabled.").format(frappe.bold(self.linked_item)))

	def _hydrate_item_fields_from_linked_item(self):
		if not self.linked_item:
			return

		item = frappe.db.get_value("Item", self.linked_item, ["stock_uom", "item_group"], as_dict=True)
		if not item:
			return

		if item.stock_uom:
			self.dosage_form_or_unit = item.stock_uom
		if not self.item_group and item.item_group:
			self.item_group = item.item_group

	def _validate_item_group(self):
		if not self.item_group:
			self.item_group = _get_medication_item_group()

		if not self.item_group:
			frappe.throw(_("Item Group is required."))

		item_group = frappe.db.get_value(
			"Item Group",
			self.item_group,
			["name", "is_group"],
			as_dict=True,
		)
		if not item_group:
			frappe.throw(_("Item Group {0} does not exist.").format(frappe.bold(self.item_group)))
		if cint(item_group.is_group):
			frappe.throw(_("Item Group {0} must be a leaf item group.").format(frappe.bold(self.item_group)))

	def _validate_default_warehouse(self):
		if not self.default_warehouse:
			return
		require_restriction_value("warehouse", self.default_warehouse)

		warehouse = frappe.db.get_value(
			"Warehouse",
			self.default_warehouse,
			["name", "disabled", "is_group"],
			as_dict=True,
		)
		if not warehouse:
			frappe.throw(_("Default Warehouse {0} does not exist.").format(frappe.bold(self.default_warehouse)))
		if cint(warehouse.disabled):
			frappe.throw(_("Default Warehouse {0} is disabled.").format(frappe.bold(self.default_warehouse)))
		if cint(warehouse.is_group):
			frappe.throw(_("Default Warehouse {0} must be a leaf warehouse.").format(frappe.bold(self.default_warehouse)))

	def _sync_item_defaults(self):
		if not self.linked_item:
			return

		item = frappe.get_doc("Item", self.linked_item)
		changed = False

		if item.item_name != self.medication_name:
			item.item_name = self.medication_name
			changed = True

		description_parts = [self.strength, self.default_instructions]
		description = "\n".join(part for part in description_parts if part)
		if description and item.description != description:
			item.description = description
			changed = True

		if self.default_price is not None and flt(item.standard_rate) != flt(self.default_price):
			item.standard_rate = flt(self.default_price)
			changed = True

		stock_uom = item.stock_uom or self.dosage_form_or_unit or "Nos"
		_ensure_uom(stock_uom)
		if not item.stock_uom:
			item.stock_uom = stock_uom
			changed = True
		if self.dosage_form_or_unit != stock_uom:
			self.dosage_form_or_unit = stock_uom

		if self.default_dispense_uom:
			_ensure_uom(self.default_dispense_uom)
			conversion_factor = 1 if self.default_dispense_uom == stock_uom else flt(self.default_conversion_factor)
			if conversion_factor <= 0:
				frappe.throw(_("Default Conversion Factor is required when Default Dispense UOM differs from Stock UOM."))
			changed = _upsert_item_uom_conversion(item, self.default_dispense_uom, conversion_factor) or changed

		self._validate_dose_options(stock_uom)

		if self.item_group and item.item_group != self.item_group:
			item.item_group = self.item_group
			changed = True

		if not cint(item.is_stock_item):
			item.is_stock_item = 1
			changed = True

		if not cint(item.is_sales_item):
			item.is_sales_item = 1
			changed = True

		if not cint(item.is_purchase_item):
			item.is_purchase_item = 1
			changed = True

		if changed:
			require_doctype_permission("Item", "write")
			item.flags.ignore_permissions = True
			item.save()

		if self.default_price is None:
			self.default_price = get_item_effective_rate(self.linked_item)
		else:
			upsert_item_price(self.linked_item, flt(self.default_price))

	def _refresh_usage_counters(self):
		counters = get_medication_usage_counters(self.linked_item)
		self.reference_count = counters["reference_count"]
		self.given_count = counters["given_count"]
		self.total_dispensed_amount = counters["total_dispensed_amount"]


	def _validate_dose_options(self, stock_uom: str):
		active_rows = [row for row in self.get("dose_options") or [] if not cint(row.get("disabled"))]
		if not active_rows:
			return

		resolve_dose_option_placeholder_uom(stock_uom)

		for row in active_rows:
			label = cstr(row.get("label")).strip() or _("Row {0}").format(row.idx)
			qty = flt(row.get("qty"))
			if qty <= 0:
				frappe.throw(_("Dose Option {0} requires Stock Deduction Qty greater than zero.").format(frappe.bold(label)))
			_validate_fractional_stock_uom(stock_uom, qty, label)


def find_matching_item(medication_name: str, code: str | None = None) -> str | None:
	medication_name = cstr(medication_name).strip()
	code = cstr(code).strip()

	if not medication_name:
		return None

	if frappe.db.exists("Item", medication_name):
		return medication_name

	exact_name_match = frappe.db.sql(
		"""
		select name
		from `tabItem`
		where disabled = 0 and lower(item_name) = lower(%s)
		order by modified desc
		limit 1
		""",
		(medication_name,),
	)
	if exact_name_match:
		return exact_name_match[0][0]

	if code and frappe.db.exists("Item", code):
		item_disabled = frappe.db.get_value("Item", code, "disabled")
		if not cint(item_disabled):
			return code

	return None


def create_item_for_medication(doc: Medication) -> str:
	require_doctype_permission("Item", "create")
	stock_uom = doc.dosage_form_or_unit or "Nos"
	_ensure_uom(stock_uom)

	item = frappe.new_doc("Item")
	item.item_code = doc.medication_name
	item.item_name = doc.medication_name
	item.item_group = doc.item_group or _get_medication_item_group()
	item.stock_uom = stock_uom
	item.is_stock_item = 1
	item.is_sales_item = 1
	item.is_purchase_item = 1
	item.description = "\n".join(part for part in [doc.strength, doc.default_instructions] if part)
	item.standard_rate = flt(doc.default_price) if doc.default_price is not None else 0
	item.flags.ignore_permissions = True
	item.insert()

	if doc.default_price is not None:
		upsert_item_price(item.name, flt(doc.default_price))

	return item.name


def _upsert_item_uom_conversion(item, uom: str, conversion_factor: float) -> bool:
	for row in item.get("uoms") or []:
		if row.uom == uom:
			if flt(row.conversion_factor) != flt(conversion_factor):
				row.conversion_factor = flt(conversion_factor)
				return True
			return False

	item.append("uoms", {"uom": uom, "conversion_factor": flt(conversion_factor)})
	return True


def _validate_fractional_stock_uom(stock_uom: str, conversion_factor: float, label: str):
	if not stock_uom or abs(flt(conversion_factor) - int(flt(conversion_factor))) <= 0.0000001:
		return
	if cint(frappe.db.get_value("UOM", stock_uom, "must_be_whole_number")):
		frappe.throw(
			_(
				"Dose Option {0} deducts a fractional stock quantity ({1}) but Stock UOM {2} is marked as whole-number only."
			).format(frappe.bold(label), frappe.bold(flt(conversion_factor)), frappe.bold(stock_uom))
		)



def resolve_dose_option_placeholder_uom(stock_uom: str | None) -> str:
	stock_uom = cstr(stock_uom).strip()
	for uom in DOSE_OPTION_PLACEHOLDER_UOM_PREFERENCES:
		if uom != stock_uom and frappe.db.exists("UOM", uom):
			return uom

	filters = {"name": ["!=", stock_uom]} if stock_uom else {}
	fallback = frappe.get_all("UOM", filters=filters, fields=["name"], order_by="name asc", limit=1)
	if fallback:
		return fallback[0].name

	frappe.throw(
		_(
			"Dose options require at least one existing transaction UOM different from Stock UOM {0}. "
			"Create or enable another UOM before using dose options."
		).format(frappe.bold(stock_uom or _("Unknown")))
	)


def _ensure_uom(uom: str):
	uom = cstr(uom).strip()
	if not uom or frappe.db.exists("UOM", uom):
		return
	frappe.get_doc({"doctype": "UOM", "uom_name": uom}).insert(ignore_permissions=True)


def _get_medication_item_group() -> str:
	for group in ("Pharmacy", "Medicines", "أدوية بيطرية", "Services"):
		if frappe.db.exists("Item Group", group) and not cint(frappe.db.get_value("Item Group", group, "is_group")):
			return group

	leaf_groups = frappe.get_all("Item Group", filters={"is_group": 0}, pluck="name", order_by="name asc", limit=1)
	if leaf_groups:
		return leaf_groups[0]

	return "All Item Groups"


def get_item_effective_rate(item_code: str) -> float:
	if not item_code:
		return 0.0

	price_list = _get_medication_price_list()
	rate = None
	if price_list:
		rate = frappe.db.get_value(
			"Item Price",
			{"item_code": item_code, "price_list": price_list, "selling": 1},
			"price_list_rate",
		)
	if rate is None:
		rate = frappe.db.get_value("Item", item_code, "standard_rate")
	return flt(rate)


def upsert_item_price(item_code: str, rate: float):
	if not item_code:
		return

	price_list = _get_medication_price_list()
	if not price_list:
		return

	existing = frappe.db.get_value(
		"Item Price",
		{"item_code": item_code, "price_list": price_list, "selling": 1},
		"name",
	)

	if existing:
		frappe.db.set_value("Item Price", existing, "uom", None, update_modified=False)

	item_price = frappe.get_doc("Item Price", existing) if existing else frappe.new_doc("Item Price")
	item_price.item_code = item_code
	item_price.price_list = price_list
	item_price.selling = 1
	item_price.price_list_rate = flt(rate)
	item_price.uom = None
	item_price.flags.ignore_permissions = True
	item_price.save()


def get_medication_usage_counters(linked_item: str | None) -> dict[str, float]:
	if not linked_item:
		return {
			"reference_count": 0,
			"given_count": 0,
			"total_dispensed_amount": 0.0,
		}

	result = frappe.db.sql(
		"""
		select
			count(*) as reference_count,
			count(*) as given_count,
			coalesce(sum(case when coalesce(vmi.dispensed_qty, 0) > 0 then vmi.dispensed_qty else coalesce(vmi.qty, 0) end), 0) as total_dispensed_amount
		from `tabVet Visit Medication Item` vmi
		inner join `tabVet Visit` vv on vv.name = vmi.parent
		where vmi.parenttype = 'Vet Visit'
			and vmi.medication_item = %s
			and coalesce(vv.status, '') != 'Cancelled'
		""",
		(linked_item,),
		as_dict=True,
	)
	row = result[0] if result else {}
	return {
		"reference_count": cint(row.get("reference_count")),
		"given_count": cint(row.get("given_count")),
		"total_dispensed_amount": flt(row.get("total_dispensed_amount")),
	}


def _get_medication_price_list() -> str | None:
	return get_veterinary_selling_price_list()


def refresh_medication_usage_counters(medication_names: Iterable[str] | None = None, linked_items: Iterable[str] | None = None):
	names = {cstr(name).strip() for name in medication_names or [] if cstr(name).strip()}
	items = {cstr(item).strip() for item in linked_items or [] if cstr(item).strip()}

	if items:
		for medication_name in frappe.get_all("Medication", filters={"linked_item": ["in", list(items)]}, pluck="name"):
			names.add(medication_name)

	for medication_name in names:
		doc = frappe.get_doc("Medication", medication_name)
		counters = get_medication_usage_counters(doc.linked_item)
		frappe.db.set_value(
			"Medication",
			doc.name,
			{
				"reference_count": counters["reference_count"],
				"given_count": counters["given_count"],
				"total_dispensed_amount": counters["total_dispensed_amount"],
			},
			update_modified=False,
		)


def sync_medication_counters_for_visit(doc, method=None):
	linked_items = {cstr(row.medication_item).strip() for row in doc.get("prescribed_medications") or [] if row.medication_item}

	previous_doc = doc.get_doc_before_save()
	if previous_doc:
		linked_items.update(
			cstr(row.medication_item).strip()
			for row in previous_doc.get("prescribed_medications") or []
			if row.medication_item
		)

	refresh_medication_usage_counters(linked_items=linked_items)
