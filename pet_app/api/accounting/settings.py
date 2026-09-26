from __future__ import annotations

import json

import frappe
from frappe import _
from pet_app.api.response import standardize_response


ACCOUNTING_ROLES = {"System Manager", "Accounts Manager", "Accounts User", "Accounting"}
SETTINGS_DOCTYPE = "Pet App Accounting Settings"


def _is_accounting_user() -> bool:
	if frappe.session.user == "Administrator":
		return True
	return bool(set(frappe.get_roles(frappe.session.user) or []) & ACCOUNTING_ROLES)


def _require_accounting_user():
	if not _is_accounting_user():
		frappe.throw(_("Not authorized."), frappe.PermissionError)


def _coerce_dict(value) -> dict:
	if value is None:
		return {}
	if isinstance(value, str):
		value = json.loads(value) if value else {}
	if hasattr(value, "as_dict"):
		value = value.as_dict()
	return dict(value or {})


def _ensure_settings_doctype():
	if not frappe.db.exists("DocType", SETTINGS_DOCTYPE):
		frappe.throw(
			_("{0} is not installed yet. Run bench migrate for pet_app.").format(SETTINGS_DOCTYPE)
		)


def _validate_company(company: str | None):
	if company and not frappe.db.exists("Company", company):
		frappe.throw(_("Company {0} does not exist.").format(frappe.bold(company)))


def _validate_mode_of_payment(mode_of_payment: str | None):
	if mode_of_payment and not frappe.db.exists("Mode of Payment", mode_of_payment):
		frappe.throw(
			_("Mode of Payment {0} does not exist.").format(frappe.bold(mode_of_payment))
		)


def _validate_cash_account(account: str | None, company: str | None, label: str):
	if not account:
		return

	row = frappe.db.get_value(
		"Account",
		account,
		["company", "is_group", "account_type"],
		as_dict=True,
	)
	if not row:
		frappe.throw(_("{0} {1} does not exist.").format(_(label), frappe.bold(account)))
	if row.is_group:
		frappe.throw(_("{0} {1} must be a ledger account.").format(_(label), frappe.bold(account)))
	if row.account_type != "Cash":
		frappe.throw(_("{0} {1} must be a Cash account.").format(_(label), frappe.bold(account)))
	if company and row.company != company:
		frappe.throw(
			_("{0} {1} does not belong to Company {2}.").format(
				_(label), frappe.bold(account), frappe.bold(company)
			)
		)


CATEGORY_FIELDS = (
	"key",
	"label_en",
	"label_ar",
	"account",
	"branch",
	"cost_center",
	"company",
	"enabled",
)


def _category_payload(row) -> dict:
	return {field: row.get(field) for field in CATEGORY_FIELDS}


def _settings_payload(doc) -> dict:
	return {
		"treasury_cash_account": doc.get("treasury_cash_account"),
		"default_company": doc.get("default_company"),
		"default_cash_mode_of_payment": doc.get("default_cash_mode_of_payment"),
		# Returned on read as well as accepted on write, so a client that reads, edits one
		# field and writes back cannot silently blank the table.
		"expense_categories": [
			_category_payload(row) for row in (doc.get("expense_categories") or [])
		],
	}


def _set_expense_categories(doc, rows):
	"""Replace the category table wholesale.

	A full replace rather than a merge: the client edits the grid as a unit, and a merge
	would leave no way to delete a row. Row-level validation lives in the controller, which
	runs on save either way - this only decides which fields a caller may set.
	"""
	if isinstance(rows, str):
		rows = json.loads(rows) if rows else []
	doc.set("expense_categories", [])
	for row in rows or []:
		row = dict(row or {})
		values = {field: row.get(field) for field in CATEGORY_FIELDS}
		# An omitted `enabled` means "on". Passing None through would append a row the
		# doctype default never gets to fill in, and a category nobody disabled would
		# quietly stop being offered at the till.
		if row.get("enabled") is None:
			values["enabled"] = 1
		doc.append("expense_categories", values)


@frappe.whitelist()
@standardize_response
def get_accounting_settings():
	_require_accounting_user()
	_ensure_settings_doctype()
	doc = frappe.get_single(SETTINGS_DOCTYPE)
	return _settings_payload(doc)


@frappe.whitelist()
@standardize_response
def update_accounting_settings(data=None, **kwargs):
	_require_accounting_user()
	_ensure_settings_doctype()

	payload = _coerce_dict(data)
	payload.update({key: value for key, value in kwargs.items() if value is not None})

	doc = frappe.get_single(SETTINGS_DOCTYPE)
	allowed_fields = (
		"treasury_cash_account",
		"default_company",
		"default_cash_mode_of_payment",
	)

	for fieldname in allowed_fields:
		if fieldname in payload:
			doc.set(fieldname, payload.get(fieldname))

	if "expense_categories" in payload:
		_set_expense_categories(doc, payload.get("expense_categories"))

	_validate_company(doc.default_company)
	_validate_mode_of_payment(doc.default_cash_mode_of_payment)
	_validate_cash_account(doc.treasury_cash_account, doc.default_company, "Treasury Cash Account")

	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)

	return {
		"success": True,
		"settings": _settings_payload(doc),
	}
