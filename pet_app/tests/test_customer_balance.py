# -*- coding: utf-8 -*-
"""What the till is told a customer owes.

`get_customer_balance` used to read `GL Entry`. That is the wrong table for this question:
it is the only one ERPNext does NOT update when an advance is reconciled.
`reconcile_against_document` writes the allocation to `Payment Ledger Entry` and leaves the
payment's GL row with an empty `against_voucher`, so the invoice's debit stands alone and
reads as an open item — while the same money is counted a second time as credit.

Measured on production before the fix: three customers, 125,000 of debt that did not exist,
four "open" invoices that were paid in full. One customer was shown `Debts 30,000` beside
`380,000 on account` when he owed nothing and held 350,000.

`test_an_invoice_settled_by_an_advance_is_not_a_debt` is the one that would have caught it.
**A `net_balance` assertion does not** — the net was right the whole time. What was wrong is
the split between `receivable` and `credit`, which is exactly what the cashier reads.

Nothing here commits; IntegrationTestCase rolls each test back.
"""

from __future__ import annotations

import frappe
from erpnext.accounts.party import get_party_account
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, nowdate

from pet_app.api.accounting.balance import get_customer_balance
from pet_app.utils.invoice_reuse import get_or_create_open_invoice

COMPANY = "Kokh-vet"


