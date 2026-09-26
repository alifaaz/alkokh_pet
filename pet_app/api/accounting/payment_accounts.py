"""Which GL accounts a payment should post to, answered server-side.

The till already resolves the common case on its own - cash side from the POS profile or
`Mode of Payment Account`, party side by filtering Account on account_type
Receivable/Payable. This endpoint exists for the two cases it cannot detect from there:

* a company with more than one Receivable account, where the client declines to guess and
  hands the cashier a picker;
* a per-party `Party Account` override, which the client never fetches - so any heuristic
  it applied would silently bypass a deliberate accounting decision.

Both are already answered correctly by `erpnext.accounts.party.get_party_account`, which
is not whitelisted for this frontend. That is the entire reason this module exists, so it
delegates rather than reimplementing the precedence.

Returns nothing rather than a guess. A field left out of the response leaves the cashier a
filtered picker; a confidently wrong one posts money silently. Every key here is omitted -
not set to null - when it does not resolve.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr

from erpnext.accounts.doctype.journal_entry.journal_entry import get_default_bank_cash_account
from erpnext.accounts.party import get_party_account
from pet_app.api.accounting.cashier import (
	_get_authorized_profile,
	_has_accounting_record_permission,
	_is_accounting_user,
	_resolve_payment_account,
)
from pet_app.api.response import standardize_response


PARTY_GROUP_DOCTYPE = {"Customer": "Customer Group", "Supplier": "Supplier Group"}
COMPANY_DEFAULT_FIELD = {"Customer": "default_receivable_account", "Supplier": "default_payable_account"}


def _require_payment_account_reader():
	"""A cashier resolves accounts; they do not need Accounts Manager to do it.

	Mirrors balance._require_balance_reader. Nothing here moves money or reads a balance -
	it answers "which account would this post to" - so the gate is the same one that lets
	someone take a payment at all.
	"""
	if _is_accounting_user():
		return
	if _has_accounting_record_permission(
		("Payment Entry", "create"), ("Sales Invoice", "read"), ("Customer", "read")
	):
		return
	frappe.throw(_("Not authorized to resolve payment accounts."), frappe.PermissionError)


def _party_account_source(party_type: str, party: str, company: str) -> str | None:
	"""Where get_party_account's answer came from.

	It returns the account but not which of its three fallbacks produced it, and the client
	needs to know: an override is a decision someone made, a company default is just a
	default. Probed in the same order erpnext.accounts.party.get_party_account uses, so the
	label always describes the value actually returned.
	"""
	if frappe.db.get_value(
		"Party Account", {"parenttype": party_type, "parent": party, "company": company}, "account"
	):
		return "party_account"

	group_doctype = PARTY_GROUP_DOCTYPE.get(party_type)
	if group_doctype:
		group = frappe.db.get_value(party_type, party, frappe.scrub(group_doctype))
		if group and frappe.db.get_value(
			"Party Account",
			{"parenttype": group_doctype, "parent": group, "company": company},
			"account",
		):
			return "party_group_account"

	default_field = COMPANY_DEFAULT_FIELD.get(party_type)
	if default_field and frappe.db.get_value("Company", company, default_field):
		return "company_default"
	return None


def _resolve_party_side(party_type: str | None, party: str | None, company: str):
	if not party_type or not party:
		return None, None
	if not frappe.db.exists(party_type, party):
		return None, None
	try:
		account = get_party_account(party_type, party, company)
	except Exception:
		# get_party_account throws on a missing company default with no override. That is
		# an unresolved party side, not a request-level failure - the cashier still gets a
		# picker.
		return None, None
	if not account:
		return None, None
	return account, _party_account_source(party_type, party, company)


def _resolve_cash_side(company: str, mode_of_payment: str | None, pos_profile: str | None):
	"""The till's cash account, or the mode of payment's, or an unambiguous company default.

	A pos_profile is not a hint: a payment and a sale rung up on the same till have to agree
	about where the cash landed, so the profile's own account wins outright and a failure to
	resolve it is reported rather than papered over with a company default.
	"""
	if pos_profile:
		profile = _get_authorized_profile(pos_profile)
		if company and profile.company != company:
			frappe.throw(
				_("Cashier profile {0} belongs to Company {1}.").format(
					frappe.bold(profile.name), frappe.bold(profile.company)
				)
			)
		try:
			return _resolve_payment_account(profile.company, mode_of_payment, profile), "pos_profile"
		except Exception:
			# The profile has no usable account for this mode of payment. Omit the cash side
			# and keep whatever the party side resolved - failing the whole call would take
			# the answer the cashier came for along with the one we could not give.
			#
			# Explicitly no fallback to the mode of payment default or the company default:
			# a profile was named, so its account is the authoritative one, and quietly
			# substituting another is how a payment and a sale from the same till end up
			# disagreeing about where the money went.
			return None, None

	if mode_of_payment:
		# Read the child table directly. ERPNext's own helper throws when a mode of payment
		# has no default account, and an omission is the contract here.
		account = frappe.db.get_value(
			"Mode of Payment Account",
			{"parent": mode_of_payment, "company": company},
			"default_account",
		)
		if account:
			return account, "mode_of_payment"

	for account_type in ("Cash", "Bank"):
		# get_default_bank_cash_account already implements exactly the rule this endpoint
		# is held to: it answers only from the company default or a single candidate, and
		# returns an empty dict rather than picking one of several.
		resolved = get_default_bank_cash_account(company, account_type, fetch_balance=False)
		if resolved.get("account"):
			return resolved.get("account"), "company_default_cash"
	return None, None


@frappe.whitelist()
@standardize_response
def resolve_payment_accounts(
	payment_type,
	company,
	party_type=None,
	party=None,
	mode_of_payment=None,
	pos_profile=None,
):
	_require_payment_account_reader()

	payment_type = cstr(payment_type).strip()
	if payment_type not in ("Receive", "Pay"):
		frappe.throw(_("Payment Type must be Receive or Pay."))

	company = cstr(company).strip()
	if not company or not frappe.db.exists("Company", company):
		frappe.throw(_("Company {0} does not exist.").format(frappe.bold(company)))

	party_type = cstr(party_type).strip() or None
	party = cstr(party).strip() or None
	mode_of_payment = cstr(mode_of_payment).strip() or None
	pos_profile = cstr(pos_profile).strip() or None

	if mode_of_payment and not frappe.db.exists("Mode of Payment", mode_of_payment):
		frappe.throw(_("Mode of Payment {0} does not exist.").format(frappe.bold(mode_of_payment)))

	party_account, party_source = _resolve_party_side(party_type, party, company)
	cash_account, cash_source = _resolve_cash_side(company, mode_of_payment, pos_profile)

	if payment_type == "Receive":
		paid_from, paid_to = party_account, cash_account
	else:
		paid_from, paid_to = cash_account, party_account

	# Built by assignment rather than by dict literal + prune, so an unresolved side is
	# absent by construction. A null here would read to the client as "resolved to nothing".
	result: dict = {}
	if paid_from:
		result["paid_from"] = paid_from
	if paid_to:
		result["paid_to"] = paid_to
	if party_source:
		result["source"] = party_source
	if cash_source:
		result["cash_source"] = cash_source
	return result
