# Copyright (c) 2026, solvers and contributors
# For license information, please see license.txt

"""Tests for the cashier expense and payment-account endpoints.

Named after the decisions rather than the functions, so a reversal fails loudly: the
till-account guard and the omit-rather-than-guess rule are the two properties the rest of
the feature is worthless without.
"""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from pet_app.api.accounting.cashier import record_cashier_expense, save_cashier_profile
from pet_app.api.accounting.expenses import get_expense_categories
from pet_app.api.accounting.payment_accounts import resolve_payment_accounts


def _unwrap(response):
	"""Endpoints return the standard envelope; these tests assert on the payload."""
	if isinstance(response, dict) and "data" in response and "ok" in response:
		return response.get("data")
	return response


def _failed(response) -> bool:
	return isinstance(response, dict) and response.get("ok") is False


def _message(response) -> str:
	return (response.get("errors") or [{}])[0].get("message", "")


class TestCashierExpense(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.company = frappe.db.get_value("Company", {}, "name")
		if not cls.company:
			return

		cls.cash_account = frappe.db.get_value(
			"Account",
			{"account_type": "Cash", "is_group": 0, "company": cls.company, "disabled": 0},
			"name",
		)
		cls.expense_account = frappe.db.get_value(
			"Account", {"root_type": "Expense", "is_group": 0, "company": cls.company}, "name"
		)
		cls.receivable_account = frappe.db.get_value(
			"Account",
			{"account_type": "Receivable", "is_group": 0, "company": cls.company},
			"name",
		)
		cls.other_cash_account = next(
			(
				name
				for name in frappe.get_all(
					"Account",
					filters={"account_type": "Cash", "is_group": 0, "company": cls.company},
					pluck="name",
				)
				if name != cls.cash_account
			),
			None,
		)

		# A real POS Profile, not a stub. Journal Entry.custom_pos_profile is a Link, so a
		# made-up profile name fails link validation the moment the voucher is inserted -
		# and stamping that field is precisely what the settlement itemisation depends on.
		cls.profile = None
		# Payment rows are passed explicitly rather than left to the endpoint's implicit
		# default: ERPNext refuses a POS Profile with no payment method, and the mode must
		# be one that actually has a Mode of Payment Account for this company.
		mode = frappe.db.get_value(
			"Mode of Payment Account", {"company": cls.company}, "parent"
		)
		for candidate in frappe.get_all(
			"Mode of Payment Account", filters={"company": cls.company}, pluck="parent"
		):
			if frappe.db.get_value("Mode of Payment", candidate, "type") == "Cash":
				mode = candidate
				break

		if cls.company and cls.cash_account and mode:
			name = f"TEST TILL {frappe.generate_hash(length=8)}"
			response = save_cashier_profile(
				profile=name,
				data={
					"company": cls.company,
					"cash_account": cls.cash_account,
					"payments": [{"mode_of_payment": mode, "default": 1}],
				},
			)
			# Only claim the profile if it really landed; setUp skips the suite otherwise
			# rather than letting every test fail with "Cashier profile is required".
			if response.get("ok") and frappe.db.exists("POS Profile", name):
				cls.profile = name
				frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		if cls.profile and frappe.db.exists("POS Profile", cls.profile):
			frappe.delete_doc("POS Profile", cls.profile, force=True, ignore_permissions=True)
			frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		if not self.company or not self.cash_account or not self.expense_account:
			self.skipTest("site has no company with both a cash and an expense ledger account")
		if not self.profile:
			self.skipTest("could not create a test POS Profile on this site")

	def tearDown(self):
		frappe.db.rollback()
		frappe.set_user("Administrator")

	def _record(self, **overrides):
		payload = {
			"category": "test_expense",
			"expense_account": self.expense_account,
			"amount": 100,
			"cash_account": self.cash_account,
			"posting_date": frappe.utils.nowdate(),
			"company": self.company,
			"pos_profile": self.profile,
		}
		payload.update(overrides)
		return record_cashier_expense(**payload)

	# -- the guard everything hangs on ---------------------------------------------

	def test_cash_account_must_be_the_callers_own_till(self):
		"""expected_cash_on_hand is computed from this account's ledger.

		An expense credited anywhere else reconciles the till against money that has
		already left the drawer - silently, until someone counts. The client is not
		trusted on this field.
		"""
		other = self.other_cash_account or self.receivable_account
		if not other:
			self.skipTest("site has only one cash account to test against")

		response = self._record(cash_account=other)

		self.assertTrue(_failed(response))
		self.assertIn("is not the cash account", _message(response))

	def test_blank_cash_account_is_refused(self):
		self.assertTrue(_failed(self._record(cash_account="")))

	# -- amount ---------------------------------------------------------------------

	def test_zero_and_negative_amounts_are_refused(self):
		for amount in (0, -1, -100.5):
			with self.subTest(amount=amount):
				response = self._record(amount=amount)
				self.assertTrue(_failed(response))
				self.assertIn("greater than zero", _message(response))

	# -- the account has to actually be an expense ----------------------------------

	def test_expense_account_must_have_root_type_expense(self):
		if not self.receivable_account:
			self.skipTest("site has no receivable account to test against")

		response = self._record(category="no_such_category", expense_account=self.receivable_account)

		self.assertTrue(_failed(response))
		self.assertIn("root type Expense", _message(response))

	def test_unknown_category_without_an_account_is_refused(self):
		response = self._record(category="no_such_category", expense_account="")

		self.assertTrue(_failed(response))
		self.assertIn("Expense Account is required", _message(response))

	def test_nonexistent_branch_is_refused(self):
		self.assertTrue(_failed(self._record(branch="no-such-branch-exists")))

	# -- the happy path -------------------------------------------------------------

	def test_recording_an_expense_submits_and_credits_the_till(self):
		response = self._record(category="no_such_category", remarks="unit test payout")
		data = _unwrap(response)

		self.assertFalse(_failed(response), _message(response) if _failed(response) else "")
		self.assertTrue(data.get("submitted"))
		self.assertEqual(data.get("voucher_type"), "Journal Entry")

		entry = frappe.get_doc("Journal Entry", data["voucher_id"])
		self.assertEqual(entry.docstatus, 1)
		self.assertEqual(entry.voucher_type, "Cash Entry")

		credited = [row for row in entry.accounts if row.credit_in_account_currency]
		debited = [row for row in entry.accounts if row.debit_in_account_currency]
		self.assertEqual([row.account for row in credited], [self.cash_account])
		self.assertEqual([row.account for row in debited], [self.expense_account])
		self.assertEqual(credited[0].credit_in_account_currency, 100)

	def test_submitted_is_reported_from_the_document_not_assumed(self):
		"""The client warns the operator explicitly when this is false, so it must not lie."""
		data = _unwrap(self._record(category="no_such_category"))
		docstatus = frappe.db.get_value("Journal Entry", data["voucher_id"], "docstatus")
		self.assertEqual(data["submitted"], docstatus == 1)


class TestExpenseCategories(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.company = frappe.db.get_value("Company", {}, "name")
		if not self.company:
			self.skipTest("no company on this site")

	def test_falls_back_to_the_chart_of_accounts_when_unconfigured(self):
		"""An unconfigured site must behave as it did before this endpoint existed."""
		settings = frappe.get_single("Pet App Accounting Settings")
		if settings.get("expense_categories"):
			self.skipTest("this site has categories configured; the fallback is not exercised")

		categories = _unwrap(get_expense_categories(company=self.company)) or []
		items = categories.get("items") if isinstance(categories, dict) else categories

		self.assertTrue(items, "the fallback must not hand the till an empty picker")
		for row in items:
			self.assertEqual(
				frappe.db.get_value("Account", row["account"], "root_type"), "Expense"
			)
			self.assertEqual(frappe.db.get_value("Account", row["account"], "is_group"), 0)

	def test_nonexistent_branch_is_refused(self):
		self.assertTrue(_failed(get_expense_categories(company=self.company, branch="no-such-branch")))


class TestResolvePaymentAccounts(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.company = frappe.db.get_value("Company", {}, "name")
		self.customer = frappe.db.get_value("Customer", {}, "name")
		if not self.company or not self.customer:
			self.skipTest("site has no company or customer")

	def test_unresolved_sides_are_omitted_not_null(self):
		"""An omitted field leaves the cashier a filtered picker; a null reads as an answer."""
		data = _unwrap(resolve_payment_accounts("Receive", self.company)) or {}

		self.assertNotIn("paid_from", data)
		self.assertIsNone(data.get("source"))

	def test_party_side_reports_where_the_answer_came_from(self):
		data = _unwrap(
			resolve_payment_accounts(
				"Receive", self.company, party_type="Customer", party=self.customer
			)
		)

		self.assertIn("paid_from", data)
		self.assertIn(
			data["source"], {"party_account", "party_group_account", "company_default"}
		)

	def test_pay_reverses_the_two_sides(self):
		receive = _unwrap(
			resolve_payment_accounts(
				"Receive", self.company, party_type="Customer", party=self.customer
			)
		)
		pay = _unwrap(
			resolve_payment_accounts(
				"Pay", self.company, party_type="Customer", party=self.customer
			)
		)

		self.assertEqual(receive.get("paid_from"), pay.get("paid_to"))

	def test_invalid_payment_type_is_refused(self):
		self.assertTrue(_failed(resolve_payment_accounts("Transfer", self.company)))
