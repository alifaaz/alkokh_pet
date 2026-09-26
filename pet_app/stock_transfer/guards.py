"""Shared transaction lock and enforcement at ERPNext's stock-ledger boundary.

A database row lock deliberately serializes stock writers across this site. It is
held until commit/rollback, including when the feature is administratively disabled.
This trades throughput for a single lock order across all existing stock consumers.
"""

import frappe
from frappe.model.document import Document
from frappe.utils import flt

from pet_app.stock_transfer.rules import fail


class WorkflowDocument(Document):
	def before_validate(self):
		self._guard()

	def on_trash(self):
		self._guard()

	def before_rename(self, *args, **kwargs):
		fail("Workflow records cannot be renamed.")

	def _guard(self):
		if not getattr(frappe.local, "stock_transfer_internal", False):
			fail("Use the Stock Transfer workflow to change this record.", "PERMISSION_DENIED")
		if (
			self.doctype in ("Stock Transfer Event", "Stock Transfer Allocation", "Stock Transfer Request")
			and not self.is_new()
		):
			fail("Shipment allocations, events and completed requests are immutable.")


def lock():
	# A current read also works on REPEATABLE READ connections and has no missing-row race.
	return bool(frappe.db.sql("select name from tabDocType where name='Stock Transfer Order' for update"))


def stock_document(doc, method=None):
	from erpnext.controllers.stock_controller import StockController

	if isinstance(doc, StockController) or doc.doctype in (
		"Stock Ledger Entry",
		"Serial and Batch Bundle",
		"Stock Reservation Entry",
	):
		lock()
	if doc.doctype == "Stock Entry":
		owned = doc.get("custom_stock_transfer_order")
		if not doc.is_new():
			if frappe.get_meta("Stock Entry").has_field("custom_stock_transfer_order"):
				owned = owned or frappe.db.get_value("Stock Entry", doc.name, "custom_stock_transfer_order")
		if doc.get("amended_from") and frappe.get_meta("Stock Entry").has_field(
			"custom_stock_transfer_order"
		):
			owned = owned or frappe.db.get_value(
				"Stock Entry", doc.amended_from, "custom_stock_transfer_order"
			)
		if owned and (
			getattr(frappe.local, "stock_transfer_posting", None) != owned
			or method in ("before_cancel", "on_trash", "before_update_after_submit")
		):
			fail("Linked stock documents are controlled by their Stock Transfer Order.", "PERMISSION_DENIED")


def reservations(item, warehouse):
	return frappe.db.sql(
		"""select name, transfer, batch_no, serial_no, stock_qty from `tabStock Transfer Reservation`
        where item_code=%s and warehouse=%s order by name for update""",
		(item, warehouse),
		as_dict=True,
	)


def stock_qty(item, warehouse):
	values = frappe.db.sql(
		"select actual_qty from tabBin where item_code=%s and warehouse=%s for update", (item, warehouse)
	)
	# An absent Bin is known empty; database failures are never converted into zero.
	return float(values[0][0]) if values else 0.0


def batch_stock(item, warehouse):
	"""Current reads matching ERPNext's bundle and legacy batch ledger quantities."""
	result = {}
	for row in frappe.db.sql(
		"""select e.batch_no, e.qty from `tabStock Ledger Entry` s
		join `tabSerial and Batch Entry` e on e.parent=s.serial_and_batch_bundle
		where s.item_code=%s and s.warehouse=%s and s.is_cancelled=0 for update""",
		(item, warehouse),
		as_dict=True,
	):
		result[row.batch_no] = result.get(row.batch_no, 0) + float(row.qty)
	for row in frappe.db.sql(
		"""select batch_no, actual_qty from `tabStock Ledger Entry`
		where item_code=%s and warehouse=%s and is_cancelled=0 and coalesce(batch_no,'')!=''
		and coalesce(serial_and_batch_bundle,'')='' for update""",
		(item, warehouse),
		as_dict=True,
	):
		result[row.batch_no] = result.get(row.batch_no, 0) + float(row.actual_qty)
	return result


