from __future__ import annotations

import frappe

# Seed the clinic-wide billing trigger with the behaviour that predates it.
#
# `Pet App Access Settings.billing_trigger` ships with an empty first option, so on an
# existing site the column is NULL until something writes it. `get_billing_trigger()`
# already reads a blank as `on_release`, which is why deploying the code before running
# this patch is safe - but leaving it blank means the setting SCREEN shows nothing
# selected while the clinic is in fact billing on release. An operator reading that would
# reasonably conclude billing was unconfigured and pick something, changing behaviour
# without meaning to. Writing the value makes the screen agree with the code.
#
# Only when blank. A site that has already chosen a trigger - including one that chose it
# between deploy and migrate - keeps its choice.

SETTINGS_DOCTYPE = "Pet App Access Settings"
FIELD = "billing_trigger"
DEFAULT = "on_release"

# Whether the Treatment boarding rate absorbs medication prescribed on a visit. Seeded
# ON, which - unlike the trigger - is a CHANGE from prior behaviour, not a preservation of
# it: before this setting, visit-prescribed medication was always billed even to a pet on
# a Treatment stay. Seeded here rather than left to the field default so the value is
# written once, explicitly, and shows in the settings screen as the decision it is.
#
# `0` is a real answer and must survive. Checks read back as 0, not as None, so the blank
# test below is `is None` - a site that deliberately turned this off is not re-seeded.
INCLUDE_MEDICATION_FIELD = "include_visit_medication_in_boarding"
INCLUDE_MEDICATION_DEFAULT = 1

# Medication bills on its own trigger, separately from the four order types. Seeded
# `on_request` - the charge is raised when the prescription is written. Like the Include
# setting and unlike `billing_trigger`, this CHANGES prior behaviour: medication used to
# reach an invoice only when the visit closed.
MEDICATION_FIELD = "medication_billing_trigger"
MEDICATION_DEFAULT = "on_request"

# Also seeds `medication_billing_trigger` (on_request) and
# `include_visit_medication_in_boarding` (1); see below.
#
# Pet Billable Item gains `sales_invoice` in the same change. Medication has no order
# document to carry per-order billing state, and the cancellation path has to find the
# invoice line to remove, so the row records its own invoice. Reloaded here so both
# arrive together.
RELOAD = (
	("pet_app", "doctype", "pet_billable_item"),
	("pet_app", "doctype", "pet_app_access_settings"),
)


def execute():
	for app, doctype, slug in RELOAD:
		frappe.reload_doc(app, doctype, slug)

	meta = frappe.get_meta(SETTINGS_DOCTYPE)

	# Reload did not bring a field. Nothing to seed for it, and throwing here would break
	# migrate for a site that simply has not received the doctype change yet.
	if meta.has_field(FIELD) and not frappe.db.get_single_value(SETTINGS_DOCTYPE, FIELD):
		frappe.db.set_single_value(SETTINGS_DOCTYPE, FIELD, DEFAULT)
		frappe.logger("pet_app.billing").info(
			{"event": "BILLING_TRIGGER_SEEDED", "value": DEFAULT}
		)

	if meta.has_field(MEDICATION_FIELD) and not frappe.db.get_single_value(
		SETTINGS_DOCTYPE, MEDICATION_FIELD
	):
		frappe.db.set_single_value(SETTINGS_DOCTYPE, MEDICATION_FIELD, MEDICATION_DEFAULT)
		frappe.logger("pet_app.billing").info(
			{"event": "MEDICATION_BILLING_TRIGGER_SEEDED", "value": MEDICATION_DEFAULT}
		)

	if meta.has_field(INCLUDE_MEDICATION_FIELD):
		current = frappe.db.get_single_value(SETTINGS_DOCTYPE, INCLUDE_MEDICATION_FIELD)
		if current is None:
			frappe.db.set_single_value(
				SETTINGS_DOCTYPE, INCLUDE_MEDICATION_FIELD, INCLUDE_MEDICATION_DEFAULT
			)
			frappe.logger("pet_app.billing").info(
				{
					"event": "INCLUDE_VISIT_MEDICATION_IN_BOARDING_SEEDED",
					"value": INCLUDE_MEDICATION_DEFAULT,
				}
			)
