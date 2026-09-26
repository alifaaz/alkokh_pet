"""Company-scoped setup API. All authority comes from native Frappe permissions."""

import frappe

from pet_app.stock_transfer import access
from pet_app.stock_transfer import configuration as config
from pet_app.stock_transfer.guards import lock
from pet_app.stock_transfer.rules import fail
from pet_app.stock_transfer.service import envelope


def company_access(company):
	if not isinstance(company, str) or not frappe.db.exists("Company", company):
		fail("Select an existing company.")
	access.permit("Company", doc=frappe.get_doc("Company", company))


def settings_doc(company):
	name = frappe.db.get_value(config.DOCTYPE, {"company": company}, "name")
	if name:
		return frappe.get_doc(config.DOCTYPE, name, for_update=True)
	doc = frappe.new_doc(config.DOCTYPE)
	doc.company = company
	return doc


def response(doc):
	write = frappe.has_permission(
		config.DOCTYPE, "create" if doc.is_new() else "write", doc=None if doc.is_new() else doc
	)
	return dict(
		company=doc.company,
		settings=config.values(doc),
		version=None if doc.is_new() else str(doc.modified),
		permissions={"read": True, "write": bool(write)},
		readiness=config.readiness(doc),
	)


@frappe.whitelist(methods=["GET"])
@envelope
def get_status(company):
	"""Safe diagnostics for ordinary stock users; exposes no configured account IDs."""
	access.permit(access.ORDER, "read")
	company_access(company)
	lock()
	doc = settings_doc(company)
	state = config.readiness(doc)
	return dict(
		company=company,
		configured=not doc.is_new(),
		**state,
		can_configure=bool(
			frappe.has_permission(config.DOCTYPE, "read")
			and frappe.has_permission(
				config.DOCTYPE, "create" if doc.is_new() else "write", doc=None if doc.is_new() else doc
			)
		),
	)


@frappe.whitelist(methods=["GET"])
@envelope
def get_setup(company=None):
	access.permit(config.DOCTYPE, "read")
	companies = frappe.get_list(
		"Company", fields=["name as id", "company_name as label"], order_by="name asc", limit_page_length=0
	)
	if not company and len(companies) == 1:
		company = companies[0].id
	if not company:
		return dict(
			company=None,
			companies=companies,
			settings=None,
			version=None,
			permissions={"read": True, "write": False},
			readiness=None,
			options={"warehouses": [], "expense_accounts": [], "cost_centers": []},
		)
	company_access(company)
	lock()
	doc = settings_doc(company)
	if not doc.is_new():
		access.permit(config.DOCTYPE, "read", doc)
	options = {}
	for key, doctype, fields, filters in (
		(
			"warehouses",
			"Warehouse",
			["name as id", "warehouse_name as label"],
			{"company": company, "disabled": 0, "is_group": 0},
		),
		(
			"expense_accounts",
			"Account",
			["name as id", "account_name as label"],
			{"company": company, "disabled": 0, "is_group": 0, "root_type": "Expense"},
		),
		(
			"cost_centers",
			"Cost Center",
			["name as id", "cost_center_name as label"],
			{"company": company, "disabled": 0, "is_group": 0},
		),
	):
		options[key] = (
			frappe.get_list(doctype, fields=fields, filters=filters, order_by="name asc", limit_page_length=0)
			if frappe.has_permission(doctype, "read")
			else []
		)
		if doctype == "Warehouse":
			options[key] = [r for r in options[key] if access.scoped_warehouse(r.id)]
	return dict(**response(doc), companies=companies, options=options)


@frappe.whitelist(methods=["POST"])
@envelope
def save_setup(company=None, expected_version=None, payload=None):
	company_access(company)
	lock()
	doc = settings_doc(company)
	access.permit(config.DOCTYPE, "create" if doc.is_new() else "write", None if doc.is_new() else doc)
	access.permit(config.DOCTYPE, "read", None if doc.is_new() else doc)
	if not isinstance(payload, dict) or set(payload) != set(config.FIELDS):
		fail("Provide the complete settings snapshot with exactly the documented fields.")
	for field in ("enabled", "reservation_tests_passed"):
		if not isinstance(payload[field], bool):
			fail(f"{field} must be a JSON boolean.")
	for field in ("transit_warehouse", "loss_expense_account", "cost_center", "public_app_url"):
		if payload[field] is not None and not isinstance(payload[field], str):
			fail(f"{field} must be text or null.")
	quarantine = payload["quarantine_warehouses"]
	if not isinstance(quarantine, list) or any(not isinstance(q, str) or not q for q in quarantine):
		fail("quarantine_warehouses must be an array of warehouse IDs.")
	version = None if doc.is_new() else str(doc.modified)
	# Settings have no stock effects: on conflict the client reloads and compares the snapshot.
	if expected_version != version:
		fail("Settings changed. Reload them before saving.", "VERSION_CONFLICT")
	for field in config.FIELDS:
		if field != "quarantine_warehouses":
			doc.set(field, payload[field])
	doc.set("quarantine_warehouses", [{"warehouse": q} for q in quarantine])
	doc.load_doc_before_save()
	config.validate_document(doc, scoped=True)
	doc.save()  # Native permission checks also apply to direct Desk saves.
	return response(doc)
