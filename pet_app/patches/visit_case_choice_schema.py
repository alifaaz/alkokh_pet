from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	ensure_visit_case_choice_fields()


def ensure_visit_case_choice_fields():
	create_custom_fields(
		{
			"Vet Visit": [
				{
					"fieldname": "doctor_case_choice",
					"fieldtype": "Select",
					"label": "Doctor Case Choice",
					"options": "\nwellness\ncontinue_case\nnew_case",
					"insert_after": "care_episode",
					"in_standard_filter": 1,
				},
				{
					"fieldname": "case_choice_by",
					"fieldtype": "Link",
					"label": "Case Choice By",
					"options": "User",
					"insert_after": "doctor_case_choice",
					"read_only": 1,
				},
				{
					"fieldname": "case_choice_at",
					"fieldtype": "Datetime",
					"label": "Case Choice At",
					"insert_after": "case_choice_by",
					"read_only": 1,
				},
				{
					"fieldname": "case_choice_note",
					"fieldtype": "Small Text",
					"label": "Case Choice Note",
					"insert_after": "case_choice_at",
				},
			]
		},
		update=True,
	)
	frappe.clear_cache()
