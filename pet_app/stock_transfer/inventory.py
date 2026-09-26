"""Backend-owned unit conversions, picks, shipment provenance and stock posting."""

import json
import uuid

import frappe
from frappe.utils import getdate, nowdate, nowtime

from pet_app.stock_transfer.access import permit
from pet_app.stock_transfer.guards import batch_stock, native_reserved, reservations, stock_qty
from pet_app.stock_transfer.rules import TransferError, digest, fail, quantity, same, stock_units


def item(code):
	doc = frappe.get_doc("Item", code)
	permit("Item", doc=doc)
	if doc.disabled or not doc.is_stock_item or doc.has_variants:
		fail(f"{code} must be an enabled stock item.")
	return doc


def line_quantity(line, value):
	precision = frappe.get_precision("Stock Entry Detail", "qty") or 6
	q = quantity(
		value, precision=precision, whole=bool(frappe.db.get_value("UOM", line.uom, "must_be_whole_number"))
	)
	quantity(
		stock_units(q, line.conversion_factor),
		precision=9,
		whole=bool(frappe.db.get_value("Item", line.item_code, "has_serial_no")),
	)
	return q


def conversion(doc, uom, submitted):
	factor = next((float(r.conversion_factor) for r in doc.uoms if r.uom == uom), None)
	if uom == doc.stock_uom:
		factor = 1.0
	if not factor or not same(quantity(submitted, precision=9), factor):
		fail(f"{doc.name}: select a valid UOM and its current conversion factor.")
	return factor


def managed(line):
	return bool(
		frappe.db.get_value("Item", line.item_code, "has_serial_no")
		or frappe.db.get_value("Item", line.item_code, "has_batch_no")
	)


def options(order, line, phase):
	if phase == "receive":
		sent = frappe.get_all(
			"Stock Transfer Allocation",
			filters={"transfer": order.name, "line_id": line.line_id, "kind": "dispatch"},
			fields=["name", "batch_no", "serial_no", "stock_qty"],
		)
		result = []
		for r in sent:
			used = sum(
				float(x.stock_qty)
				for x in frappe.get_all(
					"Stock Transfer Allocation", filters={"shipment_allocation": r.name}, fields=["stock_qty"]
				)
			)
			remaining = float(r.stock_qty) - used
			if remaining > 1e-8:
				result.append(option(r.name, r.batch_no, r.serial_no, remaining))
		return result
	if phase != "prepare":
		fail("phase must be prepare or receive.")
	doc = item(line.item_code)
	held = reservations(doc.name, order.source_warehouse)
	result = []
	if doc.has_serial_no:
		native_serials = set(
			frappe.db.sql(
				"""select e.serial_no from `tabSerial and Batch Entry` e
            join `tabStock Reservation Entry` r on r.name=e.parent
            where r.docstatus=1 and r.item_code=%s and r.warehouse=%s and e.qty>e.delivered_qty for update""",
				(doc.name, order.source_warehouse),
				pluck=True,
			)
		)
		for r in frappe.db.sql(
			"""select name, batch_no from `tabSerial No`
			where item_code=%s and warehouse=%s and status='Active' order by name for update""",
			(doc.name, order.source_warehouse),
			as_dict=True,
		):
			if r.name in native_serials:
				continue
			if any(h.serial_no == r.name and h.transfer != order.name for h in held):
				continue
			if r.batch_no and expired(r.batch_no):
				continue
			result.append(
				option(digest([doc.name, order.source_warehouse, r.batch_no, r.name]), r.batch_no, r.name, 1)
			)
	elif doc.has_batch_no:
		from erpnext.stock.doctype.batch.batch import get_batch_qty

		physical = batch_stock(doc.name, order.source_warehouse)
		for r in get_batch_qty(item_code=doc.name, warehouse=order.source_warehouse):
			if expired(r.batch_no):
				continue
			qty = min(float(r.qty), physical.get(r.batch_no, 0)) - sum(
				float(h.stock_qty) for h in held if h.batch_no == r.batch_no and h.transfer != order.name
			)
			if qty > 1e-8:
				result.append(
					option(
						digest([doc.name, order.source_warehouse, r.batch_no, None]), r.batch_no, None, qty
					)
				)
	return result


def expired(batch):
	r = frappe.db.get_value("Batch", batch, ["expiry_date", "disabled"], as_dict=True)
	return not r or r.disabled or (r.expiry_date and getdate(r.expiry_date) < getdate())


def option(id, batch, serial, qty):
	return dict(
		id=id,
		label=serial or batch or "Stock",
		batch_no=batch or None,
		serial_no=serial or None,
		expiry_date=frappe.db.get_value("Batch", batch, "expiry_date") if batch else None,
		available_stock_qty=float(qty),
	)


