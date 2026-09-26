# -*- coding: utf-8 -*-
"""Delivery partner orders, and the one rule that cannot bend.

A partner order must never count as cash in the till. Talabat charges the customer at
their end and transfers the month's takings minus commission; nobody at the counter
receives a fils of it. `expected_cash_on_hand` is a raw sum over the cashier's cash
account, so a partner sale that touched it would tell the cashier to hand over money a
delivery app is holding, and the drawer would come up short by the value of every app
order that shift - discovered by whoever counts it, hours later, with nothing to point at.

That is what `test_a_partner_sale_is_a_due_receivable_and_touches_no_till` pins, and it is
the test to read first. `is_pos = 0`, no payment row, an ordinary receivable. An
`outstanding_amount` assertion alone would NOT catch a regression here - a paid POS
invoice also has zero outstanding - so the payment rows and `is_pos` are asserted directly.

Two more properties that are easy to regress:

* **The commission rate is a SNAPSHOT.** Reading the live rate at settlement time means
  the first renegotiation silently restates every past month. Pinned by
  `test_renegotiating_the_rate_does_not_restate_a_settled_month`.

* **Gross is built from OUTSTANDING, not grand total.** A partly paid invoice must
  contribute only what it still owes, or the settlement collects money already collected.
  Pinned by `test_a_partly_paid_invoice_contributes_only_its_outstanding`.

These endpoints do NOT raise. `standardize_response` catches the exception and returns
`{ok: false, meta: {code}, errors: [...]}`, so refusals are asserted on `meta.code` and
successes read out of `data` - the same convention `tests/test_pos_sale_creation.py` uses.
A test that asserted `assertRaises` here would fail against working code.

Nothing here commits; IntegrationTestCase rolls each test back.
"""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, nowdate

from pet_app.api.delivery_partners import create_settlement
from pet_app.api.pos import create_pos_sale


def _ok(response) -> bool:
	return bool(response.get("ok"))


def _code(response):
	return (response.get("meta") or {}).get("code")


def _data(response) -> dict:
	return response.get("data") or {}


def _text(response) -> str:
	return " ".join(str(e) for e in (response.get("errors") or []))