class CustomerBalanceBase(IntegrationTestCase):
	"""A customer of this test's own, so every figure is absolute rather than a delta."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.customer = self._customer()
		self.item = self._service_item()

	def _customer(self) -> str:
		"""A FRESH customer per test, so every figure below is absolute.

		Not one shared fixture: submitting an invoice or a payment commits inside ERPNext,
		so the rollback IntegrationTestCase would normally give us does not hold here and a
		shared customer accumulates across tests. Measured while writing these: receivable
		climbed 65,000 -> 105,000 -> 145,000 as the suite ran.
		"""
		name = f"_Test Balance {frappe.generate_hash(length=10)}"
		frappe.get_doc({
			"doctype": "Customer",
			"customer_name": name,
			"customer_type": "Individual",
			"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
			"territory": frappe.db.get_value("Territory", {}, "name"),
		}).insert(ignore_permissions=True)
		return name

	def _service_item(self) -> str:
		code = "_Test Balance Service"
		if not frappe.db.exists("Item", code):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": code,
				"item_name": code,
				"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}).insert(ignore_permissions=True)
		return code

	def _balance(self) -> dict:
		response = get_customer_balance(customer=self.customer, company=COMPANY)
		self.assertTrue(response.get("ok"), msg=response)
		return response["data"]

	def _payment(self, amount: float, invoice=None):
		"""A receipt. With `invoice` it settles that invoice; without, it is an advance."""
		pe = frappe.new_doc("Payment Entry")
		pe.payment_type = "Receive"
		pe.company = COMPANY
		pe.posting_date = nowdate()
		pe.party_type = "Customer"
		pe.party = self.customer
		pe.paid_from = get_party_account("Customer", self.customer, COMPANY)
		pe.paid_to = frappe.db.get_value(
			"POS Profile", "Alkokh Vet Hotel - Cashier", "custom_cash_account"
		)
		pe.paid_amount = amount
		pe.received_amount = amount
		if invoice:
			pe.append("references", {
				"reference_doctype": "Sales Invoice",
				"reference_name": invoice.name,
				"total_amount": flt(invoice.grand_total),
				"outstanding_amount": flt(invoice.outstanding_amount),
				"allocated_amount": amount,
			})
		pe.flags.ignore_permissions = True
		pe.insert(ignore_permissions=True)
		pe.submit()
		return pe

	def _invoice(self, total: float, *, advance=None, submit=True, posting_date=None, due_date=None):
		result = get_or_create_open_invoice(
			customer=self.customer,
			items=[{"item_code": self.item, "qty": 1, "rate": total}],
			source_doctype="Pet Boarding",
			source_name="_TEST-BALANCE",
			company=COMPANY,
			posting_date=posting_date,
			due_date=due_date,
			force_new=True,
			ignore_permissions=True,
			# Without it ERPNext discards `posting_date` and stamps today, so a backdated
			# fixture silently becomes a same-day one and nothing is ever overdue.
			extra_fields={"set_posting_time": 1} if posting_date else None,
		)
		invoice = result.invoice
		if advance is not None:
			invoice.append("advances", {
				"reference_type": "Payment Entry",
				"reference_name": advance.name,
				"advance_amount": flt(advance.paid_amount),
				"allocated_amount": min(total, flt(advance.unallocated_amount)),
			})
			invoice.flags.from_custom_flow = True
			invoice.flags.ignore_permissions = True
			invoice.save(ignore_permissions=True)
		if submit:
			invoice.flags.ignore_permissions = True
			invoice.submit()
			invoice.reload()
		return invoice


class TestSettledInvoices(CustomerBalanceBase):
	def test_an_invoice_settled_by_an_advance_is_not_a_debt(self):
		"""The regression. Reading GL Entry reported this invoice as open AND as credit."""
		advance = self._payment(100000)
		invoice = self._invoice(15000, advance=advance)

		self.assertEqual(flt(invoice.outstanding_amount), 0, "fixture is wrong, not the code")

		balance = self._balance()
		self.assertEqual(flt(balance["receivable"]), 0, "a settled invoice was reported as a debt")
		self.assertEqual(balance["open_invoice_count"], 0)
		self.assertEqual(
			flt(balance["credit"]), 85000,
			"the settled 15,000 was counted a second time inside credit",
		)
		self.assertEqual(flt(balance["net_balance"]), -85000)

	def test_the_net_is_right_even_when_the_split_is_wrong(self):
		"""Why the test above exists.

		This assertion passed on the broken code. It is kept to say so out loud: never use
		net_balance alone to prove this area works.
		"""
		advance = self._payment(100000)
		self._invoice(15000, advance=advance)
		self.assertEqual(flt(self._balance()["net_balance"]), -85000)

	def test_an_invoice_settled_by_a_referenced_payment_is_not_a_debt(self):
		"""The ordinary path: invoice first, payment against it afterwards."""
		invoice = self._invoice(40000)
		self._payment(40000, invoice=invoice)

		balance = self._balance()
		self.assertEqual(flt(balance["receivable"]), 0)
		self.assertEqual(flt(balance["credit"]), 0)
		self.assertEqual(balance["open_invoice_count"], 0)


class TestRealDebtsSurvive(CustomerBalanceBase):
	"""Guard against a fix that swallows debts along with the phantom ones."""

	def test_an_unpaid_invoice_is_still_owed(self):
		invoice = self._invoice(40000)

		balance = self._balance()
		self.assertEqual(flt(balance["receivable"]), 40000)
		self.assertEqual(balance["open_invoice_count"], 1)
		self.assertEqual(flt(balance["net_balance"]), 40000)

	def test_a_partly_paid_invoice_owes_the_remainder(self):
		invoice = self._invoice(40000)
		self._payment(15000, invoice=invoice)

		balance = self._balance()
		self.assertEqual(flt(balance["receivable"]), 25000)
		self.assertEqual(balance["open_invoice_count"], 1)

	def test_an_overdue_invoice_is_reported_overdue(self):
		# Both dates move. ERPNext refuses a due date earlier than the posting date, so
		# backdating the due date alone silently leaves it at today and nothing is overdue.
		self._invoice(
			40000,
			posting_date=add_days(nowdate(), -20),
			due_date=add_days(nowdate(), -10),
		)

		balance = self._balance()
		self.assertEqual(flt(balance["overdue"]), 40000)
		self.assertIsNotNone(balance["oldest_due_date"])

	def test_debts_and_credit_are_reported_side_by_side(self):
		"""Both, never one signed number - the cashier needs both to have the conversation."""
		self._invoice(40000)
		self._payment(25000)

		balance = self._balance()
		self.assertEqual(flt(balance["receivable"]), 40000)
		self.assertEqual(flt(balance["credit"]), 25000)
		self.assertEqual(flt(balance["net_balance"]), 15000)


class TestWhatIsExcluded(CustomerBalanceBase):
	def test_an_unapplied_receipt_is_credit_not_a_debt(self):
		self._payment(50000)

		balance = self._balance()
		self.assertEqual(flt(balance["credit"]), 50000)
		self.assertEqual(flt(balance["receivable"]), 0)
		self.assertEqual(balance["open_invoice_count"], 0)

	def test_a_cancelled_invoice_counts_for_nothing(self):
		"""`delinked` is this table's `is_cancelled`; rows are marked, not deleted."""
		invoice = self._invoice(40000)
		invoice.flags.ignore_permissions = True
		invoice.cancel()

		balance = self._balance()
		self.assertEqual(flt(balance["receivable"]), 0)
		self.assertEqual(flt(balance["net_balance"]), 0)
		self.assertEqual(balance["open_invoice_count"], 0)

	def test_a_draft_is_counted_but_not_owed(self):
		"""Nothing is owed until a document is submitted."""
		self._invoice(40000, submit=False)

		balance = self._balance()
		self.assertEqual(balance["draft_invoice_count"], 1)
		self.assertEqual(flt(balance["draft_invoice_total"]), 40000)
		self.assertEqual(flt(balance["receivable"]), 0)
		self.assertEqual(flt(balance["net_balance"]), 0)

	def test_a_customer_with_no_history_is_all_zero(self):
		balance = self._balance()
		self.assertEqual(flt(balance["receivable"]), 0)
		self.assertEqual(flt(balance["credit"]), 0)
		self.assertEqual(flt(balance["net_balance"]), 0)
		self.assertEqual(balance["open_invoice_count"], 0)
