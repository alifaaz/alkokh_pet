# -*- coding: utf-8 -*-
"""Money taken against a boarding stay, from check-in to a settled invoice.

Until `record_boarding_payment` existed there was exactly one way for boarding cash to
enter the system - `check_in_boarding(deposit=...)`, which happens once. A guardian paying
in instalments could not be recorded at all, so the desk wrote hand-made Payment Entries
that carried no link to the stay. Check-out never saw them: the stay was invoiced in full,
the till asked for the whole amount again, and the money already taken sat on the customer
as credit nobody could see.

Two findings shape what is asserted here, and both are easy to regress:

* **Advances cannot survive `is_pos`.** ERPNext skips `update_against_document_in_jv()`
  outright for POS invoices, so a POS invoice never turns its `advances` rows into Payment
  Entry References, and `update_outstanding_amt` then rebuilds outstanding from GL entries
  that were never written. Measured before the fix: a 200,000 stay with 120,000 of
  allocated advances, settled at the till for the correct 80,000, submitted with
  outstanding 120,000 and all three deposits still `unallocated`. Hence
  `test_the_checkout_invoice_is_not_a_pos_invoice` and the Payment Entry Reference
  assertions - an `outstanding` check alone does NOT catch this.

* **A draft never posts.** Three drafts worth 820,000 sit on production with no GL entry,
  no receivable and no revenue, because check-out left them for a till visit that never
  happened.

Nothing here commits; IntegrationTestCase rolls each test back.
"""

from __future__ import annotations

from contextlib import contextmanager

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, nowdate

from pet_app.api.healthcare.boarding import (
	boarding_payment_entries,
	boarding_total_paid,
	check_in_boarding,
	check_out_boarding,
	record_boarding_payment,
	reserve_room,
)

COMPANY = "Kokh-vet"


@contextmanager
def as_user(user: str):
	previous = frappe.session.user
	frappe.session.user = user
	try:
		yield
	finally:
		frappe.session.user = previous


def _ok(response):
	return bool(response.get("ok"))


def _data(response):
	return response.get("data") or {}


