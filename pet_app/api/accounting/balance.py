"""Customer account balance, aggregated in the database.

Called on every customer selection at the till, between barcode scans, so it must
answer from SUMs rather than hand rows to the client to reduce.

Sourced from the **Payment Ledger Entry** against the party's receivable accounts, not
from Sales Invoice + Payment Entry and not from GL Entry. An invoices-and-payments query
cannot see a manual write-off, an opening balance, or a Journal Entry adjustment, and this
site has six receivable accounts - including a separate one for POS credit customers - so a
query pinned to the company's default receivable account would under-report by
construction. The Payment Ledger keeps both of those properties: it carries every party
posting, Journal Entries included, across every receivable account.

It is used INSTEAD of GL Entry for one reason: it is the only place ERPNext records which
invoice a payment settled. When an advance is reconciled, `reconcile_against_document`
(erpnext/accounts/utils.py) calls `create_payment_ledger_entry` and does NOT rewrite
GL Entry - so the payment's GL row keeps an empty `against_voucher` and the invoice's debit
stands alone. Reading GL Entry therefore reported a settled invoice as an open debt AND
counted the same money again as credit. Measured on this site: three customers, 125,000 of
debt that did not exist, four "open" invoices that were paid in full.

`receivable` and `credit` are reported separately and both non-negative. A customer
may owe on one invoice while holding credit from a return; a single signed number
hides one of the two, and the cashier needs both to have the conversation.

Drafts are counted but never included in `receivable`, `overdue` or `net_balance`.
Nothing is owed until a document is submitted.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr, flt, now_datetime, nowdate

from pet_app.api.accounting.cashier import (
	_has_accounting_record_permission,
	_is_accounting_user,
)
from pet_app.api.response import standardize_response
from pet_app.utils.api_response import raise_api_error
from pet_app.utils.invoice_reuse import resolve_company

# IQD carries no minor unit, but the ledger stores floats. Anything under this is
# rounding noise, not an open item, and must not be counted as one - otherwise a
# fully-settled customer shows an "open invoice" worth 0.0000001.
SETTLED_EPSILON = 0.005


def _require_balance_reader():
	"""A cashier reads balances; they do not need Accounts Manager to do it."""
	if _is_accounting_user():
		return
	if _has_accounting_record_permission(("Sales Invoice", "read"), ("Customer", "read")):
		return
	frappe.throw(_("Not authorized to read customer balances."), frappe.PermissionError)


def _receivable_accounts(company: str) -> list[str]:
	"""Every receivable account on the company, not just the default one.

	This site has six. `411110 - عملاء الكاشير / POS آجل` holds POS credit sales and is
	not the company default, so a single-account query would report a customer who owes
	only for POS-due sales as owing nothing - which is exactly the case this endpoint
	exists to surface.
	"""
	return frappe.get_all(
		"Account",
		filters={"company": company, "account_type": "Receivable"},
		pluck="name",
		ignore_permissions=True,
	)


def _empty_totals() -> dict:
	return {
		"receivable": 0.0,
		"credit": 0.0,
		"overdue": 0.0,
		"oldest_due_date": None,
		"open_invoice_count": 0,
	}


def _ledger_totals(customer: str, company: str, accounts: list[str]) -> dict:
	"""One round trip. Nets each open item, then sums the nets by sign.

	Grouping by `against_voucher_no` is what makes returns, allocated payments and
	Journal Entries land on the invoice they settle instead of standing alone. A
	receipt with nothing allocated has no `against_voucher_no` of its own, so it groups
	under its own `voucher_no`, nets negative, and counts as credit - which is what
	"unapplied receipts count toward credit" means in ledger terms.

	`delinked` is this table's `is_cancelled`: cancelling a voucher marks its rows rather
	than deleting them, so excluding it is what keeps a cancelled invoice out of the total.
	There is no docstatus to filter - the Payment Ledger only ever holds submitted postings.

	The SELECT takes no locks: it is a plain read, so it cannot block a cashier or a
	clinic submitting an invoice at the same moment.
	"""
	if not accounts:
		return _empty_totals()

	rows = frappe.db.sql(
		"""
		SELECT
			SUM(CASE WHEN t.net > %(eps)s THEN t.net ELSE 0 END) AS receivable,
			SUM(CASE WHEN t.net < -%(eps)s THEN -t.net ELSE 0 END) AS credit,
			SUM(CASE
				WHEN t.net > %(eps)s AND t.due_date IS NOT NULL AND t.due_date < %(today)s
				THEN t.net ELSE 0 END) AS overdue,
			MIN(CASE
				WHEN t.net > %(eps)s AND t.due_date IS NOT NULL AND t.due_date < %(today)s
				THEN t.due_date END) AS oldest_due_date,
			SUM(CASE WHEN t.net > %(eps)s THEN 1 ELSE 0 END) AS open_invoice_count
		FROM (
			SELECT
				COALESCE(NULLIF(ple.against_voucher_no, ''), ple.voucher_no) AS grp,
				SUM(ple.amount) AS net,
				MAX(ple.due_date) AS due_date
			FROM `tabPayment Ledger Entry` ple
			WHERE ple.party_type = 'Customer'
				AND ple.party = %(customer)s
				AND ple.company = %(company)s
				AND IFNULL(ple.delinked, 0) = 0
				AND ple.account IN %(accounts)s
			GROUP BY grp
		) t
		""",
		{
			"customer": customer,
			"company": company,
			"accounts": accounts,
			"today": nowdate(),
			"eps": SETTLED_EPSILON,
		},
		as_dict=True,
	)
	row = rows[0] if rows else None
	if not row:
		return _empty_totals()

	return {
		"receivable": flt(row.get("receivable")),
		"credit": flt(row.get("credit")),
		"overdue": flt(row.get("overdue")),
		"oldest_due_date": cstr(row.get("oldest_due_date")) or None,
		"open_invoice_count": int(row.get("open_invoice_count") or 0),
	}


def _draft_totals(customer: str, company: str) -> dict:
	"""The same scope `findOpenDraftSalesInvoice` uses, so the two surfaces agree.

	Deliberately NOT added to `receivable`. A draft is not a debt, and a cashier sent
	to collect on one would be chasing money that was never invoiced.
	"""
	rows = frappe.db.sql(
		"""
		SELECT COUNT(*) AS n, SUM(si.grand_total) AS total
		FROM `tabSales Invoice` si
		WHERE si.customer = %(customer)s
			AND si.company = %(company)s
			AND si.docstatus = 0
			AND IFNULL(si.is_return, 0) = 0
			AND IFNULL(si.is_pos, 0) = 0
		""",
		{"customer": customer, "company": company},
		as_dict=True,
	)
	row = rows[0] if rows else None
	return {
		"draft_invoice_count": int((row or {}).get("n") or 0),
		"draft_invoice_total": flt((row or {}).get("total")),
	}


@frappe.whitelist()
@standardize_response
def get_customer_balance(customer=None, company=None, branch=None):
	"""Company-wide receivable position for one customer.

	`branch` is accepted and ignored for the money, and `branch_scoped` is always
	False. That is a decision, not an omission: a debt is owed to the company, not to
	a clinic. This site runs two branches (`main`, `hotel`) over ONE company with ONE
	shared set of receivable accounts, and the branch dimension is stamped only by
	`stamp_branch_on_insert`, a before_insert hook on Sales Invoice - so a Payment
	Entry or Journal Entry settling a `main` invoice may carry `hotel` or no branch at
	all. Netting per branch would therefore split an invoice from the payment that
	cleared it and report both a phantom debt in one branch and a phantom credit in
	the other. Worse in the direction that matters: a cashier in `hotel` would be
	shown "nothing owed" for a debt raised in `main` and would hand over goods on
	credit to a customer already behind.
	"""
	_require_balance_reader()

	customer = cstr(customer).strip()
	if not customer:
		raise_api_error(_("Customer is required."), code="CUSTOMER_NOT_FOUND")
	if not frappe.db.exists("Customer", customer):
		raise_api_error(
			_("Customer {0} was not found.").format(customer), code="CUSTOMER_NOT_FOUND"
		)

	company = resolve_company(company)
	currency = (
		frappe.db.get_value("Customer", customer, "default_currency")
		or frappe.db.get_value("Company", company, "default_currency")
	)

	totals = _ledger_totals(customer, company, _receivable_accounts(company))
	drafts = _draft_totals(customer, company)

	return {
		"customer": customer,
		"company": company,
		"currency": currency,
		"as_of": now_datetime().strftime("%Y-%m-%d %H:%M:%S"),
		"receivable": totals["receivable"],
		"credit": totals["credit"],
		"net_balance": flt(totals["receivable"] - totals["credit"]),
		"overdue": totals["overdue"],
		"oldest_due_date": totals["oldest_due_date"],
		"open_invoice_count": totals["open_invoice_count"],
		"draft_invoice_count": drafts["draft_invoice_count"],
		"draft_invoice_total": drafts["draft_invoice_total"],
		"branch_scoped": False,
	}
