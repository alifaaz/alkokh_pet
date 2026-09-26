# -*- coding: utf-8 -*-
"""Who may receive boarding cash, and into which drawer.

**Only a till-holder may take boarding money.** If the acting user holds no POS Profile the
payment is refused and nothing is posted; the guardian goes to a cashier.

This reverses an earlier, more forgiving design that fell back to the stay's branch till so
an operator without a profile could still take a deposit "into the right drawer". Measured
against the real site, that was backwards. `Doctor` is in `BOARDING_WRITE_ROLES` and the
three doctors here also hold `Accounts User`, so each of them can check a stay out AND
collect its balance - and every dinar posted to the HOTEL CASHIER'S drawer. The cash sits in
the doctor's hand while the ledger says it is in Farah's till; at settlement Farah counts
short for money she never touched.

`custom_cashier_user` made that traceable but not preventable. A settlement that reconciles
to a plausible balance in the wrong drawer is the failure this area keeps reproducing, so
the door is now shut rather than redirected.

Nothing here commits; IntegrationTestCase rolls each test back.
"""

from __future__ import annotations

from contextlib import contextmanager

import frappe
from frappe.tests import IntegrationTestCase

from pet_app.api.healthcare.boarding import _resolve_boarding_deposit_account

HOTEL_PROFILE = "Alkokh Vet Hotel - Cashier"
STORE_PROFILE = "Alkokh Vet Store - Main Cashier"


@contextmanager
def as_user(user: str):
	previous = frappe.session.user
	frappe.session.user = user
	try:
		yield
	finally:
		frappe.session.user = previous


class BoardingDepositAccountBase(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.profile = frappe.get_doc("POS Profile", HOTEL_PROFILE)
		self.company = self.profile.company
		self.till_account = self.profile.get("custom_cash_account")
		self.treasury = frappe.db.get_single_value(
			"Pet App Accounting Settings", "treasury_cash_account"
		)
		frappe.db.set_value("POS Profile", HOTEL_PROFILE, {"branch": "hotel", "disabled": 0})
		frappe.clear_cache()

	def _till_holder(self) -> str:
		user = frappe.db.get_value("POS Profile User", {"parent": HOTEL_PROFILE}, "user")
		if not user or not frappe.db.exists("User", user):
			self.skipTest("no user is assigned to the hotel profile on this site")
		return user

	def _user_without_a_till(self) -> str:
		"""A real, enabled user holding no POS Profile - a doctor, in practice.

		NOT hardcoded to `Administrator`: it holds no profile on production but IS assigned
		to the Store profile on the test copy, so hardcoding it would test the opposite case
		and pass for the wrong reason.
		"""
		assigned = set(frappe.get_all("POS Profile User", pluck="user") or [])
		for user in frappe.get_all(
			"User", filters={"enabled": 1, "name": ["not in", list(assigned) or ["__none__"]]},
			pluck="name",
		):
			if user != "Guest":
				return user
		self.skipTest("every enabled user on this site holds a POS Profile")


class TestOnlyATillHolderMayTakeCash(BoardingDepositAccountBase):
	def test_a_user_without_a_till_is_refused(self):
		"""The doctor case. Nothing is posted and the message says what to do instead."""
		with as_user(self._user_without_a_till()):
			with self.assertRaises(frappe.ValidationError) as caught:
				_resolve_boarding_deposit_account(self.company, "Cash", "hotel")

		message = frappe.utils.strip_html(str(caught.exception))
		self.assertIn("no cashier till", message)
		self.assertIn("cashier", message)

	def test_the_stays_branch_does_not_rescue_a_user_without_a_till(self):
		"""The specific reversal.

		The hotel branch HAS exactly one enabled profile with a valid cash account, which is
		what the old code used to route this payment. Passing the branch must no longer make
		any difference - otherwise a doctor's cash lands in the cashier's drawer again.
		"""
		self.assertEqual(
			frappe.get_all(
				"POS Profile",
				filters={"branch": "hotel", "company": self.company, "disabled": 0},
				pluck="name",
			),
			[HOTEL_PROFILE],
			"fixture assumption broken: the branch no longer has exactly one till",
		)
		with as_user(self._user_without_a_till()):
			with self.assertRaises(frappe.ValidationError):
				_resolve_boarding_deposit_account(self.company, "Cash", "hotel")

	def test_the_treasury_is_never_used_for_cash(self):
		"""Even with no branch at all, cash is refused rather than sent to the treasury."""
		with as_user(self._user_without_a_till()):
			with self.assertRaises(frappe.ValidationError):
				_resolve_boarding_deposit_account(self.company, "Cash", None)

	def test_a_till_holder_gets_their_own_drawer(self):
		user = self._till_holder()
		with as_user(user):
			till = _resolve_boarding_deposit_account(self.company, "Cash", "hotel")

		own = frappe.get_all("POS Profile User", filters={"user": user}, pluck="parent")
		self.assertIn(till.profile, own, "the cash went to a drawer this user does not hold")
		self.assertNotEqual(till.account, self.treasury)

	def test_the_drawer_is_the_users_own_not_the_stays_branch(self):
		"""A cashier covering another branch still banks into their OWN till.

		The money is physically in the drawer they are standing at. Passing a foreign branch
		must not move it.
		"""
		user = self._till_holder()
		with as_user(user):
			here = _resolve_boarding_deposit_account(self.company, "Cash", "hotel")
			elsewhere = _resolve_boarding_deposit_account(self.company, "Cash", "main")
		self.assertEqual(here.account, elsewhere.account)

	def test_a_non_cash_mode_resolves_through_its_own_account(self):
		"""Card and transfer are not drawer cash, so the till rule does not apply."""
		mode = frappe.db.get_value(
			"Mode of Payment", {"type": ["!=", "Cash"], "enabled": 1}, "name"
		)
		if not mode or not frappe.db.exists(
			"Mode of Payment Account", {"parent": mode, "company": self.company}
		):
			self.skipTest("no non-cash mode of payment with an account on this site")

		with as_user(self._user_without_a_till()):
			resolved = _resolve_boarding_deposit_account(self.company, mode, "hotel")

		self.assertTrue(resolved.account)
		self.assertIsNone(resolved.profile)