class DeliveryPartnerBase(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.company = (
			frappe.db.get_single_value("Pet App Accounting Settings", "default_company")
			or frappe.defaults.get_global_default("company")
		)
		cls.profile = frappe.db.get_value(
			"POS Profile", {"disabled": 0, "company": cls.company}, "name"
		)
		# A non-stock item, so none of this depends on the site's stock position.
		cls.item = frappe.db.get_value("Item", {"is_stock_item": 0, "disabled": 0}, "name")

	def _partner(self, rate=25.0, active=1, *, commission_type="Percentage", amount=0.0):
		doc = frappe.get_doc({
			"doctype": "Delivery Partner",
			"partner_name": frappe.generate_hash("PARTNER", 8),
			"commission_type": commission_type,
			"commission_rate": rate,
			"commission_amount": amount,
			"is_active": active,
		})
		doc.insert(ignore_permissions=True)
		doc.reload()
		return doc

	def _sale(self, partner, *, rate=100000.0, **overrides):
		payload = dict(
			customer=partner.customer,
			pos_profile=self.profile,
			payment_status="Due",
			items=[{"item_code": self.item, "qty": 1, "rate": rate}],
			company=self.company,
			delivery_partner=partner.name,
			partner_order_ref=frappe.generate_hash("REF", 6),
		)
		payload.update(overrides)
		return create_pos_sale(**payload)

	def _sold(self, partner, **kwargs) -> str:
		response = self._sale(partner, **kwargs)
		self.assertTrue(_ok(response), _text(response))
		return _data(response)["created_invoice"]


class TestPartnerSaleIsNeverCash(DeliveryPartnerBase):
	def test_a_partner_sale_is_a_due_receivable_and_touches_no_till(self):
		"""The rule the whole feature exists to protect.

		Asserted on the payment rows and `is_pos` directly, not on `outstanding_amount`:
		a paid POS invoice also has zero outstanding, so an outstanding check alone would
		pass while the drawer silently absorbed the delivery app's money.
		"""
		partner = self._partner(rate=25.0)
		invoice = frappe.get_doc("Sales Invoice", self._sold(partner))

		self.assertEqual(invoice.is_pos, 0)
		self.assertFalse(invoice.get("payments"))
		self.assertEqual(flt(invoice.paid_amount), 0.0)
		self.assertEqual(invoice.customer, partner.customer)
		self.assertGreater(flt(invoice.outstanding_amount), 0.0)
		self.assertEqual(flt(invoice.get("custom_partner_commission_rate")), 25.0)
		self.assertEqual(flt(invoice.get("custom_partner_commission_amount")), 25000.0)

	def test_a_partner_sale_carrying_payment_is_refused(self):
		partner = self._partner()
		response = self._sale(partner, payment_status="Paid", paid_amount=100000.0)
		self.assertFalse(_ok(response))
		self.assertEqual(_code(response), "PARTNER_SALE_MUST_BE_DUE")

	def test_an_inactive_partner_is_refused_before_anything_else(self):
		"""Inactive comes FIRST in the refusal order: nothing else about the order matters
		if the partner is switched off. This request is ALSO missing its order reference,
		so it would fail that check too - the inactive code must win."""
		partner = self._partner(active=0)
		response = self._sale(partner, partner_order_ref="")
		self.assertEqual(_code(response), "PARTNER_INACTIVE")

	def test_billing_a_partner_order_to_another_customer_is_refused(self):
		partner, other = self._partner(), self._partner()
		response = self._sale(partner, customer=other.customer)
		self.assertEqual(_code(response), "PARTNER_CUSTOMER_MISMATCH")

	def test_a_partner_order_without_a_reference_is_refused(self):
		partner = self._partner()
		response = self._sale(partner, partner_order_ref="   ")
		self.assertEqual(_code(response), "PARTNER_ORDER_REF_REQUIRED")

	def test_an_ordinary_counter_sale_is_untouched(self):
		"""The frontend omits the whole partner group on a normal sale. That request body
		must behave exactly as it did before this work."""
		customer = frappe.db.get_value("Customer", {"disabled": 0}, "name")
		response = create_pos_sale(
			customer=customer,
			pos_profile=self.profile,
			payment_status="Due",
			items=[{"item_code": self.item, "qty": 1, "rate": 1000.0}],
			company=self.company,
		)
		self.assertTrue(_ok(response), _text(response))
		invoice = frappe.get_doc("Sales Invoice", _data(response)["created_invoice"])
		self.assertFalse(invoice.get("custom_delivery_partner"))
		self.assertIsNone(_data(response)["receipt"]["partner_label"])
		self.assertIsNone(_data(response)["receipt"]["partner_order_ref"])


class TestPartnerReceipt(DeliveryPartnerBase):
	def test_the_receipt_names_the_partner_and_the_order(self):
		"""Without these a Due partner sale prints as a plain unpaid invoice, and whoever
		collects it may ask the customer for money the app already took."""
		partner = self._partner()
		response = self._sale(partner, partner_order_ref="TLB-9911")
		self.assertTrue(_ok(response), _text(response))
		receipt = _data(response)["receipt"]
		self.assertEqual(receipt["partner_label"], partner.partner_name)
		self.assertEqual(receipt["partner_order_ref"], "TLB-9911")

	def test_a_replayed_sale_still_prints_the_partner(self):
		"""`_sale_result` returns the EXISTING document on an idempotent retry and never
		sees the original arguments, so the receipt has to read them off the document."""
		partner = self._partner()
		key = frappe.generate_hash("KEY", 10)
		first = self._sale(partner, idempotency_key=key, partner_order_ref="TLB-1")
		second = self._sale(partner, idempotency_key=key, partner_order_ref="TLB-2")

		self.assertTrue(_ok(second), _text(second))
		self.assertTrue(_data(second)["replayed"])
		self.assertEqual(_data(second)["created_invoice"], _data(first)["created_invoice"])
		self.assertEqual(_data(second)["receipt"]["partner_label"], partner.partner_name)
		self.assertEqual(_data(second)["receipt"]["partner_order_ref"], "TLB-1")


class TestPartnerCustomer(DeliveryPartnerBase):
	def test_a_partner_gets_its_own_customer_on_insert(self):
		partner = self._partner()
		self.assertTrue(partner.customer)
		self.assertEqual(
			frappe.db.get_value("Customer", partner.customer, "customer_type"), "Company"
		)

	def test_a_rate_outside_the_band_is_refused(self):
		"""The client refuses these too. The client is not the authority."""
		for bad in (0, -5, 100, 140):
			with self.assertRaises(frappe.ValidationError):
				self._partner(rate=bad)

	def test_a_flat_fee_that_leaves_nothing_behind_is_refused(self):
		"""The flat-fee counterpart of the 0-and-100 band on a rate."""
		for bad in (0, -2000):
			with self.assertRaises(frappe.ValidationError):
				self._partner(commission_type="Amount", amount=bad)

	def test_the_unused_figure_is_zeroed_rather_than_left_as_typed(self):
		"""Both figures are read on the strength of the type alone, so a stale one behind
		the type in use surfaces as a wrong commission months later - when somebody
		switches the type back."""
		partner = self._partner(commission_type="Amount", rate=25.0, amount=2000.0)
		self.assertEqual(flt(partner.commission_rate), 0.0)

		partner.commission_type = "Percentage"
		partner.commission_rate = 25.0
		partner.save(ignore_permissions=True)
		partner.reload()
		self.assertEqual(flt(partner.commission_amount), 0.0)

	def test_a_partner_cannot_adopt_a_customer_with_other_history(self):
		"""Billing one app's orders to another app's account surfaces nowhere until a
		statement fails to reconcile a month later."""
		partner, other = self._partner(), self._partner()
		self._sold(other)

		partner.customer = other.customer
		with self.assertRaises(frappe.ValidationError):
			partner.save(ignore_permissions=True)


class TestSettlement(DeliveryPartnerBase):
	def _bank(self):
		return frappe.db.get_value(
			"Account", {"company": self.company, "account_type": "Bank", "is_group": 0}, "name"
		)

	def _expense(self):
		return frappe.db.get_value(
			"Account", {"company": self.company, "root_type": "Expense", "is_group": 0}, "name"
		)

	def _ready_partner(self, rate=25.0, **kwargs):
		partner = self._partner(rate=rate, **kwargs)
		# `commission_expense_account`, not `commission_account`: the latter was renamed
		# away by `delivery_partner_settlement_field_alignment` and setting it here just
		# hangs an attribute on the document that no settlement ever reads.
		partner.commission_expense_account = self._expense()
		partner.save(ignore_permissions=True)
		return partner

	def test_the_ledger_balances_and_the_invoice_closes_in_full(self):
		"""100,000 at 25% arrives as 75,000, and the receivable clears for the FULL
		100,000 because the commission enters the Payment Entry as a deduction."""
		partner = self._ready_partner(rate=25.0)
		invoice = self._sold(partner, rate=100000.0)

		response = create_settlement(
			partner=partner.name,
			invoices=[invoice],
			received_in=self._bank(),
			posting_date=nowdate(),
		)
		self.assertTrue(_ok(response), _text(response))
		data = _data(response)

		self.assertEqual(flt(data["gross_amount"]), 100000.0)
		self.assertEqual(flt(data["commission_amount"]), 25000.0)
		self.assertEqual(flt(data["received_amount"]), 75000.0)
		self.assertEqual(
			flt(frappe.db.get_value("Sales Invoice", invoice, "outstanding_amount")), 0.0
		)
		self.assertEqual(
			frappe.db.get_value("Sales Invoice", invoice, "custom_partner_settlement"),
			data["settlement"],
		)

	def test_a_settlement_that_does_not_tie_out_is_refused_and_names_the_gap(self):
		partner = self._ready_partner()
		invoice = self._sold(partner, rate=100000.0)
		response = create_settlement(
			partner=partner.name,
			invoices=[invoice],
			received_amount=70000.0,
			commission_amount=25000.0,
			received_in=self._bank(),
		)
		self.assertEqual(_code(response), "SETTLEMENT_DOES_NOT_TIE_OUT")
		self.assertIn("5,000", _text(response))

	def test_an_invoice_cannot_be_settled_twice(self):
		partner = self._ready_partner()
		invoice = self._sold(partner, rate=100000.0)
		first = create_settlement(
			partner=partner.name, invoices=[invoice], received_in=self._bank()
		)
		self.assertTrue(_ok(first), _text(first))

		second = create_settlement(
			partner=partner.name, invoices=[invoice], received_in=self._bank()
		)
		self.assertEqual(_code(second), "INVOICE_ALREADY_SETTLED")
		# Names the invoice AND the settlement holding it, so it is actionable.
		self.assertIn(invoice, _text(second))
		self.assertIn(_data(first)["settlement"], _text(second))

	def test_a_retry_returns_the_original_instead_of_paying_twice(self):
		partner = self._ready_partner()
		invoice = self._sold(partner, rate=100000.0)
		key = frappe.generate_hash("SKEY", 10)

		first = create_settlement(
			partner=partner.name, invoices=[invoice], received_in=self._bank(), idempotency_key=key
		)
		second = create_settlement(
			partner=partner.name, invoices=[invoice], received_in=self._bank(), idempotency_key=key
		)
		self.assertTrue(_ok(second), _text(second))
		self.assertTrue(_data(second)["replayed"])
		self.assertEqual(_data(second)["settlement"], _data(first)["settlement"])
		self.assertEqual(_data(second)["payment_entry"], _data(first)["payment_entry"])

	def test_money_may_not_be_banked_into_a_till(self):
		"""A partner transfer passed through no drawer. Crediting a till account would
		tell that cashier to hand over cash they never received."""
		till = frappe.db.get_value(
			"POS Profile", {"custom_cash_account": ["is", "set"]}, "custom_cash_account"
		)
		if not till:
			self.skipTest("no POS Profile on this site carries a cash account")

		partner = self._ready_partner()
		invoice = self._sold(partner, rate=100000.0)
		response = create_settlement(
			partner=partner.name, invoices=[invoice], received_in=till
		)
		self.assertEqual(_code(response), "SETTLEMENT_ACCOUNT_IS_A_TILL")

	def test_a_partly_paid_invoice_contributes_only_its_outstanding(self):
		"""Gross is built from OUTSTANDING, not grand total. Otherwise the settlement
		claims to collect money the customer already paid directly."""
		partner = self._ready_partner(rate=25.0)
		invoice = frappe.get_doc("Sales Invoice", self._sold(partner, rate=100000.0))

		pe = frappe.new_doc("Payment Entry")
		pe.payment_type = "Receive"
		pe.company = self.company
		pe.posting_date = nowdate()
		pe.party_type = "Customer"
		pe.party = invoice.customer
		pe.paid_from = invoice.debit_to
		pe.paid_to = self._bank()
		pe.paid_amount = pe.received_amount = 40000.0
		pe.append("references", {
			"reference_doctype": "Sales Invoice",
			"reference_name": invoice.name,
			"allocated_amount": 40000.0,
		})
		pe.flags.ignore_permissions = True
		pe.insert(ignore_permissions=True)
		pe.submit()

		response = create_settlement(
			partner=partner.name, invoices=[invoice.name], received_in=self._bank()
		)
		self.assertTrue(_ok(response), _text(response))
		self.assertEqual(flt(_data(response)["gross_amount"]), 60000.0)
		self.assertEqual(flt(_data(response)["commission_amount"]), 15000.0)

	def test_renegotiating_the_rate_does_not_restate_a_settled_month(self):
		"""The rate on the invoice is a SNAPSHOT. Reading the partner's live rate at
		settlement time would silently restate every past month on the first change."""
		partner = self._ready_partner(rate=25.0)
		invoice = self._sold(partner, rate=100000.0)

		partner.commission_rate = 40.0
		partner.save(ignore_permissions=True)

		response = create_settlement(
			partner=partner.name, invoices=[invoice], received_in=self._bank()
		)
		self.assertTrue(_ok(response), _text(response))
		self.assertEqual(flt(_data(response)["commission_amount"]), 25000.0)

	def test_a_flat_fee_partner_is_charged_the_same_amount_whatever_the_order(self):
		"""The whole point of a flat fee: 2,000 off a 100,000 order and 2,000 off a
		10,000 one, where a percentage would have taken ten times as much from the first."""
		partner = self._ready_partner(commission_type="Amount", amount=2000.0)
		big = frappe.get_doc("Sales Invoice", self._sold(partner, rate=100000.0))
		small = frappe.get_doc("Sales Invoice", self._sold(partner, rate=10000.0))

		for invoice in (big, small):
			self.assertEqual(invoice.get("custom_partner_commission_type"), "Amount")
			self.assertEqual(flt(invoice.get("custom_partner_commission_rate")), 0.0)
			self.assertEqual(flt(invoice.get("custom_partner_commission_amount")), 2000.0)

		response = create_settlement(
			partner=partner.name, invoices=[big.name, small.name], received_in=self._bank()
		)
		self.assertTrue(_ok(response), _text(response))
		data = _data(response)
		self.assertEqual(flt(data["gross_amount"]), 110000.0)
		self.assertEqual(flt(data["commission_amount"]), 4000.0)
		self.assertEqual(flt(data["received_amount"]), 106000.0)
		for name in (big.name, small.name):
			self.assertEqual(
				flt(frappe.db.get_value("Sales Invoice", name, "outstanding_amount")), 0.0
			)

	def test_a_till_that_sends_the_partners_zero_rate_does_not_break_a_flat_fee_sale(self):
		"""The POS bundle sends the partner's rate on EVERY sale, and a flat-fee partner's
		rate is 0. Refusing that would make a working till unable to sell for them at all."""
		partner = self._ready_partner(commission_type="Amount", amount=2000.0)
		response = self._sale(partner, rate=50000.0, partner_commission_rate=0)
		self.assertTrue(_ok(response), _text(response))
		self.assertEqual(
			flt(frappe.db.get_value(
				"Sales Invoice", _data(response)["created_invoice"],
				"custom_partner_commission_amount",
			)),
			2000.0,
		)

	def test_an_order_worth_no_more_than_the_flat_fee_is_refused(self):
		"""The counterpart of refusing a rate of 100 or more: the invoice would settle to
		nothing or to a negative receipt, which the tie-out has no meaning for."""
		partner = self._ready_partner(commission_type="Amount", amount=2000.0)
		response = self._sale(partner, rate=1500.0)
		self.assertFalse(_ok(response))
		self.assertEqual(_code(response), "PARTNER_COMMISSION_EXCEEDS_TOTAL")

	def test_switching_a_partner_to_a_flat_fee_does_not_restate_a_past_order(self):
		"""The TYPE is snapshotted for the same reason the rate is."""
		partner = self._ready_partner(rate=25.0)
		invoice = self._sold(partner, rate=100000.0)

		partner.commission_type = "Amount"
		partner.commission_amount = 2000.0
		partner.save(ignore_permissions=True)

		response = create_settlement(
			partner=partner.name, invoices=[invoice], received_in=self._bank()
		)
		self.assertTrue(_ok(response), _text(response))
		self.assertEqual(flt(_data(response)["commission_amount"]), 25000.0)

	def test_the_settlements_tab_query_finds_the_partners_settlements(self):
		"""The tab showed nothing for every partner because the doctype called its link
		`delivery_partner` while the client filters on `partner`, and Frappe fails a whole
		list query on one unknown field. Asserted through a query shaped like the client's
		- filtering on `partner` and requesting the full field list - because asserting on
		`doc.partner` alone would pass while the tab stayed empty."""
		partner = self._ready_partner()
		invoice = self._sold(partner, rate=100000.0)
		created = create_settlement(
			partner=partner.name, invoices=[invoice], received_in=self._bank(),
			statement_reference="STMT-1", remarks="September",
		)
		self.assertTrue(_ok(created), _text(created))

		rows = frappe.get_all(
			"Delivery Partner Settlement",
			filters=[["partner", "=", partner.name]],
			fields=[
				"name", "partner", "partner_name", "from_date", "to_date", "posting_date",
				"statement_reference", "gross_amount", "commission_rate", "commission_amount",
				"adjustment_amount", "adjustment_account", "received_amount", "received_in",
				"payment_entry", "invoice_count", "remarks", "docstatus",
			],
			order_by="posting_date desc",
		)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].partner_name, partner.partner_name)
		self.assertEqual(rows[0].invoice_count, 1)
		self.assertEqual(flt(rows[0].commission_rate), 25.0)

	def test_an_adjustment_posts_to_its_own_account(self):
		"""A clawback is not commission. Booking both to one account makes the commission
		figure useless for the reporting it exists for."""
		partner = self._ready_partner(rate=25.0)
		invoice = self._sold(partner, rate=100000.0)
		other = frappe.db.get_value(
			"Account",
			{"company": self.company, "root_type": "Expense", "is_group": 0,
			 "name": ["!=", self._expense()]},
			"name",
		)
		response = create_settlement(
			partner=partner.name, invoices=[invoice], received_in=self._bank(),
			received_amount=73000.0, commission_amount=25000.0,
			adjustment_amount=2000.0, adjustment_account=other,
		)
		self.assertTrue(_ok(response), _text(response))
		accounts = {
			row.account: flt(row.debit)
			for row in frappe.get_all(
				"GL Entry", filters={"voucher_no": _data(response)["payment_entry"]},
				fields=["account", "debit"],
			)
		}
		self.assertEqual(accounts.get(self._expense()), 25000.0)
		self.assertEqual(accounts.get(other), 2000.0)

	def test_probe_answers_without_touching_a_document(self):
		before = frappe.db.count("Delivery Partner Settlement")
		response = create_settlement(probe=1)
		self.assertIn("available", _data(response))
		self.assertEqual(frappe.db.count("Delivery Partner Settlement"), before)


