from __future__ import annotations

import json
from collections.abc import Iterable

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, getdate, nowdate

from erpnext.accounts.party import get_party_account
from pet_app.api.accounting.expenses import (
	categories_by_account as _categories_by_account,
	resolve_category,
)
from pet_app.api.permissions import (
	filter_restricted_values,
	require_doctype_permission,
	require_restriction_value,
)
from pet_app.api.response import standardize_response
from pet_app.utils.branch import assert_can_write_to_branch


ACCOUNTING_ROLES = {"System Manager", "Accounts Manager", "Accounts User", "Accounting", "POS"}
ACCOUNTING_PAGE_ROLES = {"Cashiers Page", "POS Page"}

# Standing at a till is the qualification for booking money out of it. Kept separate from
# ACCOUNTING_ROLES because those grant far more than a payout, and the point of this
# endpoint is that a cashier needs nothing beyond their own profile.
CASHIER_EXPENSE_ROLES = {"POS Cashier", "POS Admin", "Cashiers Page", "POS Page"}


def _current_user() -> str:
	return frappe.session.user


def _user_roles(user: str | None = None) -> set[str]:
	return set(frappe.get_roles(user or _current_user()) or [])


def _has_doctype_permission(doctype: str, ptype: str, user: str | None = None) -> bool:
	try:
		return bool(frappe.has_permission(doctype, ptype=ptype, user=user or _current_user()))
	except Exception:
		return False


def _has_legacy_accounting_role(user: str | None = None) -> bool:
	user = user or _current_user()
	return user == "Administrator" or bool(_user_roles(user) & ACCOUNTING_ROLES)


def _is_accounting_user(user: str | None = None) -> bool:
	user = user or _current_user()
	return _has_legacy_accounting_role(user) or bool(_user_roles(user) & ACCOUNTING_PAGE_ROLES)


def _has_accounting_record_permission(*permissions: tuple[str, str], user: str | None = None) -> bool:
	user = user or _current_user()
	if _has_legacy_accounting_role(user):
		return True
	return any(_has_doctype_permission(doctype, ptype, user) for doctype, ptype in permissions)


def _require_accounting_record_permission(*permissions: tuple[str, str]):
	if not _has_accounting_record_permission(*permissions):
		frappe.throw(_("Not authorized."), frappe.PermissionError)


def _require_accounting_user():
	if not _is_accounting_user():
		frappe.throw(_("Not authorized."), frappe.PermissionError)


def _require_cashier_expense_permission():
	"""Deliberately not a Journal Entry permission check.

	Recording a till payout posts a submitted Journal Entry, and demanding Journal Entry
	create+submit of the caller would mean granting those rights to every cashier - which
	is exactly the thing this endpoint exists to avoid. Same trade-off, and same reasoning,
	as driver._require_journal_entry_create_submit.

	Safe to elevate because the elevation cannot widen scope. The voucher credits only the
	cash account of the caller's own POS profile - checked against the profile, never taken
	from the client - and debits one account that has already been validated as a leaf
	expense account of the same company. Neither side can be pointed at anything else, so
	holding the till is the only authority the entry needs.
	"""
	if _is_accounting_user():
		return
	if _user_roles() & CASHIER_EXPENSE_ROLES:
		return
	frappe.throw(_("Not authorized to record cashier expenses."), frappe.PermissionError)


def _coerce_dict(value) -> dict:
	if value is None:
		return {}
	if isinstance(value, str):
		value = json.loads(value) if value else {}
	if hasattr(value, "as_dict"):
		value = value.as_dict()
	return dict(value or {})


def _coerce_list(value) -> list:
	if value is None:
		return []
	if isinstance(value, str):
		value = json.loads(value) if value else []
	if isinstance(value, dict):
		return [value]
	if isinstance(value, Iterable):
		return list(value)
	return []


def _has_field(doctype: str, fieldname: str) -> bool:
	try:
		return bool(frappe.get_meta(doctype).has_field(fieldname))
	except Exception:
		return False


def _set_if_field(doc, fieldname: str, value):
	if doc.meta.has_field(fieldname):
		doc.set(fieldname, value)


def _get_settings_doc():
	if not frappe.db.exists("DocType", "Pet App Accounting Settings"):
		return frappe._dict()
	return frappe.get_single("Pet App Accounting Settings")


def _settings_payload(settings=None) -> dict:
	settings = settings or _get_settings_doc()
	return {
		"treasury_cash_account": settings.get("treasury_cash_account"),
		"default_company": settings.get("default_company"),
		"default_cash_mode_of_payment": settings.get("default_cash_mode_of_payment"),
	}


def _get_first_company() -> str | None:
	row = frappe.get_all("Company", fields=["name"], limit=1, order_by="creation asc")
	return row[0].name if row else None


def _get_default_company(preferred: str | None = None) -> str | None:
	if preferred:
		return preferred
	settings = _get_settings_doc()
	return (
		settings.get("default_company")
		or frappe.defaults.get_user_default("Company")
		or frappe.defaults.get_global_default("company")
		or _get_first_company()
	)


def _default_cash_mode_of_payment(profile=None) -> str | None:
	settings = _get_settings_doc()
	if settings.get("default_cash_mode_of_payment"):
		return settings.default_cash_mode_of_payment

	if profile:
		for row in profile.get("payments") or []:
			if row.default and _is_cash_mode(row.mode_of_payment):
				return row.mode_of_payment
		for row in profile.get("payments") or []:
			if _is_cash_mode(row.mode_of_payment):
				return row.mode_of_payment

	if frappe.db.exists("Mode of Payment", "Cash"):
		return "Cash"
	return None


def _is_cash_mode(mode_of_payment: str | None) -> bool:
	if not mode_of_payment:
		return True
	settings = _get_settings_doc()
	if settings.get("default_cash_mode_of_payment") == mode_of_payment:
		return True
	mode_type = frappe.db.get_value("Mode of Payment", mode_of_payment, "type")
	return mode_type == "Cash" or cstr(mode_of_payment).strip().lower() == "cash"


def _validate_account(
	account: str | None,
	*,
	company: str | None = None,
	account_type: str | None = None,
	label: str = "Account",
) -> frappe._dict:
	if not account:
		frappe.throw(_("{0} is required.").format(_(label)))

	fields = ["name", "account_name", "company", "account_type", "account_currency", "is_group"]
	if _has_field("Account", "disabled"):
		fields.append("disabled")

	row = frappe.db.get_value(
		"Account",
		account,
		fields,
		as_dict=True,
	)
	if not row:
		frappe.throw(_("{0} {1} does not exist.").format(_(label), frappe.bold(account)))
	if cint(row.get("disabled")):
		frappe.throw(_("{0} {1} is disabled.").format(_(label), frappe.bold(account)))
	if row.is_group:
		frappe.throw(_("{0} {1} must be a ledger account.").format(_(label), frappe.bold(account)))
	if company and row.company != company:
		frappe.throw(
			_("{0} {1} does not belong to Company {2}.").format(
				_(label), frappe.bold(account), frappe.bold(company)
			)
		)
	if account_type and row.account_type != account_type:
		frappe.throw(
			_("{0} {1} must be an account of type {2}.").format(
				_(label), frappe.bold(account), frappe.bold(account_type)
			)
		)
	return frappe._dict(row)


def _get_account_balance(account: str | None, posting_date: str | None = None) -> float:
	if not account:
		return 0.0
	conditions = ["account = %s", "is_cancelled = 0"]
	values = [account]
	if posting_date:
		conditions.append("posting_date <= %s")
		values.append(getdate(posting_date))

	return flt(
		frappe.db.sql(
			f"""
			SELECT COALESCE(SUM(debit) - SUM(credit), 0)
			FROM `tabGL Entry`
			WHERE {" AND ".join(conditions)}
			""",
			values,
		)[0][0]
	)


def _get_cash_activity(account: str, posting_date: str | None = None) -> frappe._dict:
	posting_date = getdate(posting_date or nowdate())
	row = frappe.db.sql(
		"""
		SELECT COALESCE(SUM(debit), 0) AS incoming,
		       COALESCE(SUM(credit), 0) AS outgoing
		FROM `tabGL Entry`
		WHERE account = %s
		  AND posting_date = %s
		  AND is_cancelled = 0
		""",
		(account, posting_date),
		as_dict=True,
	)[0]

	current_balance = _get_account_balance(account, posting_date)
	return frappe._dict(
		{
			"posting_date": posting_date,
			"current_balance": flt(current_balance),
			"today_incoming": flt(row.incoming),
			"today_outgoing": flt(row.outgoing),
			"expected_cash_on_hand": flt(current_balance),
		}
	)


def _get_settlement_accounts(profile_doc) -> frappe._dict:
	company = profile_doc.company
	cash_account = profile_doc.get("custom_cash_account")
	_validate_account(cash_account, company=company, account_type="Cash", label="Cashier Cash Account")

	settings = _get_settings_doc()
	treasury_account = settings.get("treasury_cash_account")
	_validate_account(treasury_account, company=company, account_type="Cash", label="Treasury Cash Account")

	return frappe._dict(
		{
			"company": company,
			"cash_account": cash_account,
			"treasury_cash_account": treasury_account,
		}
	)


def _difference_type(difference_amount: float) -> str:
	difference_amount = flt(difference_amount)
	if difference_amount < 0:
		return "Short"
	if difference_amount > 0:
		return "Over"
	return "Matched"


def _settlement_payload(settlement) -> dict:
	return {
		"name": settlement.name,
		"posting_date": cstr(settlement.posting_date) if settlement.posting_date else None,
		"cashier_profile": settlement.cashier_profile,
		"cashier_user": settlement.get("cashier_user"),
		"company": settlement.company,
		"cash_account": settlement.cash_account,
		"treasury_cash_account": settlement.treasury_cash_account,
		"expected_cash": flt(settlement.expected_cash),
		"counted_cash": flt(settlement.counted_cash),
		"transfer_amount": flt(settlement.transfer_amount),
		"difference_amount": flt(settlement.difference_amount),
		"difference_type": settlement.difference_type,
		"payment_entry": settlement.get("payment_entry"),
		"status": settlement.status,
		"docstatus": settlement.docstatus,
		"remarks": settlement.get("remarks"),
	}


def _profile_assigned_to_user(profile: str, user: str) -> bool:
	return bool(
		frappe.db.exists(
			"POS Profile User",
			{"parenttype": "POS Profile", "parent": profile, "user": user},
		)
	)


def _get_authorized_profile(profile: str, *, allow_disabled: bool = False):
	if not profile or not frappe.db.exists("POS Profile", profile):
		frappe.throw(_("Cashier profile is required."))

	doc = frappe.get_doc("POS Profile", profile)
	if doc.disabled and not allow_disabled:
		frappe.throw(_("Cashier profile {0} is disabled.").format(frappe.bold(profile)))
	require_restriction_value("cashier_profile", doc.name)

	if _has_accounting_record_permission(("POS Profile", "read")):
		return doc

	if not _profile_assigned_to_user(profile, _current_user()):
		frappe.throw(_("You are not assigned to cashier profile {0}.").format(frappe.bold(profile)), frappe.PermissionError)

	return doc


