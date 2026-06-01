from __future__ import annotations

import frappe

from pet_app.api import guardian_portal
from pet_app.api.v1._helpers import call_api


@frappe.whitelist()
def get_my_pets_dashboard():
	return call_api(guardian_portal.get_my_pets_dashboard)


@frappe.whitelist()
def get_pet_medical_timeline(pet=None, pet_id=None, limit=50):
	return call_api(guardian_portal.get_pet_medical_timeline, pet=pet, pet_id=pet_id, limit=limit)


@frappe.whitelist()
def get_pet_documents(pet=None, pet_id=None):
	return call_api(guardian_portal.get_pet_documents, pet=pet, pet_id=pet_id)


@frappe.whitelist()
def get_upcoming_appointments(limit=20):
	return call_api(guardian_portal.get_upcoming_appointments, limit=limit)


@frappe.whitelist()
def get_guardian_invoices(outstanding_only=0, limit=50):
	return call_api(guardian_portal.get_guardian_invoices, outstanding_only=outstanding_only, limit=limit)
