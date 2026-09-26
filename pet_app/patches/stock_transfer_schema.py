"""Install contract v1 without enabling it or migrating historical Stock Entries."""

import json

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

DOCTYPES = (
	"Stock Transfer Line",
	"Stock Transfer Quarantine",
	"Stock Transfer Settings",
	"Stock Transfer Order",
	"Stock Transfer Reservation",
	"Stock Transfer Allocation",
	"Stock Transfer Event",
	"Stock Transfer Request",
)


def execute():
	for name in DOCTYPES:
		frappe.reload_doc("pet_app", "doctype", frappe.scrub(name))
	create_custom_fields(
		{
			"Stock Entry": [
				dict(
					fieldname="custom_stock_transfer_order",
					label="Stock Transfer Order",
					fieldtype="Link",
					options="Stock Transfer Order",
					read_only=1,
					no_copy=1,
				),
				dict(
					fieldname="custom_stock_transfer_kind",
					label="Stock Transfer Movement",
					fieldtype="Select",
					options="\ndispatch\nreceipt\ndamage\nloss",
					read_only=1,
					no_copy=1,
				),
			]
		},
		update=True,
		ignore_validate=True,
	)
	frappe.db.add_index("Stock Transfer Reservation", ["item_code", "warehouse"])
	frappe.db.add_unique("Stock Transfer Reservation", ["serial_no"], "uniq_transfer_reserved_serial")
	frappe.db.add_index("Stock Transfer Reservation", ["transfer", "line_id"])
	frappe.db.add_index("Stock Transfer Allocation", ["transfer", "line_id", "kind"])
	frappe.db.add_index("Stock Transfer Allocation", ["shipment_allocation"])
	frappe.db.add_index("Stock Transfer Event", ["transfer"])
	frappe.db.add_unique("Stock Transfer Line", ["line_id"], "uniq_stock_transfer_line_id")
	settings = frappe.get_single("Pet App Access Settings")
	if not any(r.page_key == "page.warehouse.stock_transfers" for r in settings.pages):
		settings.append(
			"pages",
			dict(
				page_key="page.warehouse.stock_transfers",
				label="Stock Transfers",
				module_key="module.warehouse",
				page_doctype="Stock Transfer Order",
				enabled=1,
				paths_json=json.dumps(["/warehouse/stock-transfers"]),
				path_prefixes_json=json.dumps(["/warehouse/stock-transfers/"]),
				route_names_json="[]",
				roles_json=json.dumps(["Stock User", "Stock Manager", "System Manager"]),
			),
		)
		settings.save(ignore_permissions=True)
	frappe.clear_cache()