class TestInsidePartner(TestSettlement):
	""""Bill the App Customer": the order is billed to the real customer, the partner still
	collects the money and settles later.

	Inherits TestSettlement's helpers, so its tests run again here too - harmlessly, and as a
	check that nothing about an ordinary partner moved.
	"""

	def _inside_partner(self, **kwargs):
		partner = self._ready_partner(**kwargs)
		partner.is_inside = 1
		partner.save(ignore_permissions=True)
		return partner

	def _real_customer(self):
		doc = frappe.get_doc({
			"doctype": "Customer",
			"customer_name": frappe.generate_hash("APPCUST", 8),
			"customer_type": "Company",
		})
		doc.insert(ignore_permissions=True)
		return doc.name

	def test_an_inside_order_is_billed_to_the_chosen_customer(self):
		partner = self._inside_partner(rate=25.0)
		customer = self._real_customer()
		invoice = frappe.get_doc("Sales Invoice", self._sold(partner, customer=customer))

		self.assertEqual(invoice.customer, customer)
		self.assertEqual(invoice.custom_delivery_partner, partner.name)
		self.assertEqual(int(invoice.get("custom_partner_is_inside") or 0), 1)
		# Still the partner's money: Due, nothing in the till, commission recorded.
		self.assertEqual(invoice.is_pos, 0)
		self.assertFalse(invoice.get("payments"))
		self.assertEqual(flt(invoice.get("custom_partner_commission_amount")), 25000.0)

	def test_an_inside_order_may_not_land_on_a_partners_account(self):
		"""The debt must sit on a person. A partner's billing Customer is the account the
		till lands on when the cashier picked the partner instead of the customer."""
		partner = self._inside_partner()
		response = self._sale(partner, customer=partner.customer)
		self.assertFalse(_ok(response))
		self.assertEqual(_code(response), "PARTNER_INSIDE_NEEDS_REAL_CUSTOMER")

	def test_an_inside_order_may_not_land_on_the_walk_in_customer(self):
		walk_in = frappe.db.get_value("POS Profile", self.profile, "customer")
		if not walk_in:
			self.skipTest("This POS Profile has no walk-in customer.")
		partner = self._inside_partner()
		response = self._sale(partner, customer=walk_in)
		self.assertFalse(_ok(response))
		self.assertEqual(_code(response), "PARTNER_INSIDE_NEEDS_REAL_CUSTOMER")

	def test_an_ordinary_partner_still_refuses_another_customer(self):
		"""Regression: the inside rule must not have loosened the ordinary one."""
		partner = self._ready_partner()
		response = self._sale(partner, customer=self._real_customer())
		self.assertFalse(_ok(response))
		self.assertEqual(_code(response), "PARTNER_CUSTOMER_MISMATCH")

	def test_settling_orders_of_two_customers_posts_one_balanced_journal_entry(self):
		partner = self._inside_partner(rate=25.0)
		first, second = self._real_customer(), self._real_customer()
		a = self._sold(partner, customer=first, rate=100000.0)
		b = self._sold(partner, customer=second, rate=40000.0)

		response = create_settlement(partner=partner.name, invoices=[a, b], received_in=self._bank())
		self.assertTrue(_ok(response), _text(response))
		data = _data(response)

		self.assertTrue(data.get("journal_entry"))
		self.assertFalse(data.get("payment_entry"))
		self.assertEqual(flt(data["gross_amount"]), 140000.0)
		self.assertEqual(flt(data["commission_amount"]), 35000.0)
		self.assertEqual(flt(data["received_amount"]), 105000.0)

		je = frappe.get_doc("Journal Entry", data["journal_entry"])
		self.assertEqual(je.docstatus, 1)
		self.assertEqual(flt(je.total_debit), flt(je.total_credit))
		for invoice in (a, b):
			self.assertEqual(flt(frappe.db.get_value("Sales Invoice", invoice, "outstanding_amount")), 0.0)
			self.assertEqual(
				frappe.db.get_value("Sales Invoice", invoice, "custom_partner_settlement"), data["settlement"]
			)

	def test_an_ordinary_partner_still_settles_with_a_payment_entry(self):
		"""Regression: the Journal Entry path is only for orders billed to app customers."""
		partner = self._ready_partner()
		invoice = self._sold(partner)
		response = create_settlement(partner=partner.name, invoices=[invoice], received_in=self._bank())
		self.assertTrue(_ok(response), _text(response))
		self.assertTrue(_data(response).get("payment_entry"))
		self.assertFalse(_data(response).get("journal_entry"))

	def test_the_till_refuses_to_collect_an_unsettled_inside_order(self):
		"""The partner holds this money; collecting it at the till charges the customer twice."""
		from pet_app.api.delivery_partners import refuse_partner_collected_invoices

		partner = self._inside_partner()
		invoice = self._sold(partner, customer=self._real_customer())
		with self.assertRaises(frappe.ValidationError) as caught:
			refuse_partner_collected_invoices([invoice])
		self.assertEqual(getattr(caught.exception, "code", None), "PARTNER_INVOICE_COLLECTED_BY_PARTNER")

		# Once settled it is closed anyway, and nothing is refused.
		create_settlement(partner=partner.name, invoices=[invoice], received_in=self._bank())
		refuse_partner_collected_invoices([invoice])

	def test_an_ordinary_partner_order_is_not_caught_by_the_guard(self):
		from pet_app.api.delivery_partners import refuse_partner_collected_invoices

		invoice = self._sold(self._ready_partner())
		refuse_partner_collected_invoices([invoice])


