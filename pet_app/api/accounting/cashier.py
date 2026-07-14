from __future__ import annotations

import json
from collections.abc import Iterable

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, getdate, nowdate

from erpnext.accounts.party import get_party_account
from pet_app.api.permissions import (
	filter_restricted_values,
	require_doctype_permission,
	require_restriction_value,
)
from pet_app.api.response import standardize_response


ACCOUNTING_ROLES = {"System Manager", "Accounts Manager", "Accounts User", "Accounting", "POS"}
ACCOUNTING_PAGE_ROLES = {"Cashiers Page", "POS Page"}


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
			return {"data": [], "total": 0}
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
	data = []
	for name in profile_names:
		profile = frappe.get_doc("POS Profile", name)
		if search_text and search_text not in cstr(profile.name).lower():
			continue
		data.append(_profile_payload(profile, include_balance=False))

	return {"data": data, "total": len(data)}


@frappe.whitelist()
@standardize_response
def get_cashier_profile_detail(pos_profile=None, profile=None, cashier_profile=None):
	doc = _get_authorized_profile(pos_profile or profile or cashier_profile)
	return _profile_payload(doc)


@frappe.whitelist()
@standardize_response
def get_cashier_runtime_defaults(pos_profile=None, company=None):
	settings = _settings_payload()
	resolved_company = _get_default_company(company)
	profiles = list_cashier_profiles_for_user.__wrapped__(company=resolved_company).get("data", [])

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
		active_profile = get_cashier_profile_detail.__wrapped__(selected_profile)

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
		"last_settlement": dict(last_settlement) if last_settlement else None,
	}


@frappe.whitelist()
@standardize_response
def settle_cashier_to_treasury(
	pos_profile=None, profile=None, cashier_profile=None, amount=None, posting_date=None, submit=1, **kwargs
):
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
		payload.get("transfer_amount")
		if payload.get("transfer_amount") is not None
		else amount if amount is not None else expected_cash
	)
	counted_cash = flt(
		payload.get("counted_cash")
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
