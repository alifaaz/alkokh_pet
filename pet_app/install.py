from __future__ import annotations

import frappe


# Every patch here also sits in patches.txt, and install_app() marks all of patches.txt
# as already-applied without running it (frappe.installer.set_all_patches_as_completed).
# So a patch that builds schema has to be re-run explicitly here or its doctypes never
# exist on a fresh install. Keep this in patches.txt order - the notification patches
# build on each other (p1_6 extends p1_5's doctypes, p1_9/10/11 add fields to the Push
# Subscription doctype p1_8 creates).
#
# This list is covered by test_install_schema_patches.py, which fails if a patch that
# creates a DocType is missing from it. Add the patch here when that test tells you to;
# do not silence it.
INSTALL_SCHEMA_PATCHES = (
	"pet_app.patches.p2_product_growth_schema.execute",
	"pet_app.patches.p1_5_notification_engine_schema.execute",
	"pet_app.patches.p0_5_medical_core_schema.execute",
	"pet_app.patches.p1_6_whatsapp_actions_inbox_schema.execute",
	"pet_app.patches.p1_7_whatsapp_rule_designer.execute",
	"pet_app.patches.p1_8_onesignal_push_notifications.execute",
	"pet_app.patches.p1_9_push_subscription_status.execute",
	"pet_app.patches.p1_10_push_frontend_base_url.execute",
	"pet_app.patches.p1_11_push_subscription_app_id.execute",
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
