from __future__ import annotations

import frappe

from pet_app.patches.p1_5_notification_engine_schema import check, ensure_doctype, txt


def execute():
	ensure_doctype(
		"Pet App Push Subscription",
		{
			"fields": [
				txt("push_token"),
				check("opted_in"),
			],
		},
	)
	frappe.clear_cache()
