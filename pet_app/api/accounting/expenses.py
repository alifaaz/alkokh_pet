"""Expense categories a cashier may book cash against at the till.

Configured as a table on `Pet App Accounting Settings` rather than derived from the
chart of accounts, because the two things the till actually needs are the two the chart
of accounts cannot carry: an Arabic label, and the clinic the expense belongs to. Both
are answered here so a cashier never picks a GL account.

Falls back to deriving categories from leaf, enabled, root-type-Expense accounts when
nothing is configured. That is exactly what the client did before this endpoint existed,
so an unconfigured site behaves as it did yesterday rather than showing an empty picker.

Deliberately imports nothing from `cashier`: `cashier.record_cashier_expense` resolves
categories through this module, and the dependency has to stay one-directional.
"""

from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.utils import cint, cstr

from pet_app.api.response import standardize_response
from pet_app.utils.branch_warehouse import branch_warehouse


SETTINGS_DOCTYPE = "Pet App Accounting Settings"


def _slugify(value: str) -> str:
	"""Derive a stable key from an account name for the fallback path.

	Only used for categories that were never configured, so the key it produces is not a
	promise: configure the row and the admin's own key wins.
	"""
	slug = re.sub(r"[^a-z0-9]+", "_", cstr(value).strip().lower()).strip("_")
	return slug or "expense"


def _settings():
	if not frappe.db.exists("DocType", SETTINGS_DOCTYPE):
		return frappe._dict()
	return frappe.get_single(SETTINGS_DOCTYPE)


def _default_company(preferred: str | None = None) -> str | None:
	if preferred:
		return preferred
	settings = _settings()
	if settings.get("default_company"):
		return settings.get("default_company")
	return (
		frappe.defaults.get_user_default("Company")
		or frappe.defaults.get_global_default("company")
		or None
	)


def _row_payload(row) -> dict:
	return {
		"key": cstr(row.get("key")).strip(),
		"label_en": cstr(row.get("label_en")).strip() or cstr(row.get("key")).strip(),
		"label_ar": cstr(row.get("label_ar")).strip() or None,
		"account": row.get("account") or None,
		"branch": row.get("branch") or None,
		"cost_center": row.get("cost_center") or None,
	}


def _configured_categories(company: str | None, branch: str | None) -> list[dict]:
	settings = _settings()
	rows = settings.get("expense_categories") or []

	out = []
	for row in rows:
		if not cint(row.get("enabled")):
			continue
		row_company = row.get("company")
		if company and row_company and row_company != company:
			continue
		row_branch = row.get("branch")
		# A category with no branch belongs to no single clinic, so it is offered whatever
		# branch was asked for. Filtering it out would hide the shared categories from
		# every branch-scoped caller, which is the opposite of what a blank means.
		if branch and row_branch and row_branch != branch:
			continue
		out.append(_row_payload(row))
	return out


def _derived_categories(company: str | None) -> list[dict]:
	"""The chart of accounts, shaped like a category list.

	No Arabic label and no branch - if those mattered, someone would have configured the
	table. Reporting them as None rather than guessing keeps the client's own fallback
	and this one indistinguishable.
	"""
	filters = {"is_group": 0, "root_type": "Expense"}
	if company:
		filters["company"] = company
	if frappe.get_meta("Account").has_field("disabled"):
		filters["disabled"] = 0

	accounts = frappe.get_all(
		"Account",
		filters=filters,
		fields=["name", "account_name"],
		order_by="account_name asc",
		ignore_permissions=True,
	)

	seen: set[str] = set()
	out = []
	for account in accounts:
		key = _slugify(account.account_name or account.name)
		# Two accounts can slug to the same key across companies; suffix rather than drop,
		# because a missing category is a category the cashier cannot book against.
		if key in seen:
			key = f"{key}_{len(seen)}"
		seen.add(key)
		out.append(
			{
				"key": key,
				"label_en": account.account_name or account.name,
				"label_ar": None,
				"account": account.name,
				"branch": None,
				"cost_center": None,
			}
		)
	return out


def resolve_category(category: str | None, company: str | None = None) -> dict | None:
	"""Look one category up by key, configured rows first.

	Returns None for an unknown key rather than throwing, so the caller decides whether an
	unrecognised category is fatal. `record_cashier_expense` treats it as advisory: the
	account it was given still has to stand on its own.
	"""
	key = cstr(category).strip()
	if not key:
		return None

	for row in _configured_categories(company, None):
		if row["key"] == key:
			return row

	for row in _derived_categories(company):
		if row["key"] == key:
			return row
	return None


@frappe.whitelist()
@standardize_response
def get_expense_categories(company=None, branch=None):
	company = _default_company(cstr(company).strip() or None)
	branch = cstr(branch).strip() or None

	if branch and not frappe.db.exists("Branch", branch):
		frappe.throw(_("Branch {0} does not exist.").format(frappe.bold(branch)))

	categories = _configured_categories(company, branch)
	if not categories:
		categories = _derived_categories(company)
	warehouses = {}
	for category in categories:
		category_branch = category.get("branch") or branch
		if category_branch and category_branch not in warehouses:
			warehouses[category_branch] = branch_warehouse(category_branch)
		category["default_warehouse"] = warehouses.get(category_branch)
	return categories


def categories_by_account(company: str | None = None) -> dict[str, dict]:
	"""Configured categories keyed by the account they book to.

	The inverse of `resolve_category`, for reporting paths that start from a GL account
	rather than from a picker: a ledger row knows the account it hit and nothing else.

	Configured rows only. `_derived_categories` mints keys by slugifying account names and
	suffixes collisions by position, so a derived key is stable only within one call -
	fine for filling a picker, wrong to stamp on a report row somebody may later filter
	on. An unconfigured account comes back absent, and the caller falls back to the
	account name for a label.

	First row wins when two categories point at one account, matching `resolve_category`'s
	own first-match-wins order, so the two never disagree about which category an account
	belongs to.
	"""
	index: dict[str, dict] = {}
	for row in _configured_categories(company, None):
		if row.get("account"):
			index.setdefault(row["account"], row)
	return index
