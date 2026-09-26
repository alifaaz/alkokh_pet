"""Medication invoice conversions and legacy Stock Entry return helpers.

Current visit/boarding dispensing records clinical state only. Sales Invoice
submission owns stock movement. Dose options supply stock units per billed dose;
that conversion is stored on the invoice, keeping its price and dose count intact.

The legacy issue/receipt helpers remain for historical callers and recorded returns.
No current visit, boarding or care-service completion path calls the issue helpers.
Historical issued quantities are never erased or silently invoiced a second time.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr, flt

from erpnext.stock.stock_ledger import is_negative_stock_allowed

from pet_app.api.permissions import require_restriction_value


ISSUE_ENTRY_TYPE = "Material Issue"
RETURN_ENTRY_TYPE = "Material Receipt"

# Fractional stock below this is treated as nothing at all, so floating-point
# residue from a division never produces a 1e-16 Stock Entry.
QTY_EPSILON = 0.000001


def stock_per_dose(medication: str | None, dose_option: str | None = None) -> float | None:
	"""Stock units consumed by ONE dose, or None when this medication is not opted in.

	None and 0 mean different things here and must not be collapsed. None is "this
	medication has no dose option, behave exactly as before"; a configured option
	with a non-positive qty is a misconfiguration and throws, because a medication
	the owner deliberately configured must not silently deduct nothing.

	Ambiguity throws rather than guesses. A medication carrying 3ml/4ml/5ml options
	cannot have its dose inferred from a row that names none - picking the first,
	or the smallest, would be inventing a clinical fact. The message names the
	options so the fix is obvious.
	"""
	medication = cstr(medication).strip()
	if not medication:
		return None

	# Opt-in is decided by the MEDICATION before anything the row says is examined.
	# Rows carry dose_option pointers to options that were later deleted - there are
	# several live today - and on a medication with no usable options those pointers
	# must stay as harmless as they are now. Validating the pointer first would turn
	# them into hard failures on medications the owner never opted in.
	options = frappe.get_all(
		"Medication Dose Option",
		filters={"parent": medication, "parenttype": "Medication", "disabled": 0},
		fields=["name", "label", "qty"],
		order_by="idx asc",
		ignore_permissions=True,
	)
	usable = [row for row in options if flt(row.qty) > 0]
	if not usable:
		# Not opted in. This is the 260-of-264 case and must stay silent.
		return None

	chosen = cstr(dose_option).strip()
	if chosen:
		option = frappe.db.get_value(
			"Medication Dose Option", chosen, ["parent", "label", "qty", "disabled"], as_dict=True
		)
		if not option:
			frappe.throw(_("Dose Option {0} was not found.").format(frappe.bold(chosen)))
		if cstr(option.parent).strip() != medication:
			frappe.throw(
				_("Dose Option {0} does not belong to Medication {1}.").format(
					frappe.bold(option.label or chosen), frappe.bold(medication)
				)
			)
		if option.disabled:
			frappe.throw(_("Dose Option {0} is disabled.").format(frappe.bold(option.label or chosen)))
		qty = flt(option.qty)
		if qty <= 0:
			frappe.throw(
				_("Dose Option {0} has no Stock Deduction Qty. Set it on Medication {1} before dispensing.").format(
					frappe.bold(option.label or chosen), frappe.bold(medication)
				)
			)
		return qty

	if len(usable) > 1:
		frappe.throw(
			_(
				"Medication {0} has several dose options ({1}). Choose one on the order so the stock deduction is unambiguous."
			).format(frappe.bold(medication), ", ".join(cstr(row.label or row.name) for row in usable))
		)
	return flt(usable[0].qty)


def is_opted_in(medication: str | None) -> bool:
	"""Whether this medication deducts at all, without resolving which dose.

	Deliberately tolerant of the ambiguous case: a medication with three options is
	opted in even though a particular row may fail to resolve. Used for reporting
	and for the invoice-side guard, where "does this medication move stock now" is
	the question and "which dose" is not.
	"""
	medication = cstr(medication).strip()
	if not medication:
		return False
	return bool(
		frappe.db.exists(
			"Medication Dose Option",
			{"parent": medication, "parenttype": "Medication", "disabled": 0, "qty": [">", 0]},
		)
	)


def medication_invoice_context(row, *, item_code=None, medication=None, branch=None, warehouse_required=True):
	"""Snapshot dose conversion on the invoice; ERPNext moves stock on submission.

	Visit rows already record their chosen conversion. Boarding rows have only a
	Dose Option link, resolved when invoicing. Billed dose counts and rates stay intact.
	"""
	from pet_app.utils.invoice_stock import assert_not_preissued
	from pet_app.pet_app.doctype.medication.medication import resolve_dose_option_placeholder_uom

	item_code = item_code or row.get("medication_item") or row.get("item_code")
	medication = medication or row.get("medication")
	if not medication and (row.get("medication_item") or row.get("item_type") == "Medication"):
		medication = frappe.db.get_value("Medication", {"linked_item": item_code}, "name")
	item = frappe.db.get_value("Item", item_code, ["is_stock_item", "stock_uom"], as_dict=True)
	if not item or not item.is_stock_item:
		return {}
	assert_not_preissued(row, medication or item_code)
	defaults = frappe.db.get_value("Medication", medication,
		["default_dispense_uom", "default_conversion_factor"], as_dict=True) if medication else None
	defaults = defaults or {}
	stock_uom = item.stock_uom
	uom = row.get("dispense_uom")
	factor = flt(row.get("conversion_factor"))
	if not uom or factor <= 0:
		per_dose = stock_per_dose(medication, row.get("dose_option"))
		if per_dose is not None:
			uom = resolve_dose_option_placeholder_uom(stock_uom)
			factor = per_dose
		else:
			uom = uom or defaults.get("default_dispense_uom") or stock_uom
			factor = factor or flt(defaults.get("default_conversion_factor"))
			if not factor and uom == stock_uom:
				factor = 1
	if factor <= 0:
		frappe.throw(_("Medication {0} needs an explicit conversion from {1} to {2} before invoicing.").format(
			medication or item_code, uom, stock_uom))
	warehouse = resolve_dispense_warehouse(medication=medication, medication_item=item_code,
		row_warehouse=row.get("warehouse"), branch=branch, label=medication or item_code,
		purpose=_("invoice")) if warehouse_required else cstr(row.get("warehouse")).strip()
	context = {"warehouse": warehouse, "uom": uom, "stock_uom": stock_uom, "conversion_factor": factor}
	if row.get("batch_no"):
		context.update(batch_no=row.get("batch_no"), use_serial_batch_fields=1)
	return context


def assert_row_can_record_stock(meta, label: str | None = None):
	"""Refuse to move stock we cannot write down.

	Issuing goods and then failing to persist `stock_issued_qty` is the one failure
	mode worse than not deducting at all: the invoice-side suppression keys on that
	number, so a lost record means the same medication is deducted a second time at
	the till. On a site whose code is deployed but not yet migrated the columns do
	not exist, so this refuses the dispense outright.

	Only reached for a medication that is actually opted in, so an unmigrated site
	keeps dispensing every unconfigured medication exactly as before.
	"""
	missing = [field for field in ("stock_issued_qty", "stock_entry") if not meta.has_field(field)]
	if missing:
		frappe.throw(
			_("Run migrations before dispensing {0} with stock deduction. Missing fields: {1}").format(
				frappe.bold(label or _("medication")), ", ".join(missing)
			)
		)


def resolve_dispense_warehouse(
	*,
	medication: str | None,
	medication_item: str | None,
	row_warehouse: str | None = None,
	label: str | None = None,
	purpose: str | None = None,
	branch: str | None = None,
) -> str:
	"""The warehouse the goods leave, or a throw naming what to fill in.

	The row's own warehouse, then the Medication default, then the branch, then Stock
	Settings - so a medication issues from the same place whether it is dispensed on a
	visit, dispensed on a boarding, or billed. There is one chain, and this is it.

	The branch step was added when per-medication warehouses were retired. Without it,
	blanking ``Medication.default_warehouse`` would drop every dispense straight onto
	Stock Settings' single site-wide default - a warehouse that holds almost no stock and
	that branch-restricted staff are not permitted to touch, so ``require_restriction_value``
	below would refuse the dispense outright. See ``pet_app/utils/branch_warehouse.py``.

	``branch`` is the branch that owns the work. Callers that know it - an order carrying
	a snapshotted ``performing_branch`` - should pass it, so a service performed at one
	branch on another branch's order relieves the performing branch's shelf. Left None it
	falls back to the acting user's own branch, which is the common case.

	The last step is Stock Settings' configured default. That is a setting an
	administrator chose, not a value invented here; if it is also empty this throws
	rather than picking any warehouse that happens to hold stock.
	"""
	from pet_app.utils.branch_warehouse import branch_warehouse

	warehouse = cstr(row_warehouse).strip()

	if not warehouse and medication:
		warehouse = cstr(frappe.db.get_value("Medication", medication, "default_warehouse")).strip()

	if not warehouse and medication_item:
		warehouse = cstr(
			frappe.db.get_value("Medication", {"linked_item": medication_item}, "default_warehouse")
		).strip()

	if not warehouse:
		warehouse = cstr(branch_warehouse(branch)).strip()

	if not warehouse:
		warehouse = cstr(frappe.db.get_single_value("Stock Settings", "default_warehouse")).strip()

	if not warehouse:
		frappe.throw(
			_(
				"Warehouse is required to {0} medication {1}. Set a Warehouse on the order row, "
				"the Warehouse on your Branch, or Stock Settings Default Warehouse."
			).format(purpose or _("dispense"), frappe.bold(label or medication or medication_item or ""))
		)

	require_restriction_value("warehouse", warehouse)
	return warehouse


def issue_for_dispense(
	*,
	medication: str | None,
	medication_item: str | None,
	dose_count: float,
	dose_option: str | None = None,
	row_warehouse: str | None = None,
	reference: str,
	label: str | None = None,
) -> dict | None:
	"""Take the goods out of the warehouse. None when the medication is not opted in.

	Returns what actually happened so the caller can record it on the row:
	stock_entry, qty, warehouse, per_dose. The caller must persist `qty` - the
	invoice-side suppression and the return path both key on it.
	"""
	per_dose = stock_per_dose(medication, dose_option)
	if per_dose is None:
		return None

	dose_count = flt(dose_count)
	if dose_count <= 0:
		return None

	qty = flt(dose_count * per_dose)
	if qty <= QTY_EPSILON:
		return None

	warehouse = resolve_dispense_warehouse(
		medication=medication, medication_item=medication_item, row_warehouse=row_warehouse, label=label
	)
	stock_entry = _post_stock_entry(
		entry_type=ISSUE_ENTRY_TYPE,
		item_code=medication_item,
		qty=qty,
		warehouse=warehouse,
		is_issue=True,
		remark=_("Dispensed: {0} ({1} x {2} per dose). {3}").format(
			label or medication or medication_item, dose_count, per_dose, reference
		),
		label=label or medication or medication_item,
	)
	return {"stock_entry": stock_entry, "qty": qty, "warehouse": warehouse, "per_dose": per_dose}


def issue_explicit_qty(
	*,
	medication: str | None,
	medication_item: str | None,
	qty: float,
	branch: str | None = None,
	row_warehouse: str | None = None,
	reference: str,
	label: str | None = None,
) -> dict | None:
	"""Issue a quantity somebody else already worked out. Sibling of `issue_for_dispense`.

	Same machinery, different source for the number. A dispense multiplies a dose count by
	the medication's own per-dose quantity; a care service reads the quantity straight off
	the weight band the operator picked, because for deworming the band IS the dose - it
	sets the price and the tablets together and nobody types a quantity.

	`qty` is therefore taken as given, in the item's OWN stock UOM, and handed to
	`_post_stock_entry` with conversion_factor 1 - the same sidestep of the UOM machinery
	this module's docstring argues for. Nothing here converts anything: if the item is
	stocked in Strip, `qty` is strips.

	Returns None only when there is nothing to do (no quantity). Every other failure -
	no item, a group warehouse, a fraction against a whole-number UOM, no warehouse at
	all - throws from the shared helpers, naming what to fix.
	"""
	qty = flt(qty)
	if qty <= QTY_EPSILON:
		return None

	warehouse = resolve_dispense_warehouse(
		medication=medication,
		medication_item=medication_item,
		row_warehouse=row_warehouse,
		branch=branch,
		label=label,
		purpose=_("issue"),
	)
	stock_entry = _post_stock_entry(
		entry_type=ISSUE_ENTRY_TYPE,
		item_code=medication_item,
		qty=qty,
		warehouse=warehouse,
		is_issue=True,
		remark=_("Issued: {0} ({1}). {2}").format(label or medication or medication_item, qty, reference),
		label=label or medication or medication_item,
	)
	return {"stock_entry": stock_entry, "qty": qty, "warehouse": warehouse}


def receive_for_return(
	*,
	medication_item: str | None,
	qty: float,
	warehouse: str | None,
	medication: str | None = None,
	reference: str,
	label: str | None = None,
) -> str | None:
	"""Put back exactly what a reversal is owed.

	`qty` is computed by the caller from what was ISSUED, not from the master - see
	`returnable_stock_qty`. The warehouse is the one the goods left, so a return
	cannot quietly relocate stock into a different store.
	"""
	qty = flt(qty)
	if qty <= QTY_EPSILON:
		return None

	warehouse = cstr(warehouse).strip()
	if not warehouse:
		warehouse = resolve_dispense_warehouse(
			medication=medication, medication_item=medication_item, label=label
		)
	else:
		require_restriction_value("warehouse", warehouse)

	return _post_stock_entry(
		entry_type=RETURN_ENTRY_TYPE,
		item_code=medication_item,
		qty=qty,
		warehouse=warehouse,
		is_issue=False,
		remark=_("Returned: {0} ({1}). {2}").format(label or medication_item, qty, reference),
		label=label or medication or medication_item,
	)


def returnable_stock_qty(*, stock_issued_qty, dispensed_qty, return_doses) -> float:
	"""Stock to put back for `return_doses`, at the rate the dispense actually used.

	Derived from the row's own history rather than from the dose option, because
	the option is editable. If the owner corrects 0.5 ml to 5 ml between dispense
	and return, reading the master would put back ten times what was taken.
	"""
	dispensed_qty = flt(dispensed_qty)
	stock_issued_qty = flt(stock_issued_qty)
	return_doses = flt(return_doses)
	if dispensed_qty <= 0 or stock_issued_qty <= 0 or return_doses <= 0:
		return 0.0
	return flt(stock_issued_qty * (return_doses / dispensed_qty))


def _post_stock_entry(
	*, entry_type: str, item_code: str | None, qty: float, warehouse: str, is_issue: bool, remark: str, label: str
) -> str:
	item_code = cstr(item_code).strip()
	if not item_code:
		frappe.throw(_("Medication {0} is not linked to an Item and cannot move stock.").format(frappe.bold(label)))

	item = frappe.db.get_value(
		"Item", item_code, ["name", "is_stock_item", "stock_uom", "has_batch_no", "has_serial_no"], as_dict=True
	)
	if not item:
		frappe.throw(_("Item {0} was not found.").format(frappe.bold(item_code)))
	if not item.is_stock_item:
		frappe.throw(
			_("Item {0} is not a stock item, so medication {1} cannot deduct stock. Remove its dose option or make the item a stock item.").format(
				frappe.bold(item_code), frappe.bold(label)
			)
		)
	# Batch and serial tracking need a specific batch chosen at the bedside, which
	# no dispense endpoint asks for today. Refusing is the only honest answer -
	# guessing a batch would put the wrong expiry on the animal's record.
	if item.has_batch_no or item.has_serial_no:
		frappe.throw(
			_("Item {0} is batch or serial tracked, which dispensing does not support yet. Dispense it from a Stock Entry instead.").format(
				frappe.bold(item_code)
			)
		)

	# Only checked where stock actually moves. The invoice builder shares this
	# module's warehouse chain but not this guard, so an existing configuration
	# that never moved stock cannot start failing at billing time.
	if frappe.db.get_value("Warehouse", warehouse, "is_group"):
		frappe.throw(
			_("Warehouse {0} is a group warehouse and cannot hold stock. Choose a leaf warehouse.").format(
				frappe.bold(warehouse)
			)
		)

	_validate_whole_number_qty(item.stock_uom, qty, label)

	if is_issue:
		_validate_availability(item_code, warehouse, qty, label)

	company = cstr(frappe.db.get_value("Warehouse", warehouse, "company")).strip()
	if not company:
		frappe.throw(_("Warehouse {0} has no Company and cannot be used.").format(frappe.bold(warehouse)))

	entry = frappe.new_doc("Stock Entry")
	entry.stock_entry_type = entry_type
	entry.purpose = entry_type
	entry.company = company
	entry.remarks = remark
	row = {
		"item_code": item_code,
		"qty": qty,
		# The item's own stock UOM, factor 1. We already did the conversion; letting
		# ERPNext do a second one is exactly the multiplier bug this design avoids.
		"uom": item.stock_uom,
		"stock_uom": item.stock_uom,
		"conversion_factor": 1,
	}
	row["s_warehouse" if is_issue else "t_warehouse"] = warehouse
	entry.append("items", row)
	entry.flags.ignore_permissions = True
	entry.insert(ignore_permissions=True)
	entry.submit()
	return entry.name


def _validate_whole_number_qty(stock_uom: str | None, qty: float, label: str):
	stock_uom = cstr(stock_uom).strip()
	if not stock_uom:
		return
	if abs(flt(qty) - int(flt(qty))) <= QTY_EPSILON:
		return
	if not frappe.db.get_value("UOM", stock_uom, "must_be_whole_number"):
		return
	frappe.throw(
		_(
			"Dispensing {0} needs {1} {2}, but {2} only accepts whole numbers. "
			"Correct the Stock Deduction Qty on the medication, or change the item's Stock UOM."
		).format(frappe.bold(label), frappe.bold(flt(qty)), frappe.bold(stock_uom))
	)


def _validate_availability(item_code: str, warehouse: str, qty: float, label: str):
	"""Say what the warehouse cannot cover, and refuse only where the item says to.

	Whether a shortfall is fatal is not this module's decision to make. ERPNext already
	answers it in one place - `is_negative_stock_allowed`, which reads the global Stock
	Settings flag and then the per-Item one - and medication items carry the per-Item
	flag precisely so a dose is never blocked by a ledger that is behind the shelf.
	Asking that helper keeps one authority instead of two that can disagree.

	Where the shortfall is allowed we still say so at the moment it is created, rather
	than leaving it to be discovered at the next count: the bin is about to go negative
	and the person holding the syringe is the only one who can explain why. Where it is
	not allowed the throw stands, and it stays worded in terms they can act on - ERPNext
	would refuse at submit anyway, but only after a Stock Entry had been built.
	"""
	available = flt(
		frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty")
	)
	if available + QTY_EPSILON >= flt(qty):
		return

	if is_negative_stock_allowed(item_code=item_code):
		frappe.msgprint(
			_(
				"Dispensing {0} takes {1} below zero: it holds {2} but {3} is being dispensed. Recount and receive the difference."
			).format(
				frappe.bold(label),
				frappe.bold(warehouse),
				frappe.bold(available),
				frappe.bold(flt(qty)),
			),
			title=_("Stock going negative"),
			indicator="orange",
		)
		return

	frappe.throw(
		_(
			"Cannot dispense {0}: {1} needs {2} but holds {3}. Receive stock into {1} first, or dispense from a warehouse that has it."
		).format(frappe.bold(label), frappe.bold(warehouse), frappe.bold(flt(qty)), frappe.bold(available))
	)
