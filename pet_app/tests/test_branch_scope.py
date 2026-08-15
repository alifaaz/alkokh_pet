"""Clinic (branch) separation tests.

These lock in the *decisions*, not just the code. Several assert that something stays
**global** -- if a later change starts scoping pet history, diagnostics or billing, the
failing test names the decision rather than looking like a bug.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from pet_app.utils.branch import (
	apply_branch_filter,
	assert_can_write_to_branch,
	get_current_branch,
	get_user_branches,
	user_sees_all_branches,
)


SECOND_BRANCH = "_Test Clinic B"


class TestBranchScope(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.default_branch = frappe.get_all(
			"Branch", pluck="name", order_by="creation asc", limit_page_length=1
		)
		cls.default_branch = cls.default_branch[0] if cls.default_branch else None

	def tearDown(self):
		frappe.set_user("Administrator")

	# -- resolution ----------------------------------------------------------------

	def test_administrator_is_unrestricted(self):
		"""Administrator must see every branch -- the core 'only admin sees both' rule."""
		self.assertTrue(user_sees_all_branches("Administrator"))

	def test_unrestricted_user_gets_no_filter(self):
		filters = {"docstatus": ["<", 2]}
		apply_branch_filter(filters, "Vet Visit", "Administrator")
		self.assertNotIn("branch", filters)

	def test_restricted_user_gets_filter(self):
		user = _a_restricted_user()
		if not user:
			self.skipTest("no branch-restricted user on this site")
		filters = {"docstatus": ["<", 2]}
		apply_branch_filter(filters, "Vet Visit", user)
		self.assertEqual(filters.get("branch"), get_current_branch(user))

	def test_filter_is_noop_for_unscoped_doctype(self):
		"""Lab has no branch field by decision; the helper must not invent one."""
		user = _a_restricted_user()
		if not user:
			self.skipTest("no branch-restricted user on this site")
		filters = {}
		apply_branch_filter(filters, "Lab", user)
		self.assertNotIn("branch", filters)

	# -- write guard ---------------------------------------------------------------

	def test_restricted_user_cannot_write_to_another_branch(self):
		user = _a_restricted_user()
		if not user:
			self.skipTest("no branch-restricted user on this site")
		_ensure_branch(SECOND_BRANCH)
		if SECOND_BRANCH in get_user_branches(user):
			self.skipTest("test branch is assigned to the sample user")
		with self.assertRaises(frappe.PermissionError):
			assert_can_write_to_branch(SECOND_BRANCH, user)

	def test_restricted_user_can_write_to_own_branch(self):
		user = _a_restricted_user()
		if not user:
			self.skipTest("no branch-restricted user on this site")
		own = get_current_branch(user)
		assert_can_write_to_branch(own, user)  # must not raise

	def test_unrestricted_user_can_write_to_any_branch(self):
		_ensure_branch(SECOND_BRANCH)
		assert_can_write_to_branch(SECOND_BRANCH, "Administrator")  # must not raise

	def test_unknown_branch_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			assert_can_write_to_branch("_no_such_branch_", "Administrator")

	# -- deliberate non-scoping ----------------------------------------------------

	def test_diagnostics_are_not_branch_scoped(self):
		"""Lab/Imaging are shared across all clinics by decision.

		If this fails, someone added a branch field to diagnostics. That is a product
		decision reversal, not a bug fix -- see docs/CLINIC_BRANCH_SEPARATION.md.
		"""
		for doctype in ("Lab", "Imaging"):
			self.assertFalse(
				frappe.get_meta(doctype).has_field("branch"),
				f"{doctype} must stay global -- all clinics share diagnostics",
			)

	def test_patient_record_is_not_branch_scoped(self):
		"""Pet history follows the animal, not the clinic that produced it."""
		for doctype in ("Pet", "Guardian", "Pet Medical Profile", "Pet Care Episode"):
			if not frappe.db.exists("DocType", doctype):
				continue
			self.assertFalse(
				frappe.get_meta(doctype).has_field("branch"),
				f"{doctype} must stay global -- pets and guardians are shared",
			)

	def test_sales_invoice_list_is_branch_scoped(self):
		"""An invoice belongs to the clinic that raised it."""
		user = _a_restricted_user()
		if not user:
			self.skipTest("no branch-restricted user on this site")
		filters = {}
		apply_branch_filter(filters, "Sales Invoice", user)
		self.assertEqual(filters.get("branch"), get_current_branch(user))

	def test_boarding_is_not_branch_scoped(self):
		"""One boarding facility serves every clinic.

		The Service Room pool is shared and has no branch, so scoping the booking
		would let one clinic reserve a room another clinic could neither see nor
		check out. Accountability lives on the optional `practitioner` field instead.
		"""
		from pet_app.utils.branch import SCOPED_DOCTYPES

		self.assertNotIn("Pet Boarding", SCOPED_DOCTYPES)
		filters = {}
		apply_branch_filter(filters, "Pet Boarding", _a_restricted_user() or "Administrator")
		self.assertNotIn("branch", filters)

	def test_boarding_practitioner_is_optional_and_not_auto_filled(self):
		"""`practitioner` is frontend-owned: the backend must never infer it."""
		if not frappe.db.has_column("Pet Boarding", "practitioner"):
			self.skipTest("practitioner field not present")
		meta_field = frappe.get_meta("Pet Boarding").get_field("practitioner")
		self.assertFalse(meta_field.reqd, "practitioner must stay optional")

	def test_customer_and_ledger_stay_global(self):
		"""The money itself is never scoped.

		One Company means one receivable ledger. Scoping any of these would make a
		customer look settled at one clinic while owing money at another, and would
		break collecting a Clinic A debt at Clinic B.
		"""
		user = _a_restricted_user() or "Administrator"
		for doctype in ("Customer", "Payment Entry", "GL Entry", "Journal Entry"):
			filters = {}
			apply_branch_filter(filters, doctype, user)
			self.assertNotIn(
				"branch", filters, f"{doctype} must stay global -- balances are company-wide"
			)

	# -- backfill integrity --------------------------------------------------------

	def test_no_scoped_record_has_a_null_branch(self):
		"""A NULL branch is visible to *every* clinic, so it is a leak, not a default."""
		from pet_app.utils.branch import SCOPED_DOCTYPES

		for doctype in sorted(SCOPED_DOCTYPES):
			if not frappe.db.exists("DocType", doctype) or not frappe.db.has_column(doctype, "branch"):
				continue
			orphans = frappe.db.sql(
				f"select count(*) from `tab{doctype}` where ifnull(branch,'')=''"
			)[0][0]
			self.assertEqual(orphans, 0, f"{doctype} has {orphans} rows with no branch")


def _a_restricted_user() -> str | None:
	rows = frappe.get_all(
		"User Permission",
		filters={"allow": "Branch"},
		fields=["user"],
		limit_page_length=0,
	)
	for row in rows:
		if row.user and row.user != "Administrator" and not user_sees_all_branches(row.user):
			return row.user
	return None


def _ensure_branch(name: str):
	if not frappe.db.exists("Branch", name):
		frappe.get_doc({"doctype": "Branch", "branch": name}).insert(ignore_permissions=True)
