from __future__ import annotations

import frappe

from pet_app.api import scheduling, workspace
from pet_app.api.v1._helpers import call_api


@frappe.whitelist()
def get_my_workspace(**kwargs):
	return call_api(workspace.get_my_workspace, **kwargs)


@frappe.whitelist()
def get_record(source_type, name):
	return call_api(workspace.get_record, source_type, name)


@frappe.whitelist(methods=["POST"])
def perform_action(source_type, name, action, payload=None):
	return call_api(workspace.perform_action, source_type, name, action, payload=payload)


@frappe.whitelist()
def get_available_slots(**kwargs):
	return call_api(scheduling.get_available_slots, **kwargs)


@frappe.whitelist(methods=["POST"])
def book_appointment(data=None, **kwargs):
	return call_api(scheduling.book_appointment, data=data, **kwargs)


@frappe.whitelist(methods=["POST"])
def reschedule_appointment(appointment=None, data=None, **kwargs):
	return call_api(scheduling.reschedule_appointment, appointment=appointment, data=data, **kwargs)


@frappe.whitelist(methods=["POST"])
def cancel_appointment(appointment=None, reason=None):
	return call_api(scheduling.cancel_appointment, appointment=appointment, reason=reason)


@frappe.whitelist()
def get_doctor_calendar(**kwargs):
	return call_api(scheduling.get_doctor_calendar, **kwargs)
