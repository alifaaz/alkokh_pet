from __future__ import annotations

import frappe


DOCTYPE = "Care Service Billing Option"
TEST_OPTION = "CSBO-00017"


def execute():
	if not frappe.db.exists("DocType", DOCTYPE):
		return

	row = frappe.db.get_value(
		DOCTYPE,
		TEST_OPTION,
		["name", "category_care_services", "service_title", "option_label"],
		as_dict=True,
	)
	if not row:
		return
	if row.option_label == "1" and row.category_care_services == "CategoryCareServices-0008":
		frappe.db.set_value(DOCTYPE, TEST_OPTION, "disabled", 1, update_modified=False)
