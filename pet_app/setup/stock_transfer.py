"""Explicit administrator provisioning; never runs on install or ordinary API reads."""

import frappe

from pet_app.api.stock_transfer_settings import save_setup
from pet_app.stock_transfer import access
from pet_app.stock_transfer.guards import lock
from pet_app.stock_transfer.rules import fail


def configure_company(
	company,
	transit_parent,
	quarantine_parent,
	public_app_url,
	enabled=False,
	reservation_tests_passed=False,
	transit_name="Stock Transfer Transit",
	quarantine_name="Stock Transfer Quarantine",
):
	"""Create dedicated leaves and use the company's configured stock-adjustment defaults.

	No stock documents are posted. Existing settings are returned without overwriting
	administrator choices; subsequent edits belong to the settings API.
	"""
	access.permit("Stock Transfer Settings", "create")
	access.permit("Warehouse", "create")
	access.permit("Company", doc=frappe.get_doc("Company", company))
	lock()
	existing = access.settings(company, required=False)
	if existing:
		from pet_app.api.stock_transfer_settings import get_setup

		return get_setup(company)
	frappe.db.savepoint("stock_transfer_provision")
	try:

		def leaf(parent, label):
			root = frappe.get_doc("Warehouse", parent)
			access.permit("Warehouse", doc=root)
			if root.company != company or root.disabled or not root.is_group:
				fail("Choose an enabled company warehouse group as parent.", "WAREHOUSE_MISMATCH")
			name = frappe.db.get_value("Warehouse", {"company": company, "warehouse_name": label}, "name")
			if name:
				row = access.warehouse(name, company)
				if row.parent_warehouse != parent:
					fail("An existing warehouse with this name has a different parent.")
				return row.name
			return (
				frappe.get_doc(
					dict(
						doctype="Warehouse",
						company=company,
						warehouse_name=label,
						parent_warehouse=parent,
						is_group=0,
					)
				)
				.insert()
				.name
			)

		company_doc = frappe.get_doc("Company", company)
		result = save_setup(
			company=company,
			expected_version=None,
			payload=dict(
				enabled=enabled,
				reservation_tests_passed=reservation_tests_passed,
				transit_warehouse=leaf(transit_parent, transit_name),
				quarantine_warehouses=[leaf(quarantine_parent, quarantine_name)],
				loss_expense_account=company_doc.stock_adjustment_account,
				cost_center=company_doc.cost_center,
				public_app_url=public_app_url,
			),
		)
		if not result["ok"]:
			frappe.db.rollback()
		return result
	except Exception:
		frappe.db.rollback()
		raise
