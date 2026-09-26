"""The counter sale is the case where a NEW invoice per transaction is correct.

`sales_invoice_guard.before_insert` refuses direct Sales Invoice creation because every
billable event used to open its own document - one customer collected ten invoices in
sixty-eight minutes. `create_pos_sale` is the named exception, and these tests cover the
four ways that exception could quietly go wrong and cost money rather than throw:

* A **Due** counter sale is `is_pos = 0`, which is exactly the shape
  `invoice_reuse.get_or_create_open_invoice` reuses. Without `force_new` it would append
  the counter's goods to a clinic draft nobody submits at the till - the goods sold, the
  shelf never relieved, and no error anywhere. This is the reason the endpoint exists in
  the form it does, so it is tested against a real open draft rather than in the abstract.

* `SalesInvoice.before_save` runs `set_account_for_mode_of_payment()`, which rewrites
  every payments row to the Mode of Payment's COMPANY default. Both tills share one mode
  of payment, so the Hotel's takings posted to the Store's drawer and reconciled to a
  plausible balance in the wrong branch. The assertion is on the GL Entry, not on the
  document field, because the document field is not what the money follows.

* Partial payment is refused by ERPNext only for `is_created_using_pos`, which this flow
  deliberately does not set. The profile's `allow_partial_payment` is therefore enforced
  here or nowhere.

* The guard must still be shut. A test that only proves the new door opens would not
  notice the old one being left open beside it.

Nothing here commits; IntegrationTestCase rolls each test back.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import cstr, flt

from pet_app.api.pos import create_pos_sale, settle_open_invoice, settle_open_invoices
from pet_app.api.sales import create_manual_sales_invoice
from pet_app.utils import sales_invoice_guard
from pet_app.utils.invoice_reuse import get_or_create_open_invoice

HOTEL_PROFILE = "Alkokh Vet Hotel - Cashier"
STORE_PROFILE = "Alkokh Vet Store - Main Cashier"


@contextmanager
def as_user(user: str):
	"""Move the session user and put it back.

	`frappe.set_user` insists on a real User row, and these tests are about the guard's
	decision rather than about any particular account existing on the site.
	"""
	previous = frappe.session.user
	frappe.session.user = user
	try:
		yield
	finally:
		frappe.session.user = previous


def _ok(response):
	return bool(response.get("ok"))


def _code(response):
	return (response.get("meta") or {}).get("code")


class PosSaleBase(IntegrationTestCase):
	"""Fixtures that do not depend on the site's live POS Profile configuration.

	The profiles are read, then overwritten for the duration of each test. Reading a
	value the site happens to hold today would make these tests pass or fail for reasons
	that have nothing to do with the code under test.
	"""

	def setUp(self):
		super().setUp()
		self.profile = frappe.get_doc("POS Profile", HOTEL_PROFILE)
		self.company = self.profile.company
		self.till_account = self.profile.get("custom_cash_account")

		frappe.db.set_value(
			"POS Profile", HOTEL_PROFILE, {"branch": "hotel", "allow_partial_payment": 1}
		)
		frappe.clear_cache()

		self.customer = self._customer()
		self.item = self._service_item()

	def _customer(self) -> str:
		name = "_Test POS Counter Customer"
		if not frappe.db.exists("Customer", name):
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": name,
					"customer_type": "Individual",
					"customer_group": frappe.db.get_value("Customer Group", {}, "name"),
					"territory": frappe.db.get_value("Territory", {}, "name"),
				}
			).insert(ignore_permissions=True)
		return name

	def _service_item(self) -> str:
		"""A NON-stock item on purpose.

		Stock relief is covered by its own test. Everywhere else it would only add a
		warehouse balance the assertion does not care about, and a negative-stock throw
		that has nothing to do with what is being tested.
		"""
		code = "_Test POS Counter Service"
		if not frappe.db.exists("Item", code):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": code,
					"item_name": code,
					"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
					"stock_uom": "Nos",
					"is_stock_item": 0,
				}
			).insert(ignore_permissions=True)
		return code

	def _sell(self, **overrides):
		payload = {
			"customer": self.customer,
			"pos_profile": HOTEL_PROFILE,
			"payment_status": "Paid",
			"company": self.company,
			"items": json.dumps([{"item_code": self.item, "qty": 2, "rate": 1000}]),
			"paid_amount": 2000,
		}
		payload.update(overrides)
		return create_pos_sale(**payload)


class TestPosSaleCreation(PosSaleBase):
	def test_paid_sale_is_created_and_submitted_in_one_call(self):
		response = self._sell()
		self.assertTrue(_ok(response), msg=response)

		invoice = frappe.get_doc("Sales Invoice", response["data"]["created_invoice"])
		self.assertEqual(invoice.docstatus, 1, "the sale must not be left as a draft")
		self.assertEqual(invoice.is_pos, 1)
		self.assertEqual(flt(invoice.paid_amount), 2000.0)
		self.assertEqual(flt(invoice.outstanding_amount), 0.0)

	def test_takings_are_credited_to_the_till_that_took_them(self):
		"""before_save rewrites payments.account to the mode of payment's company default.

		Asserted on the GL Entry because make_pos_gl_entries debits `payment_mode.account`
		directly - the ledger is what the money follows, and a document field that agrees
		with the ledger by accident would still pass a weaker assertion.
		"""
		response = self._sell()
		self.assertTrue(_ok(response), msg=response)
		name = response["data"]["created_invoice"]

		debited = frappe.get_all(
			"GL Entry",
			filters={"voucher_no": name, "debit": [">", 0]},
			pluck="account",
		)
		self.assertIn(
			self.till_account,
			debited,
			f"the Hotel till's cash was credited elsewhere: {debited}",
		)

	def test_branch_comes_from_the_profile_not_the_cashier(self):
		response = self._sell()
		self.assertTrue(_ok(response), msg=response)
		self.assertEqual(
			frappe.db.get_value("Sales Invoice", response["data"]["created_invoice"], "branch"),
			"hotel",
		)

	def test_paid_amount_is_clamped_to_the_grand_total(self):
		"""Over-tendering is change in the drawer, not revenue."""
		response = self._sell(paid_amount=5000)
		self.assertTrue(_ok(response), msg=response)

		invoice = frappe.get_doc("Sales Invoice", response["data"]["created_invoice"])
		self.assertEqual(flt(invoice.paid_amount), 2000.0)
		self.assertEqual(flt(response["data"]["receipt"]["payment"]["change"]), 3000.0)

	def test_partial_payment_is_allowed_when_the_profile_allows_it(self):
		response = self._sell(paid_amount=500)
		self.assertTrue(_ok(response), msg=response)

		invoice = frappe.get_doc("Sales Invoice", response["data"]["created_invoice"])
		self.assertEqual(flt(invoice.paid_amount), 500.0)
		self.assertEqual(flt(invoice.outstanding_amount), 1500.0)

	def test_partial_payment_is_refused_when_the_profile_forbids_it(self):
		frappe.db.set_value("POS Profile", HOTEL_PROFILE, "allow_partial_payment", 0)
		frappe.clear_cache()

		response = self._sell(paid_amount=500)
		self.assertFalse(_ok(response))
		self.assertEqual(_code(response), "POS_PARTIAL_PAYMENT_NOT_ALLOWED")

	def test_stock_is_relieved_on_submit(self):
		item = "_Test POS Counter Goods"
		if not frappe.db.exists("Item", item):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": item,
					"item_name": item,
					"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
					"stock_uom": "Nos",
					"is_stock_item": 1,
				}
			).insert(ignore_permissions=True)

		warehouse = self.profile.warehouse
		receipt = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Receipt",
				"company": self.company,
				"items": [
					{"item_code": item, "qty": 10, "t_warehouse": warehouse, "basic_rate": 100}
				],
			}
		)
		receipt.insert(ignore_permissions=True)
		receipt.submit()

		response = self._sell(
			items=json.dumps([{"item_code": item, "qty": 3, "rate": 500}]), paid_amount=1500
		)
		self.assertTrue(_ok(response), msg=response)

		moved = frappe.get_all(
			"Stock Ledger Entry",
			filters={"voucher_no": response["data"]["created_invoice"]},
			fields=["item_code", "warehouse", "actual_qty"],
		)
		self.assertEqual(len(moved), 1)
		self.assertEqual(flt(moved[0].actual_qty), -3.0)
		self.assertEqual(moved[0].warehouse, warehouse)

	def test_a_sale_for_goods_that_are_not_there_is_refused_before_anything_is_written(self):
		if frappe.db.get_single_value("Stock Settings", "allow_negative_stock"):
			self.skipTest("negative stock is permitted on this site")

		item = "_Test POS Counter Empty Shelf"
		if not frappe.db.exists("Item", item):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": item,
					"item_name": item,
					"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
					"stock_uom": "Nos",
					"is_stock_item": 1,
				}
			).insert(ignore_permissions=True)

		before = frappe.db.count("Sales Invoice")
		response = self._sell(
			items=json.dumps([{"item_code": item, "qty": 5, "rate": 100}]), paid_amount=500
		)
		self.assertFalse(_ok(response))
		self.assertEqual(_code(response), "INSUFFICIENT_STOCK")
		self.assertEqual(frappe.db.count("Sales Invoice"), before, "nothing should have been written")


class TestDueCounterSale(PosSaleBase):
	"""A Due sale is is_pos = 0 - the exact shape invoice_reuse appends to."""

	def _open_clinic_draft(self):
		return get_or_create_open_invoice(
			customer=self.customer,
			items=[{"item_code": self.item, "qty": 1, "rate": 500}],
			source_doctype="Guardian",
			source_name="_probe",
			company=self.company,
			branch="hotel",
			requires_stock=False,
			ignore_permissions=True,
			branch_authorised=True,
		).invoice

	def test_due_sale_does_not_append_to_the_customers_open_clinic_draft(self):
		draft = self._open_clinic_draft()
		rows_before = frappe.db.count("Sales Invoice Item", {"parent": draft.name})
		modified_before = frappe.db.get_value("Sales Invoice", draft.name, "modified")

		response = self._sell(payment_status="Due", paid_amount=0)
		self.assertTrue(_ok(response), msg=response)

		created = response["data"]["created_invoice"]
		self.assertNotEqual(created, draft.name, "the counter sale went into the clinic's draft")
		self.assertEqual(
			frappe.db.count("Sales Invoice Item", {"parent": draft.name}),
			rows_before,
			"the clinic draft gained a counter line",
		)
		self.assertEqual(
			frappe.db.get_value("Sales Invoice", draft.name, "modified"),
			modified_before,
			"the clinic draft was written to",
		)
		self.assertEqual(frappe.db.get_value("Sales Invoice", draft.name, "docstatus"), 0)

	def test_due_sale_is_submitted_and_left_outstanding(self):
		response = self._sell(payment_status="Due", paid_amount=0)
		self.assertTrue(_ok(response), msg=response)

		invoice = frappe.get_doc("Sales Invoice", response["data"]["created_invoice"])
		self.assertEqual(invoice.docstatus, 1)
		self.assertEqual(invoice.is_pos, 0)
		self.assertEqual(len(invoice.payments), 0)
		self.assertEqual(flt(invoice.outstanding_amount), flt(invoice.grand_total))
		# is_pos is 0, so the till is recorded on the custom field or not at all.
		self.assertEqual(invoice.get("custom_pos_profile"), HOTEL_PROFILE)

	def test_a_due_sale_carrying_money_is_refused_rather_than_reinterpreted(self):
		response = self._sell(payment_status="Due", paid_amount=500)
		self.assertFalse(_ok(response))
		self.assertEqual(_code(response), "DUE_SALE_CANNOT_CARRY_PAYMENT")


class TestSettlementCounterAutoSplit(PosSaleBase):
	def _stock_item(self) -> str:
		item = "_Test POS Auto Split Goods"
		if not frappe.db.exists("Item", item):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": item,
					"item_name": item,
					"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
					"stock_uom": "Nos",
					"is_stock_item": 1,
				}
			).insert(ignore_permissions=True)
		return item

	def _stock_up(self, item: str, qty: float = 10):
		receipt = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Receipt",
				"company": self.company,
				"items": [
					{
						"item_code": item,
						"qty": qty,
						"t_warehouse": self.profile.warehouse,
						"basic_rate": 100,
					}
				],
			}
		)
		receipt.insert(ignore_permissions=True)
		receipt.submit()

	def _open_clinic_draft(self, *, requires_stock=False, item=None, rate=500):
		result = get_or_create_open_invoice(
			customer=self.customer,
			items=[{"item_code": item or self.item, "qty": 1, "rate": rate}],
			source_doctype="Guardian",
			source_name="_auto_split",
			company=self.company,
			branch="hotel",
			requires_stock=requires_stock,
			force_new=True,
			ignore_permissions=True,
			branch_authorised=True,
		)
		return result.invoice

	def _settle(self, invoice, *, extra_items, payment_status="Paid", paid_amount=None, key=None):
		return settle_open_invoice(
			invoice=invoice.name,
			pos_profile=HOTEL_PROFILE,
			payment_status=payment_status,
			paid_amount=paid_amount if paid_amount is not None else flt(invoice.grand_total),
			expected_modified=frappe.db.get_value("Sales Invoice", invoice.name, "modified"),
			company=self.company,
			customer=self.customer,
			extra_items=json.dumps(extra_items),
			idempotency_key=key,
		)

	def test_stock_counter_item_on_non_stock_clinic_invoice_creates_separate_pos_invoice(self):
		item = self._stock_item()
		self._stock_up(item)
		clinic = self._open_clinic_draft()

		response = self._settle(
			clinic,
			extra_items=[{"item_code": item, "qty": 3, "rate": 500}],
			paid_amount=2000,
			key="auto-split-paid",
		)
		self.assertTrue(_ok(response), msg=response)

		data = response["data"]
		counter = data["created_counter_invoice"]
		self.assertTrue(data["auto_split_counter_items"])
		self.assertEqual(data["settled_invoice_ids"], [clinic.name, counter])
		self.assertEqual(frappe.db.count("Sales Invoice Item", {"parent": clinic.name}), 1)

		counter_doc = frappe.get_doc("Sales Invoice", counter)
		self.assertEqual(counter_doc.docstatus, 1)
		self.assertEqual(counter_doc.update_stock, 1)
		self.assertEqual(counter_doc.items[0].item_code, item)

		moved = frappe.get_all(
			"Stock Ledger Entry",
			filters={"voucher_no": counter, "item_code": item},
			fields=["warehouse", "actual_qty"],
		)
		self.assertEqual(len(moved), 1)
		self.assertEqual(moved[0].warehouse, self.profile.warehouse)
		self.assertEqual(flt(moved[0].actual_qty), -3.0)

		block_keys = [block["key"] for block in data["receipt"]["blocks"]]
		self.assertEqual(block_keys, ["clinic", "counter"])

	def test_due_auto_split_submits_counter_invoice_without_payment_rows(self):
		item = self._stock_item()
		self._stock_up(item)
		clinic = self._open_clinic_draft()

		response = self._settle(
			clinic,
			extra_items=[{"item_code": item, "qty": 1, "rate": 500}],
			payment_status="Due",
			paid_amount=0,
			key="auto-split-due",
		)
		self.assertTrue(_ok(response), msg=response)

		counter = frappe.get_doc("Sales Invoice", response["data"]["created_counter_invoice"])
		self.assertEqual(counter.docstatus, 1)
		self.assertEqual(counter.update_stock, 1)
		self.assertEqual(len(counter.payments), 0)
		self.assertEqual(flt(counter.outstanding_amount), flt(counter.grand_total))

	def test_non_stock_counter_item_still_appends_to_clinic_invoice(self):
		clinic = self._open_clinic_draft()
		response = self._settle(
			clinic,
			extra_items=[{"item_code": self.item, "qty": 2, "rate": 100}],
			paid_amount=700,
			key="append-service-counter",
		)
		self.assertTrue(_ok(response), msg=response)
		self.assertFalse(response["data"]["auto_split_counter_items"])
		self.assertIsNone(response["data"]["created_counter_invoice"])
		self.assertEqual(frappe.db.count("Sales Invoice Item", {"parent": clinic.name}), 2)

	def test_stock_moving_clinic_invoice_still_takes_counter_stock_item_on_same_invoice(self):
		item = self._stock_item()
		self._stock_up(item, qty=20)
		clinic = self._open_clinic_draft(requires_stock=True, item=item, rate=500)
		self.assertEqual(frappe.db.get_value("Sales Invoice", clinic.name, "update_stock"), 1)

		response = self._settle(
			clinic,
			extra_items=[{"item_code": item, "qty": 2, "rate": 500}],
			paid_amount=1500,
			key="same-stock-invoice",
		)
		self.assertTrue(_ok(response), msg=response)
		self.assertFalse(response["data"]["auto_split_counter_items"])
		self.assertEqual(response["data"]["settled_invoice_ids"], [clinic.name])
		self.assertEqual(frappe.db.count("Sales Invoice Item", {"parent": clinic.name}), 2)

	def test_auto_split_retry_returns_existing_counter_invoice(self):
		item = self._stock_item()
		self._stock_up(item)
		clinic = self._open_clinic_draft()

		first = self._settle(
			clinic,
			extra_items=[{"item_code": item, "qty": 1, "rate": 500}],
			paid_amount=1000,
			key="auto-split-retry",
		)
		self.assertTrue(_ok(first), msg=first)

		second = self._settle(
			clinic,
			extra_items=[{"item_code": item, "qty": 1, "rate": 500}],
			paid_amount=1000,
			key="auto-split-retry",
		)
		self.assertTrue(_ok(second), msg=second)
		self.assertTrue(second["data"]["replayed"])
		self.assertEqual(second["data"]["created_counter_invoice"], first["data"]["created_counter_invoice"])
		self.assertEqual(
			frappe.db.count(
				"Sales Invoice",
				{"remarks": ["like", "%[alkokh-pos-sale:auto-split-retry]%"]},
			),
			2,
		)

	def test_merged_non_stock_clinic_invoices_auto_split_counter_stock(self):
		item = self._stock_item()
		self._stock_up(item)
		first = self._open_clinic_draft(rate=500)
		second = self._open_clinic_draft(rate=700)

		response = settle_open_invoices(
			invoices=json.dumps(
				[
					{
						"invoice": first.name,
						"expected_modified": cstr(
							frappe.db.get_value("Sales Invoice", first.name, "modified")
						),
					},
					{
						"invoice": second.name,
						"expected_modified": cstr(
							frappe.db.get_value("Sales Invoice", second.name, "modified")
						),
					},
				]
			),
			pos_profile=HOTEL_PROFILE,
			payment_status="Paid",
			paid_amount=1700,
			company=self.company,
			customer=self.customer,
			extra_items=json.dumps([{"item_code": item, "qty": 1, "rate": 500}]),
			idempotency_key="merged-auto-split",
		)
		self.assertTrue(_ok(response), msg=response)

		data = response["data"]
		counter = data["created_counter_invoice"]
		self.assertTrue(data["auto_split_counter_items"])
		self.assertEqual(data["settled_invoice_ids"], [first.name, second.name, counter])
		self.assertEqual(frappe.db.count("Sales Invoice Item", {"parent": first.name}), 1)
		self.assertEqual(frappe.db.count("Sales Invoice Item", {"parent": second.name}), 1)
		self.assertEqual(frappe.db.get_value("Sales Invoice", counter, "update_stock"), 1)

		moved = frappe.get_all(
			"Stock Ledger Entry",
			filters={"voucher_no": counter, "item_code": item},
			fields=["actual_qty"],
		)
		self.assertEqual(len(moved), 1)
		self.assertEqual(flt(moved[0].actual_qty), -1.0)


class TestPosSaleIdempotency(PosSaleBase):
	def test_a_retry_returns_the_first_sale_instead_of_charging_twice(self):
		first = self._sell(idempotency_key="probe-key")
		self.assertTrue(_ok(first), msg=first)

		second = self._sell(idempotency_key="probe-key")
		self.assertTrue(_ok(second), msg=second)
		self.assertEqual(second["data"]["created_invoice"], first["data"]["created_invoice"])
		self.assertTrue(second["data"]["replayed"])

	def test_a_sale_without_a_key_is_not_deduplicated(self):
		first = self._sell()
		second = self._sell()
		self.assertNotEqual(second["data"]["created_invoice"], first["data"]["created_invoice"])


class TestGuardStillClosed(IntegrationTestCase):
	"""The new door must not have been opened by leaving the old one ajar.

	Driven through the hook directly rather than through an insert, so the assertion is
	about the guard's decision and not about whichever user the test happens to run as -
	`before_insert` returns early for Administrator, which is what a test would otherwise
	silently be exercising.
	"""

	def _guard(self, doc):
		# frappe.session is a thread-local proxy, so patch.object cannot reach it.
		# The guard returns early for Administrator, which is what a test would
		# otherwise be silently exercising, so the user has to be moved for real.
		with as_user("cashier@example.com"):
			sales_invoice_guard.before_insert(doc)

	def test_a_bare_direct_insert_is_still_refused(self):
		doc = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"customer": "_Test POS Counter Customer",
				"items": [{"item_code": "_Test POS Counter Service", "qty": 1, "rate": 10}],
			}
		)
		with self.assertRaises(frappe.PermissionError):
			self._guard(doc)

	def test_setting_is_pos_is_not_enough_to_get_past_the_guard(self):
		"""The exemption Option A would have added, proved absent.

		A client that sets is_pos or pos_profile itself must still be refused, or the
		guard would be decorative.
		"""
		doc = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"customer": "_Test POS Counter Customer",
				"is_pos": 1,
				"pos_profile": HOTEL_PROFILE,
				"items": [{"item_code": "_Test POS Counter Service", "qty": 1, "rate": 10}],
			}
		)
		with self.assertRaises(frappe.PermissionError):
			self._guard(doc)

	def test_the_custom_flow_flag_is_what_opens_it(self):
		doc = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"customer": "_Test POS Counter Customer",
				"items": [{"item_code": "_Test POS Counter Service", "qty": 1, "rate": 10}],
			}
		)
		doc.flags.from_custom_flow = True
		self._guard(doc)  # must not raise


class TestManualInvoiceDoor(PosSaleBase):
	def _raise_by_hand(self, **overrides):
		payload = {
			"customer": self.customer,
			"company": self.company,
			"branch": "hotel",
			"items": json.dumps([{"item_code": self.item, "qty": 1, "rate": 100}]),
		}
		payload.update(overrides)
		return create_manual_sales_invoice(**payload)

	def test_an_accounting_user_gets_a_draft_not_a_submitted_invoice(self):
		response = self._raise_by_hand(allow_duplicate=1)
		self.assertTrue(_ok(response), msg=response)
		self.assertEqual(
			frappe.db.get_value("Sales Invoice", response["data"]["sales_invoice"], "docstatus"), 0
		)

	def test_a_till_cashier_cannot_raise_an_invoice_by_hand(self):
		"""`POS Page` satisfies accounting.cashier._is_accounting_user; it must not satisfy this."""
		with as_user("cashier@example.com"), patch(
			"frappe.get_roles", return_value=["POS Cashier", "POS Page"]
		):
			response = self._raise_by_hand(allow_duplicate=1)
		self.assertFalse(_ok(response))
		self.assertEqual(_code(response), "MANUAL_INVOICE_NOT_PERMITTED")

	def test_manual_charges_reuse_even_with_the_legacy_duplicate_flag(self):
		first = self._raise_by_hand(allow_duplicate=1)
		self.assertTrue(_ok(first), msg=first)
		for payload in ({}, {"allow_duplicate": 1}):
			reused = self._raise_by_hand(**payload)
			self.assertTrue(_ok(reused), msg=reused)
			self.assertEqual(reused["data"]["sales_invoice"], first["data"]["sales_invoice"])
			self.assertFalse(reused["data"]["created"])


class TestSettlementNetsAdvances(PosSaleBase):
	"""An invoice carrying prepayments must not be settled at the till.

	The first attempt at this made `_settle` collect `grand_total - total_advance` and
	assumed ERPNext would then reconcile the advances on submit. It does not. ERPNext skips
	`update_against_document_in_jv()` outright when `is_pos` is set (`sales_invoice.py`:
	`if cint(self.is_pos) != 1 and not self.is_return`), so a POS invoice never turns its
	`advances` rows into Payment Entry References, and `update_outstanding_amt` then
	rebuilds outstanding from GL entries that were never written.

	Measured on the test site before this guard: a 200,000 stay with 120,000 of allocated
	advances, settled at the till for the correct 80,000, submitted with outstanding
	120,000 and all three deposits still `unallocated`. The customer had paid in full and
	still owed on paper - charged once, owing twice.

	Netting the amount was therefore necessary but not sufficient, and the till is simply
	the wrong place for these invoices. Boarding is the only writer of `advances` in this
	app and `check_out_boarding` now submits its invoices itself
	(`healthcare/boarding.py::_submit_checkout_invoice`), so nothing should reach the guard
	in normal running. A draft that does is a check-out whose submit failed, and it is paid
	as an ordinary invoice.

	`_amount_due` is kept and still asserted: it is what the receipt shows the cashier, and
	it keeps the arithmetic right if the guard is ever relaxed.

	There is deliberately no "nothing was written" test. `settle_open_invoice` rolls the
	transaction back on any error, which is the guarantee that matters - but it takes this
	test's own fixtures with it, so the assertion could only be made about a document the
	rollback has already removed.
	"""

	def _advance_payment_entry(self, amount: float):
		"""Money taken before the invoice exists - a deposit's real shape."""
		from erpnext.accounts.party import get_party_account

		pe = frappe.new_doc("Payment Entry")
		pe.payment_type = "Receive"
		pe.company = self.company
		pe.posting_date = frappe.utils.nowdate()
		pe.party_type = "Customer"
		pe.party = self.customer
		pe.paid_from = get_party_account("Customer", self.customer, self.company)
		pe.paid_to = self.till_account
		pe.paid_amount = amount
		pe.received_amount = amount
		pe.flags.ignore_permissions = True
		pe.insert(ignore_permissions=True)
		pe.submit()
		return pe

	def _draft_with_advance(self, total: float, deposit: float):
		"""An open draft carrying a prepayment, as a failed check-out would leave it."""
		result = get_or_create_open_invoice(
			customer=self.customer,
			items=[{"item_code": self.item, "qty": 1, "rate": total}],
			source_doctype="Pet Boarding",
			source_name="_TEST-BRD-ADVANCE",
			company=self.company,
			force_new=True,
			ignore_permissions=True,
		)
		invoice = result.invoice
		pe = self._advance_payment_entry(deposit)
		invoice.append(
			"advances",
			{
				"reference_type": "Payment Entry",
				"reference_name": pe.name,
				"advance_amount": deposit,
				"allocated_amount": deposit,
			},
		)
		invoice.flags.from_custom_flow = True
		invoice.flags.ignore_permissions = True
		invoice.save(ignore_permissions=True)
		return invoice, pe

	def _settle_it(self, invoice, paid_amount):
		from pet_app.api.pos import settle_open_invoice

		return settle_open_invoice(
			invoice=invoice.name,
			pos_profile=HOTEL_PROFILE,
			payment_status="Paid",
			paid_amount=paid_amount,
			expected_modified=frappe.db.get_value("Sales Invoice", invoice.name, "modified"),
			company=self.company,
			customer=self.customer,
		)

	def test_the_till_refuses_an_invoice_carrying_prepayments(self):
		"""Refusing is the whole protection. Settling would take money and lose the record."""
		invoice, _pe = self._draft_with_advance(175000, 70000)
		response = self._settle_it(invoice, 105000)

		self.assertFalse(_ok(response), "the till settled an invoice carrying advances")
		self.assertEqual(_code(response), "INVOICE_CARRIES_ADVANCES")

	def test_amount_due_is_the_total_less_the_prepayment(self):
		"""What the cashier is shown. `grand_total` keeps its own meaning."""
		from pet_app.api.pos import _amount_due

		invoice, _pe = self._draft_with_advance(175000, 70000)
		invoice.reload()
		self.assertEqual(flt(_amount_due(invoice)), 105000)
		self.assertEqual(flt(invoice.grand_total), 175000)

	def test_amount_due_is_zero_when_the_prepayment_covers_everything(self):
		from pet_app.api.pos import _amount_due

		invoice, _pe = self._draft_with_advance(25000, 25000)
		invoice.reload()
		self.assertEqual(flt(_amount_due(invoice)), 0)

	def test_an_ordinary_invoice_is_unaffected(self):
		"""Regression guard: the guard and the netting must be inert without advances."""
		from pet_app.api.pos import _amount_due

		result = get_or_create_open_invoice(
			customer=self.customer,
			items=[{"item_code": self.item, "qty": 1, "rate": 40000}],
			source_doctype="Pet Boarding",
			source_name="_TEST-BRD-NO-ADVANCE",
			company=self.company,
			force_new=True,
			ignore_permissions=True,
		)
		self.assertEqual(flt(_amount_due(result.invoice)), 40000)

		response = self._settle_it(result.invoice, 40000)
		self.assertTrue(_ok(response), msg=response)
		settled = frappe.get_doc("Sales Invoice", result.invoice.name)
		self.assertEqual(flt(settled.paid_amount), 40000)
		self.assertEqual(flt(settled.outstanding_amount), 0)
