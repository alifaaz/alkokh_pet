"""Isolated real ERPNext ledger tests; never run against the operational database."""

import json
import unittest
import uuid
from unittest.mock import patch

import frappe
from frappe.utils import nowdate

from pet_app.api import stock_transfer as api
from pet_app.stock_transfer.rules import TransferError, public_url, quantity, shipment


class TestTransferRules(unittest.TestCase):
	def test_reject_invalid_numbers(self):
		for value in (-1, float("nan"), float("inf"), True, "2", None):
			with self.assertRaises(TransferError):
				quantity(value)
		with self.assertRaises(TransferError):
			quantity(0.5, whole=True)
		with self.assertRaises(TransferError):
			quantity(0.1234, precision=3)

	def test_shipment_and_public_url(self):
		p = dict(
			driver_name="Staff",
			driver_contact="",
			vehicle="",
			packages=None,
			seal="",
			departure_time="2026-09-09T10:00:00Z",
			expected_arrival=None,
		)
		self.assertEqual(shipment(p), p)
		with self.assertRaises(TransferError):
			shipment({**p, "departure_time": "2026-09-09T10:00:00"})
		with self.assertRaises(TransferError):
			shipment({**p, "departure_time": "2026-09-09T11:00:00Z"}, p)
		with self.assertRaises(TransferError):
			shipment({**p, "expected_arrival": "2026-09-09T09:00:00Z"})
		self.assertEqual(public_url("https://inventory.example.com/#/"), "https://inventory.example.com/#/")
		for url in ("file:///tmp/app", "https://user:pass@example.com/", "https://example.com/?token=a"):
			with self.assertRaises(TransferError):
				public_url(url)


