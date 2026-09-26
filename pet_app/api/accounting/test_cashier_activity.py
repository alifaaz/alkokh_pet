# Copyright (c) 2026, solvers and contributors
# For license information, please see license.txt

"""Tests for the cashier activity dashboard endpoints.

Named after the decisions rather than the functions. Every test here is a property the
dashboard's accuracy rests on, and each one is a thing the client used to get wrong:

* cash and stock expenses stay separate, because a combined total reconciles with nothing;
* a multi-debit voucher reports per account, not once against whichever account matched;
* a credit to a till whose contra is not an expense is not an expense;
* settlement status comes from the settlement, not from a capped global scan.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from pet_app.api.accounting.cashier import (
	get_cashier_activity,
	get_cashier_settlement_snapshot,
	list_cashier_expenses,
	list_cashier_profiles_for_user,
	settle_cashier_to_treasury,
)


def _unwrap(response):
	"""Endpoints return the standard envelope; these tests assert on the payload."""
	if isinstance(response, dict) and "data" in response and "ok" in response:
		return response.get("data")
	return response


def _failed(response) -> bool:
	return isinstance(response, dict) and response.get("ok") is False


class TestCashierActivity(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.ready = False

		cls.company = frappe.db.get_value("POS Profile", {"disabled": 0}, "company")
		if not cls.company:
			return
		cls.profile = frappe.db.get_value(
			"POS Profile", {"disabled": 0, "company": cls.company, "custom_cash_account": ["!=", ""]}, "name"
		)
		if not cls.profile:
			return

		profile_doc = frappe.get_doc("POS Profile", cls.profile)
		cls.cash_account = profile_doc.get("custom_cash_account")
		cls.cost_center = frappe.db.get_value(
			"Cost Center", {"company": cls.company, "is_group": 0}, "name"
		)
		expense_accounts = frappe.get_all(
			"Account",
			filters={"company": cls.company, "is_group": 0, "root_type": "Expense"},
			fields=["name"],
			order_by="name asc",
			limit=2,
		)
		other_cash = frappe.db.get_value(
			"Account",
			{
				"company": cls.company,
				"is_group": 0,
				"account_type": "Cash",
				"name": ["!=", cls.cash_account],
			},
			"name",
		)
		if len(expense_accounts) < 2 or not cls.cost_center or not other_cash:
			return

		cls.expense_a = expense_accounts[0].name
		cls.expense_b = expense_accounts[1].name
		cls.other_cash = other_cash
		cls.posting_date = frappe.utils.nowdate()
		cls.ready = True

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		if not self.ready:
			self.skipTest("site has no configured till to test against")
		frappe.db.savepoint("cashier_activity_test")
		self.addCleanup(self._rollback_test)
		# Isolate balances and settlements from existing till activity on any test date.
		account = frappe.copy_doc(frappe.get_doc("Account", self.cash_account))
		account.account_name = f"Activity Test {frappe.generate_hash(length=10)}"
		account.account_number = None
		account.insert(ignore_permissions=True)
		profile = frappe.copy_doc(frappe.get_doc("POS Profile", self.profile))
		profile.name = f"Activity Test {frappe.generate_hash(length=10)}"
		profile.custom_cash_account = account.name
		profile.insert(ignore_permissions=True)
		self.profile = profile.name
		self.cash_account = account.name

	def _rollback_test(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="cashier_activity_test")

	def _journal(self, lines, remark="test", posting_date=None):
		je = frappe.new_doc("Journal Entry")
		je.voucher_type = "Journal Entry"
		je.company = self.company
		je.posting_date = posting_date or self.posting_date
		je.user_remark = remark
		for account, debit, credit in lines:
			je.append(
				"accounts",
				{
					"account": account,
					"debit_in_account_currency": debit,
					"credit_in_account_currency": credit,
					"cost_center": self.cost_center,
				},
			)
		je.insert(ignore_permissions=True)
		je.submit()
		return je.name

	def _fund_till(self, amount):
		return self._journal([(self.cash_account, amount, 0), (self.other_cash, 0, amount)], "fund")

	def _rows_for(self, voucher_no):
		response = _unwrap(
			list_cashier_expenses(
				from_date=self.posting_date, to_date=self.posting_date, profile=self.profile
			)
		)
		return [row for row in response["data"] if row["voucher_no"] == voucher_no]

	def test_multi_debit_voucher_reports_one_row_per_expense_account(self):
		"""The inference this replaces reads GL Entry.`against` - a comma-joined string.

		A Journal Entry with two expense debits against one credit matches on either, and
		the client reports the whole credit once. Reading both sides of the voucher gives
		a row per account for the amount actually posted to it.
		"""
		if not self.ready:
			self.skipTest("site has no configured till to test against")

		self._fund_till(100)
		voucher = self._journal(
			[(self.expense_a, 30, 0), (self.expense_b, 70, 0), (self.cash_account, 0, 100)],
			"multi debit",
		)

		rows = self._rows_for(voucher)
		self.assertEqual(len(rows), 2)
		self.assertEqual({row["account"] for row in rows}, {self.expense_a, self.expense_b})
		self.assertEqual({row["amount"] for row in rows}, {30.0, 70.0})
		# One credit account, so nothing was apportioned and the posted amounts stand.
		self.assertTrue(all(row["exact"] for row in rows))
		self.assertTrue(all(row["kind"] == "cash" for row in rows))

	def test_credit_to_till_with_non_expense_contra_is_not_an_expense(self):
		"""A handover, a refund and a settlement all credit the till. None is an expense.

		The client's rule - credit > 0 and a contra that looks like an expense - has no way
		to tell these apart when the contra string happens to contain an expense account.
		The root-type test does, and needs no voucher-type allowlist to do it.
		"""
		if not self.ready:
			self.skipTest("site has no configured till to test against")

		self._fund_till(100)
		voucher = self._journal([(self.other_cash, 100, 0), (self.cash_account, 0, 100)], "handover")

		self.assertEqual(self._rows_for(voucher), [])

	def test_split_credit_with_one_expense_account_is_exact(self):
		"""Every funding source paid the same expense account; no allocation is needed."""
		if not self.ready:
			self.skipTest("site has no configured till to test against")

		self._fund_till(100)
		voucher = self._journal(
			[(self.expense_a, 100, 0), (self.cash_account, 0, 75), (self.other_cash, 0, 25)],
			"split credit",
		)

		rows = self._rows_for(voucher)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["amount"], 75.0)
		self.assertTrue(rows[0]["exact"])

	def test_mixed_funding_and_debits_are_not_estimated(self):
		voucher = self._journal([
			(self.expense_a, 30, 0), (self.expense_b, 70, 0),
			(self.cash_account, 0, 75), (self.other_cash, 0, 25),
		])
		response = _unwrap(list_cashier_expenses(self.posting_date, self.posting_date, self.profile))
		self.assertFalse(response["cash_total_complete"])
		self.assertEqual(response["totals"]["cash"], 0)
		self.assertFalse(any(row["voucher_no"] == voucher for row in response["data"]))
		unresolved = next(row for row in response["unresolved_cash_vouchers"] if row["voucher_no"] == voucher)
		self.assertEqual(unresolved["cash_outflow"], 75)
		self.assertEqual(unresolved["reason"], "ambiguous_expense_allocation")

	def test_expense_reversals_are_netted(self):
		voucher = self._journal([
			(self.expense_a, 100, 0), (self.expense_a, 0, 20), (self.cash_account, 0, 80),
		])
		rows = self._rows_for(voucher)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["amount"], 80)
		self.assertTrue(rows[0]["exact"])

	def test_same_expense_on_two_tills_has_distinct_ids(self):
		other = frappe.copy_doc(frappe.get_doc("POS Profile", self.profile))
		other.name = f"Activity Other {frappe.generate_hash(length=10)}"
		other.custom_cash_account = self.other_cash
		other.insert(ignore_permissions=True)
		voucher = self._journal([
			(self.expense_a, 100, 0), (self.cash_account, 0, 75), (self.other_cash, 0, 25),
		])
		result = _unwrap(list_cashier_expenses(self.posting_date, self.posting_date))
		rows = [row for row in result["data"] if row["voucher_no"] == voucher]
		self.assertEqual(len(rows), 2)
		self.assertEqual(len({row["id"] for row in rows}), 2)
		self.assertEqual(sum(row["amount"] for row in rows), 100)

	def test_cash_and_stock_totals_are_never_merged(self):
		"""Cash left the drawer; stock did not. A combined total reconciles with nothing."""
		if not self.ready:
			self.skipTest("site has no configured till to test against")

		response = _unwrap(
			list_cashier_expenses(
				from_date=self.posting_date, to_date=self.posting_date, profile=self.profile
			)
		)
		self.assertIn("cash", response["totals"])
		self.assertIn("stock", response["totals"])
		self.assertNotIn("total", response["totals"])
		self.assertTrue(all(row["kind"] in ("cash", "stock") for row in response["data"]))

	def test_settlement_status_comes_from_the_settlement(self):
		"""settled_at and settlement_difference, on the snapshot and on the activity row.

		The client matched a globally capped list of the latest two hundred settlements on
		cashier_profile plus posting_date, which stops being correct at record two hundred
		and one. Both endpoints now read the settlement for the till and date in question.
		"""
		if not self.ready:
			self.skipTest("site has no configured till to test against")

		before = _unwrap(
			get_cashier_settlement_snapshot(pos_profile=self.profile, posting_date=self.posting_date)
		)
		self.assertFalse(before["is_settled"])
		# Never 0.0 for an unsettled till - zero is what a till that balanced reports.
		self.assertIsNone(before["settlement_difference"])

		self._fund_till(500)
		settled = settle_cashier_to_treasury(
			pos_profile=self.profile,
			counted_cash=520,
			transfer_amount=500,
			posting_date=self.posting_date,
		)
		if _failed(settled):
			self.skipTest("site has no treasury account configured")
		expected = _unwrap(settled)["settlement"]["expected_cash"]

		after = _unwrap(
			get_cashier_settlement_snapshot(pos_profile=self.profile, posting_date=self.posting_date)
		)
		self.assertTrue(after["is_settled"])
		self.assertEqual(after["settled_at"], str(self.posting_date))
		# counted_cash is a named parameter now, so the difference is a real count result.
		self.assertEqual(after["settlement_difference"], 520.0 - expected)

		activity = _unwrap(
			get_cashier_activity(
				from_date=self.posting_date, to_date=self.posting_date, profile=self.profile
			)
		)
		row = activity["cashiers"][0]
		self.assertEqual(row["settled_at"], str(self.posting_date))
		self.assertEqual(row["settlement_difference"], after["settlement_difference"])

	def test_activity_expected_cash_agrees_with_the_snapshot(self):
		"""Two endpoints, one number: it is what a cashier is asked to count against."""
		if not self.ready:
			self.skipTest("site has no configured till to test against")

		activity = _unwrap(
			get_cashier_activity(
				from_date=self.posting_date, to_date=self.posting_date, profile=self.profile
			)
		)
		snapshot = _unwrap(
			get_cashier_settlement_snapshot(pos_profile=self.profile, posting_date=self.posting_date)
		)
		self.assertEqual(
			activity["cashiers"][0]["expected_cash_on_hand"], snapshot["expected_cash_on_hand"]
		)

	def test_activity_does_not_load_detail_payload_per_profile(self):
		with patch("pet_app.api.accounting.cashier._profile_payload", side_effect=AssertionError("detail read")):
			response = get_cashier_activity(self.posting_date, self.posting_date)
		self.assertTrue(response["ok"], response["errors"])
		self.assertGreaterEqual(response["data"]["total"], 2)

	def test_zero_count_and_legacy_positional_arguments(self):
		self._fund_till(50)
		response = settle_cashier_to_treasury(
			self.profile, None, None, 0, self.posting_date, 0, counted_cash=0,
		)
		self.assertTrue(response["ok"], response["errors"])
		settlement = response["data"]["settlement"]
		self.assertEqual(settlement["counted_cash"], 0)
		self.assertEqual(settlement["difference_amount"], -50)
		self.assertEqual(settlement["docstatus"], 0)
		self.assertFalse(_unwrap(get_cashier_settlement_snapshot(profile=self.profile))["is_settled"])

	def test_reversed_date_range_is_refused(self):
		response = get_cashier_activity(from_date="2026-09-06", to_date="2026-01-01")
		self.assertTrue(_failed(response))
		self.assertTrue(_failed(list_cashier_expenses("2026-09-06", "2026-01-01")))

	def test_range_carries_opening_balance_and_excludes_cancellation(self):
		previous = frappe.utils.add_days(self.posting_date, -1)
		self._journal([(self.cash_account, 100, 0), (self.other_cash, 0, 100)], posting_date=previous)
		self._fund_till(50)
		self._journal([(self.expense_a, 20, 0), (self.cash_account, 0, 20)])
		cancelled = self._fund_till(999)
		frappe.get_doc("Journal Entry", cancelled).cancel()
		row = _unwrap(get_cashier_activity(self.posting_date, self.posting_date, self.profile))["cashiers"][0]
		self.assertEqual((row["incoming"], row["outgoing"], row["current_balance"]), (50, 20, 130))
		self.assertEqual(row["expected_cash_on_hand"], 130)
		ranged = _unwrap(get_cashier_activity(previous, self.posting_date, self.profile))["cashiers"][0]
		self.assertEqual(ranged["incoming"], 150)

	def test_settlement_history_is_not_capped_and_ignores_drafts_and_cancellations(self):
		# Query fixtures avoid creating hundreds of treasury transfers to test a read limit.
		for i in range(207):
			row = frappe.get_doc({
				"doctype": "Pet App Cashier Settlement", "name": f"activity-{self.profile}-{i:03}",
				"cashier_profile": self.profile, "posting_date": self.posting_date,
				"company": self.company, "cash_account": self.cash_account,
				"docstatus": 1 if i < 205 else (0 if i == 205 else 2),
				"difference_amount": i, "difference_type": "Over", "transfer_amount": 1,
				"creation": f"{self.posting_date} 01:{i // 60:02}:{i % 60:02}",
			})
			row.db_insert()
		for result in (
			_unwrap(get_cashier_activity(self.posting_date, self.posting_date, self.profile))["cashiers"][0],
			_unwrap(get_cashier_settlement_snapshot(profile=self.profile, posting_date=self.posting_date)),
		):
			self.assertEqual(result["settlement_count"], 205)
			self.assertEqual(result["settled_total"], 205)
			self.assertEqual(result["settlement_difference"], 204)

	def test_profile_restrictions_apply_to_list_detail_and_shared_account_metadata(self):
		other_profile = type(self).profile
		# Deliberately shared: the warning must survive without revealing the hidden name.
		frappe.db.set_value("POS Profile", other_profile, "custom_cash_account", self.cash_account)
		user = frappe.get_doc({
			"doctype": "User", "email": f"activity-{frappe.generate_hash(length=10)}@example.test",
			"first_name": "Activity Test", "send_welcome_email": 0,
			"roles": [{"role": "POS Cashier"}],
		}).insert(ignore_permissions=True)
		frappe.get_doc({
			"doctype": "User Permission", "user": user.name,
			"allow": "POS Profile", "for_value": self.profile, "apply_to_all_doctypes": 1,
		}).insert(ignore_permissions=True)
		frappe.clear_cache(user=user.name)
		voucher = self._journal([(self.expense_a, 20, 0), (self.cash_account, 0, 20)])
		frappe.set_user(user.name)
		picker = _unwrap(list_cashier_profiles_for_user())["data"]
		self.assertEqual([row["name"] for row in picker], [self.profile])
		activity = _unwrap(get_cashier_activity(self.posting_date, self.posting_date))
		self.assertEqual([row["profile_name"] for row in activity["cashiers"]], [self.profile])
		warning = next(w for w in activity["cashiers"][0]["cash_account_warnings"] if w["code"] == "shared_cash_account")
		self.assertGreater(warning["other_profile_count"], 0)
		self.assertNotIn(other_profile, str(warning))
		self.assertTrue(_failed(get_cashier_activity(self.posting_date, self.posting_date, other_profile)))
		self.assertTrue(_failed(list_cashier_expenses(self.posting_date, self.posting_date, other_profile)))
		self.assertTrue(_failed(get_cashier_settlement_snapshot(profile=other_profile)))
		rows = self._rows_for(voucher)
		self.assertEqual(rows[0]["profile_candidates"], [self.profile])
		self.assertIsNone(rows[0]["profile"])

	def test_stock_categories_branch_and_date_filter_have_no_200_row_limit(self):
		warehouse = frappe.db.get_value("POS Profile", self.profile, "warehouse")
		branch = frappe.db.get_value("Branch", {}, "name")
		if not warehouse or not branch:
			self.skipTest("site needs a warehouse and branch")
		# Read-model fixtures exercise the actual parent/child SQL over more than 200 rows.
		for i in range(210):
			entry = frappe.get_doc({
				"doctype": "Stock Entry", "name": f"activity-stock-{self.profile}-{i:03}",
				"company": self.company, "docstatus": 2 if i == 206 else (0 if i == 207 else 1),
				"purpose": "Material Transfer" if i == 209 else "Material Issue",
				"stock_entry_type": "Material Issue", "branch": None if i == 208 else branch,
				"posting_date": self.posting_date if i != 205 else frappe.utils.add_days(self.posting_date, -1),
			})
			entry.db_insert()
			for account, amount in ((self.expense_a, 3), (self.expense_b, 7)):
				frappe.get_doc({
					"doctype": "Stock Entry Detail", "parent": entry.name,
					"parenttype": "Stock Entry", "parentfield": "items",
					"expense_account": account, "s_warehouse": warehouse, "amount": amount,
				}).db_insert()
		with patch("pet_app.api.accounting.cashier._categories_by_account", return_value={
			self.expense_a: {"key": "clinic", "label_en": "Clinic"},
			self.expense_b: {"key": "hotel", "label_en": "Hotel"},
		}):
			response = _unwrap(list_cashier_expenses(self.posting_date, self.posting_date, self.profile, branch))
		rows = [row for row in response["data"] if row["voucher_no"].startswith(f"activity-stock-{self.profile}-")]
		self.assertEqual(len(rows), 410)
		self.assertEqual(sum(row["amount"] for row in rows), 2050)
		self.assertEqual({row["category"] for row in rows}, {"clinic", "hotel"})
		self.assertTrue(all(row["kind"] == "stock" for row in rows))
		self.assertEqual(len({row["id"] for row in rows}), len(rows))

	def test_category_default_warehouse_uses_its_branch(self):
		from pet_app.api.accounting.expenses import get_expense_categories
		with (
			patch("pet_app.api.accounting.expenses._configured_categories", return_value=[
				{"key": "clinic", "branch": "clinic"}, {"key": "shared", "branch": None},
			]),
			patch("pet_app.api.accounting.expenses.branch_warehouse", return_value="Clinic Warehouse"),
		):
			rows = _unwrap(get_expense_categories(company=self.company))["items"]
		self.assertEqual(rows[0]["default_warehouse"], "Clinic Warehouse")
		self.assertIsNone(rows[1]["default_warehouse"])
