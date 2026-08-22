from __future__ import annotations

import frappe

# The eighteen Custom Fields erpnext.regional.united_arab_emirates.setup.make_custom_fields
# installs, in the order that setup defines them.
UAE_FIELDS = (
	("Address", "emirate"),
	("Sales Invoice", "vat_section"),
	("Sales Invoice", "permit_no"),
	("Sales Invoice", "company_trn"),
	("Sales Invoice", "customer_name_in_arabic"),
	("Sales Invoice", "vat_emirate"),
	("Sales Invoice", "tourist_tax_return"),
	("Sales Invoice", "exempt_from_sales_tax"),
	("Sales Order", "vat_section"),
	("Sales Order", "permit_no"),
	("Sales Order", "company_trn"),
	("Sales Order", "customer_name_in_arabic"),
	("Sales Order", "vat_emirate"),
	("Sales Order", "tourist_tax_return"),
	("Sales Order", "exempt_from_sales_tax"),
	("Item", "tax_code"),
	("Item", "is_zero_rated"),
	("Item", "is_exempt"),
)

# Fieldtypes that carry no database column, so "is it populated" has no meaning for them.
LAYOUT_FIELDTYPES = {"Section Break", "Column Break", "Tab Break", "HTML", "Heading"}


def execute():
	"""Removes the UAE VAT custom fields this app should never have been installing.

	They are ERPNext's, from erpnext/regional/united_arab_emirates/setup.py, and ERPNext
	gates them twice: the v13 patch returns early unless some Company has country
	"United Arab Emirates", and install_country_fixtures() resolves
	erpnext.regional.{country}.setup - which for Iraq does not exist and raises ImportError.
	Neither gate can pass here. Every Company on this site is Iraq.

	How they arrived is the part worth recording. ERPNext's own test records include
	"_Test Company UAE VAT" with country "United Arab Emirates"; creating it runs the UAE
	regional setup. A test run against this database installed the eighteen fields, a
	later pass normalised every company to Iraq and left them, and then bench
	export-fixtures - whose Custom Field filter selected by DOCTYPE, not by module -
	swept them into pet_app/fixtures/custom_field.json, where they were committed. From
	that point this app, not ERPNext, was installing UAE VAT fields on every site.

	Deleting the rows alone would fix nothing: sync_fixtures reinstates them on the next
	migrate, which is exactly what happened once already. The fixture entries were removed
	first and hooks.py now selects Custom Fields by name, so this patch is the second half
	of that change and is a no-op without it.

	Refuses rather than destroys. If any site has data in one of these - a real UAE
	deployment, or a shared database - the field is skipped and logged, not dropped. On
	this site all eighteen are empty; the two Section Breaks hold no column at all.

	Idempotent: a field already gone is skipped.
	"""
	skipped = []
	removed = []

	for doctype, fieldname in UAE_FIELDS:
		name = frappe.db.get_value("Custom Field", {"dt": doctype, "fieldname": fieldname}, "name")
		if not name:
			continue

		populated = _populated_count(doctype, fieldname)
		if populated:
			skipped.append(f"{doctype}.{fieldname} ({populated} rows)")
			continue

		frappe.delete_doc("Custom Field", name, force=True, ignore_permissions=True)
		removed.append(name)

	for doctype in {doctype for doctype, _ in UAE_FIELDS}:
		frappe.clear_cache(doctype=doctype)

	if removed:
		frappe.logger().info(f"[UAE CLEANUP] removed {len(removed)} custom fields: {', '.join(removed)}")
	if skipped:
		# Deliberately not a throw. A site with UAE data should keep its fields and its
		# migrate should still succeed; the operator needs to see this and decide.
		frappe.logger().warning(
			f"[UAE CLEANUP] kept {len(skipped)} field(s) that hold data: {', '.join(skipped)}"
		)


def _populated_count(doctype: str, fieldname: str) -> int:
	"""Rows where the field holds something. 0 for a field with no column."""
	meta = frappe.get_meta(doctype)
	field = meta.get_field(fieldname)
	if not field or field.fieldtype in LAYOUT_FIELDTYPES:
		return 0
	if not frappe.db.has_column(doctype, fieldname):
		return 0

	table = f"tab{doctype}"
	if field.fieldtype in ("Check", "Int", "Float", "Currency", "Percent"):
		condition = f"IFNULL(`{fieldname}`, 0) != 0"
	else:
		condition = f"IFNULL(`{fieldname}`, '') != ''"

	return frappe.db.sql(f"SELECT COUNT(*) FROM `{table}` WHERE {condition}")[0][0]
