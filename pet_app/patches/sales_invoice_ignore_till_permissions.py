"""Sales Invoices are scoped by Branch, not by the till that rang them up.

Sales Invoice.pos_profile (ERPNext) and custom_pos_profile (the cashier stamp) record which
till took the sale. As ordinary Link fields they made Frappe hide an invoice from anyone
holding a POS Profile User Permission for a different till, so a cashier saw only their
own till's sales plus the till-less clinic drafts: the POS "Recent Purchases" card came
back empty for a customer whose purchases were all rung up at the next till, and the list
API returned `[]` rather than an error.

Branch, warehouse and every other link stay enforced, so a main cashier still sees no
hotel invoice. Same treatment Sales Order got in driver_orders_ignore_till_permissions.

Only the read filter is relaxed. Every pet_app writer sets pos_profile explicitly
(api/pos.py), so losing Frappe's "one User Permission becomes the default" fill for this
field changes nothing that is saved.
"""
import frappe
from frappe.custom.doctype.property_setter.property_setter import make_property_setter


def execute():
	# Standard ERPNext field: needs a Property Setter.
	if frappe.get_meta("Sales Invoice").has_field("pos_profile"):
		make_property_setter(
			"Sales Invoice", "pos_profile", "ignore_user_permissions", "1", "Check",
			validate_fields_for_doctype=False,
		)

	# pet_app Custom Field: set it on the field itself.
	if frappe.db.exists("Custom Field", "Sales Invoice-custom_pos_profile"):
		frappe.db.set_value("Custom Field", "Sales Invoice-custom_pos_profile", "ignore_user_permissions", 1)

	frappe.clear_cache(doctype="Sales Invoice")
