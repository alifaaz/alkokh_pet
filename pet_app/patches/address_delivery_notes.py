from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

FIELDNAME = "custom_notes"


def execute():
	"""A delivery note on Address, deliberately separate from address_line2.

	The next person will ask why this is not just more text in address_line2. It is
	because address_line2 is part of the *printed* address: it goes on the label, it is
	concatenated into address_display, and it renders anywhere ERPNext shows an Address
	on an invoice or a delivery note. Anything put there becomes part of the postal
	address of record.

	This field is the opposite of that. It is a free-text instruction for whoever
	physically makes the delivery - "gate code 4471", "call on arrival, dog in the yard",
	"third floor, lift is out" - which is correct to show a driver and wrong to print on
	a document. print_hide = 1 enforces that split rather than relying on convention.

	Small Text mirrors address_line2's own fieldtype: multi-line, no rich text, no length
	cap anyone will hit. Optional - most addresses will never carry one.

	Idempotent: create_custom_fields(update=True) is a no-op on a site that already has
	the field, so this is safe to re-run.
	"""
	create_custom_fields(
		{
			"Address": [
				{
					"fieldname": FIELDNAME,
					"label": "Delivery Notes",
					"fieldtype": "Small Text",
					"insert_after": "address_line2",
					"description": (
						"Instructions for the person delivering. Not part of the printed "
						"address - use Address Line 2 for that."
					),
					"translatable": 0,
					"permlevel": 0,
					"reqd": 0,
					"hidden": 0,
					"print_hide": 1,
					"module": "Pet App",
				},
			]
		},
		update=True,
	)
	frappe.clear_cache(doctype="Address")
