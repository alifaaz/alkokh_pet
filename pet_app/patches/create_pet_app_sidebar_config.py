from __future__ import annotations

import frappe


SIDEBAR_CONFIG_DOCTYPE = "Pet App Sidebar Config"


def execute():
	frappe.reload_doc("pet_app", "doctype", "pet_app_sidebar_config")

	if not frappe.db.get_single_value(SIDEBAR_CONFIG_DOCTYPE, "config"):
		frappe.db.set_single_value(SIDEBAR_CONFIG_DOCTYPE, "config", "{}")

	frappe.clear_cache(doctype=SIDEBAR_CONFIG_DOCTYPE)
