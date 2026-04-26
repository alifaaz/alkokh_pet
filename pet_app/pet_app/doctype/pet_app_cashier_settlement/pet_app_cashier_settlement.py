from __future__ import annotations

import frappe
from frappe.model.document import Document
from frappe.utils import cint, flt


ACCOUNTING_ROLES = {"System Manager", "Accounts Manager", "Accounts User"}


class PetAppCashierSettlement(Document):
	def validate(self):
		self._validate_cashier_access()
		self._validate_accounts()
		self._set_difference()
		self._set_status()
		self._validate_amounts()

	def before_submit(self):
		self.status = "Submitted"

	def before_cancel(self):
		self.status = "Cancelled"

	def _set_difference(self):
		self.difference_amount = flt(self.counted_cash) - flt(self.expected_cash)
		if self.difference_amount < 0:
			self.difference_type = "Short"
		elif self.difference_amount > 0:
			self.difference_type = "Over"
		else:
			self.difference_type = "Matched"

	def _set_status(self):
		if self.docstatus == 1:
			self.status = "Submitted"
		elif self.docstatus == 2:
			self.status = "Cancelled"
		else:
			self.status = "Draft"

	def _validate_amounts(self):
		for fieldname, label in (
			("counted_cash", "Counted Cash"),
			("transfer_amount", "Transfer Amount"),
		):
			if flt(self.get(fieldname)) < 0:
				frappe.throw(frappe._("{0} cannot be negative.").format(frappe._(label)))
		if flt(self.transfer_amount) > flt(self.counted_cash):
			frappe.throw(frappe._("Transfer Amount cannot exceed Counted Cash."))

	def _validate_cashier_access(self):
		user = frappe.session.user
		if user == "Administrator" or bool(set(frappe.get_roles(user) or []) & ACCOUNTING_ROLES):
			return
		if not frappe.db.exists(
			"POS Profile User",
			{"parenttype": "POS Profile", "parent": self.cashier_profile, "user": user},
		):
			frappe.throw(
				frappe._("You are not assigned to cashier profile {0}.").format(
					frappe.bold(self.cashier_profile)
				),
				frappe.PermissionError,
			)

	def _validate_accounts(self):
		company = self.company
		self._validate_account(self.cash_account, company, "Cashier Cash Account")
		self._validate_account(self.treasury_cash_account, company, "Treasury Cash Account")

	def _validate_account(self, account, company, label):
		if not account:
			frappe.throw(frappe._("{0} is required.").format(frappe._(label)))
		fields = ["name", "company", "account_type", "is_group"]
		if frappe.get_meta("Account").has_field("disabled"):
			fields.append("disabled")
		row = frappe.db.get_value("Account", account, fields, as_dict=True)
		if not row:
			frappe.throw(frappe._("{0} {1} does not exist.").format(frappe._(label), frappe.bold(account)))
		if row.company != company:
			frappe.throw(
				frappe._("{0} {1} does not belong to Company {2}.").format(
					frappe._(label), frappe.bold(account), frappe.bold(company)
				)
			)
		if row.account_type != "Cash":
			frappe.throw(frappe._("{0} {1} must be a Cash account.").format(frappe._(label), frappe.bold(account)))
		if cint(row.is_group):
			frappe.throw(frappe._("{0} {1} must be a ledger account.").format(frappe._(label), frappe.bold(account)))
		if cint(row.get("disabled")):
			frappe.throw(frappe._("{0} {1} is disabled.").format(frappe._(label), frappe.bold(account)))