class TestAttachInvoiceToPartner(DeliveryPartnerBase):
	"""A sale saved as Due, and only afterwards found to be an app order."""

	def _inside_partner(self, rate=25.0):
		partner = self._partner(rate=rate)
		partner.commission_expense_account = frappe.db.get_value(
			"Account", {"company": self.company, "root_type": "Expense", "is_group": 0}, "name"
		)
		partner.is_inside = 1
		partner.save(ignore_permissions=True)
		return partner

	def _bank(self):
		return frappe.db.get_value(
			"Account", {"company": self.company, "account_type": "Bank", "is_group": 0}, "name"
		)

	def _real_customer(self):
		doc = frappe.get_doc({
			"doctype": "Customer",
			"customer_name": frappe.generate_hash("APPCUST", 8),
			"customer_type": "Company",
		})
		doc.insert(ignore_permissions=True)
		return doc.name

	def _due_sale(self, customer=None, rate=100000.0) -> str:
		response = create_pos_sale(
			customer=customer or self._real_customer(),
			pos_profile=self.profile,
			payment_status="Due",
			items=[{"item_code": self.item, "qty": 1, "rate": rate}],
			company=self.company,
		)
		self.assertTrue(_ok(response), _text(response))
		return _data(response)["created_invoice"]

	def _attach(self, invoice, partner, **overrides):
		from pet_app.api.delivery_partners import attach_invoice_to_partner

		payload = dict(
			sales_invoice=invoice,
			delivery_partner=partner.name,
			partner_order_ref=frappe.generate_hash("REF", 6),
		)
		payload.update(overrides)
		return attach_invoice_to_partner(**payload)

	def test_a_due_sale_becomes_an_inside_partner_order(self):
		from pet_app.api.delivery_partners import refuse_partner_collected_invoices

		partner = self._inside_partner(rate=25.0)
		invoice = self._due_sale(rate=100000.0)
		response = self._attach(invoice, partner, partner_order_ref="APP-1")
		self.assertTrue(_ok(response), _text(response))

		doc = frappe.get_doc("Sales Invoice", invoice)
		self.assertEqual(doc.custom_delivery_partner, partner.name)
		self.assertEqual(doc.custom_partner_order_ref, "APP-1")
		self.assertEqual(int(doc.custom_partner_is_inside or 0), 1)
		self.assertEqual(flt(doc.custom_partner_commission_amount), 25000.0)
		self.assertEqual(flt(doc.outstanding_amount), 100000.0)

		# The till now refuses to collect it...
		with self.assertRaises(frappe.ValidationError) as caught:
			refuse_partner_collected_invoices([invoice])
		self.assertEqual(getattr(caught.exception, "code", None), "PARTNER_INVOICE_COLLECTED_BY_PARTNER")

		# ...and the partner's settlement clears it.
		settled = create_settlement(partner=partner.name, invoices=[invoice], received_in=self._bank())
		self.assertTrue(_ok(settled), _text(settled))
		self.assertEqual(flt(frappe.db.get_value("Sales Invoice", invoice, "outstanding_amount")), 0.0)

	def test_an_invoice_already_given_to_a_partner_is_refused(self):
		partner = self._inside_partner()
		invoice = self._due_sale()
		self.assertTrue(_ok(self._attach(invoice, partner)))
		response = self._attach(invoice, partner)
		self.assertEqual(_code(response), "PARTNER_ALREADY_ATTACHED")

	def test_a_partly_paid_invoice_is_refused(self):
		partner = self._inside_partner()
		doc = frappe.get_doc("Sales Invoice", self._due_sale(rate=100000.0))
		pe = frappe.new_doc("Payment Entry")
		pe.payment_type = "Receive"
		pe.company = self.company
		pe.posting_date = nowdate()
		pe.party_type = "Customer"
		pe.party = doc.customer
		pe.paid_from = doc.debit_to
		pe.paid_to = self._bank()
		pe.paid_amount = pe.received_amount = 40000.0
		pe.append("references", {
			"reference_doctype": "Sales Invoice",
			"reference_name": doc.name,
			"allocated_amount": 40000.0,
		})
		pe.insert(ignore_permissions=True)
		pe.submit()

		response = self._attach(doc.name, partner)
		self.assertEqual(_code(response), "PARTNER_INVOICE_ALREADY_PAID")
		self.assertFalse(frappe.db.get_value("Sales Invoice", doc.name, "custom_delivery_partner"))

	def test_a_paid_sale_is_refused(self):
		partner = self._inside_partner()
		response = create_pos_sale(
			customer=self._real_customer(),
			pos_profile=self.profile,
			payment_status="Paid",
			paid_amount=100000.0,
			items=[{"item_code": self.item, "qty": 1, "rate": 100000.0}],
			company=self.company,
		)
		if not _ok(response):
			self.skipTest("This till cannot ring up a paid sale here: " + _text(response))
		response = self._attach(_data(response)["created_invoice"], partner)
		self.assertEqual(_code(response), "PARTNER_INVOICE_ALREADY_PAID")

	def test_an_invoice_on_the_walk_in_customer_is_refused(self):
		walk_in = frappe.db.get_value("POS Profile", self.profile, "customer")
		if not walk_in:
			self.skipTest("This POS Profile has no walk-in customer.")
		partner = self._inside_partner()
		response = self._attach(self._due_sale(customer=walk_in), partner)
		self.assertEqual(_code(response), "PARTNER_INSIDE_NEEDS_REAL_CUSTOMER")

	def test_an_ordinary_partner_still_needs_its_own_customer(self):
		partner = self._partner()
		response = self._attach(self._due_sale(), partner)
		self.assertEqual(_code(response), "PARTNER_CUSTOMER_MISMATCH")

	def test_the_order_number_is_required(self):
		partner = self._inside_partner()
		response = self._attach(self._due_sale(), partner, partner_order_ref="")
		self.assertEqual(_code(response), "PARTNER_ORDER_REF_REQUIRED")
