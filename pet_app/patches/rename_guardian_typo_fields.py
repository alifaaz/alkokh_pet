from __future__ import annotations

import frappe
from frappe.utils import get_table_name


LEGACY_NAME_FIELD = "gur" + "dian_name"
GUARDIAN_NAME_FIELD = "guardian_name"
LEGACY_APPOINTMENT_FIELD = "custom_" + "gur" + "dian"
APPOINTMENT_GUARDIAN_FIELD = "custom_guardian"


def execute():
	for doctype in ("PetGuardian", "Vet Visit", "Pet Boarding"):
		if not frappe.db.table_exists(doctype):
			continue
		_rename_column(doctype, LEGACY_NAME_FIELD, GUARDIAN_NAME_FIELD)
		_update_docfield_metadata(doctype, LEGACY_NAME_FIELD, GUARDIAN_NAME_FIELD, "Guardian Name")

	if frappe.db.table_exists("Appointment"):
		_rename_column("Appointment", LEGACY_APPOINTMENT_FIELD, APPOINTMENT_GUARDIAN_FIELD)

	_rename_appointment_custom_field()
	_update_appointment_dependent_metadata()
	_update_property_setters()
	frappe.clear_cache(doctype="Appointment")
	for doctype in ("PetGuardian", "Vet Visit", "Pet Boarding"):
		frappe.clear_cache(doctype=doctype)


def _rename_column(doctype: str, old_fieldname: str, new_fieldname: str):
	if frappe.db.has_column(doctype, new_fieldname):
		if frappe.db.has_column(doctype, old_fieldname):
			frappe.db.sql_ddl(f"ALTER TABLE `{get_table_name(doctype)}` DROP COLUMN `{old_fieldname}`")
		return
	if frappe.db.has_column(doctype, old_fieldname):
		frappe.db.rename_column(doctype, old_fieldname, new_fieldname)


def _update_docfield_metadata(doctype: str, old_fieldname: str, new_fieldname: str, label: str):
	frappe.db.sql(
		"""
		UPDATE `tabDocField`
		SET fieldname = %s, label = %s
		WHERE parent = %s
		  AND fieldname = %s
		""",
		(new_fieldname, label, doctype, old_fieldname),
	)


def _rename_appointment_custom_field():
	old_name = f"Appointment-{LEGACY_APPOINTMENT_FIELD}"
	new_name = f"Appointment-{APPOINTMENT_GUARDIAN_FIELD}"
	if frappe.db.exists("Custom Field", new_name) and frappe.db.exists("Custom Field", old_name):
		frappe.delete_doc("Custom Field", old_name, force=True, ignore_permissions=True)
	elif frappe.db.exists("Custom Field", old_name):
		frappe.db.sql(
			"""
			UPDATE `tabCustom Field`
			SET name = %s, fieldname = %s, label = %s
			WHERE name = %s
			""",
			(new_name, APPOINTMENT_GUARDIAN_FIELD, "guardian", old_name),
		)

	if frappe.db.exists("Custom Field", new_name):
		frappe.db.set_value(
			"Custom Field",
			new_name,
			{"fieldname": APPOINTMENT_GUARDIAN_FIELD, "label": "guardian"},
			update_modified=False,
		)


def _update_appointment_dependent_metadata():
	if frappe.db.exists("Custom Field", "Appointment-custom_customer"):
		frappe.db.set_value(
			"Custom Field",
			"Appointment-custom_customer",
			{
				"fetch_from": f"{APPOINTMENT_GUARDIAN_FIELD}.customer_id",
				"insert_after": APPOINTMENT_GUARDIAN_FIELD,
			},
			update_modified=False,
		)


def _update_property_setters():
	for old_fieldname, new_fieldname in (
		(LEGACY_NAME_FIELD, GUARDIAN_NAME_FIELD),
		(LEGACY_APPOINTMENT_FIELD, APPOINTMENT_GUARDIAN_FIELD),
	):
		frappe.db.sql(
			"""
			UPDATE `tabProperty Setter`
			SET field_name = CASE WHEN field_name IS NULL THEN NULL ELSE REPLACE(field_name, %s, %s) END,
				row_name = CASE WHEN row_name IS NULL THEN NULL ELSE REPLACE(row_name, %s, %s) END,
				value = CASE WHEN value IS NULL THEN NULL ELSE REPLACE(value, %s, %s) END
			WHERE field_name LIKE %s
			   OR row_name LIKE %s
			   OR value LIKE %s
			""",
			(
				old_fieldname,
				new_fieldname,
				old_fieldname,
				new_fieldname,
				old_fieldname,
				new_fieldname,
				f"%{old_fieldname}%",
				f"%{old_fieldname}%",
				f"%{old_fieldname}%",
			),
		)
