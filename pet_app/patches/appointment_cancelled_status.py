from __future__ import annotations

import frappe
from frappe.custom.doctype.property_setter.property_setter import make_property_setter


APPOINTMENT_STATUS_OPTIONS = "Open\nUnverified\nClosed\nCancelled"


def execute():
	if not frappe.db.exists("DocType", "Appointment"):
		return
	name = "Appointment-status-options"
	if frappe.db.exists("Property Setter", name):
		frappe.db.set_value("Property Setter", name, "value", APPOINTMENT_STATUS_OPTIONS, update_modified=True)
	else:
		make_property_setter(
			"Appointment",
			"status",
			"options",
			APPOINTMENT_STATUS_OPTIONS,
			"Text",
		)
	frappe.clear_cache(doctype="Appointment")
