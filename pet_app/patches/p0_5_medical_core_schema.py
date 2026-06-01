from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	ensure_medical_core_custom_fields()


def ensure_medical_core_custom_fields():
	create_custom_fields(
		{
			"Vet Visit": [
				{
					"fieldname": "care_episode",
					"fieldtype": "Link",
					"label": "Care Episode",
					"options": "Pet Care Episode",
					"insert_after": "case_sheet",
					"read_only": 1,
				},
				{
					"fieldname": "outcome",
					"fieldtype": "Select",
					"label": "Outcome",
					"options": "\nRecovered\nImproved\nStable\nReferred\nFollow-up Required\nDeath\nEuthanasia\nUnknown",
					"insert_after": "status",
				},
				{
					"fieldname": "death_during_visit",
					"fieldtype": "Check",
					"label": "Death During Visit",
					"insert_after": "outcome",
				},
				{
					"fieldname": "death_record",
					"fieldtype": "Link",
					"label": "Death Record",
					"options": "Pet Death Record",
					"insert_after": "death_during_visit",
					"read_only": 1,
				},
			],
			"Appointment": [
				{
					"fieldname": "custom_care_plan_item",
					"fieldtype": "Link",
					"label": "Care Plan Item",
					"options": "Pet Care Plan Item",
					"insert_after": "custom_follow_up_of_visit_id",
					"read_only": 1,
				},
				{
					"fieldname": "custom_cancelled_due_to_death",
					"fieldtype": "Check",
					"label": "Cancelled Due To Death",
					"insert_after": "custom_converted_at",
					"default": 0,
					"read_only": 1,
				},
				{
					"fieldname": "custom_death_record",
					"fieldtype": "Link",
					"label": "Death Record",
					"options": "Pet Death Record",
					"insert_after": "custom_cancelled_due_to_death",
					"read_only": 1,
				},
			],
			"Pet": [
				{
					"fieldname": "is_deceased",
					"fieldtype": "Check",
					"label": "Is Deceased",
					"insert_after": "pet_status",
					"default": 0,
				},
				{
					"fieldname": "death_date",
					"fieldtype": "Date",
					"label": "Death Date",
					"insert_after": "is_deceased",
				},
				{
					"fieldname": "death_record",
					"fieldtype": "Link",
					"label": "Death Record",
					"options": "Pet Death Record",
					"insert_after": "death_date",
					"read_only": 1,
				},
			],
		},
		update=True,
	)
	frappe.clear_cache()