def native_reserved(item, warehouse):
	values = frappe.db.sql(
		"""select reserved_qty, delivered_qty from `tabStock Reservation Entry`
		where item_code=%s and warehouse=%s and docstatus=1 for update""",
		(item, warehouse),
		as_dict=True,
	)
	return sum(max(0, float(r.reserved_qty) - float(r.delivered_qty)) for r in values)


def ledger_guard(doc, method=None):
	if not lock():  # Before this app's schema has been installed.
		return
	held = reservations(doc.item_code, doc.warehouse)
	if not held:
		return
	# Cancellation marks all original SLEs cancelled before generating reversals.
	# Refuse while any affected item is held, avoiding invalid intermediate balances.
	if doc.is_cancelled or doc.voucher_type == "Stock Reconciliation":
		fail(
			f"{doc.item_code}: release or settle transfer reservations before reconciliation or cancellation.",
			"RESERVATION_CONFLICT",
		)
	if flt(doc.actual_qty) >= 0:
		return
	available = stock_qty(doc.item_code, doc.warehouse)
	if available + flt(doc.actual_qty) < sum(float(r.stock_qty) for r in held) - 1e-8:
		fail(f"{doc.item_code}: this movement would consume reserved transfer stock.", "RESERVATION_CONFLICT")
	batches, serials = {}, set()
	if doc.serial_and_batch_bundle:
		for r in frappe.get_all(
			"Serial and Batch Entry",
			filters={"parent": doc.serial_and_batch_bundle},
			fields=["batch_no", "serial_no", "qty"],
		):
			if r.serial_no:
				serials.add(r.serial_no)
			if r.batch_no:
				batches[r.batch_no] = batches.get(r.batch_no, 0) + abs(float(r.qty))
	else:
		serials.update((doc.serial_no or "").splitlines())
		if doc.batch_no:
			batches[doc.batch_no] = abs(float(doc.actual_qty))
	if any(r.serial_no in serials for r in held if r.serial_no):
		fail(f"{doc.item_code}: a selected serial is reserved for a transfer.", "RESERVATION_CONFLICT")
	physical_batches = batch_stock(doc.item_code, doc.warehouse) if batches else {}
	for batch, used in batches.items():
		reserved = sum(float(r.stock_qty) for r in held if r.batch_no == batch)
		if reserved and physical_batches.get(batch, 0) - used < reserved - 1e-8:
			fail(f"{doc.item_code}: batch {batch} is reserved for a transfer.", "RESERVATION_CONFLICT")


def native_reservation_guard(doc, method=None):
	if not lock():
		return
	held = reservations(doc.item_code, doc.warehouse)
	if not held:
		return
	native = frappe.db.sql(
		"""select reserved_qty, delivered_qty from `tabStock Reservation Entry`
        where item_code=%s and warehouse=%s and docstatus=1 and name!=%s for update""",
		(doc.item_code, doc.warehouse, doc.name),
		as_dict=True,
	)
	total = sum(max(0, float(r.reserved_qty) - float(r.delivered_qty)) for r in native)
	total += max(0, float(doc.reserved_qty) - float(doc.delivered_qty or 0))
	if total + sum(float(r.stock_qty) for r in held) > stock_qty(doc.item_code, doc.warehouse) + 1e-8:
		fail(f"{doc.item_code}: stock is already reserved by a transfer.", "RESERVATION_CONFLICT")
	for r in doc.get("sb_entries") or []:
		if r.serial_no and any(h.serial_no == r.serial_no for h in held):
			fail(f"{r.serial_no}: serial is already reserved by a transfer.", "RESERVATION_CONFLICT")
		if r.batch_no:
			from erpnext.stock.doctype.batch.batch import get_batch_qty

			available = float(get_batch_qty(batch_no=r.batch_no, warehouse=doc.warehouse))
			held_batch = sum(float(h.stock_qty) for h in held if h.batch_no == r.batch_no)
			wanted = sum(
				max(0, float(b.qty) - float(b.delivered_qty or 0))
				for b in doc.sb_entries
				if b.batch_no == r.batch_no
			)
			if wanted + held_batch > available + 1e-8:
				fail(f"{r.batch_no}: batch is already reserved by a transfer.", "RESERVATION_CONFLICT")
