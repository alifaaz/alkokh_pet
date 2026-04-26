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