def resolve_session_cashier_till(company: str | None = None) -> frappe._dict:
	"""The acting user's own cashier profile and the till behind it.

	Added here rather than in the caller because profile resolution belongs to this
	module: it is composed from _get_authorized_profile (assignment, disabled and
	restriction checks) and _resolve_payment_account (the cash-account lookup plus
	_validate_account), so there is exactly one place that decides which till a user
	stands behind.

	Never falls back to a default. Every user who receives cash does so through their
	own profile - admins and the doctor included - so an unassigned user is an error to
	report, not a case to paper over.
	"""
	user = _current_user()
	# Sorted in Python, not SQL: `default` is a reserved word, and Frappe's ORDER BY
	# validator rejects the backticks that would be needed to sort on it.
	assigned = frappe.get_all(
		"POS Profile User",
		filters={"parenttype": "POS Profile", "user": user},
		fields=["parent", "default", "idx"],
		ignore_permissions=True,
	)
	assigned.sort(key=lambda row: (-cint(row.get("default")), cint(row.get("idx"))))
	if not assigned:
		frappe.throw(
			_(
				"{0} has no cashier profile, so cash cannot be received. "
				"Assign the user to a POS Profile before collecting driver cash."
			).format(frappe.bold(user))
		)

	candidates = [row.parent for row in assigned]
	if company:
		matching = [
			name for name in candidates
			if frappe.db.get_value("POS Profile", name, "company") == company
		]
		if not matching:
			frappe.throw(
				_(
					"{0} has no cashier profile for Company {1}. Profiles held: {2}."
				).format(frappe.bold(user), frappe.bold(company), ", ".join(candidates))
			)
		candidates = matching

	# _get_authorized_profile enforces existence, not-disabled, restriction scope and
	# assignment; it throws with its own message when any of those fail.
	profile = _get_authorized_profile(candidates[0])

	# Same call the cashier payment path uses to answer "where does this cashier's cash
	# land", which validates the account exists, is a ledger, is not disabled, is of type
	# Cash and belongs to the profile's company.
	account = _resolve_payment_account(
		profile.company, _default_cash_mode_of_payment(profile), profile
	)
	return frappe._dict(profile=profile.name, company=profile.company, cash_account=account)


def _get_cashier_employee(user: str | None = None) -> str | None:
	user = user or _current_user()
	if not frappe.db.exists("DocType", "Employee"):
		return None
	return frappe.db.get_value("Employee", {"user_id": user}, "name")


def _resolve_payment_account(
	company: str,
	mode_of_payment: str | None,
	profile,
	*,
	required: bool = True,
) -> str | None:
	if _is_cash_mode(mode_of_payment):
		account = profile.get("custom_cash_account")
		if required:
			_validate_account(account, company=company, account_type="Cash", label="Cashier Cash Account")
		return account

	if mode_of_payment and not frappe.db.exists("Mode of Payment", mode_of_payment):
		frappe.throw(_("Mode of Payment {0} does not exist.").format(frappe.bold(mode_of_payment)))

	account = frappe.db.get_value(
		"Mode of Payment Account",
		{"parent": mode_of_payment, "company": company},
		"default_account",
	)
	if required:
		_validate_account(account, company=company, label="Mode of Payment Account")
	return account


def _profile_user_rows(profile) -> list[dict]:
	return [
		{
			"user": row.user,
			"default": cint(row.default),
			"full_name": frappe.db.get_value("User", row.user, "full_name") if row.user else None,
		}
		for row in profile.get("applicable_for_users") or []
	]


def _profile_payment_rows(profile) -> list[dict]:
	rows = []
	for row in profile.get("payments") or []:
		mode = row.mode_of_payment
		account = _resolve_payment_account(profile.company, mode, profile, required=False)
		rows.append(
			{
				"mode_of_payment": mode,
				"type": frappe.db.get_value("Mode of Payment", mode, "type") if mode else None,
				"default": cint(row.default),
				"allow_in_returns": cint(row.allow_in_returns),
				"account": account,
				"uses_cashier_cash_account": cint(_is_cash_mode(mode)),
			}
		)
	return rows


def _profile_payload(profile, *, include_balance: bool = True) -> dict:
	cash_account = profile.get("custom_cash_account")
	account_row = None
	if cash_account and frappe.db.exists("Account", cash_account):
		account_row = frappe.db.get_value(
			"Account",
			cash_account,
			["name", "account_name", "company", "account_type", "account_currency"],
			as_dict=True,
		)

	return {
		"name": profile.name,
		"title": profile.name,
		"company": profile.company,
		"currency": profile.currency,
		"customer": profile.customer,
		"warehouse": profile.warehouse,
		"disabled": cint(profile.disabled),
		"cash_account": dict(account_row) if account_row else None,
		"cash_account_balance": _get_account_balance(cash_account) if include_balance and cash_account else 0.0,
		"users": _profile_user_rows(profile),
		"payments": _profile_payment_rows(profile),
		"setup_complete": cint(bool(profile.company and cash_account and profile.get("payments"))),
		"can_manage": cint(_is_accounting_user()),
	}


def _payment_entry_payload(pe) -> dict:
	return {
		"name": pe.name,
		"doctype": pe.doctype,
		"docstatus": pe.docstatus,
		"status": pe.get("status"),
		"posting_date": cstr(pe.posting_date) if pe.posting_date else None,
		"company": pe.company,
		"payment_type": pe.payment_type,
		"mode_of_payment": pe.mode_of_payment,
		"party_type": pe.party_type,
		"party": pe.party,
		"paid_from": pe.paid_from,
		"paid_to": pe.paid_to,
		"paid_amount": flt(pe.paid_amount),
		"received_amount": flt(pe.received_amount),
		"custom_pos_profile": pe.get("custom_pos_profile"),
		"custom_cashier_user": pe.get("custom_cashier_user"),
		"custom_cashier_employee": pe.get("custom_cashier_employee"),
		"custom_cashier_cash_account": pe.get("custom_cashier_cash_account"),
		"references": [
			{
				"reference_doctype": row.reference_doctype,
				"reference_name": row.reference_name,
				"allocated_amount": flt(row.allocated_amount),
				"outstanding_amount": flt(row.outstanding_amount),
				"total_amount": flt(row.total_amount),
			}
			for row in pe.get("references") or []
		],
	}


def _infer_party_from_reference(reference: dict, payment_type: str) -> tuple[str | None, str | None]:
	doctype = reference.get("reference_doctype")
	name = reference.get("reference_name")
	if not doctype or not name or not frappe.db.exists(doctype, name):
		return None, None

	if doctype in ("Sales Invoice", "Sales Order"):
		return "Customer", frappe.db.get_value(doctype, name, "customer")
	if doctype in ("Purchase Invoice", "Purchase Order"):
		return "Supplier", frappe.db.get_value(doctype, name, "supplier")
	if payment_type == "Receive":
		return "Customer", None
	if payment_type == "Pay":
		return "Supplier", None
	return None, None


def _get_reference_default_amount(raw_references) -> float:
	references = _coerce_list(raw_references)
	if len(references) != 1:
		return 0.0

	row = _coerce_dict(references[0])
	doctype = row.get("reference_doctype")
	name = row.get("reference_name")
	if not doctype or not name or not frappe.db.exists(doctype, name):
		return 0.0

	if doctype in ("Sales Invoice", "Purchase Invoice"):
		return flt(frappe.db.get_value(doctype, name, "outstanding_amount"))
	if doctype in ("Sales Order", "Purchase Order"):
		values = frappe.db.get_value(
			doctype,
			name,
			["rounded_total", "grand_total", "advance_paid"],
			as_dict=True,
		)
		return flt(flt(values.rounded_total or values.grand_total) - flt(values.advance_paid)) if values else 0.0
	return 0.0


def _normalize_references(raw_references, amount: float | None = None) -> list[dict]:
	references = []
	for raw in _coerce_list(raw_references):
		row = _coerce_dict(raw)
		if not row.get("reference_doctype") or not row.get("reference_name"):
			continue
		references.append(
			{
				"reference_doctype": row.get("reference_doctype"),
				"reference_name": row.get("reference_name"),
				"allocated_amount": flt(row.get("allocated_amount")),
			}
		)

	if len(references) == 1 and not references[0]["allocated_amount"] and amount:
		references[0]["allocated_amount"] = flt(amount)

	return references


def _stamp_sales_invoice_references(references: list[dict], profile: str, cashier_user: str) -> list[str]:
	if not (_has_field("Sales Invoice", "custom_pos_profile") or _has_field("Sales Invoice", "custom_cashier_user")):
		return []

	stamped = []
	for row in references:
		if row.get("reference_doctype") != "Sales Invoice":
			continue
		invoice = row.get("reference_name")
		if not invoice or not frappe.db.exists("Sales Invoice", invoice):
			continue

		updates = {}
		if _has_field("Sales Invoice", "custom_pos_profile"):
			updates["custom_pos_profile"] = profile
		if _has_field("Sales Invoice", "custom_cashier_user"):
			updates["custom_cashier_user"] = cashier_user
		if updates:
			frappe.db.set_value("Sales Invoice", invoice, updates, update_modified=False)
			stamped.append(invoice)
	return stamped


@frappe.whitelist()
@standardize_response
def list_cashier_profiles_for_user(user=None, company=None, search=None, include_disabled=0):
	return _list_cashier_profiles_for_user(
		user=user, company=company, search=search, include_disabled=include_disabled
	)


def _list_cashier_profiles_for_user(user=None, company=None, search=None, include_disabled=0):
	"""The plain implementation, callable from other server code.

	Separate from the endpoint because `.__wrapped__` does not reach it: @frappe.whitelist
	wraps @standardize_response, so unwrapping one layer still lands on the decorator and
	returns the envelope rather than this dict. Calling the endpoint internally and reading
	`["data"]` therefore yielded a dict where a list was expected, and iterating it gave
	field names instead of rows.
	"""
	profile_names = _visible_profile_names(user, company, search, include_disabled)
	data = [_profile_payload(frappe.get_doc("POS Profile", name), include_balance=False) for name in profile_names]
	return {"data": data, "total": len(data)}


