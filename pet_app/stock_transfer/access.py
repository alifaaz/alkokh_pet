"""Frappe DocPerm and User Permission checks; page visibility grants no authority."""

import frappe

from pet_app.stock_transfer.rules import TransferError, fail, public_url

ORDER = "Stock Transfer Order"
ACTIONS = (
	"create_transfer",
	"update_transfer",
	"start_preparation",
	"save_preparation",
	"approve_reduction",
	"dispatch_transfer",
	"update_shipment",
	"record_arrival",
	"receive_transfer",
	"resolve_exception",
	"cancel_transfer",
)
STATES = {
	"update_transfer": "Ordered",
	"start_preparation": "Ordered",
	"save_preparation": "Preparing",
	"approve_reduction": "Preparing",
	"dispatch_transfer": "Preparing",
	"update_shipment": "Transferring",
	"record_arrival": "Transferring",
	"receive_transfer": "Transferring",
	"resolve_exception": "Transferring",
}
MANAGER = ("approve_reduction", "resolve_exception", "cancel_transfer")
POSTING = ("dispatch_transfer", "receive_transfer", "resolve_exception")


def permit(doctype, ptype="read", doc=None):
	if not frappe.has_permission(doctype, ptype=ptype, doc=doc):
		fail(f"You do not have {ptype} access to {doctype}.", "PERMISSION_DENIED")


def warehouse(name, company=None, scoped=True):
	if not name or not frappe.db.exists("Warehouse", name):
		fail("Select an existing warehouse.", "WAREHOUSE_MISMATCH")
	doc = frappe.get_doc("Warehouse", name)
	if doc.disabled or doc.is_group or (company and doc.company != company):
		fail(f"{name} must be an enabled leaf warehouse in this company.", "WAREHOUSE_MISMATCH")
	if scoped:
		permit("Warehouse", doc=doc)
		from pet_app.api.permissions import require_restriction_value

		require_restriction_value("warehouse", name)
	return doc


def settings(company, required=True):
	name = frappe.db.get_value("Stock Transfer Settings", {"company": company}, "name")
	if not name:
		if required:
			fail("Stock transfers are disabled for this company.", "STOCK_TRANSFER_DISABLED")
		return None
	return frappe.get_doc("Stock Transfer Settings", name)


def prerequisites(cfg):
	if not cfg or not cfg.enabled or not cfg.reservation_tests_passed:
		fail(
			"Stock transfers are disabled pending setup and reservation verification.",
			"STOCK_TRANSFER_DISABLED",
		)
	from pet_app.stock_transfer.configuration import configuration_issues

	issues = configuration_issues(cfg)
	if issues:
		fail(issues[0]["message"], issues[0]["code"])
	return cfg


def special_warehouses():
	names = set(frappe.get_all("Stock Transfer Settings", pluck="transit_warehouse"))
	names.update(frappe.get_all("Stock Transfer Quarantine", pluck="warehouse"))
	return names


def regular(name, company=None):
	doc = warehouse(name, company)
	if name in special_warehouses():
		fail("Transit and quarantine cannot be selected as regular warehouses.", "WAREHOUSE_MISMATCH")
	return doc


def scoped_warehouse(name, user=None):
	try:
		if not frappe.has_permission("Warehouse", doc=frappe.get_doc("Warehouse", name), user=user):
			return False
		from pet_app.api.permissions import require_restriction_value

		require_restriction_value("warehouse", name, user=user)
		return True
	except (TransferError, frappe.PermissionError):
		return False


def read_order(doc):
	permit(ORDER, doc=doc)
	if not (scoped_warehouse(doc.source_warehouse) or scoped_warehouse(doc.target_warehouse)):
		fail("This transfer is outside your warehouse scope.", "PERMISSION_DENIED")


def document_permission(doc, user=None, ptype=None):
	# Used by generic readers as well as the workflow API. Native DocPerm remains required.
	return scoped_warehouse(doc.source_warehouse, user) or scoped_warehouse(doc.target_warehouse, user)


def query_conditions(user=None):
	user = user or frappe.session.user
	if user == "Administrator":
		return ""
	from pet_app.api.permissions import filter_restricted_values

	names = frappe.get_list("Warehouse", pluck="name", limit_page_length=0, user=user)
	names = filter_restricted_values("warehouse", names, user=user)
	if not names:
		return "1=0"
	values = ",".join(frappe.db.escape(n) for n in names)
	return f"(`tabStock Transfer Order`.source_warehouse in ({values}) or `tabStock Transfer Order`.target_warehouse in ({values}))"


def authorize(action, doc=None):
	ptype = (
		"create"
		if action == "create_transfer"
		else "cancel"
		if action == "cancel_transfer"
		else "submit"
		if action in MANAGER
		else "write"
	)
	permit(ORDER, ptype, doc)
	if doc:
		read_order(doc)
		scope = (
			doc.target_warehouse
			if action in ("record_arrival", "receive_transfer", "resolve_exception")
			else doc.source_warehouse
		)
		warehouse(scope, doc.company)
	if action in POSTING:
		permit("Stock Entry", "create")
		permit("Stock Entry", "submit")
	if doc:
		if action == "cancel_transfer":
			if doc.status not in ("Ordered", "Preparing"):
				fail("Dispatched transfers cannot be cancelled.", "INVALID_STATE")
		elif doc.status != STATES.get(action):
			fail(f"{action} is unavailable while this transfer is {doc.status}.", "INVALID_STATE")
		if action == "dispatch_transfer" and not doc.ready:
			fail("Complete preparation and mark the order ready before dispatch.", "INVALID_STATE")
		if action == "record_arrival" and doc.arrived_at:
			fail("Arrival has already been recorded.", "INVALID_STATE")


def allowed(doc=None):
	from pet_app.stock_transfer.rules import TransferError

	result = []
	candidates = ACTIONS if doc is None else ACTIONS[1:]
	for action in candidates:
		try:
			authorize(action, doc)
			if doc:
				prerequisites(settings(doc.company))
			result.append(action)
		except (TransferError, frappe.PermissionError):
			continue
	return result
