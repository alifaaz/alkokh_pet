from __future__ import annotations

import frappe


DOCTYPE = "Care Service Billing Option"
GROOMING_CATEGORY = "CategoryCareServices-0008"
OPTION_LABEL = "Cats"


def execute():
	if not frappe.db.exists("DocType", DOCTYPE):
		return

	names = frappe.get_all(
		DOCTYPE,
		filters={
			"category_care_services": GROOMING_CATEGORY,
			"option_label": OPTION_LABEL,
			"min_weight": 0,
			"max_weight": 0,
		},
		pluck="name",
	)
	for name in names:
		frappe.db.set_value(
			DOCTYPE,
			name,
			{"min_weight": 0, "max_weight": 100},
			update_modified=False,
		)