def _visible_profile_names(user=None, company=None, search=None, include_disabled=0):
	"""Shared profile-picker scope without loading each profile's detail payload."""
	user = user or _current_user()
	if user != _current_user() and not _has_accounting_record_permission(("POS Profile", "read")):
		frappe.throw(_("Not authorized to list cashier profiles for another user."), frappe.PermissionError)

	filters = {}
	if company:
		filters["company"] = company
	if not cint(include_disabled) or not _has_accounting_record_permission(("POS Profile", "read")):
		filters["disabled"] = 0

	if _has_accounting_record_permission(("POS Profile", "read")):
		profile_names = [
			row.name
			for row in frappe.get_all(
				"POS Profile",
				filters=filters,
				fields=["name"],
				order_by="modified desc",
				ignore_permissions=True,
			)
		]
	else:
		assigned = [
			row.parent
			for row in frappe.get_all(
				"POS Profile User",
				filters={"parenttype": "POS Profile", "user": user},
				fields=["parent"],
				ignore_permissions=True,
			)
		]
		if not assigned:
			return []
		filters["name"] = ["in", assigned]
		profile_names = [
			row.name
			for row in frappe.get_all(
				"POS Profile",
				filters=filters,
				fields=["name"],
				order_by="modified desc",
				ignore_permissions=True,
			)
		]

	search_text = cstr(search).strip().lower()
	profile_names = filter_restricted_values("cashier_profile", profile_names, user=user)
	return [name for name in profile_names if not search_text or search_text in cstr(name).lower()]


@frappe.whitelist()
@standardize_response
def get_cashier_profile_detail(pos_profile=None, profile=None, cashier_profile=None):
	return _cashier_profile_detail(pos_profile or profile or cashier_profile)


def _cashier_profile_detail(profile):
	doc = _get_authorized_profile(profile)
	return _profile_payload(doc)


@frappe.whitelist()
@standardize_response
def get_cashier_runtime_defaults(pos_profile=None, company=None):
	settings = _settings_payload()
	resolved_company = _get_default_company(company)
	profiles = _list_cashier_profiles_for_user(company=resolved_company).get("data", [])

	active_profile = None
	selected_profile = pos_profile
	if not selected_profile and profiles:
		default_profiles = [
			row
			for row in profiles
			if any(user.get("user") == _current_user() and user.get("default") for user in row.get("users") or [])
		]
		selected_profile = (default_profiles[0] if default_profiles else profiles[0]).get("name")

	if selected_profile:
		active_profile = _cashier_profile_detail(selected_profile)

	return {
		"settings": settings,
		"company": resolved_company,
		"default_cash_mode_of_payment": settings.get("default_cash_mode_of_payment")
		or _default_cash_mode_of_payment(frappe.get_doc("POS Profile", selected_profile) if selected_profile else None),
		"treasury_cash_account": settings.get("treasury_cash_account"),
		"cashier_user": _current_user(),
		"cashier_employee": _get_cashier_employee(),
		"can_manage_profiles": cint(_is_accounting_user()),
		"profiles": profiles,
		"active_profile": active_profile,
	}


def _normalize_user_rows(users) -> list[dict]:
	rows = []
	for raw in _coerce_list(users):
		if isinstance(raw, str):
			user = raw
			default = 0
		else:
			row = _coerce_dict(raw)
			user = row.get("user") or row.get("name")
			default = cint(row.get("default"))
		if not user:
			continue
		if not frappe.db.exists("User", user):
			frappe.throw(_("User {0} does not exist.").format(frappe.bold(user)))
		rows.append({"user": user, "default": default})
	return rows


def _normalize_payment_rows(payments, default_mode: str | None = None) -> list[dict]:
	rows = []
	for raw in _coerce_list(payments):
		if isinstance(raw, str):
			mode = raw
			default = 0
			allow_in_returns = 0
		else:
			row = _coerce_dict(raw)
			mode = row.get("mode_of_payment") or row.get("name")
			default = cint(row.get("default"))
			allow_in_returns = cint(row.get("allow_in_returns"))
		if not mode:
			continue
		if not frappe.db.exists("Mode of Payment", mode):
			frappe.throw(_("Mode of Payment {0} does not exist.").format(frappe.bold(mode)))
		rows.append({"mode_of_payment": mode, "default": default, "allow_in_returns": allow_in_returns})

	if not rows and default_mode:
		if not frappe.db.exists("Mode of Payment", default_mode):
			frappe.throw(_("Mode of Payment {0} does not exist.").format(frappe.bold(default_mode)))
		rows.append({"mode_of_payment": default_mode, "default": 1, "allow_in_returns": 1})

	if rows and not any(row["default"] for row in rows):
		rows[0]["default"] = 1
	if sum(cint(row["default"]) for row in rows) > 1:
		seen_default = False
		for row in rows:
			if row["default"] and not seen_default:
				seen_default = True
			else:
				row["default"] = 0

	return rows


def _apply_profile_defaults(doc):
	if not doc.company:
		doc.company = _get_default_company()
	if not doc.company:
		return

	company_defaults = frappe.db.get_value(
		"Company",
		doc.company,
		["default_currency", "write_off_account", "cost_center"],
		as_dict=True,
	)
	if not company_defaults:
		return

	if not doc.currency:
		doc.currency = company_defaults.default_currency
	if not doc.write_off_account:
		doc.write_off_account = company_defaults.write_off_account
	if not doc.write_off_cost_center:
		doc.write_off_cost_center = company_defaults.cost_center
	if not doc.warehouse:
		warehouse = frappe.db.get_single_value("Stock Settings", "default_warehouse")
		if warehouse and frappe.db.get_value("Warehouse", warehouse, "company") != doc.company:
			warehouse = None
		if not warehouse:
			warehouse = frappe.db.get_value(
				"Warehouse",
				{"company": doc.company, "is_group": 0, "disabled": 0},
				"name",
			)
		if warehouse:
			doc.warehouse = warehouse


@frappe.whitelist()
@standardize_response
def save_cashier_profile(profile=None, data=None, **kwargs):
	payload = _coerce_dict(data)
	payload.update({key: value for key, value in kwargs.items() if value is not None})
	if "cash_account" in payload and "custom_cash_account" not in payload:
		payload["custom_cash_account"] = payload.get("cash_account")
	profile_name = profile or payload.get("name") or payload.get("pos_profile") or payload.get("cashier_profile")
	require_restriction_value("cashier_profile", profile_name)

	if profile_name and frappe.db.exists("POS Profile", profile_name):
		_require_accounting_record_permission(("POS Profile", "write"))
		doc = frappe.get_doc("POS Profile", profile_name)
	else:
		_require_accounting_record_permission(("POS Profile", "create"))
		if not profile_name:
			frappe.throw(_("Cashier profile name is required."))
		doc = frappe.get_doc({"doctype": "POS Profile", "__newname": profile_name})

	editable_fields = (
		"company",
		"customer",
		"warehouse",
		"currency",
		"selling_price_list",
		"write_off_account",
		"write_off_cost_center",
		"income_account",
		"expense_account",
		"cost_center",
		"taxes_and_charges",
		"tax_category",
		"disabled",
		"update_stock",
		"allow_rate_change",
		"allow_discount_change",
		"allow_partial_payment",
		"custom_cash_account",
	)
	for fieldname in editable_fields:
		if fieldname in payload:
			doc.set(fieldname, payload.get(fieldname))

	_apply_profile_defaults(doc)
	require_restriction_value("warehouse", doc.get("warehouse"))
	if doc.get("custom_cash_account"):
		_validate_account(doc.custom_cash_account, company=doc.company, account_type="Cash", label="Cashier Cash Account")

	if "users" in payload or "applicable_for_users" in payload:
		doc.set("applicable_for_users", [])
		for row in _normalize_user_rows(payload.get("users") or payload.get("applicable_for_users")):
			doc.append("applicable_for_users", row)

	if "payments" in payload:
		doc.set("payments", [])
		for row in _normalize_payment_rows(payload.get("payments"), _default_cash_mode_of_payment(doc)):
			doc.append("payments", row)
	elif doc.is_new() and not doc.get("payments"):
		for row in _normalize_payment_rows([], _default_cash_mode_of_payment(doc)):
			doc.append("payments", row)

	doc.flags.ignore_permissions = True
	if doc.is_new():
		doc.insert(ignore_permissions=True)
	else:
		doc.save(ignore_permissions=True)

	return {"success": True, "profile": _profile_payload(doc)}


@frappe.whitelist()
@standardize_response
def create_payment_entry_with_cashier_context(pos_profile=None, data=None, submit=1, **kwargs):
	_require_accounting_record_permission(("Payment Entry", "create"))
	if cint(submit):
		_require_accounting_record_permission(("Payment Entry", "submit"))

	payload = _coerce_dict(data)
	payload.update({key: value for key, value in kwargs.items() if value is not None})

	profile = _get_authorized_profile(pos_profile or payload.get("pos_profile") or payload.get("cashier_profile"))
	company = payload.get("company") or profile.company or _get_default_company()
	if not company:
		frappe.throw(_("Company is required."))
	if company != profile.company:
		frappe.throw(_("Cashier profile {0} belongs to Company {1}.").format(profile.name, profile.company))

	payment_type = payload.get("payment_type") or "Receive"
	if payment_type not in ("Receive", "Pay"):
		frappe.throw(_("Cashier payment entries must use Receive or Pay payment type."))

	raw_references = payload.get("references")
	if not raw_references and payload.get("sales_invoice"):
		raw_references = [{"reference_doctype": "Sales Invoice", "reference_name": payload.get("sales_invoice")}]
	elif not raw_references and payload.get("reference_doctype") and payload.get("reference_name"):
		raw_references = [
			{
				"reference_doctype": payload.get("reference_doctype"),
				"reference_name": payload.get("reference_name"),
				"allocated_amount": payload.get("allocated_amount"),
			}
		]

	amount = flt(payload.get("amount") or payload.get("paid_amount") or payload.get("received_amount"))
	if amount <= 0:
		amount = _get_reference_default_amount(raw_references)
	if amount <= 0:
		frappe.throw(_("Payment amount must be greater than zero."))

	references = _normalize_references(raw_references, amount)
	if any(row.get("reference_doctype") == "Sales Invoice" for row in references):
		_require_accounting_record_permission(("Sales Invoice", "read"))
		# A "Bill the App Customer" partner order sits on the customer's account while the
		# partner holds the money; taking it here as well charges the customer twice.
		from pet_app.api.delivery_partners import refuse_partner_collected_invoices

		refuse_partner_collected_invoices(
			[row.get("reference_name") for row in references if row.get("reference_doctype") == "Sales Invoice"]
		)

	party_type = payload.get("party_type")
	party = payload.get("party")
	if (not party_type or not party) and references:
		inferred_party_type, inferred_party = _infer_party_from_reference(references[0], payment_type)
		party_type = party_type or inferred_party_type
		party = party or inferred_party

	if not party_type or not party:
		frappe.throw(_("Party Type and Party are required for cashier payments."))

	mode_of_payment = payload.get("mode_of_payment") or _default_cash_mode_of_payment(profile)
	cash_or_bank_account = _resolve_payment_account(company, mode_of_payment, profile)
	party_account = payload.get("party_account") or get_party_account(party_type, party, company)
	_validate_account(party_account, company=company, label="Party Account")

	pe = frappe.new_doc("Payment Entry")
	pe.payment_type = payment_type
	pe.company = company
	pe.posting_date = getdate(payload.get("posting_date") or nowdate())
	pe.mode_of_payment = mode_of_payment
	pe.party_type = party_type
	pe.party = party
	pe.cost_center = payload.get("cost_center") or profile.get("cost_center")

	if payment_type == "Receive":
		pe.paid_from = party_account
		pe.paid_to = cash_or_bank_account
	else:
		pe.paid_from = cash_or_bank_account
		pe.paid_to = party_account

	pe.paid_amount = flt(payload.get("paid_amount") or amount)
	pe.received_amount = flt(payload.get("received_amount") or amount)
	pe.reference_no = payload.get("reference_no")
	pe.reference_date = getdate(payload.get("reference_date")) if payload.get("reference_date") else None
	pe.remarks = payload.get("remarks")

	_set_if_field(pe, "custom_pos_profile", profile.name)
	_set_if_field(pe, "custom_cashier_user", _current_user())
	_set_if_field(pe, "custom_cashier_employee", payload.get("cashier_employee") or _get_cashier_employee())
	_set_if_field(pe, "custom_cashier_cash_account", profile.get("custom_cash_account"))

	for row in references:
		pe.append("references", row)

	pe.flags.ignore_permissions = True
	pe.insert(ignore_permissions=True)
	if cint(submit):
		pe.submit()

	stamped_invoices = _stamp_sales_invoice_references(references, profile.name, _current_user())
	pe.reload()
	return {
		"success": True,
		"payment_entry": _payment_entry_payload(pe),
		"stamped_sales_invoices": stamped_invoices,
	}


