from __future__ import annotations

import frappe


LEGACY_PROPERTY_SETTERS = (
	"Patient-main-field_order",
	"Patient-main-naming_rule",
	"Patient-main-autoname",
	"Patient Encounter-status-default",
	"Patient Encounter-naming_series-options",
	"Patient Encounter-main-field_order",
)


def execute():
	remove_legacy_property_setters()
	hide_legacy_profile_patient_link()
	sync_workspace()
	frappe.clear_cache()


def remove_legacy_property_setters():
	for name in LEGACY_PROPERTY_SETTERS:
		if frappe.db.exists("Property Setter", name):
			frappe.delete_doc("Property Setter", name, ignore_permissions=True, force=True)


def hide_legacy_profile_patient_link():
	field = frappe.db.get_value(
		"DocField",
		{"parent": "Pet Medical Profile", "fieldname": "healthcare_patient"},
		"name",
	)
	if field:
		frappe.db.set_value(
			"DocField",
			field,
			{
				"label": "Legacy External Patient",
				"hidden": 1,
				"read_only": 1,
				"description": "Legacy external patient link. Pet is the clinical patient in pet-app flows.",
			},
			update_modified=False,
		)


def sync_workspace():
	try:
		from pet_app.patches.sync_alkohk_workspace import execute as sync_alkohk_workspace

		sync_alkohk_workspace()
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Pet Patient Bridge Workspace Sync Failed")
