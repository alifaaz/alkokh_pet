from __future__ import annotations

import frappe


INSTALL_SCHEMA_PATCHES = (
	"pet_app.patches.p2_product_growth_schema.execute",
	"pet_app.patches.p1_5_notification_engine_schema.execute",
	"pet_app.patches.p0_5_medical_core_schema.execute",
)


def after_install():
	original_in_patch = frappe.flags.in_patch
	original_in_migrate = frappe.flags.in_migrate
	original_in_install = frappe.flags.in_install
	frappe.flags.in_patch = True
	frappe.flags.in_migrate = True
	frappe.flags.in_install = original_in_install or "pet_app"
	try:
		for patch in INSTALL_SCHEMA_PATCHES:
			frappe.get_attr(patch)()
	finally:
		frappe.flags.in_patch = original_in_patch
		frappe.flags.in_migrate = original_in_migrate
		frappe.flags.in_install = original_in_install
