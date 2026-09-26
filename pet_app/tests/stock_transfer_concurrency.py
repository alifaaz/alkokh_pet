"""Explicit two-connection probes. Fixtures are committed only in the isolated test DB."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import frappe

from pet_app.api import stock_transfer as api
from pet_app.tests.test_stock_transfer import TestStockTransfer


def run():
	assert str(frappe.conf.db_name).startswith("test_driver_orders_"), "Never run on production"
	fixture = TestStockTransfer()
	fixture.setUp()
	site = frappe.local.site
	fixture.create(24)
	first, line1 = fixture.current.copy(), fixture.line
	fixture.call("start_preparation", {})
	first = fixture.current.copy()
	fixture.create(24)
	fixture.call("start_preparation", {})
	second, line2 = fixture.current.copy(), fixture.line
	frappe.db.commit()

	def pair(functions):
		gate = Barrier(2)

		def worker(fn):
			frappe.init(site=site)
			frappe.connect()
			frappe.set_user("Administrator")
			try:
				gate.wait(timeout=20)
				result = fn()
				if result["ok"]:
					frappe.db.commit()
				else:
					frappe.db.rollback()
				return result
			finally:
				frappe.destroy()

		with ThreadPoolExecutor(max_workers=2) as pool:
			jobs = [pool.submit(worker, f) for f in functions]
			results = [j.result(timeout=50) for j in jobs]
		frappe.db.rollback()
		return results

	def reserve(order, line):
		return api.save_preparation(
			transfer_id=order["id"],
			expected_version=order["version"],
			idempotency_key=uuid.uuid4().hex,
			payload={"ready": True, "items": [{"line_id": line, "prepared_qty": 24, "allocations": []}]},
		)

	try:
		results = pair([lambda: reserve(first, line1), lambda: reserve(second, line2)])
		assert sum(r["ok"] for r in results) == 1, results
		loser = next(r for r in results if not r["ok"])
		assert loser["meta"]["code"] == "INSUFFICIENT_STOCK", results
		winner = next(r["data"] for r in results if r["ok"])
		request = dict(
			transfer_id=winner["id"],
			expected_version=winner["version"],
			idempotency_key=uuid.uuid4().hex,
			payload=dict(
				driver_name="Concurrency probe",
				driver_contact="",
				vehicle="",
				packages=None,
				seal="",
				departure_time="2026-09-09T10:00:00Z",
				expected_arrival=None,
			),
		)
		results = pair([lambda: api.dispatch_transfer(**request), lambda: api.dispatch_transfer(**request)])
		assert results[0]["ok"] and results[0] == results[1], results
		assert frappe.db.count("Stock Entry", {"custom_stock_transfer_order": winner["id"]}) == 1
		shipped = results[0]["data"]
		line = shipped["items"][0]["line_id"]

		def receive():
			return api.receive_transfer(
				transfer_id=shipped["id"],
				expected_version=shipped["version"],
				idempotency_key=uuid.uuid4().hex,
				payload={"items": [{"line_id": line, "accepted_qty": 24}]},
			)

		results = pair([receive, receive])
		assert sum(r["ok"] for r in results) == 1, results
		assert next(r for r in results if not r["ok"])["meta"]["code"] == "VERSION_CONFLICT", results
		assert frappe.db.count("Stock Entry", {"custom_stock_transfer_order": winner["id"]}) == 2
		assert fixture.balance(fixture.transit) == 0
		# A normal Material Issue uses the same ledger path as medication consumption.
		fixture.seed(fixture.item, 24)
		losing_order, losing_line = (second, line2) if winner["id"] == first["id"] else (first, line1)
		frappe.db.commit()

		def issue():
			from pet_app.stock_transfer.rules import TransferError

			try:
				entry = frappe.get_doc(
					dict(
						doctype="Stock Entry",
						stock_entry_type="Material Issue",
						company=fixture.company,
						items=[dict(item_code=fixture.item, qty=24, s_warehouse=fixture.source)],
					)
				)
				entry.insert(ignore_permissions=True)
				entry.submit()
				return {"ok": True}
			except (TransferError, frappe.QueryDeadlockError) as exc:
				return {"ok": False, "code": getattr(exc, "code", "RESERVATION_CONFLICT")}

		results = pair([lambda: reserve(losing_order, losing_line), issue])
		assert sum(r["ok"] for r in results) == 1, results
		remainder = api.get_transfer(losing_order["id"])["data"]
		cancelled = api.cancel_transfer(
			transfer_id=remainder["id"],
			expected_version=remainder["version"],
			idempotency_key=uuid.uuid4().hex,
			payload={"reason": "Concurrency probe complete"},
		)
		assert cancelled["ok"], cancelled
		frappe.db.commit()
		return {
			"reservation_vs_material_issue": "one winner",
			"last_stock_reservation": "one winner",
			"duplicate_dispatch": "one stock entry, identical replay",
			"concurrent_receipts": "one receipt, one version conflict",
		}
	finally:
		frappe.db.rollback()
		# Keep test ledger history, remove only this probe's settings so reruns can create their own.
		frappe.delete_doc("Stock Transfer Settings", fixture.cfg.name, ignore_permissions=True)
		frappe.db.commit()
		fixture.tearDown()
