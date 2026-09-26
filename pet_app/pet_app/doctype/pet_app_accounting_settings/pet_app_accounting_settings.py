# Copyright (c) 2026, solvers and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class PetAppAccountingSettings(Document):
	def validate(self):
		if self.default_company and not frappe.db.exists("Company", self.default_company):
			frappe.throw(_("Default Company {0} does not exist.").format(frappe.bold(self.default_company)))
		if self.default_cash_mode_of_payment and not frappe.db.exists(
			"Mode of Payment", self.default_cash_mode_of_payment
		):
			frappe.throw(
				_("Default Cash Mode of Payment {0} does not exist.").format(
					frappe.bold(self.default_cash_mode_of_payment)
				)
			)

		self._validate_account("treasury_cash_account", required=False, account_type="Cash")
		self._validate_expense_categories()

	def _validate_expense_categories(self):
		"""Guard the rows the till will book real money against.

		Validated here as well as in the API layer because a settings single is editable
		from the desk too, and a category pointing at a group account or a non-expense
		account does not fail at configuration time - it fails later, at the till, on a
		cashier who cannot do anything about it.
		"""
		seen: set[str] = set()
		for row in self.get("expense_categories") or []:
			key = (row.key or "").strip()
			if not key:
				frappe.throw(_("Expense category in row {0} needs a key.").format(row.idx))
			if key in seen:
				frappe.throw(
					_("Expense category key {0} is used more than once.").format(frappe.bold(key))
				)
			seen.add(key)
			row.key = key

			if row.branch and not frappe.db.exists("Branch", row.branch):
				frappe.throw(
					_("Branch {0} in expense category {1} does not exist.").format(
						frappe.bold(row.branch), frappe.bold(key)
					)
				)

			if not row.account:
				continue

			account = frappe.db.get_value(
				"Account",
				row.account,
				["name", "company", "is_group", "root_type", "disabled"],
				as_dict=True,
			)
			if not account:
				frappe.throw(
					_("Account {0} in expense category {1} does not exist.").format(
						frappe.bold(row.account), frappe.bold(key)
					)
				)
			if account.is_group:
				frappe.throw(
					_("Account {0} in expense category {1} must be a ledger account.").format(
						frappe.bold(row.account), frappe.bold(key)
					)
				)
			if account.root_type != "Expense":
				frappe.throw(
					_("Account {0} in expense category {1} must have root type Expense.").format(
						frappe.bold(row.account), frappe.bold(key)
					)
				)
			company = row.company or self.default_company
			if company and account.company != company:
				frappe.throw(
					_("Account {0} in expense category {1} does not belong to Company {2}.").format(
						frappe.bold(row.account), frappe.bold(key), frappe.bold(company)
					)
				)

	def _validate_account(self, fieldname: str, required: bool = False, account_type: str | None = None):
		account = self.get(fieldname)
		if required and not account:
			frappe.throw(_("{0} is required.").format(_(self.meta.get_label(fieldname))))
		if not account:
			return

		row = frappe.db.get_value(
			"Account",
			account,
			["name", "company", "is_group", "account_type"],
			as_dict=True,
		)
		if not row:
			frappe.throw(_("Account {0} does not exist.").format(frappe.bold(account)))
		if row.is_group:
			frappe.throw(_("Account {0} must be a ledger account.").format(frappe.bold(account)))
		if self.default_company and row.company != self.default_company:
			frappe.throw(
				_("Account {0} does not belong to Company {1}.").format(
					frappe.bold(account), frappe.bold(self.default_company)
				)
			)
		if account_type and row.account_type != account_type:
			frappe.throw(
				_("Account {0} must be an account of type {1}.").format(
					frappe.bold(account), frappe.bold(account_type)
				)
			)
