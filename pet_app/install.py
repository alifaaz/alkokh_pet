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
	"pet_app.patches.p1_12_meta_template_mirror.execute",
	"pet_app.patches.p1_13_manual_meta_template_send.execute",
	"pet_app.patches.p1_14_meta_template_slot_map.execute",
	"pet_app.patches.p1_15_meta_template_slot_map_stale.execute",
	"pet_app.patches.p1_16_action_rule_meta_template.execute",
	"pet_app.patches.p1_17_meta_template_source_doctype.execute",
	"pet_app.patches.p1_22_reminder_meta_template.execute",
	# Data-only, unlike everything above it: the doctype and the field come from JSON at
	# model sync. It is here because install_app() marks patches.txt complete without
	# running it, so without this entry a fresh site would come up with an empty send-
	# default table - and an empty table means every surface's picker opens blank, which
	# on a fresh install is silence rather than the seeded defaults.
	"pet_app.patches.p1_23_whatsapp_send_defaults.execute",
	# Data-only, like p1_23. A fresh install seeds the corrected key straight from p1_23
	# and this is a no-op there; it is here so the two stay in step and a site can never
	# be installed with the guessed key.
	"pet_app.patches.p1_24_invoice_send_surface_key.execute",
	# Adds replied_to_message (Link) to Pet App WhatsApp Message via ensure_doctype, but
	# the doctype name is a module constant (DOCTYPE), not a string literal at the call
	# site - test_install_schema_patches.py's static detector only recognises a literal
	# first argument, so it cannot see this one and will not fail this list into staying
	# correct. Without this entry a fresh install could receive no inbound reply and a
	# quick-reply tap would be unattributable from the first message onward.
	"pet_app.patches.p1_25_whatsapp_inbound_reply_context.execute",
	# Adds pet_app_reviewed_report (Check) to File via create_custom_fields, a shape the
	# detector does not parse at all (it only recognises ensure_doctype and a literal
	# DOCTYPES dict). Without this entry a fresh install has no marker field, so nothing
	# can ever be recorded as a deliverable report and the whole reviewed-report chain
	# is silently absent rather than merely unconfigured.
	"pet_app.patches.p1_26_reviewed_report_attachment.execute",
	# Adds delivers_reviewed_report (Check) to Pet App WhatsApp Meta Template. The
	# detector does see this one (a literal DOCTYPES dict), but seeing it only makes the
	# test fail until the patch is also listed here - it does not run the patch itself.
	"pet_app.patches.p1_27_template_promises_report.execute",
	"pet_app.patches.driver_orders_schema.execute",
	"pet_app.patches.stock_transfer_schema.execute",
	"pet_app.patches.boarding_invoice_discount.execute",
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