def allocations(order, line, values, stock_total, phase, usage):
	if not isinstance(values, list) or any(not isinstance(v, dict) for v in values):
		fail(f"{line.line_id}: allocations must be an array.", "INVALID_ALLOCATION")
	if not managed(line):
		if values:
			fail(f"{line.line_id}: this item uses empty allocation arrays.", "INVALID_ALLOCATION")
		return []
	available = {r["id"]: r for r in options(order, line, phase)}
	result, seen = [], set()
	for v in values:
		id = v.get("allocation_id")
		if not isinstance(id, str) or id not in available or id in seen:
			fail(f"{line.line_id}: allocation is foreign, duplicated or unavailable.", "INVALID_ALLOCATION")
		seen.add(id)
		r = available[id]
		qty = quantity(v.get("stock_qty"), precision=9, whole=bool(r["serial_no"]))
		if not qty:
			fail("Allocation quantities must be positive.", "INVALID_ALLOCATION")
		usage[id] = usage.get(id, 0) + qty
		if usage[id] > r["available_stock_qty"] + 1e-8:
			fail(f"{line.line_id}: combined allocations exceed available stock.", "INVALID_ALLOCATION")
		result.append(dict(allocation_id=id, stock_qty=qty, batch_no=r["batch_no"], serial_no=r["serial_no"]))
	if not same(sum(r["stock_qty"] for r in result), stock_total):
		fail(f"{line.line_id}: allocations must exactly equal the stock-UOM quantity.", "INVALID_ALLOCATION")
	return result


def readable(value):
	"""Trim a quantity for display; %g would flip to exponents above a million."""
	return f"{float(value):.6f}".rstrip("0").rstrip(".") or "0"


def line_issues(order, line, doc, free, demanded):
	"""Every refusal preparation could raise for this line, as readable data.

	The prepare screen renders these, so each entry mirrors exactly one fail() on the
	write path; a line without issues must be one that Ready accepts. Stock is measured
	against approved demand rather than what is prepared so far, because approved demand
	is what Ready will require.
	"""
	result = []

	def issue(code, message):
		result.append(dict(code=code, message=message))

	approved, prepared = float(line.approved_qty or 0), float(line.prepared_qty or 0)
	if doc.disabled or not doc.is_stock_item or doc.has_variants:
		issue("ITEM_UNAVAILABLE", f"{doc.name} is no longer an enabled stock item.")
	try:
		conversion(doc, line.uom, float(line.conversion_factor))
	except TransferError:
		issue("UOM_INVALID", f"{line.uom} is no longer a valid unit for {doc.name}.")
	# free is None when the source warehouse is outside the reader's scope: unknown, not short.
	if free is not None and demanded > free + 1e-8:
		issue(
			"INSUFFICIENT_STOCK",
			f"Needs {readable(demanded)} {doc.stock_uom},"
			f" {readable(max(0.0, free))} free in {order.source_warehouse}.",
		)
	if prepared and managed(line):
		try:
			allocations(
				order,
				line,
				json.loads(line.allocations_json or "[]"),
				stock_units(prepared, line.conversion_factor),
				"prepare",
				{},
			)
		except TransferError as exc:
			issue("INVALID_ALLOCATION", str(exc))
	# Unfinished picking is only a finding while picking is the job: flagging every line of a
	# fresh order teaches operators to ignore the column, which is the one thing it cannot afford.
	if order.status == "Preparing" and prepared < approved - 1e-8:
		issue("NOT_PREPARED", f"Prepared {readable(prepared)} of {readable(approved)} {line.uom}.")
	return result


def reserve(order):
	"""Replace the entire held snapshot, including empty/reduced picks."""
	frappe.db.delete("Stock Transfer Reservation", {"transfer": order.name})
	totals = {}
	for line in order.items:
		picked = json.loads(line.allocations_json or "[]")
		qty = stock_units(float(line.prepared_qty), line.conversion_factor)
		totals[line.item_code] = totals.get(line.item_code, 0) + qty
		for r in picked or ([dict(stock_qty=qty)] if qty else []):
			insert_reservation(order, line, order.source_warehouse, r)
	# Every short code is named at once; one blocker at a time is unusable on long orders.
	short = []
	for code in totals:
		all_held = sum(float(r.stock_qty) for r in reservations(code, order.source_warehouse))
		# ERPNext's native reservations also remain unavailable to this workflow.
		native = native_reserved(code, order.source_warehouse)
		on_hand = stock_qty(code, order.source_warehouse)
		if all_held + native > on_hand + 1e-8:
			short.append(
				f"{code} (needs {readable(all_held + native)},"
				f" {readable(max(0.0, on_hand - native))} free)"
			)
	if short:
		fail("Insufficient unreserved stock: " + ", ".join(sorted(short)) + ".", "INSUFFICIENT_STOCK")


