from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	create_custom_fields(
		{
			"Brand": [
				{
					"fieldname": "arabic_name",
					"fieldtype": "Data",
					"label": "Arabic Name",
					"insert_after": "brand",
					"translatable": 0,
					"permlevel": 0,
					"hidden": 0,
				},
			]
		},
		update=True,
	)
	frappe.clear_cache(doctype="Brand")