@frappe.whitelist()
@standardize_response
def get_cashier_settlement_snapshot(
	pos_profile=None, profile=None, cashier_profile=None, posting_date=None, from_date=None, to_date=None
):
	doc = _get_authorized_profile(pos_profile or profile or cashier_profile)
	accounts = _get_settlement_accounts(doc)
	cash_account = accounts.cash_account
	treasury_account = accounts.treasury_cash_account
	posting_date = getdate(posting_date or nowdate())
	activity = _get_cash_activity(cash_account, posting_date)

	conditions = ["docstatus = 1", "custom_pos_profile = %s"]
	values = [doc.name]
	if from_date:
		conditions.append("posting_date >= %s")
		values.append(getdate(from_date))
	if to_date:
		conditions.append("posting_date <= %s")
		values.append(getdate(to_date))

	summary = []
	if _has_field("Payment Entry", "custom_pos_profile"):
		summary = frappe.db.sql(
			f"""
			SELECT payment_type, mode_of_payment, COUNT(*) AS count,
			       COALESCE(SUM(paid_amount), 0) AS paid_amount,
			       COALESCE(SUM(received_amount), 0) AS received_amount
			FROM `tabPayment Entry`
			WHERE {" AND ".join(conditions)}
			GROUP BY payment_type, mode_of_payment
			ORDER BY payment_type, mode_of_payment
			""",
			values,
			as_dict=True,
		)

	recent_entries = []
	if _has_field("Payment Entry", "custom_pos_profile"):
		recent_entries = frappe.get_all(
			"Payment Entry",
			filters={"custom_pos_profile": doc.name, "docstatus": 1},
			fields=[
				"name",
				"posting_date",
				"payment_type",
				"mode_of_payment",
				"party_type",
				"party",
				"paid_amount",
				"received_amount",
				"paid_from",
				"paid_to",
			],
			order_by="posting_date desc, creation desc",
			limit=20,
			ignore_permissions=True,
		)

	last_settlement = None
	if treasury_account and _has_field("Payment Entry", "custom_pos_profile"):
		rows = frappe.get_all(
			"Payment Entry",
			filters={
				"custom_pos_profile": doc.name,
				"payment_type": "Internal Transfer",
				"paid_from": cash_account,
				"paid_to": treasury_account,
				"docstatus": 1,
			},
			fields=["name", "posting_date", "paid_amount", "received_amount"],
			order_by="posting_date desc, creation desc",
			limit=1,
			ignore_permissions=True,
		)
		last_settlement = rows[0] if rows else None

	# expected_cash_on_hand already counts these - it is a raw SUM over GL Entry keyed on
	# the account, with no voucher_type filter, so any submitted Journal Entry crediting
	# the till moves it. What it cannot do is show them: activity_summary and
	# recent_payment_entries read Payment Entry, so a till payout shifts the number the
	# cashier is asked to reconcile against while appearing nowhere in the breakdown.
	expenses = _list_till_expenses(doc.name, cash_account, posting_date)

	# The settlement for the day being snapshotted, read here rather than left to the
	# client. `CashierSettlementSnapshot` carried no settlement fields, so the dashboard
	# was pulling the latest two hundred settlements site-wide - unfiltered, unscoped by
	# permission - and matching on cashier_profile plus posting_date. That answer is
	# simply wrong once the site passes two hundred settlements, and it read other tills'
	# settlements to produce it.
	settlement_rows = _settlements_in_range([doc.name], posting_date, posting_date)
	settlement = _settlement_summary(settlement_rows.get(doc.name) or [])

	return {
		"profile": _profile_payload(doc),
		"cash_account": cash_account,
		"treasury_cash_account": treasury_account,
		"posting_date": cstr(posting_date),
		"current_balance": activity.current_balance,
		"today_incoming": activity.today_incoming,
		"today_outgoing": activity.today_outgoing,
		"expected_cash_on_hand": activity.expected_cash_on_hand,
		"cash_balance": activity.current_balance,
		"suggested_settlement_amount": flt(activity.expected_cash_on_hand)
		if flt(activity.expected_cash_on_hand) > 0
		else 0.0,
		"activity_summary": [dict(row) for row in summary],
		"recent_payment_entries": [dict(row) for row in recent_entries],
		"today_expenses": expenses,
		"today_expense_total": flt(sum(row["amount"] for row in expenses)),
		"last_settlement": dict(last_settlement) if last_settlement else None,
		# `last_settlement` above is the last Payment Entry transfer ever made from this
		# till, whenever it happened. These are about `posting_date` specifically: whether
		# this day has been settled, and by how much it was out. Different questions, so
		# both stay.
		**settlement,
	}


@frappe.whitelist()
@standardize_response
def settle_cashier_to_treasury(
	pos_profile=None,
	profile=None,
	cashier_profile=None,
	amount=None,
	posting_date=None,
	submit=1,
	*,
	counted_cash=None,
	transfer_amount=None,
	**kwargs
):
	"""Move a till's cash to the treasury and record what the count said.

	`counted_cash` is what the cashier physically counted; `transfer_amount` is what they
	handed over. `difference_amount` on `Pet App Cashier Settlement` is
	counted_cash - expected_cash - the till being over or short - and it originates here
	and nowhere else.

	Both were already accepted through **kwargs. Named keyword-only parameters make
	them discoverable while preserving positional compatibility for posting_date/submit.
	The `payload` fallback is kept for callers posting a nested `data` object.

	Omitting `counted_cash` retains the legacy transfer-amount fallback. Clients must
	send the physical count to record a measured difference.
	"""
	doc = _get_authorized_profile(pos_profile or profile or cashier_profile)
	accounts = _get_settlement_accounts(doc)
	company = accounts.company
	cash_account = accounts.cash_account
	treasury_account = accounts.treasury_cash_account

	payload = _coerce_dict(kwargs.get("data"))
	payload.update({key: value for key, value in kwargs.items() if value is not None and key != "data"})

	posting_date = getdate(posting_date or nowdate())
	activity = _get_cash_activity(cash_account, posting_date)
	expected_cash = flt(activity.expected_cash_on_hand)

	transfer_amount = flt(
		transfer_amount
		if transfer_amount is not None
		else payload.get("transfer_amount")
		if payload.get("transfer_amount") is not None
		else amount if amount is not None else expected_cash
	)
	counted_cash = flt(
		counted_cash
		if counted_cash is not None
		else payload.get("counted_cash")
		if payload.get("counted_cash") is not None
		else transfer_amount
	)
	if counted_cash < 0:
		frappe.throw(_("Counted cash cannot be negative."))
	if transfer_amount < 0:
		frappe.throw(_("Transfer amount cannot be negative."))
	if transfer_amount > counted_cash and not cint(payload.get("allow_transfer_over_counted")):
		frappe.throw(_("Transfer amount cannot exceed counted cash without an explicit override."))
	if transfer_amount > counted_cash and not cstr(payload.get("override_reason")).strip():
		frappe.throw(_("Override reason is required when transfer amount exceeds counted cash."))

	difference_amount = flt(counted_cash - expected_cash)
	difference_type = _difference_type(difference_amount)

	mode_of_payment = payload.get("mode_of_payment") or _default_cash_mode_of_payment(doc)
	remarks = payload.get("remarks") or _("Cashier settlement from {0} to treasury.").format(doc.name)
	pe = None
	if transfer_amount > 0:
		_require_accounting_record_permission(("Payment Entry", "create"))
		if cint(submit):
			_require_accounting_record_permission(("Payment Entry", "submit"))

		pe = frappe.new_doc("Payment Entry")
		pe.payment_type = "Internal Transfer"
		pe.company = company
		pe.posting_date = posting_date
		pe.mode_of_payment = mode_of_payment
		pe.paid_from = cash_account
		pe.paid_to = treasury_account
		pe.paid_amount = transfer_amount
		pe.received_amount = transfer_amount
		pe.reference_no = payload.get("reference_no") or _("Cashier Settlement {0}").format(doc.name)
		pe.reference_date = getdate(payload.get("reference_date")) if payload.get("reference_date") else posting_date
		pe.remarks = remarks

		_set_if_field(pe, "custom_pos_profile", doc.name)
		_set_if_field(pe, "custom_cashier_user", _current_user())
		_set_if_field(pe, "custom_cashier_employee", payload.get("cashier_employee") or _get_cashier_employee())
		_set_if_field(pe, "custom_cashier_cash_account", cash_account)

		pe.flags.ignore_permissions = True
		pe.insert(ignore_permissions=True)
		if cint(submit):
			pe.submit()

	settlement = frappe.new_doc("Pet App Cashier Settlement")
	settlement.posting_date = posting_date
	settlement.cashier_profile = doc.name
	settlement.cashier_user = payload.get("cashier_user") or _current_user()
	settlement.company = company
	settlement.cash_account = cash_account
	settlement.treasury_cash_account = treasury_account
	settlement.expected_cash = expected_cash
	settlement.counted_cash = counted_cash
	settlement.transfer_amount = transfer_amount
	settlement.difference_amount = difference_amount
	settlement.difference_type = difference_type
	settlement.payment_entry = pe.name if pe else None
	settlement.remarks = remarks
	settlement.flags.ignore_permissions = True
	settlement.insert(ignore_permissions=True)
	if cint(submit):
		settlement.submit()

	if pe and _has_field("Payment Entry", "custom_cashier_settlement"):
		frappe.db.set_value("Payment Entry", pe.name, "custom_cashier_settlement", settlement.name, update_modified=False)

	if pe:
		pe.reload()
	settlement.reload()
	return {
		"success": True,
		"settlement": _settlement_payload(settlement),
		"payment_entry": _payment_entry_payload(pe) if pe else None,
		"cash_balance_before": activity.current_balance,
		"cash_balance_after": _get_account_balance(cash_account, posting_date),
	}