class BoardingPaymentBase(IntegrationTestCase):
	"""A real stay: a free room, a dog whose rate resolves off the catalogue, a guardian."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.check_in = add_days(nowdate(), -7)
		self.pet, self.guardian = self._free_pet()
		self.room = self._free_room()

	def _free_room(self) -> str:
		"""A room with no ACTIVE OCCUPANT in it.

		Occupancy is decided by `room_occupancy`, which reads occupants and not bookings -
		and this site has occupants left Active behind closed bookings, so a room that looks
		free by booking status still refuses a reservation.
		"""
		from pet_app.utils.boarding_occupancy import room_occupancy

		for name in frappe.get_all("Service Room", filters={"status": "Active"}, pluck="name"):
			if not room_occupancy(name):
				return name
		self.skipTest("every Service Room on this site has an active occupant")

	def _free_pet(self):
		busy = (frappe.get_all(
			"Pet Boarding",
			filters={"record_status": ["in", ["Reserved", "Checked In"]], "docstatus": ["<", 2]},
			pluck="pet") or []) + (frappe.get_all(
			"Pet Boarding Occupant", filters={"status": "Active"}, pluck="pet") or []) + ["__none__"]
		rows = frappe.db.sql(
			"""select p.name pet, pg.guardian_id guardian
			   from `tabPet` p
			   join `tabPetGuardian` pg on pg.pet_id = p.name
			   join `tabGuardian` g on g.name = pg.guardian_id
			   where p.animal_type = 'Dog' and p.name not in %(busy)s
			     and ifnull(g.customer_id, '') != ''
			   limit 1""",
			{"busy": tuple(set(busy))},
			as_dict=1,
		)
		if not rows:
			self.skipTest("no unbooked dog with a guardian on this site")
		return rows[0].pet, rows[0].guardian

	def _open_stay(self, deposit=None) -> str:
		res = reserve_room(
			roomId=self.room, petId=self.pet, guardianId=self.guardian,
			checkIn=self.check_in, boardingType="Travel",
		)
		self.assertTrue(_ok(res), msg=res)
		name = _data(res)["boarding_id"]
		res = check_in_boarding(boarding_id=name, deposit=deposit, check_in=self.check_in)
		self.assertTrue(_ok(res), msg=res)
		return name

	def _customer(self, boarding) -> str:
		return frappe.db.get_value("Pet Boarding", boarding, "customer")

	def _ledger_balance(self, customer) -> float:
		return flt(frappe.db.sql(
			"""select sum(debit) - sum(credit) from `tabGL Entry`
			   where party_type = 'Customer' and party = %s and is_cancelled = 0""",
			customer)[0][0])

	def _unallocated(self, customer) -> float:
		return flt(frappe.db.sql(
			"""select ifnull(sum(unallocated_amount), 0) from `tabPayment Entry`
			   where party = %s and docstatus = 1""", customer)[0][0])

	def _stay_unallocated(self, stay) -> float:
		"""Money left floating on THIS stay's own payments.

		Absolute per-customer figures are useless here: this site is a copy of production
		and the guardians these tests borrow already carry balances of their own. Everything
		asserted is either scoped to the stay or measured as a delta.
		"""
		return flt(frappe.db.sql(
			"""select ifnull(sum(unallocated_amount), 0) from `tabPayment Entry`
			   where reference_no = %s and docstatus = 1""", stay)[0][0])


class TestOnlyACashierTakesMoney(BoardingPaymentBase):
	"""Ending a stay and taking money are different jobs.

	`Doctor` is in BOARDING_WRITE_ROLES and the doctors on this site also hold
	`Accounts User`, so each can check a stay out. If they could also collect its balance the
	cash would sit in their hand while the ledger booked it into the cashier's drawer.
	"""

	def _doctor_without_a_till(self) -> str:
		"""A real doctor holding no POS Profile - not just any user without one.

		A user with no boarding permission at all is refused by `_require_boarding_*` long
		before the cash rule is reached, so testing with one proves nothing. Picked the wrong
		way, this test passes for entirely the wrong reason.
		"""
		assigned = set(frappe.get_all("POS Profile User", pluck="user") or [])
		for user in frappe.db.sql(
			"""select distinct parent from `tabHas Role`
			   where role in ('Doctor', 'Healthcare Practitioner') and parenttype = 'User'""",
			pluck=True,
		):
			if user in assigned or not frappe.db.get_value("User", user, "enabled"):
				continue
			roles = set(frappe.db.sql(
				"select role from `tabHas Role` where parent=%s", user, pluck=True))
			if roles & {"Accounts User", "Accounts Manager", "System Manager"}:
				return user
		self.skipTest("no doctor without a POS Profile on this site")

	def test_a_doctor_cannot_take_a_deposit(self):
		doctor = self._doctor_without_a_till()
		res = reserve_room(
			roomId=self.room, petId=self.pet, guardianId=self.guardian,
			checkIn=self.check_in, boardingType="Travel",
		)
		stay = _data(res)["boarding_id"]

		with as_user(doctor):
			refused = check_in_boarding(boarding_id=stay, deposit=50000, check_in=self.check_in)

		self.assertFalse(_ok(refused), "a doctor was allowed to take cash")
		self.assertIn(
			"no cashier till",
			frappe.utils.strip_html((refused.get("errors") or [{}])[0].get("message", "")),
		)

	def test_the_refusal_leaves_the_stay_untouched(self):
		"""A guard that half-checks-in the animal is worse than no guard.

		The stay must still be Reserved, with no deposit and no Payment Entry, so the
		operator can simply check in again without one.
		"""
		doctor = self._doctor_without_a_till()
		res = reserve_room(
			roomId=self.room, petId=self.pet, guardianId=self.guardian,
			checkIn=self.check_in, boardingType="Travel",
		)
		stay = _data(res)["boarding_id"]

		with as_user(doctor):
			check_in_boarding(boarding_id=stay, deposit=50000, check_in=self.check_in)

		after = frappe.db.get_value(
			"Pet Boarding", stay, ["record_status", "deposit", "deposit_payment_entry"], as_dict=True
		)
		self.assertEqual(after.record_status, "Reserved", "the stay was checked in anyway")
		self.assertFalse(flt(after.deposit))
		self.assertIsNone(after.deposit_payment_entry)
		self.assertEqual(frappe.db.count("Payment Entry", {"reference_no": stay}), 0)

	def test_a_doctor_can_still_check_in_without_money(self):
		"""The refusal must be about the cash, not about the doctor."""
		doctor = self._doctor_without_a_till()
		res = reserve_room(
			roomId=self.room, petId=self.pet, guardianId=self.guardian,
			checkIn=self.check_in, boardingType="Travel",
		)
		stay = _data(res)["boarding_id"]

		with as_user(doctor):
			ok = check_in_boarding(boarding_id=stay, check_in=self.check_in)

		self.assertTrue(_ok(ok), msg=ok)
		self.assertEqual(frappe.db.get_value("Pet Boarding", stay, "record_status"), "Checked In")

	def test_a_doctor_cannot_collect_the_balance_after_checkout(self):
		doctor = self._doctor_without_a_till()
		stay = self._open_stay(deposit=0)
		check_out_boarding(boarding_id=stay)
		invoice = frappe.get_doc(
			"Sales Invoice", frappe.db.get_value("Pet Boarding", stay, "sales_invoice")
		)

		with as_user(doctor):
			refused = record_boarding_payment(
				boarding_id=stay, amount=flt(invoice.outstanding_amount)
			)

		self.assertFalse(_ok(refused), "a doctor collected the balance")
		invoice.reload()
		self.assertGreater(
			flt(invoice.outstanding_amount), 0, "the invoice was settled by the refused payment"
		)


class TestPaymentsDuringTheStay(BoardingPaymentBase):
	def test_a_second_payment_is_recorded_rather_than_swallowed(self):
		"""The whole reason this endpoint exists.

		The deposit path returns the EXISTING Payment Entry when one is present, so before
		this endpoint a second payment silently became no payment at all.
		"""
		stay = self._open_stay(deposit=50000)
		first = frappe.db.get_value("Pet Boarding", stay, "deposit_payment_entry")

		res = record_boarding_payment(boarding_id=stay, amount=35000)
		self.assertTrue(_ok(res), msg=res)
		second = _data(res)["payment_entry"]

		self.assertNotEqual(second, first, "the second payment reused the deposit's entry")
		self.assertEqual(flt(frappe.db.get_value("Payment Entry", second, "paid_amount")), 35000)

	def test_the_running_total_and_balance_follow_the_payments(self):
		stay = self._open_stay(deposit=50000)
		record_boarding_payment(boarding_id=stay, amount=35000)
		res = record_boarding_payment(boarding_id=stay, amount=35000)

		self.assertEqual(flt(_data(res)["total_paid"]), 120000)
		doc = frappe.get_doc("Pet Boarding", stay)
		self.assertEqual(flt(doc.deposit), 120000)
		self.assertEqual(flt(doc.balance), flt(doc.total_cost) - 120000)

	def test_every_payment_is_linked_to_the_stay(self):
		"""`reference_no` is the link check-out reads. Without it the money is invisible."""
		stay = self._open_stay(deposit=50000)
		record_boarding_payment(boarding_id=stay, amount=35000)

		entries = boarding_payment_entries(stay, self._customer(stay))
		self.assertEqual(len(entries), 2)
		self.assertEqual(boarding_total_paid(stay, self._customer(stay)), 85000)

	def test_a_payment_lands_in_a_till_and_is_stamped(self):
		"""Unstamped cash appears in no settlement breakdown, which is how it goes missing."""
		stay = self._open_stay(deposit=50000)
		res = record_boarding_payment(boarding_id=stay, amount=35000)
		pe = _data(res)["payment_entry"]

		debited = frappe.get_all(
			"GL Entry", filters={"voucher_no": pe, "debit": [">", 0]}, pluck="account"
		)
		self.assertTrue(debited, "the payment wrote no debit")
		self.assertTrue(
			frappe.db.get_value("Payment Entry", pe, "custom_pos_profile"),
			"the payment carries no POS Profile and will not appear in any till breakdown",
		)

	def test_a_zero_or_negative_payment_is_refused(self):
		stay = self._open_stay(deposit=50000)
		for bad in (0, -5000):
			res = record_boarding_payment(boarding_id=stay, amount=bad)
			self.assertFalse(_ok(res), msg=f"{bad} was accepted")


class TestCheckoutAppliesEveryPayment(BoardingPaymentBase):
	def test_all_payments_reach_the_invoice_not_just_the_deposit(self):
		stay = self._open_stay(deposit=50000)
		record_boarding_payment(boarding_id=stay, amount=35000)
		record_boarding_payment(boarding_id=stay, amount=35000)

		res = check_out_boarding(boarding_id=stay)
		self.assertTrue(_ok(res), msg=res)
		invoice = frappe.get_doc(
			"Sales Invoice", frappe.db.get_value("Pet Boarding", stay, "sales_invoice")
		)
		self.assertEqual(len(invoice.get("advances") or []), 3)
		self.assertEqual(flt(invoice.total_advance), 120000)

	def test_the_checkout_invoice_is_not_a_pos_invoice(self):
		"""Load-bearing, and the least obvious assertion in this file.

		ERPNext skips advance reconciliation entirely when `is_pos` is set. Stamping this
		invoice POS - or routing it through the till's settle path, which does - silently
		strands every payment taken during the stay.
		"""
		stay = self._open_stay(deposit=50000)
		record_boarding_payment(boarding_id=stay, amount=35000)
		check_out_boarding(boarding_id=stay)

		invoice = frappe.db.get_value(
			"Sales Invoice", frappe.db.get_value("Pet Boarding", stay, "sales_invoice"),
			["is_pos", "docstatus"], as_dict=True,
		)
		self.assertEqual(invoice.is_pos, 0)
		self.assertEqual(invoice.docstatus, 1, "the checkout invoice must not be left a draft")

	def test_the_advances_are_actually_consumed(self):
		"""What an `outstanding` check cannot tell you: whether the deposits were spent."""
		stay = self._open_stay(deposit=50000)
		record_boarding_payment(boarding_id=stay, amount=35000)
		check_out_boarding(boarding_id=stay)

		customer = self._customer(stay)
		invoice = frappe.db.get_value("Pet Boarding", stay, "sales_invoice")
		refs = frappe.get_all(
			"Payment Entry Reference", filters={"reference_name": invoice}, pluck="parent"
		)
		self.assertEqual(len(refs), 2, "the payments were not reconciled against the invoice")
		self.assertEqual(
			self._stay_unallocated(stay), 0,
			"money stayed unallocated - it will show as customer credit against their own bill",
		)

	def test_the_stay_gets_its_own_invoice(self):
		"""Reuse used to append a stay to whatever draft the customer had open.

		ACC-SINV-2026-01888 on production carries three different stays because of it.
		"""
		first = self._open_stay(deposit=0)
		check_out_boarding(boarding_id=first)
		first_invoice = frappe.db.get_value("Pet Boarding", first, "sales_invoice")

		self.room = self._free_room()
		second = self._open_stay(deposit=0)
		check_out_boarding(boarding_id=second)
		second_invoice = frappe.db.get_value("Pet Boarding", second, "sales_invoice")

		self.assertNotEqual(first_invoice, second_invoice)

	def test_a_stay_nobody_paid_for_becomes_a_real_receivable(self):
		"""Being in debt is a correct outcome. Being in debt on an unposted draft is not."""
		stay = self._open_stay(deposit=0)
		before = self._ledger_balance(self._customer(stay))

		res = check_out_boarding(boarding_id=stay)
		invoice = frappe.get_doc(
			"Sales Invoice", frappe.db.get_value("Pet Boarding", stay, "sales_invoice")
		)
		self.assertEqual(invoice.docstatus, 1)
		self.assertTrue(_data(res)["invoice_submitted"])
		self.assertEqual(flt(invoice.outstanding_amount), flt(invoice.grand_total))
		self.assertEqual(
			self._ledger_balance(self._customer(stay)) - before,
			flt(invoice.grand_total),
			"the unpaid stay did not raise the customer's debt by its own value",
		)

	def test_a_fully_prepaid_stay_closes_at_zero(self):
		stay = self._open_stay(deposit=500000)
		check_out_boarding(boarding_id=stay)

		customer = self._customer(stay)
		invoice = frappe.get_doc(
			"Sales Invoice", frappe.db.get_value("Pet Boarding", stay, "sales_invoice")
		)
		self.assertEqual(flt(invoice.outstanding_amount), 0)


class TestCheckoutWarnsAboutTheBalance(BoardingPaymentBase):
	"""Check-out is never blocked by an unpaid balance - it warns instead.

	The animal is going home either way, and holding the stay open for an accounting reason
	keeps a kennel occupied and the record wrong. But whoever ends the stay is often not a
	cashier (`Doctor` is in BOARDING_WRITE_ROLES), so without a warning the guardian walks
	out, the invoice posts as a receivable, and nobody at the counter knows to collect.
	"""

	def test_an_unpaid_balance_is_warned_about_not_blocked(self):
		stay = self._open_stay(deposit=50000)
		res = check_out_boarding(boarding_id=stay)

		self.assertTrue(_ok(res), "check-out was blocked by an unpaid balance")
		warnings = _data(res)["warnings"]
		self.assertEqual(len(warnings), 1)
		self.assertEqual(warnings[0]["code"], "BOARDING_BALANCE_DUE")
		self.assertGreater(flt(warnings[0]["amount"]), 0)
		self.assertEqual(
			flt(warnings[0]["amount"]), flt(_data(res)["invoice_outstanding"])
		)

	def test_the_warning_names_the_door_that_collects(self):
		"""Not "settle it at the till": by now the invoice is submitted and non-POS, and
		`settle_open_invoice` refuses both."""
		stay = self._open_stay(deposit=0)
		res = check_out_boarding(boarding_id=stay)
		self.assertIn("record_boarding_payment", _data(res)["warnings"][0]["collect_via"])

	def test_a_fully_paid_stay_warns_about_nothing(self):
		stay = self._open_stay(deposit=500000)
		res = check_out_boarding(boarding_id=stay)
		self.assertEqual(_data(res)["warnings"], [])


class TestSettlingAfterCheckout(BoardingPaymentBase):
	def _checked_out_stay(self):
		# Taken BEFORE check-in, so the deposit falls inside the measured window. Captured
		# after it, the 50,000 credit sits in the baseline and the stay appears to end
		# 50,000 short of zero.
		customer_before = frappe.db.get_value(
			"Guardian", self.guardian, "customer_id"
		)
		self.opening_balance = self._ledger_balance(customer_before)
		stay = self._open_stay(deposit=50000)
		record_boarding_payment(boarding_id=stay, amount=35000)
		check_out_boarding(boarding_id=stay)
		return stay, frappe.get_doc(
			"Sales Invoice", frappe.db.get_value("Pet Boarding", stay, "sales_invoice")
		)

	def test_the_balance_clears_the_invoice_rather_than_floating(self):
		"""An unreferenced payment leaves the invoice outstanding AND the customer in
		credit for the same money - the exact state eleven customers are in on production."""
		stay, invoice = self._checked_out_stay()
		due = flt(invoice.outstanding_amount)
		self.assertGreater(due, 0)

		res = record_boarding_payment(boarding_id=stay, amount=due)
		self.assertTrue(_ok(res), msg=res)
		self.assertEqual(flt(_data(res)["invoice_outstanding"]), 0)

		self.assertEqual(
			self._ledger_balance(self._customer(stay)) - self.opening_balance,
			0,
			"the stay did not net to zero on the customer's ledger",
		)
		self.assertEqual(self._stay_unallocated(stay), 0)

	def test_collecting_more_than_the_bill_is_refused(self):
		stay, invoice = self._checked_out_stay()
		res = record_boarding_payment(
			boarding_id=stay, amount=flt(invoice.outstanding_amount) + 50000
		)
		self.assertFalse(_ok(res), "the till was allowed to over-collect")

	def test_paying_a_settled_invoice_is_refused(self):
		stay, invoice = self._checked_out_stay()
		record_boarding_payment(boarding_id=stay, amount=flt(invoice.outstanding_amount))

		res = record_boarding_payment(boarding_id=stay, amount=1000)
		self.assertFalse(_ok(res), "a second settlement was accepted on a paid invoice")

	def test_a_cancelled_stay_takes_no_payment(self):
		stay = self._open_stay(deposit=0)
		frappe.db.set_value("Pet Boarding", stay, "record_status", "Cancelled")
		res = record_boarding_payment(boarding_id=stay, amount=10000)
		self.assertFalse(_ok(res))