class TestStockTransfer(unittest.TestCase):
	def setUp(self):
		if not str(frappe.conf.db_name).startswith("test_driver_orders_"):
			self.skipTest("Requires an isolated test_driver_orders_* database.")
		frappe.set_user("Administrator")
		self.addCleanup(frappe.db.rollback)
		frappe.flags.warehouse_account_map = {}
		self.company = frappe.db.get_value("Company", {}, "name")
		self.suffix = uuid.uuid4().hex[:10]
		self.warehouses = []
		for label in ("Source", "Target", "Transit", "Quarantine"):
			w = frappe.get_doc(
				dict(doctype="Warehouse", company=self.company, warehouse_name=f"_ST {label} {self.suffix}")
			).insert(ignore_permissions=True)
			self.warehouses.append(w.name)
		self.source, self.target, self.transit, self.quarantine = self.warehouses
		self.cfg = frappe.get_doc(
			dict(
				doctype="Stock Transfer Settings",
				company=self.company,
				enabled=1,
				reservation_tests_passed=1,
				transit_warehouse=self.transit,
				public_app_url="https://inventory.example.com/#/",
				loss_expense_account=frappe.db.get_value(
					"Account",
					{"company": self.company, "root_type": "Expense", "is_group": 0, "disabled": 0},
					"name",
				),
				cost_center=frappe.db.get_value(
					"Cost Center", {"company": self.company, "is_group": 0, "disabled": 0}, "name"
				),
				quarantine_warehouses=[{"warehouse": self.quarantine}],
			)
		).insert(ignore_permissions=True)
		self.item = (
			frappe.get_doc(
				dict(
					doctype="Item",
					item_code=f"_ST {self.suffix}",
					item_name=f"Transfer {self.suffix}",
					stock_uom="Nos",
					is_stock_item=1,
					item_group=frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
					valuation_rate=10,
					uoms=[{"uom": "Nos", "conversion_factor": 1}, {"uom": "Box", "conversion_factor": 12}],
				)
			)
			.insert(ignore_permissions=True)
			.name
		)
		self.seed(self.item, 24)
		self.current = None

	def tearDown(self):
		frappe.db.rollback()
		frappe.set_user("Administrator")
		frappe.clear_cache()

	def seed(self, code, qty, **kw):
		d = frappe.get_doc(
			dict(
				doctype="Stock Entry",
				company=self.company,
				stock_entry_type="Material Receipt",
				items=[dict(item_code=code, qty=qty, t_warehouse=self.source, basic_rate=10, **kw)],
			)
		)
		d.insert(ignore_permissions=True)
		d.submit()
		return d

	def call(self, method, payload, **kw):
		kw.setdefault("idempotency_key", uuid.uuid4().hex)
		if method != "create_transfer":
			kw.setdefault("transfer_id", self.current["id"])
			kw.setdefault("expected_version", self.current["version"])
		result = getattr(api, method)(payload=payload, **kw)
		self.assertTrue(result["ok"], json.dumps(result, default=str))
		self.assertEqual(result, getattr(api, method)(payload=payload, **kw))
		self.current = result["data"]
		return self.current

	def create(self, qty=10, uom="Nos", factor=1, code=None):
		self.line = uuid.uuid4().hex
		return self.call(
			"create_transfer",
			dict(
				source_warehouse=self.source,
				target_warehouse=self.target,
				requester="Administrator",
				requested_date=nowdate(),
				priority="Normal",
				notes=self.suffix,
				items=[
					dict(
						line_id=self.line,
						item_code=code or self.item,
						uom=uom,
						conversion_factor=factor,
						requested_qty=qty,
					)
				],
			),
		)

	def prepare(self, qty=10, allocations=None):
		self.call("start_preparation", {})
		return self.call(
			"save_preparation",
			dict(
				ready=True, items=[dict(line_id=self.line, prepared_qty=qty, allocations=allocations or [])]
			),
		)

	def dispatch(self):
		return self.call(
			"dispatch_transfer",
			dict(
				driver_name="Manual driver",
				driver_contact="",
				vehicle="",
				packages=1,
				seal="",
				departure_time="2026-09-09T10:00:00Z",
				expected_arrival=None,
			),
		)

	def balance(self, warehouse):
		return float(
			frappe.db.get_value("Bin", {"item_code": self.item, "warehouse": warehouse}, "actual_qty") or 0
		)

	def test_partial_damage_receipts_replay_and_generic_guard(self):
		self.create()
		self.prepare()
		self.dispatch()
		self.assertEqual(
			(self.balance(self.source), self.balance(self.transit), self.balance(self.target)), (14, 10, 0)
		)
		kwargs = dict(
			transfer_id=self.current["id"],
			expected_version=self.current["version"],
			idempotency_key=uuid.uuid4().hex,
			payload={"items": [{"line_id": self.line, "accepted_qty": 6, "damaged_qty": 0}]},
		)
		first = api.receive_transfer(**kwargs)
		self.assertTrue(first["ok"], first)
		self.assertEqual(first, api.receive_transfer(**kwargs))
		self.current = first["data"]
		self.call(
			"receive_transfer",
			{
				"quarantine_warehouse": self.quarantine,
				"items": [dict(line_id=self.line, accepted_qty=3, damaged_qty=1, notes="Broken package")],
			},
		)
		self.assertEqual(self.current["status"], "Received")
		self.assertTrue(self.current["has_exceptions"])
		self.assertEqual(
			(self.balance(self.transit), self.balance(self.target), self.balance(self.quarantine)), (0, 9, 1)
		)
		self.assertEqual(len(self.current["documents"]), 4)
		self.assertEqual(len(self.current["history"]), 6)
		stock = frappe.get_doc("Stock Entry", self.current["documents"][0]["id"])
		with self.assertRaises(TransferError):
			stock.cancel()
		stock.reload()
		stock.custom_stock_transfer_order = None
		with self.assertRaises(TransferError):
			stock.save(ignore_permissions=True)

	def test_reservation_blocks_unrelated_issue_and_cancel_releases(self):
		self.create(24)
		self.prepare(24)
		entry = frappe.get_doc(
			dict(
				doctype="Stock Entry",
				stock_entry_type="Material Issue",
				company=self.company,
				items=[dict(item_code=self.item, qty=1, s_warehouse=self.source)],
			)
		)
		entry.insert(ignore_permissions=True)
		frappe.db.savepoint("unrelated_issue")
		with self.assertRaises(TransferError):
			entry.submit()
		frappe.db.rollback(save_point="unrelated_issue")
		self.call("cancel_transfer", {"reason": "No longer needed"})
		entry.reload()
		entry.submit()
		self.assertEqual(self.balance(self.source), 23)

	def test_reduction_and_loss(self):
		self.create(12)
		self.call("start_preparation", {})
		self.call(
			"save_preparation",
			dict(ready=False, items=[dict(line_id=self.line, prepared_qty=10, allocations=[])]),
		)
		self.call(
			"approve_reduction",
			dict(reason="Only ten needed", items=[dict(line_id=self.line, approved_qty=10)]),
		)
		self.assertFalse(self.current["ready"])
		self.assertEqual(self.current["items"][0]["requested_qty"], 12)
		self.call(
			"save_preparation",
			dict(ready=True, items=[dict(line_id=self.line, prepared_qty=10, allocations=[])]),
		)
		self.dispatch()
		self.call("receive_transfer", {"items": [dict(line_id=self.line, accepted_qty=8)]})
		self.call(
			"resolve_exception",
			dict(reason="Lost in transit", items=[dict(line_id=self.line, lost_qty=2, allocations=[])]),
		)
		self.assertEqual(self.current["status"], "Received")
		self.assertEqual(self.balance(self.transit), 0)
		self.assertEqual(self.current["items"][0]["lost_qty"], 2)
		loss = next(d for d in self.current["documents"] if d["kind"] == "loss")
		self.assertTrue(
			frappe.db.exists("GL Entry", {"voucher_no": loss["id"], "account": self.cfg.loss_expense_account})
		)

	def test_versions_conflicts_and_atomic_receipt_refusal(self):
		self.create()
		self.prepare()
		self.dispatch()
		before = self.current
		args = dict(
			transfer_id=before["id"],
			expected_version=before["version"],
			idempotency_key=uuid.uuid4().hex,
			payload={"items": [dict(line_id=self.line, accepted_qty=11)]},
		)
		result = api.receive_transfer(**args)
		self.assertEqual(result["meta"]["code"], "OVER_RECEIPT")
		self.assertEqual(api.get_transfer(before["id"])["data"]["version"], before["version"])
		self.call("record_arrival", {})
		self.assertEqual(api.receive_transfer(**args)["meta"]["code"], "VERSION_CONFLICT")
		args.update(
			expected_version=self.current["version"],
			payload={"items": [dict(line_id=self.line, accepted_qty=1)]},
		)
		ok = api.receive_transfer(**args)
		self.assertTrue(ok["ok"], ok)
		args["payload"]["items"][0]["accepted_qty"] = 2
		self.assertEqual(api.receive_transfer(**args)["meta"]["code"], "IDEMPOTENCY_CONFLICT")

	def test_uom_and_capabilities(self):
		self.create(2, "Box", 12)
		self.prepare(2)
		self.dispatch()
		entry = frappe.get_doc("Stock Entry", self.current["documents"][0]["id"])
		self.assertEqual(
			(entry.items[0].qty, entry.items[0].uom, entry.items[0].transfer_qty), (2, "Box", 24)
		)
		self.call("receive_transfer", {"items": [dict(line_id=self.line, accepted_qty=2)]})
		self.assertEqual(self.balance(self.target), 24)
		self.cfg.enabled = 0
		self.cfg.save(ignore_permissions=True)
		caps = api.get_capabilities()["data"]
		self.assertFalse(caps["enabled"])
		self.assertEqual(caps["actions"], [])
		self.assertEqual(api.get_transfer(self.current["id"])["data"]["allowed_actions"], [])
		listing = api.list_transfers(search=self.suffix)["data"]
		self.assertEqual(listing["total"], 1)
		self.assertEqual(listing["counts"]["Received"], 1)

	def managed_item(self, serial=False):
		code = (
			frappe.get_doc(
				dict(
					doctype="Item",
					item_code="_ST Managed " + self.suffix,
					stock_uom="Nos",
					is_stock_item=1,
					has_batch_no=1,
					has_serial_no=int(serial),
					item_group=frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
					valuation_rate=10,
					uoms=[{"uom": "Nos", "conversion_factor": 1}, {"uom": "Box", "conversion_factor": 12}],
				)
			)
			.insert(ignore_permissions=True)
			.name
		)
		batch = frappe.get_doc(dict(doctype="Batch", batch_id="_ST Batch " + self.suffix, item=code)).insert(
			ignore_permissions=True
		)
		serials = [f"_ST-{self.suffix}-{i}" for i in range(24)] if serial else []
		self.seed(code, 24, batch_no=batch.name, serial_no="\n".join(serials), use_serial_batch_fields=1)
		return code

	def test_batch_receipts_and_aggregate_reuse(self):
		self.item = self.managed_item()
		self.create(2, "Box", 12)
		options = api.get_allocations(self.current["id"], self.line, "prepare")
		self.assertTrue(options["ok"], options)
		pick = options["data"]["items"][0]["id"]
		self.prepare(2, [{"allocation_id": pick, "stock_qty": 24}])
		self.dispatch()
		pick = api.get_allocations(self.current["id"], self.line, "receive")["data"]["items"][0]["id"]
		invalid = api.receive_transfer(
			transfer_id=self.current["id"],
			expected_version=self.current["version"],
			idempotency_key=uuid.uuid4().hex,
			payload={
				"quarantine_warehouse": self.quarantine,
				"items": [
					dict(
						line_id=self.line,
						accepted_qty=1,
						damaged_qty=1,
						notes="Broken",
						accepted_allocations=[{"allocation_id": pick, "stock_qty": 12}],
						damaged_allocations=[{"allocation_id": pick, "stock_qty": 24}],
					)
				],
			},
		)
		self.assertEqual(invalid["meta"]["code"], "INVALID_ALLOCATION")
		self.assertEqual(self.balance(self.transit), 24)
		self.call(
			"receive_transfer",
			{
				"quarantine_warehouse": self.quarantine,
				"items": [
					dict(
						line_id=self.line,
						accepted_qty=1,
						damaged_qty=1,
						notes="Broken",
						accepted_allocations=[{"allocation_id": pick, "stock_qty": 12}],
						damaged_allocations=[{"allocation_id": pick, "stock_qty": 12}],
					)
				],
			},
		)
		self.assertEqual(self.current["status"], "Received")
		self.assertEqual(
			(self.balance(self.target), self.balance(self.quarantine), self.balance(self.transit)),
			(12, 12, 0),
		)

	def test_serial_bundle_preserves_box_uom_and_reserved_serial(self):
		self.item = self.managed_item(serial=True)
		self.create(1, "Box", 12)
		options = api.get_allocations(self.current["id"], self.line, "prepare")["data"]["items"]
		picks = [{"allocation_id": a["id"], "stock_qty": 1} for a in options[:12]]
		self.prepare(1, picks)
		row = options[0]
		entry = frappe.get_doc(
			dict(
				doctype="Stock Entry",
				company=self.company,
				stock_entry_type="Material Issue",
				items=[
					dict(
						item_code=self.item,
						qty=1,
						s_warehouse=self.source,
						use_serial_batch_fields=1,
						serial_no=row["serial_no"],
						batch_no=row["batch_no"],
					)
				],
			)
		)
		entry.insert(ignore_permissions=True)
		frappe.db.savepoint("serial_issue")
		with self.assertRaises(TransferError):
			entry.submit()
		frappe.db.rollback(save_point="serial_issue")
		self.dispatch()
		stock = frappe.get_doc("Stock Entry", self.current["documents"][0]["id"])
		self.assertEqual(len(stock.items), 1)
		self.assertEqual(
			(stock.items[0].uom, stock.items[0].qty, stock.items[0].transfer_qty), ("Box", 1, 12)
		)
		options = api.get_allocations(self.current["id"], self.line, "receive")["data"]["items"]
		self.call(
			"receive_transfer",
			{
				"items": [
					dict(
						line_id=self.line,
						accepted_qty=1,
						accepted_allocations=[dict(allocation_id=a["id"], stock_qty=1) for a in options],
					)
				]
			},
		)
		for row in options:
			self.assertEqual(frappe.db.get_value("Serial No", row["serial_no"], "warehouse"), self.target)

	def test_damage_posting_failure_rolls_back_accepted_posting(self):
		from pet_app.stock_transfer import inventory
		from pet_app.stock_transfer.rules import fail

		self.create()
		self.prepare()
		self.dispatch()
		original = inventory.post

		def fail_damage(order, cfg, kind, movements):
			if kind == "damage":
				fail("Quarantine posting refused.")
			return original(order, cfg, kind, movements)

		with patch.object(inventory, "post", side_effect=fail_damage):
			result = api.receive_transfer(
				transfer_id=self.current["id"],
				expected_version=self.current["version"],
				idempotency_key=uuid.uuid4().hex,
				payload={
					"quarantine_warehouse": self.quarantine,
					"items": [dict(line_id=self.line, accepted_qty=8, damaged_qty=2, notes="Broken")],
				},
			)
		self.assertFalse(result["ok"])
		self.assertEqual((self.balance(self.target), self.balance(self.transit)), (0, 10))
		after = api.get_transfer(self.current["id"])["data"]
		self.assertEqual(after["version"], self.current["version"])
		self.assertEqual(len(after["documents"]), 1)
		self.assertEqual(after["items"][0]["accepted_qty"], 0)

	def test_native_permissions_and_target_only_scope(self):
		self.create()
		self.prepare()
		self.dispatch()
		user = frappe.get_doc(
			dict(
				doctype="User",
				email=f"st-{self.suffix}@example.com",
				first_name="Transfer staff",
				enabled=1,
				send_welcome_email=0,
				user_type="System User",
				roles=[{"role": "Stock User"}],
			)
		).insert(ignore_permissions=True)
		frappe.get_doc(
			dict(
				doctype="User Permission",
				user=user.name,
				allow="Warehouse",
				for_value=self.target,
				apply_to_all_doctypes=1,
			)
		).insert(ignore_permissions=True)
		frappe.set_user(user.name)
		try:
			viewed = api.get_transfer(self.current["id"])
			self.assertTrue(viewed["ok"], viewed)
			self.assertIsNone(viewed["data"]["items"][0]["current_stock"])
			actions = viewed["data"]["allowed_actions"]
			self.assertIn("receive_transfer", actions)
			self.assertNotIn("update_shipment", actions)
			self.assertNotIn("resolve_exception", actions)
			self.assertEqual(api.list_transfers(search=self.suffix)["data"]["total"], 1)
			denied = api.resolve_exception(
				transfer_id=self.current["id"],
				expected_version=self.current["version"],
				idempotency_key=uuid.uuid4().hex,
				payload={"reason": "Missing", "items": [dict(line_id=self.line, lost_qty=10)]},
			)
			self.assertEqual(denied["meta"]["code"], "PERMISSION_DENIED")
			self.call("receive_transfer", {"items": [dict(line_id=self.line, accepted_qty=10)]})
			self.assertEqual(self.current["received_by"], user.name)
			frappe.set_user("Administrator")
			user.reload()
			user.set("roles", [])
			user.save(ignore_permissions=True)
			frappe.clear_cache(user=user.name)
			frappe.set_user(user.name)
			self.assertEqual(api.get_transfer(self.current["id"])["meta"]["code"], "PERMISSION_DENIED")
		finally:
			frappe.set_user("Administrator")

	def test_competing_preparation_snapshot_and_counts(self):
		self.create(24)
		self.prepare(24)
		first = self.current.copy()
		first_line = self.line
		self.create(1)
		self.call("start_preparation", {})
		refused = api.save_preparation(
			transfer_id=self.current["id"],
			expected_version=self.current["version"],
			idempotency_key=uuid.uuid4().hex,
			payload={"ready": True, "items": [dict(line_id=self.line, prepared_qty=1, allocations=[])]},
		)
		self.assertEqual(refused["meta"]["code"], "INSUFFICIENT_STOCK")
		view = api.list_transfers(search=self.suffix, status="Ordered", limit=1)["data"]
		self.assertEqual(view["total"], 0)
		self.assertEqual(view["counts"]["Preparing"], 2)
		self.current, self.line = first, first_line
		self.call(
			"save_preparation",
			{"ready": True, "items": [dict(line_id=self.line, prepared_qty=24, allocations=[])]},
		)
		held = frappe.get_all(
			"Stock Transfer Reservation", filters={"transfer": first["id"]}, fields=["stock_qty"]
		)
		self.assertEqual(sum(float(r.stock_qty) for r in held), 24)

	def test_sales_and_medication_cannot_consume_reserved_stock(self):
		from pet_app.stock_transfer.guards import native_reservation_guard
		from pet_app.utils.medication_stock import _post_stock_entry

		self.create(24)
		self.prepare(24)
		frappe.db.savepoint("medication_issue")
		with self.assertRaises(TransferError):
			_post_stock_entry(
				entry_type="Material Issue",
				item_code=self.item,
				qty=1,
				warehouse=self.source,
				is_issue=True,
				remark="Transfer reservation test",
				label="Test medication",
			)
		frappe.db.rollback(save_point="medication_issue")
		with self.assertRaises(TransferError):
			native_reservation_guard(
				frappe._dict(
					name="probe",
					item_code=self.item,
					warehouse=self.source,
					reserved_qty=1,
					delivered_qty=0,
					sb_entries=[],
				)
			)
		customer = frappe.get_doc(
			dict(
				doctype="Customer",
				customer_name="_ST Customer " + self.suffix,
				customer_type="Company",
				customer_group=frappe.db.get_value("Customer Group", {}, "name"),
				territory=frappe.db.get_value("Territory", {}, "name"),
			)
		).insert(ignore_permissions=True)
		branch = frappe.get_doc(
			dict(doctype="Branch", branch="_ST " + self.suffix, custom_wharehouse=self.source)
		).insert(ignore_permissions=True)
		invoice = frappe.get_doc(
			dict(
				doctype="Sales Invoice",
				branch=branch.name,
				company=self.company,
				customer=customer.name,
				update_stock=1,
				items=[dict(item_code=self.item, qty=1, rate=10, warehouse=self.source)],
			)
		)
		invoice.insert(ignore_permissions=True)
		frappe.db.savepoint("sales_issue")
		with self.assertRaises(TransferError):
			invoice.submit()
		frappe.db.rollback(save_point="sales_issue")
		self.assertEqual(self.balance(self.source), 24)

	def test_order_edit_and_shipment_edit_keep_line_and_departure(self):
		self.create()
		self.call(
			"update_transfer",
			dict(
				source_warehouse=self.source,
				target_warehouse=self.target,
				requester="Administrator",
				requested_date=nowdate(),
				priority="High",
				notes="Edited " + self.suffix,
				items=[
					dict(
						line_id=self.line,
						item_code=self.item,
						uom="Nos",
						conversion_factor=1,
						requested_qty=10,
					)
				],
			),
		)
		self.assertEqual(self.current["items"][0]["line_id"], self.line)
		self.prepare()
		self.dispatch()
		old_departure = self.current["shipment"]["departure_time"]
		self.call("update_shipment", {**self.current["shipment"], "vehicle": "Replacement vehicle"})
		self.assertEqual(self.current["shipment"]["departure_time"], old_departure)
		self.call("record_arrival", {})
		self.assertIsNotNone(self.current["arrived_at"])

	def test_never_stocked_line_is_named_and_reducible(self):
		"""Regression for ST-2026-892CBAF517E54CF7: one dead line froze a 108-line order.

		The item existed in the catalogue but had never had a stock ledger entry, so Ready
		was unreachable and nothing on screen said which of the lines was to blame.
		"""
		dead = (
			frappe.get_doc(
				dict(
					doctype="Item",
					item_code=f"_ST dead {self.suffix}",
					item_name=f"Never stocked {self.suffix}",
					stock_uom="Nos",
					is_stock_item=1,
					item_group=frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
					valuation_rate=10,
					uoms=[{"uom": "Nos", "conversion_factor": 1}],
				)
			)
			.insert(ignore_permissions=True)
			.name
		)
		live_line, dead_line = uuid.uuid4().hex, uuid.uuid4().hex
		order = self.call(
			"create_transfer",
			dict(
				source_warehouse=self.source,
				target_warehouse=self.target,
				requester="Administrator",
				requested_date=nowdate(),
				priority="Normal",
				notes=self.suffix,
				items=[
					dict(line_id=live_line, item_code=self.item, uom="Nos", conversion_factor=1, requested_qty=10),
					dict(line_id=dead_line, item_code=dead, uom="Nos", conversion_factor=1, requested_qty=7),
				],
			),
		)
		lines = {r["line_id"]: r for r in order["items"]}
		# Flagged the moment it is created, before anyone starts picking. Nothing is prepared
		# yet at Ordered, so unfinished picking must stay silent and leave only the real fault.
		self.assertEqual([i["code"] for i in lines[dead_line]["issues"]], ["INSUFFICIENT_STOCK"])
		self.assertIn("7", lines[dead_line]["issues"][0]["message"])
		self.assertEqual(lines[live_line]["issues"], [])

		started = self.call("start_preparation", {})
		lines = {r["line_id"]: r for r in started["items"]}
		self.assertEqual(
			[i["code"] for i in lines[dead_line]["issues"]], ["INSUFFICIENT_STOCK", "NOT_PREPARED"]
		)
		self.assertEqual([i["code"] for i in lines[live_line]["issues"]], ["NOT_PREPARED"])
		full = dict(
			ready=True,
			items=[
				dict(line_id=live_line, prepared_qty=10, allocations=[]),
				dict(line_id=dead_line, prepared_qty=7, allocations=[]),
			],
		)
		refused = api.save_preparation(
			payload=full,
			transfer_id=self.current["id"],
			expected_version=self.current["version"],
			idempotency_key=uuid.uuid4().hex,
		)
		self.assertFalse(refused["ok"])
		self.assertEqual(refused["meta"]["code"], "INSUFFICIENT_STOCK")
		self.assertIn(dead, refused["errors"][0])
		self.assertNotIn(self.item, refused["errors"][0])

		# The documented escape hatch: reduce the dead line away, then Ready becomes reachable.
		self.call(
			"approve_reduction",
			dict(
				reason="Never stocked",
				items=[
					dict(line_id=live_line, approved_qty=10),
					dict(line_id=dead_line, approved_qty=0),
				],
			),
		)
		reduced = {r["line_id"]: r for r in self.current["items"]}
		# Zeroed away, so it no longer blocks: this is the supported "remove the item".
		self.assertEqual(reduced[dead_line]["issues"], [])
		self.assertEqual(reduced[dead_line]["approved_qty"], 0)
		ready = self.call(
			"save_preparation",
			dict(
				ready=True,
				items=[
					dict(line_id=live_line, prepared_qty=10, allocations=[]),
					dict(line_id=dead_line, prepared_qty=0, allocations=[]),
				],
			),
		)
		self.assertTrue(ready["ready"])
		self.assertEqual([r["issues"] for r in ready["items"]], [[], []])
		self.assertIn("dispatch_transfer", ready["allowed_actions"])

	def test_shortage_refusal_names_every_short_item(self):
		second = (
			frappe.get_doc(
				dict(
					doctype="Item",
					item_code=f"_ST thin {self.suffix}",
					item_name=f"Thin {self.suffix}",
					stock_uom="Nos",
					is_stock_item=1,
					item_group=frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
					valuation_rate=10,
					uoms=[{"uom": "Nos", "conversion_factor": 1}],
				)
			)
			.insert(ignore_permissions=True)
			.name
		)
		self.seed(second, 2)
		a, b = uuid.uuid4().hex, uuid.uuid4().hex
		self.call(
			"create_transfer",
			dict(
				source_warehouse=self.source,
				target_warehouse=self.target,
				requester="Administrator",
				requested_date=nowdate(),
				priority="Normal",
				notes=self.suffix,
				items=[
					dict(line_id=a, item_code=self.item, uom="Nos", conversion_factor=1, requested_qty=99),
					dict(line_id=b, item_code=second, uom="Nos", conversion_factor=1, requested_qty=99),
				],
			),
		)
		self.call("start_preparation", {})
		refused = api.save_preparation(
			payload=dict(
				ready=False,
				items=[
					dict(line_id=a, prepared_qty=99, allocations=[]),
					dict(line_id=b, prepared_qty=99, allocations=[]),
				],
			),
			transfer_id=self.current["id"],
			expected_version=self.current["version"],
			idempotency_key=uuid.uuid4().hex,
		)
		self.assertFalse(refused["ok"])
		# Both, in one refusal: discovering shortages one attempt at a time is unusable.
		self.assertIn(self.item, refused["errors"][0])
		self.assertIn(second, refused["errors"][0])

	def test_refused_mutations_are_recorded_and_reads_are_not(self):
		"""A refusal returns ok:false without raising, so nothing else in Frappe logs it."""
		self.create()
		self.call("start_preparation", {})
		before = frappe.db.count("Error Log")
		refused = api.save_preparation(
			payload=dict(ready=True, items=[dict(line_id=self.line, prepared_qty=1, allocations=[])]),
			transfer_id=self.current["id"],
			expected_version=self.current["version"],
			idempotency_key=uuid.uuid4().hex,
		)
		self.assertFalse(refused["ok"])
		logged = frappe.get_all(
			"Error Log",
			filters={"reference_name": self.current["id"]},
			fields=["method", "error"],
			order_by="creation desc",
		)
		self.assertEqual(len(logged), 1)
		self.assertIn("save_preparation", logged[0]["method"])
		self.assertIn(refused["meta"]["code"], logged[0]["method"])
		self.assertIn(f"actor: {frappe.session.user}", logged[0]["error"])
		self.assertIn(self.line, logged[0]["error"])

		# Reads are chatty; even a refused one must not add a row.
		missing = api.get_transfer(transfer_id="ST-2026-NOPE")
		self.assertFalse(missing["ok"])
		self.assertEqual(frappe.db.count("Error Log"), before + 1)

	def test_setup_api_validation_and_stale_save(self):
		from pet_app.api import stock_transfer_settings as setup

		result = setup.get_setup(self.company)
		self.assertTrue(result["ok"], result)
		data = result["data"]
		self.assertTrue(data["readiness"]["enabled"])
		self.assertTrue(data["options"]["warehouses"])
		payload = {k: v for k, v in data["settings"].items() if k != "company"}
		invalid = setup.save_setup(
			self.company, data["version"], {**payload, "quarantine_warehouses": [self.transit]}
		)
		self.assertEqual(invalid["meta"]["code"], "WAREHOUSE_MISMATCH")
		self.assertEqual(setup.get_setup(self.company)["data"]["version"], data["version"])
		saved = setup.save_setup(self.company, data["version"], {**payload, "enabled": False})
		self.assertTrue(saved["ok"], saved)
		self.assertFalse(saved["data"]["readiness"]["enabled"])
		self.assertTrue(saved["data"]["readiness"]["ready"])
		self.assertEqual(
			setup.save_setup(self.company, data["version"], payload)["meta"]["code"], "VERSION_CONFLICT"
		)
		self.assertFalse(setup.get_status(self.company)["data"]["enabled"])

	def test_setup_draft_missing_fields_and_direct_save_guard(self):
		from pet_app.api import stock_transfer_settings as setup

		data = setup.get_setup(self.company)["data"]
		payload = dict(
			enabled=False,
			reservation_tests_passed=False,
			transit_warehouse=None,
			quarantine_warehouses=[],
			loss_expense_account=None,
			cost_center=None,
			public_app_url="",
		)
		saved = setup.save_setup(self.company, data["version"], payload)
		self.assertTrue(saved["ok"], saved)
		fields = {i["field"] for i in saved["data"]["readiness"]["issues"]}
		self.assertEqual(
			fields,
			{
				"transit_warehouse",
				"quarantine_warehouses",
				"loss_expense_account",
				"cost_center",
				"public_app_url",
				"reservation_tests_passed",
			},
		)
		self.cfg.reload()
		self.cfg.enabled = 1
		with self.assertRaises(TransferError):
			self.cfg.save(ignore_permissions=True)
		self.assertFalse(setup.get_status(self.company)["data"]["enabled"])

	def test_stock_staff_setup_is_readable_status_only(self):
		from pet_app.api import stock_transfer_settings as setup

		user = frappe.get_doc(
			dict(
				doctype="User",
				email=f"st-setup-{self.suffix}@example.com",
				first_name="Stock staff",
				enabled=1,
				send_welcome_email=0,
				user_type="System User",
				roles=[{"role": "Stock User"}],
			)
		).insert(ignore_permissions=True)
		frappe.set_user(user.name)
		try:
			status = setup.get_status(self.company)
			self.assertTrue(status["ok"], status)
			self.assertFalse(status["data"]["can_configure"])
			self.assertNotIn("settings", status["data"])
			self.assertEqual(setup.get_setup(self.company)["meta"]["code"], "PERMISSION_DENIED")
			self.assertEqual(setup.save_setup(self.company, None, {})["meta"]["code"], "PERMISSION_DENIED")
		finally:
			frappe.set_user("Administrator")


def run():
	suite = unittest.TestSuite(
		[unittest.defaultTestLoader.loadTestsFromTestCase(c) for c in (TestTransferRules, TestStockTransfer)]
	)
	result = unittest.TextTestRunner(verbosity=2).run(suite)
	if not result.wasSuccessful():
		raise AssertionError("Stock transfer tests failed")
	return {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors)}