@frappe.whitelist()
@standardize_response
def list_cashier_settlements(
	profile=None,
	cashier_profile=None,
	from_date=None,
	to_date=None,
	difference_type=None,
	status=None,
	limit_start=0,
	limit_page_length=50,
):
	filters = {}
	profile_name = profile or cashier_profile
	if profile_name:
		_get_authorized_profile(profile_name)
		filters["cashier_profile"] = profile_name
	elif not _has_accounting_record_permission(("Pet App Cashier Settlement", "read")):
		assigned_profiles = [
			row.parent
			for row in frappe.get_all(
				"POS Profile User",
				filters={"parenttype": "POS Profile", "user": _current_user()},
				fields=["parent"],
				ignore_permissions=True,
			)
		]
		if not assigned_profiles:
			return {"data": [], "total": 0}
		assigned_profiles = filter_restricted_values("cashier_profile", assigned_profiles)
		if not assigned_profiles:
			return {"data": [], "total": 0}
		filters["cashier_profile"] = ["in", assigned_profiles]

	if from_date:
		filters["posting_date"] = [">=", getdate(from_date)]
	if to_date:
		if "posting_date" in filters:
			filters["posting_date"] = ["between", [getdate(from_date), getdate(to_date)]]
		else:
			filters["posting_date"] = ["<=", getdate(to_date)]
	if difference_type:
		filters["difference_type"] = difference_type
	if status:
		filters["status"] = status

	fields = [
		"name",
		"posting_date",
		"cashier_profile",
		"cashier_user",
		"company",
		"cash_account",
		"treasury_cash_account",
		"expected_cash",
		"counted_cash",
		"transfer_amount",
		"difference_amount",
		"difference_type",
		"payment_entry",
		"status",
		"docstatus",
		"remarks",
	]
	rows = frappe.get_all(
		"Pet App Cashier Settlement",
		filters=filters,
		fields=fields,
		order_by="posting_date desc, creation desc",
		limit_start=cint(limit_start),
		limit_page_length=cint(limit_page_length) or 50,
		ignore_permissions=True,
	)
	total = frappe.db.count("Pet App Cashier Settlement", filters=filters)
	return {"data": [dict(row) for row in rows], "total": total}


@frappe.whitelist()
@standardize_response
def get_cashier_settlement_detail(name):
	if not name or not frappe.db.exists("Pet App Cashier Settlement", name):
		frappe.throw(_("Cashier settlement is required."))

	settlement = frappe.get_doc("Pet App Cashier Settlement", name)
	_get_authorized_profile(settlement.cashier_profile)
	response = {"success": True, "settlement": _settlement_payload(settlement)}
	if settlement.payment_entry:
		response["payment_entry"] = _payment_entry_payload(frappe.get_doc("Payment Entry", settlement.payment_entry))
	return response


EXPENSE_VOUCHER_TYPE = "Cash Entry"


def _validate_expense_account(account: str | None, company: str) -> frappe._dict:
	"""Like _validate_account, but on root_type rather than account_type.

	Expense accounts carry account_type values that vary by chart ("Expense Account",
	"Cost of Goods Sold", or blank), so matching on it would reject valid accounts. root_type
	is the invariant, and it is the thing that actually decides whether this shows up as an
	expense.
	"""
	row = _validate_account(account, company=company, label="Expense Account")
	root_type = frappe.db.get_value("Account", account, "root_type")
	if root_type != "Expense":
		frappe.throw(
			_("Expense Account {0} must have root type Expense, not {1}.").format(
				frappe.bold(account), frappe.bold(root_type or _("none"))
			)
		)
	return row


def _list_till_expenses(profile: str, cash_account: str, posting_date) -> list[dict]:
	"""Today's payouts from one till, itemised.

	Keyed on the stamped POS profile rather than on the account, so a driver handover or a
	settlement transfer crediting the same account is not misreported as an expense. Returns
	nothing at all on a site where the stamp field has not been created yet - the totals in
	the snapshot stay correct either way, since those read GL Entry.
	"""
	if not _has_field("Journal Entry", "custom_pos_profile"):
		return []

	fields = ["name", "posting_date", "total_debit", "user_remark"]
	for optional in ("custom_expense_category", "branch", "custom_cashier_user"):
		if _has_field("Journal Entry", optional):
			fields.append(optional)

	entries = frappe.get_all(
		"Journal Entry",
		filters={
			"custom_pos_profile": profile,
			"docstatus": 1,
			"posting_date": getdate(posting_date),
			"voucher_type": EXPENSE_VOUCHER_TYPE,
		},
		fields=fields,
		order_by="creation desc",
		ignore_permissions=True,
	)
	if not entries:
		return []

	# The debited side is the expense; the credited side is the till. Read it back rather
	# than trusting total_debit alone, so a multi-line entry reports the account it hit.
	accounts = frappe.get_all(
		"Journal Entry Account",
		filters={"parent": ["in", [row.name for row in entries]], "parenttype": "Journal Entry"},
		fields=["parent", "account", "debit_in_account_currency"],
		ignore_permissions=True,
	)
	debited: dict[str, dict] = {}
	for row in accounts:
		if row.account == cash_account or not flt(row.debit_in_account_currency):
			continue
		debited.setdefault(row.parent, {"account": row.account, "amount": 0.0})
		debited[row.parent]["amount"] += flt(row.debit_in_account_currency)

	out = []
	for row in entries:
		hit = debited.get(row.name) or {}
		out.append(
			{
				"voucher_id": row.name,
				"posting_date": cstr(row.posting_date),
				"category": row.get("custom_expense_category"),
				"account": hit.get("account"),
				"amount": flt(hit.get("amount") or row.total_debit),
				"branch": row.get("branch"),
				"cashier_user": row.get("custom_cashier_user"),
				"remarks": row.user_remark,
			}
		)
	return out


def _existing_expense_voucher(idempotency_key: str) -> str | None:
	if not _has_field("Journal Entry", "custom_idempotency_key"):
		return None
	return frappe.db.get_value(
		"Journal Entry",
		{"custom_idempotency_key": idempotency_key, "docstatus": ["<", 2]},
		"name",
	)


@frappe.whitelist()
@standardize_response
def record_cashier_expense(
	category=None,
	expense_account=None,
	amount=None,
	cash_account=None,
	posting_date=None,
	company=None,
	pos_profile=None,
	branch=None,
	cost_center=None,
	remarks=None,
	idempotency_key=None,
):
	"""Book cash out of a till as a submitted Journal Entry.

	A Journal Entry rather than a Payment Entry because ERPNext refuses the latter outright:
	Payment Entry.set_missing_values throws "Party Type is mandatory" for every payment_type
	except Internal Transfer, and a till payout has no party. The voucher shape matches what
	driver.py already posts against these accounts - voucher_type "Cash Entry", Dr expense /
	Cr till - so there is one shape to reconcile, not two.
	"""
	_require_cashier_expense_permission()

	# The till comes from the session unless one was named, and naming one still goes
	# through the assignment and restriction checks. resolve_session_cashier_till throws
	# rather than defaulting, which is right here: cash that left a drawer nobody can name
	# is worse than a refusal.
	if pos_profile:
		profile = _get_authorized_profile(pos_profile)
	else:
		profile = _get_authorized_profile(resolve_session_cashier_till(company=company).profile)

	company = company or profile.company
	if company != profile.company:
		frappe.throw(
			_("Cashier profile {0} belongs to Company {1}.").format(
				frappe.bold(profile.name), frappe.bold(profile.company)
			)
		)

	# The one field the client is not trusted on. expected_cash_on_hand is computed from
	# this account's ledger, so an expense credited anywhere else reconciles the till
	# against money that has already left the drawer - silently, until someone counts.
	till_account = profile.get("custom_cash_account")
	_validate_account(till_account, company=company, account_type="Cash", label="Cashier Cash Account")
	cash_account = cstr(cash_account).strip()
	if not cash_account:
		frappe.throw(_("Cash Account is required."))
	if cash_account != till_account:
		frappe.throw(
			_(
				"Cash Account {0} is not the cash account of cashier profile {1}. "
				"A till expense must credit {2}, otherwise it will not reconcile."
			).format(frappe.bold(cash_account), frappe.bold(profile.name), frappe.bold(till_account))
		)

	amount = flt(amount)
	if amount <= 0:
		frappe.throw(_("Expense amount must be greater than zero."))

	category = cstr(category).strip()
	if not category:
		frappe.throw(_("Expense category is required."))

	resolved = resolve_category(category, company)
	expense_account = cstr(expense_account).strip()
	if not expense_account:
		expense_account = (resolved or {}).get("account")
	if not expense_account:
		frappe.throw(
			_("Expense Account is required: category {0} has no account configured.").format(
				frappe.bold(category)
			)
		)
	# A configured category is an accounting decision. If the client sends a different
	# account, refuse rather than picking a winner - silently preferring either one books
	# the money somewhere nobody chose.
	if resolved and resolved.get("account") and resolved["account"] != expense_account:
		frappe.throw(
			_("Expense category {0} is configured for account {1}, not {2}.").format(
				frappe.bold(category), frappe.bold(resolved["account"]), frappe.bold(expense_account)
			)
		)
	_validate_expense_account(expense_account, company)

	branch = cstr(branch).strip() or (resolved or {}).get("branch")
	if branch:
		if not frappe.db.exists("Branch", branch):
			frappe.throw(_("Branch {0} does not exist.").format(frappe.bold(branch)))
		# Journal Entry is not in SCOPED_DOCTYPES, so stamp_branch_on_insert never fires for
		# it and the branch is set explicitly below. The write check still has to happen, or
		# a branch-restricted cashier could file a payout against another clinic.
		assert_can_write_to_branch(branch)

	cost_center = cstr(cost_center).strip() or (resolved or {}).get("cost_center")
	if cost_center and not frappe.db.exists("Cost Center", cost_center):
		frappe.throw(_("Cost Center {0} does not exist.").format(frappe.bold(cost_center)))

	idempotency_key = cstr(idempotency_key).strip() or None
	if idempotency_key:
		existing = _existing_expense_voucher(idempotency_key)
		if existing:
			docstatus = frappe.db.get_value("Journal Entry", existing, "docstatus")
			return {
				"voucher_id": existing,
				"submitted": cint(docstatus) == 1,
				"voucher_type": "Journal Entry",
				"duplicate": True,
			}

	posting_date = getdate(posting_date or nowdate())
	remarks = cstr(remarks).strip() or _("Cashier expense: {0}").format(
		(resolved or {}).get("label_en") or category
	)

	# standardize_response catches every exception and returns an error envelope, so Frappe
	# never sees one and never rolls back on its own. Without this savepoint a failure
	# between insert and submit would commit a draft: money the cashier was told had moved,
	# that never reached the ledger.
	frappe.db.savepoint("cashier_expense")
	try:
		je = frappe.new_doc("Journal Entry")
		je.voucher_type = EXPENSE_VOUCHER_TYPE
		je.company = company
		je.posting_date = posting_date
		je.user_remark = remarks

		je.append(
			"accounts",
			{
				"account": expense_account,
				"debit_in_account_currency": amount,
				"credit_in_account_currency": 0,
				"cost_center": cost_center,
			},
		)
		je.append(
			"accounts",
			{
				"account": cash_account,
				"debit_in_account_currency": 0,
				"credit_in_account_currency": amount,
				"cost_center": cost_center,
			},
		)

		_set_if_field(je, "branch", branch)
		_set_if_field(je, "custom_pos_profile", profile.name)
		_set_if_field(je, "custom_cashier_user", _current_user())
		_set_if_field(je, "custom_expense_category", category)
		_set_if_field(je, "custom_idempotency_key", idempotency_key)

		je.flags.ignore_permissions = True
		je.insert(ignore_permissions=True)
		je.submit()
	except Exception:
		frappe.db.rollback(save_point="cashier_expense")
		raise

	je.reload()
	return {
		"voucher_id": je.name,
		# Read back rather than assumed. The client warns the operator explicitly when this
		# is false, so it must never claim a submission that did not happen.
		"submitted": cint(je.docstatus) == 1,
		"voucher_type": "Journal Entry",
		"category": category,
		"expense_account": expense_account,
		"cash_account": cash_account,
		"amount": amount,
		"branch": branch,
		"posting_date": cstr(posting_date),
	}


