from __future__ import annotations

import frappe


PET_CARE_SERVICE = "PetCareService"
CARE_SERVICE_TEMPLATE = "CareService template"
BILLING_OPTION = "Care Service Billing Option"


def execute():
	if not frappe.db.exists("DocType", PET_CARE_SERVICE):
		return

	rows = frappe.get_all(
		PET_CARE_SERVICE,
		filters=[[PET_CARE_SERVICE, "category", "is", "not set"]],
		fields=["name", "care_service_id", "service_option"],
		limit_page_length=0,
	)
	for row in rows:
		category = _category_from_care_service(row.get("care_service_id"))
		if not category:
			category = _category_from_billing_option(row.get("service_option"))
		if category:
			frappe.db.set_value(PET_CARE_SERVICE, row.name, "category", category, update_modified=False)


def _category_from_care_service(care_service_id: str | None) -> str | None:
	if not care_service_id or not frappe.db.exists("DocType", CARE_SERVICE_TEMPLATE):
		return None
	return frappe.db.get_value(CARE_SERVICE_TEMPLATE, care_service_id, "category_id")


def _category_from_billing_option(service_option: str | None) -> str | None:
	if not service_option or not frappe.db.exists("DocType", BILLING_OPTION):
		return None
	return frappe.db.get_value(BILLING_OPTION, service_option, "category_care_services")
