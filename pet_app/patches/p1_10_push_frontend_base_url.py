from __future__ import annotations

import frappe

from pet_app.patches.p1_5_notification_engine_schema import ensure_doctype


def execute():
	ensure_doctype(
		"Pet App Notification Settings",
		{
			"issingle": 1,
			"fields": [
				{
					"fieldname": "push_frontend_base_url",
					"fieldtype": "Data",
					"label": "Push Frontend Base URL",
				},
			],
		},
	)
	frappe.clear_cache()