# ---------------------------------------------------------------------------
# Cashier activity dashboard
#
# Two endpoints that state outright what the dashboard was reconstructing from
# whatever the generic list APIs happened to expose. Nothing here books anything;
# both are pure reads over the same scope rule the profile picker already uses.
# ---------------------------------------------------------------------------

CASH_EXPENSE_KIND = "cash"
STOCK_EXPENSE_KIND = "stock"
STOCK_EXPENSE_PURPOSE = "Material Issue"


def _resolve_date_range(from_date, to_date) -> tuple:
	"""A closed range, defaulting to a single day.

	Both bounds default to today rather than to an open range: an unbounded scan over
	`tabGL Entry` is not a reasonable thing for a mistyped request to trigger, and every
	caller of these endpoints is looking at a dated view.
	"""
	from_date = getdate(from_date or nowdate())
	to_date = getdate(to_date or from_date)
	if from_date > to_date:
		frappe.throw(_("From Date {0} cannot be after To Date {1}.").format(from_date, to_date))
	return from_date, to_date


def _chunked(values, size: int = 400):
	values = list(values)
	for start in range(0, len(values), size):
		yield values[start : start + size]


def _scoped_profile_payloads(profile=None, company=None, include_disabled=0) -> list[dict]:
	"""The tills this caller is allowed to see.

	Routed through the two gates the profile picker already uses - POS Profile read or an
	assignment row, then `filter_restricted_values` - rather than a rule of its own, so a
	till that is invisible in the picker cannot surface in a report. Naming one profile
	still goes through `_get_authorized_profile`, which applies the same checks plus the
	user-permission restriction.

	`allow_disabled` because this is a report: a till retired last week still has last
	week's money in it, and refusing to show that is not a safety property.
	"""
	if profile:
		doc = _get_authorized_profile(profile, allow_disabled=True)
		if company and doc.company != company:
			return []
		names = [doc.name]
	else:
		names = _visible_profile_names(company=company, include_disabled=include_disabled)
	if not names:
		return []

	fields = ["name", "company", "warehouse", "disabled"]
	if _has_field("POS Profile", "custom_cash_account"):
		fields.append("custom_cash_account")
	profiles = []
	payments = {}
	for chunk in _chunked(names):
		profiles.extend(frappe.get_all("POS Profile", filters={"name": ["in", chunk]}, fields=fields))
		for row in frappe.get_all(
			"POS Payment Method",
			filters={"parenttype": "POS Profile", "parent": ["in", chunk]},
			fields=["parent", "mode_of_payment"],
		):
			payments.setdefault(row.parent, []).append(row.mode_of_payment)
	mode_names = sorted({mode for modes in payments.values() for mode in modes if mode})
	mode_types = {}
	mode_accounts = {}
	for chunk in _chunked(mode_names):
		mode_types.update({
			row.name: row.type
			for row in frappe.get_all(
				"Mode of Payment", filters={"name": ["in", chunk]}, fields=["name", "type"]
			)
		})
		for row in frappe.get_all(
			"Mode of Payment Account", filters={"parent": ["in", chunk]},
			fields=["parent", "company", "default_account"],
		):
			mode_accounts[(row.parent, row.company)] = row.default_account
	default_mode = _get_settings_doc().get("default_cash_mode_of_payment")
	for row in profiles:
		account = row.get("custom_cash_account")
		row["cash_account"] = {"name": account} if account else None
		row["payments"] = [
			{
				"mode_of_payment": mode,
				"uses_cashier_cash_account": cint(
					mode == default_mode or mode_types.get(mode) == "Cash" or cstr(mode).lower() == "cash"
				),
				"default_account": mode_accounts.get((mode, row.company)),
			}
			for mode in payments.get(row.name, [])
		]
	index = {row.name: row for row in profiles}
	return [index[name] for name in names if name in index]


def _payload_cash_account(payload: dict) -> str | None:
	return (payload.get("cash_account") or {}).get("name")


def _profiles_by_cash_account() -> dict[str, list[str]]:
	"""Every till keyed by its cash account, permissions deliberately not applied.

	Unscoped because the question is not "whose money is this" but "can this account
	identify one till at all". A caller who can see one of two profiles sharing an account
	still has to be told their figures are a merge of both, and scoping this lookup would
	hide precisely the case it exists to expose. Response builders only expose names
	within the requested, authorized scope; other owners contribute to the shared flag.
	"""
	if not _has_field("POS Profile", "custom_cash_account"):
		return {}
	index: dict[str, list[str]] = {}
	for row in frappe.get_all(
		"POS Profile", fields=["name", "custom_cash_account"], order_by="name asc", ignore_permissions=True
	):
		if row.get("custom_cash_account"):
			index.setdefault(row.custom_cash_account, []).append(row.name)
	return index


def _profiles_by_warehouse() -> dict[str, list[str]]:
	"""Same idea for stock. See `_stock_expense_rows` for why a warehouse is the only link."""
	index: dict[str, list[str]] = {}
	for row in frappe.get_all(
		"POS Profile", fields=["name", "warehouse"], order_by="name asc", ignore_permissions=True
	):
		if row.get("warehouse"):
			index.setdefault(row.warehouse, []).append(row.name)
	return index


def _cash_account_warnings(payload: dict, siblings: list[str], visible_names=None) -> list[dict]:
	"""Everything known to break the account-is-the-till assumption, reported per till.

	A General Ledger row carries no `pos_profile`, so every per-cashier figure here is
	really a per-account figure wearing a profile's name. That holds exactly as long as
	one account belongs to one till and every voucher for that till posts to it. Neither
	is enforced anywhere, and the client cannot check either, so the checks live here and
	travel with the numbers they qualify.

	Reported rather than thrown. The figures are still the best available answer; what
	they must not do is arrive looking unconditional.
	"""
	warnings = []
	account = _payload_cash_account(payload)

	if not account:
		warnings.append(
			{
				"code": "no_cash_account",
				"message": _("Cashier profile {0} has no cash account, so it has no figures.").format(
					payload.get("name")
				),
			}
		)
		return warnings

	if siblings:
		warnings.append(
			{
				"code": "shared_cash_account",
				"message": _(
					"Cash account {0} is shared by multiple cashier profiles. "
					"Money in, money out and expenses below are the total for all of them."
				).format(account),
				"profiles": [name for name in siblings if name in (visible_names or [])],
				"other_profile_count": len(siblings),
				"account": account,
			}
		)

	# ERPNext's invoice default is company-wide. pet_app POS pins the chosen payment
	# account after save, but other invoice paths may still use the company default.
	for row in payload.get("payments") or []:
		if not cint(row.get("uses_cashier_cash_account")):
			continue
		mode = row.get("mode_of_payment")
		mop_account = row.get("default_account")
		if not mop_account or mop_account == account:
			continue
		warnings.append(
			{
				"code": "mode_of_payment_account_mismatch",
				"message": _(
					"Mode of Payment {0} posts to {1} for company {2}, not to this till's cash "
					"account {3}. Invoice paths using the Mode of Payment default can post "
					"this till's cash to {1}; inspect the voucher's payment account."
				).format(mode, mop_account, payload.get("company"), account),
				"mode_of_payment": mode,
				"account": mop_account,
			}
		)
	return warnings


def _cash_movement_by_account(accounts, from_date, to_date) -> dict[str, frappe._dict]:
	"""Debits and credits per till account over the range, in one query for all tills.

	One statement rather than one per profile: this is the call that replaces roughly two
	round trips per till, and it would be a poor trade to answer it with two queries per
	till instead.
	"""
	out: dict[str, frappe._dict] = {}
	for chunk in _chunked(accounts):
		rows = frappe.db.sql(
			"""
			SELECT account,
				   COALESCE(SUM(debit), 0) AS incoming,
				   COALESCE(SUM(credit), 0) AS outgoing
			FROM `tabGL Entry`
			WHERE account IN %(accounts)s
			  AND posting_date BETWEEN %(from_date)s AND %(to_date)s
			  AND is_cancelled = 0
			GROUP BY account
			""",
			{"accounts": chunk, "from_date": from_date, "to_date": to_date},
			as_dict=True,
		)
		for row in rows:
			out[row.account] = frappe._dict({"incoming": flt(row.incoming), "outgoing": flt(row.outgoing)})
	return out