def insert_reservation(order, line, warehouse, allocation):
	frappe.get_doc(
		dict(
			doctype="Stock Transfer Reservation",
			transfer=order.name,
			line_id=line.line_id,
			item_code=line.item_code,
			warehouse=warehouse,
			**allocation,
		)
	).insert(ignore_permissions=True)


def post(order, cfg, kind, movements):
	if not movements:
		return None
	doc = frappe.new_doc("Stock Entry")
	doc.update(
		dict(
			company=order.company,
			stock_entry_type="Material Issue" if kind == "loss" else "Material Transfer",
			posting_date=nowdate(),
			posting_time=nowtime(),
			custom_stock_transfer_order=order.name,
			custom_stock_transfer_kind=kind,
		)
	)
	source = order.source_warehouse if kind == "dispatch" else order.transit_warehouse
	for line, qty, picks, target in movements:
		values = dict(
			item_code=line.item_code,
			qty=qty,
			uom=line.uom,
			stock_uom=line.stock_uom,
			conversion_factor=line.conversion_factor,
			s_warehouse=source,
			t_warehouse=target,
			expense_account=cfg.loss_expense_account if kind == "loss" else None,
			cost_center=cfg.cost_center,
		)
		if managed(line):
			bundle = frappe.get_doc(
				dict(
					doctype="Serial and Batch Bundle",
					company=order.company,
					item_code=line.item_code,
					warehouse=source,
					voucher_type="Stock Entry",
					type_of_transaction="Outward",
					posting_date=doc.posting_date,
					posting_time=doc.posting_time,
					entries=[
						dict(batch_no=a.get("batch_no"), serial_no=a.get("serial_no"), qty=-a["stock_qty"])
						for a in picks
					],
				)
			)
			bundle.insert(ignore_permissions=True)
			values.update(serial_and_batch_bundle=bundle.name, use_serial_batch_fields=0)
		doc.append("items", values)
	previous = getattr(frappe.local, "stock_transfer_posting", None)
	frappe.local.stock_transfer_posting = order.name
	try:
		doc.insert(ignore_permissions=True)
		doc.submit()
	finally:
		frappe.local.stock_transfer_posting = previous
	for line, qty, picks, target in movements:
		for a in picks or [dict(stock_qty=stock_units(qty, line.conversion_factor))]:
			alloc = frappe.get_doc(
				dict(
					doctype="Stock Transfer Allocation",
					transfer=order.name,
					line_id=line.line_id,
					item_code=line.item_code,
					warehouse=target or source,
					kind=kind,
					stock_qty=a["stock_qty"],
					batch_no=a.get("batch_no"),
					serial_no=a.get("serial_no"),
					stock_entry=doc.name,
					shipment_allocation=a.get("allocation_id") if kind != "dispatch" else None,
				)
			).insert(ignore_permissions=True)
			if kind == "dispatch":
				insert_reservation(
					order,
					line,
					order.transit_warehouse,
					dict(
						allocation_id=alloc.name,
						stock_qty=a["stock_qty"],
						batch_no=a.get("batch_no"),
						serial_no=a.get("serial_no"),
					),
				)
	return doc.name


def consume_transit(order, line, qty, picks):
	"""Release exactly the delta before posting; rollback restores it on any refusal."""
	held = frappe.get_all(
		"Stock Transfer Reservation",
		filters={"transfer": order.name, "line_id": line.line_id},
		fields=["name", "allocation_id", "stock_qty"],
	)
	changes = {a["allocation_id"]: a["stock_qty"] for a in picks}
	remaining = stock_units(qty, line.conversion_factor)
	for r in held:
		used = changes.get(r.allocation_id, 0) if picks else min(remaining, float(r.stock_qty))
		if used > float(r.stock_qty) + 1e-8:
			fail("Transit reservation is inconsistent.", "RESERVATION_CONFLICT")
		if used:
			remaining -= used
			new = float(r.stock_qty) - used
			if new > 1e-8:
				frappe.db.set_value("Stock Transfer Reservation", r.name, "stock_qty", new)
			else:
				frappe.db.delete("Stock Transfer Reservation", {"name": r.name})
	if not same(remaining, 0):
		fail("Transit reservation is missing; reconcile this transfer.", "RESERVATION_CONFLICT")