def _cash_balance_by_account(accounts, as_of) -> dict[str, float]:
	"""Closing balance per till account as of `as_of`, batched the same way.

	Deliberately the same arithmetic as `_get_account_balance` - SUM(debit) - SUM(credit)
	over everything up to the date, cancellations excluded - so a till's balance here and
	in `get_cashier_settlement_snapshot` are the same number computed once each, not two
	definitions that can drift.
	"""
	out: dict[str, float] = {}
	for chunk in _chunked(accounts):
		rows = frappe.db.sql(
			"""
			SELECT account, COALESCE(SUM(debit) - SUM(credit), 0) AS balance
			FROM `tabGL Entry`
			WHERE account IN %(accounts)s
			  AND posting_date <= %(as_of)s
			  AND is_cancelled = 0
			GROUP BY account
			""",
			{"accounts": chunk, "as_of": as_of},
			as_dict=True,
		)
		for row in rows:
			out[row.account] = flt(row.balance)
	return out


SETTLEMENT_FIELDS = [
	"name",
	"posting_date",
	"cashier_profile",
	"cashier_user",
	"company",
	"cash_account",
	"treasury_cash_account",
	"expected_cash",
	"counted_cash",
	"transfer_amount",
	"difference_amount",
	"difference_type",
	"payment_entry",
	"status",
	"remarks",
]


def _settlements_in_range(profile_names, from_date, to_date) -> dict[str, list[frappe._dict]]:
	"""Submitted settlements per till inside the range, newest first.

	Replaces the client's scan of the latest two hundred settlements site-wide: filtered
	to tills the caller can already see, and bounded by the date range rather than by a
	row count, so the answer stops changing once the site passes its two-hundredth
	settlement.
	"""
	out: dict[str, list[frappe._dict]] = {}
	if not profile_names:
		return out
	for chunk in _chunked(profile_names):
		rows = frappe.get_all(
			"Pet App Cashier Settlement",
			filters={
				"cashier_profile": ["in", chunk],
				"docstatus": 1,
				"posting_date": ["between", [from_date, to_date]],
			},
			fields=SETTLEMENT_FIELDS,
			order_by="posting_date desc, creation desc, name desc",
			ignore_permissions=True,
		)
		for row in rows:
			out.setdefault(row.cashier_profile, []).append(row)
	return out


def _settlement_summary(rows: list[frappe._dict]) -> dict:
	"""The settlement facts a dashboard row needs, from the newest settlement in range.

	`settlement_difference` is None rather than 0.0 when nothing was settled. Zero is a
	real and common outcome - a till that balanced - and reporting an unsettled till as
	0.0 would render it as "counted, and it matched".
	"""
	latest = rows[0] if rows else None
	return {
		"is_settled": cint(bool(latest)),
		"settled_at": cstr(latest.posting_date) if latest and latest.posting_date else None,
		"settlement_difference": flt(latest.difference_amount) if latest else None,
		"settlement_difference_type": latest.difference_type if latest else None,
		"settlement": dict(latest) if latest else None,
		"settlement_count": len(rows),
		"settled_total": flt(sum(flt(row.transfer_amount) for row in rows)) if rows else 0.0,
	}


@frappe.whitelist()
@standardize_response
def get_cashier_activity(from_date=None, to_date=None, profile=None, company=None, include_disabled=0):
	"""Every till the caller can see, summarised over a date range, in one call.

	Replaces one list call plus a detail call and a snapshot call per profile - about
	twenty-one round trips for ten tills. Profile metadata, movement, closing balance
	and settlements are read in batches, without a detail or snapshot call per till.

	This is also why the dashboard's till section takes a range where
	`get_cashier_settlement_snapshot` takes a single `posting_date`. That endpoint answers
	one till on one day, which is the right shape for the count-and-settle screen it was
	written for and the wrong shape for a trend; charting a month across ten tills through
	it would be three hundred calls. Ranged aggregation belongs on this endpoint, and a
	trend over days is a further endpoint, not a loop over that one.

	Field definitions, which the client cannot infer and must not re-derive:

	* `incoming` / `outgoing` - debits and credits posted to the till's cash account
	  between the two dates inclusive. Every voucher type, cancellations excluded.
	* `current_balance` - the account's closing balance as of `to_date`, over all time
	  rather than over the range, so it carries the opening balance the range does not.
	* `expected_cash_on_hand` - the same number as `current_balance`, and the same number
	  `get_cashier_settlement_snapshot` returns for `to_date`. It is what a cashier is
	  asked to count against, so the two endpoints have to agree to the fils; if a
	  deduction ever belongs in it, it belongs in both.
	* `settled_at` / `settlement_difference` - from the newest submitted settlement inside
	  the range. `settlement_difference` is None when nothing was settled, never 0.0.

	`cash_account_warnings` qualifies all of the above. A GL row carries no `pos_profile`,
	so these are per-account figures presented per profile, and the two are the same thing
	only while one account belongs to one till and every voucher for that till posts to
	it. See `_cash_account_warnings`. A row with a non-empty list is still the best answer
	available; it just is not an unconditional one.
	"""
	from_date, to_date = _resolve_date_range(from_date, to_date)
	profiles = _scoped_profile_payloads(profile, company=company, include_disabled=include_disabled)

	accounts = sorted({account for account in (_payload_cash_account(row) for row in profiles) if account})
	movement = _cash_movement_by_account(accounts, from_date, to_date)
	balances = _cash_balance_by_account(accounts, to_date)
	settlements = _settlements_in_range([row["name"] for row in profiles], from_date, to_date)
	account_owners = _profiles_by_cash_account()
	visible_names = {row["name"] for row in profiles}

	cashiers = []
	for row in profiles:
		account = _payload_cash_account(row)
		moved = movement.get(account) or frappe._dict({"incoming": 0.0, "outgoing": 0.0})
		balance = flt(balances.get(account))
		siblings = [name for name in account_owners.get(account, []) if name != row["name"]]

		cashier = {
			"profile_name": row["name"],
			"company": row.get("company"),
			"warehouse": row.get("warehouse"),
			"cash_account": account,
			"disabled": cint(row.get("disabled")),
			"current_balance": balance,
			"incoming": flt(moved.incoming),
			"outgoing": flt(moved.outgoing),
			"expected_cash_on_hand": balance,
			"cash_account_warnings": _cash_account_warnings(row, siblings, visible_names),
			"aggregation_basis": "cash_account",
		}
		cashier.update(_settlement_summary(settlements.get(row["name"]) or []))
		cashiers.append(cashier)

	return {
		"cashiers": cashiers,
		"from_date": cstr(from_date),
		"to_date": cstr(to_date),
		"total": len(cashiers),
	}


def _voucher_field_map(voucher_type: str, names, fields) -> dict[str, frappe._dict]:
	"""Read a few parent columns for a batch of vouchers, skipping fields the site lacks.

	Every optional column here is a custom field created by a patch, so a site part-way
	through migration has some and not others. Missing ones are dropped from the select
	rather than defaulted, which keeps `.get()` returning None for them downstream.
	"""
	usable = [field for field in fields if _has_field(voucher_type, field)]
	if not usable or not names:
		return {}
	out: dict[str, frappe._dict] = {}
	for chunk in _chunked(list(names)):
		for row in frappe.get_all(
			voucher_type,
			filters={"name": ["in", chunk]},
			fields=["name", *usable],
			ignore_permissions=True,
		):
			out[row.name] = row
	return out


def _expense_category_for(account, category_index) -> tuple:
	row = category_index.get(account) if account else None
	if row:
		return row.get("key"), row.get("label_en"), row.get("label_ar")
	return None, None, None


def _cash_expense_rows(
	accounts, account_owners, category_index, from_date, to_date, branch=None,
	*, visible_profiles, unresolved,
) -> list[dict]:
	"""Return provable expense allocations; never apportion mixed funding sources.

	Net postings per voucher/account avoid counting an expense reversal as a new payout.
	A single net credit account funds every net debit exactly. With multiple funding
	accounts, only a single net debit account can be attributed exactly. Other mixed
	vouchers go to `unresolved`, outside the reported expense totals.
	"""
	if not accounts:
		return []

	credits = []
	for chunk in _chunked(accounts):
		credits += frappe.db.sql(
			"""
			SELECT voucher_type, voucher_no, account, posting_date,
				   COALESCE(SUM(credit), 0) AS credit,
				   COALESCE(SUM(debit), 0) AS debit
			FROM `tabGL Entry`
			WHERE account IN %(accounts)s
			  AND posting_date BETWEEN %(from_date)s AND %(to_date)s
			  AND is_cancelled = 0
			GROUP BY voucher_type, voucher_no, account, posting_date
			HAVING SUM(credit) > SUM(debit)
			""",
			{"accounts": chunk, "from_date": from_date, "to_date": to_date},
			as_dict=True,
		)
	if not credits:
		return []

	# Both sides of every candidate voucher, joined to Account for the root type. This is
	# the query the client cannot make: `against` is the only contra information the list
	# API exposes, and it is a display string.
	ledger: dict[tuple, list] = {}
	for chunk in _chunked(sorted({row.voucher_no for row in credits})):
		for row in frappe.db.sql(
			"""
			SELECT gl.voucher_type, gl.voucher_no, gl.posting_date, gl.account,
				   SUM(gl.debit) AS debit, SUM(gl.credit) AS credit,
				   CASE WHEN COUNT(DISTINCT COALESCE(gl.cost_center, '')) = 1
				        THEN MAX(gl.cost_center) ELSE NULL END AS cost_center,
				   MAX(gl.remarks) AS remarks, acc.root_type, acc.account_name
			FROM `tabGL Entry` gl
			INNER JOIN `tabAccount` acc ON acc.name = gl.account
			WHERE gl.voucher_no IN %(names)s
			  AND gl.posting_date BETWEEN %(from_date)s AND %(to_date)s
			  AND gl.is_cancelled = 0
			GROUP BY gl.voucher_type, gl.voucher_no, gl.posting_date, gl.account,
			         acc.root_type, acc.account_name
			""",
			{"names": chunk, "from_date": from_date, "to_date": to_date},
			as_dict=True,
		):
			ledger.setdefault((row.voucher_type, row.voucher_no, row.posting_date), []).append(row)

	by_type: dict[str, set] = {}
	for row in credits:
		by_type.setdefault(row.voucher_type, set()).add(row.voucher_no)

	parents: dict[str, dict[str, frappe._dict]] = {}
	for voucher_type, names in by_type.items():
		fields = ["branch"]
		if voucher_type == "Journal Entry":
			fields += ["custom_pos_profile", "custom_expense_category", "custom_cashier_user", "user_remark"]
		parents[voucher_type] = _voucher_field_map(voucher_type, names, fields)

	rows = []
	for credit in credits:
		key = (credit.voucher_type, credit.voucher_no, credit.posting_date)
		lines = ledger.get(key) or []
		till_credit = flt(credit.credit - credit.debit)
		debits = [line for line in lines if flt(line.debit - line.credit) > 0]
		expense_lines = [line for line in debits if line.root_type == "Expense"]
		if till_credit <= 0 or not expense_lines:
			continue
		credited_accounts = {line.account for line in lines if flt(line.credit - line.debit) > 0}
		single_funder = len(credited_accounts) == 1
		exact = single_funder or len(debits) == 1

		parent = (parents.get(credit.voucher_type) or {}).get(credit.voucher_no) or frappe._dict()
		voucher_branch = parent.get("branch")
		# An explicit branch filter drops what it cannot confirm, including vouchers whose
		# doctype has no branch column at all. Reporting an unbranded voucher under a named
		# branch would be a guess, and the point of this endpoint is to stop guessing.
		if branch and voucher_branch != branch:
			continue

		# `custom_pos_profile` is the till this app stamped on its own payouts. Where it is
		# present it is authoritative; where it is absent the account is the only link, and
		# that link is only as good as the account being exclusive to one till.
		stamped_profile = parent.get("custom_pos_profile")
		owners = account_owners.get(credit.account) or []
		if stamped_profile and stamped_profile not in visible_profiles:
			continue
		resolved_profile = stamped_profile or (owners[0] if len(owners) == 1 else None)
		visible_owners = [name for name in owners if name in visible_profiles]
		if resolved_profile not in visible_profiles:
			resolved_profile = None
		if not exact:
			unresolved.append({
				"id": f"{credit.voucher_type}:{credit.voucher_no}:{credit.account}",
				"posting_date": cstr(credit.posting_date),
				"voucher_type": credit.voucher_type,
				"voucher_no": credit.voucher_no,
				"cash_account": credit.account,
				"cash_outflow": till_credit,
				"branch": voucher_branch,
				"reason": "ambiguous_expense_allocation",
			})
			continue

		for line in expense_lines:
			amount = flt(line.debit - line.credit) if single_funder else till_credit
			if not amount:
				continue

			category = parent.get("custom_expense_category")
			label_en = label_ar = None
			if not category:
				category, label_en, label_ar = _expense_category_for(line.account, category_index)
			else:
				resolved = category_index.get(line.account)
				label_en = (resolved or {}).get("label_en")
				label_ar = (resolved or {}).get("label_ar")

			rows.append(
				{
					"id": f"{credit.voucher_type}:{credit.voucher_no}:{line.account}:{credit.account}",
					"posting_date": cstr(credit.posting_date),
					"kind": CASH_EXPENSE_KIND,
					"category": category,
					"category_label_en": label_en or line.account_name,
					"category_label_ar": label_ar,
					"account": line.account,
					"account_name": line.account_name,
					"amount": amount,
					"voucher_type": credit.voucher_type,
					"voucher_no": credit.voucher_no,
					"source": "cashier_expense" if stamped_profile else "ledger",
					"remarks": parent.get("user_remark") or line.remarks,
					"profile": resolved_profile,
					"profile_candidates": visible_owners,
					"shared_cash_account": cint(len(owners) > 1),
					"cash_account": credit.account,
					"branch": voucher_branch,
					"cashier_user": parent.get("custom_cashier_user"),
					"cost_center": line.cost_center,
					"exact": cint(exact),
				}
			)
	return rows


def _stock_expense_rows(
	warehouses, warehouse_owners, companies, category_index, from_date, to_date, branch=None,
	*, visible_profiles,
) -> list[dict]:
	"""Stock consumed out of a till's warehouse, categorised by the account it was booked to.

	Two things the client cannot do at all. `expense_account` lives on `Stock Entry
	Detail`, and the list API returns parent columns only, so every one of these arrives
	uncategorised no matter what the accountant configured. And `Stock Entry` has no date
	filter on that path, so the client pulls the two hundred most recent Material Issues
	and slices by date - which quietly loses the start of any busy month. Both are fixed
	by reading the child table with a real `posting_date` predicate.

	Grouped per entry per expense account rather than per item line: an entry issuing
	forty items against one account is one expense, and forty rows would bury it.

	Attribution is by source warehouse, because that is the only link a Stock Entry has to
	a till - it carries no `pos_profile`, and adding one would mean stamping it at issue
	time. Where two profiles share a warehouse, `profile` comes back None and
	`shared_warehouse` is true. Only visible names appear in `profile_candidates`.

	No root-type filter, unlike the cash side. There the filter is what separates a payout
	from a settlement; here `purpose = Material Issue` has already done that job, and
	dropping rows whose account is unset or non-Expense would silently lose stock that
	genuinely left. `root_type` travels on the row instead.
	"""
	if not warehouses:
		return []

	conditions = [
		"se.docstatus = 1",
		"se.purpose = %(purpose)s",
		"se.posting_date BETWEEN %(from_date)s AND %(to_date)s",
		"sed.s_warehouse IN %(warehouses)s",
	]
	values = {
		"purpose": STOCK_EXPENSE_PURPOSE,
		"from_date": from_date,
		"to_date": to_date,
		"warehouses": list(warehouses),
	}
	if companies:
		conditions.append("se.company IN %(companies)s")
		values["companies"] = list(companies)
	if branch:
		if not _has_field("Stock Entry", "branch"):
			return []
		conditions.append("se.branch = %(branch)s")
		values["branch"] = branch

	rows = frappe.db.sql(
		f"""
		SELECT se.name AS voucher_no, se.posting_date, se.company,
			   sed.expense_account AS account, sed.s_warehouse AS warehouse,
			   COALESCE(SUM(sed.amount), 0) AS amount,
			   COUNT(*) AS item_count,
			   acc.root_type, acc.account_name
		FROM `tabStock Entry Detail` sed
		INNER JOIN `tabStock Entry` se ON se.name = sed.parent
		LEFT JOIN `tabAccount` acc ON acc.name = sed.expense_account
		WHERE {" AND ".join(conditions)}
		GROUP BY se.name, se.posting_date, se.company, sed.expense_account,
				 sed.s_warehouse, acc.root_type, acc.account_name
		""",
		values,
		as_dict=True,
	)
	if not rows:
		return []

	parents = _voucher_field_map(
		"Stock Entry", {row.voucher_no for row in rows}, ["branch", "stock_entry_type", "remarks"]
	)

	out = []
	for row in rows:
		owners = warehouse_owners.get(row.warehouse) or []
		parent = parents.get(row.voucher_no) or frappe._dict()
		category, label_en, label_ar = _expense_category_for(row.account, category_index)
		out.append(
			{
				"id": f"Stock Entry:{row.voucher_no}:{row.account or 'unset'}:{row.warehouse}",
				"posting_date": cstr(row.posting_date),
				"kind": STOCK_EXPENSE_KIND,
				"category": category,
				"category_label_en": label_en or row.account_name,
				"category_label_ar": label_ar,
				"account": row.account,
				"account_name": row.account_name,
				"amount": flt(row.amount),
				"voucher_type": "Stock Entry",
				"voucher_no": row.voucher_no,
				"source": "stock_entry",
				"remarks": parent.get("remarks"),
				"profile": owners[0] if len(owners) == 1 and owners[0] in visible_profiles else None,
				"profile_candidates": [name for name in owners if name in visible_profiles],
				"shared_warehouse": cint(len(owners) > 1),
				"cash_account": None,
				"branch": parent.get("branch"),
				"cashier_user": None,
				"warehouse": row.warehouse,
				"company": row.company,
				"root_type": row.root_type,
				"stock_entry_type": parent.get("stock_entry_type"),
				"item_count": cint(row.item_count),
				# Never touched a drawer, so there is nothing to be exact or approximate
				# about relative to a cash figure. Present so both kinds carry the key.
				"exact": 1,
			}
		)
	return out


@frappe.whitelist()
@standardize_response
def list_cashier_expenses(
	from_date=None, to_date=None, profile=None, branch=None, company=None, include_disabled=0
):
	"""Itemised expenses for the tills the caller can see, cash and stock kept apart.

	`kind` is "cash" or "stock", and the two must never be summed. Cash left the drawer:
	it is already inside that till's `outgoing` and `expected_cash_on_hand`, and a cashier
	counting the till will be short by exactly that amount. Stock never touched the
	drawer - the money left when the item was bought, and the Material Issue only moves
	the cost from inventory to an expense account. Adding them produces a number that
	reconciles against nothing: not the till, not the ledger, not the P&L. Hence
	`totals.cash` and `totals.stock` and deliberately no combined figure; the caller who
	genuinely wants one has to write the addition down and own it.

	Scope is the same as `get_cashier_activity`: cash rows come from the till accounts of
	visible profiles, stock rows from their warehouses. `branch`, when given, filters both
	and drops anything it cannot confirm.
	"""
	from_date, to_date = _resolve_date_range(from_date, to_date)
	branch = cstr(branch).strip() or None
	if branch and not frappe.db.exists("Branch", branch):
		frappe.throw(_("Branch {0} does not exist.").format(frappe.bold(branch)))

	profiles = _scoped_profile_payloads(profile, company=company, include_disabled=include_disabled)
	empty = {
		"data": [],
		"total": 0,
		"totals": {"cash": 0.0, "stock": 0.0},
		"from_date": cstr(from_date),
		"to_date": cstr(to_date),
		"profiles": [],
		"unresolved_cash_vouchers": [],
		"cash_total_complete": True,
	}
	if not profiles:
		return empty

	accounts = sorted({account for account in (_payload_cash_account(row) for row in profiles) if account})
	warehouses = sorted({row.get("warehouse") for row in profiles if row.get("warehouse")})
	companies = sorted({row.get("company") for row in profiles if row.get("company")})

	# One category index for the whole call. Built per company only when the visible tills
	# span exactly one, which is the ordinary case; across companies the configured rows
	# are read unfiltered and the account itself keeps them apart, since an account belongs
	# to one company.
	category_index = _categories_by_account(companies[0] if len(companies) == 1 else None)

	account_owners = _profiles_by_cash_account()
	warehouse_owners = _profiles_by_warehouse()

	unresolved = []
	visible_profiles = {row["name"] for row in profiles}
	rows = _cash_expense_rows(
		accounts, account_owners, category_index, from_date, to_date, branch=branch,
		visible_profiles=visible_profiles, unresolved=unresolved,
	) + _stock_expense_rows(
		warehouses, warehouse_owners, companies, category_index, from_date, to_date, branch=branch,
		visible_profiles=visible_profiles,
	)
	rows.sort(key=lambda row: (row["posting_date"], row["voucher_no"], row["id"]), reverse=True)

	return {
		"data": rows,
		"total": len(rows),
		"totals": {
			"cash": flt(sum(row["amount"] for row in rows if row["kind"] == CASH_EXPENSE_KIND)),
			"stock": flt(sum(row["amount"] for row in rows if row["kind"] == STOCK_EXPENSE_KIND)),
		},
		"from_date": cstr(from_date),
		"to_date": cstr(to_date),
		"profiles": [row["name"] for row in profiles],
		"unresolved_cash_vouchers": unresolved,
		"cash_total_complete": not unresolved,
	}
