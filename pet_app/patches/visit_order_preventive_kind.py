"""Two Select fields learn the word `preventive`, so a dose can be ordered from a visit.

`Visit Order.kind` gains `preventive` - how the order is placed.
`Pet Billable Item.item_type` gains `Preventive` - how the resulting charge is labelled on
the visit's billable list. BOTH ARE REQUIRED and neither is optional: the order creates the
record, the record's `after_insert` puts a billable row on the visit, and that row is
refused by Frappe with `Item Type cannot be "Preventive"` if only the first has been done.

A preventive charge is deliberately NOT filed as `Service`. Reusing that value would need no
migration at all, and would put vaccinations back in the same bucket as grooming and
bathing - the exact conflation this doctype exists to end.

WHY A PATCH AT ALL. `kind` is a Select, and its options live in the doctype row in the
database, not only in the JSON. Without a reload the new value is refused by Frappe's own
Select validation the moment a client posts it - and the refusal names the field, not the
missing option, so it reads as a client bug rather than a missing migration.

WHAT IT CHANGES. One option string on one child doctype. No column is added, no row is
written, and no existing order is touched: every current row carries lab / radiology /
service / procedure / medication / other and keeps carrying it. `preventive` is additive and
nothing selects it until a client asks for it.

THE ORDER ID IS NOT AFFECTED. `_deterministic_order_id` gained a `service_option` component
FOR THE PREVENTIVE KIND ONLY, so the digest of every existing kind is byte-identical to what
it was. Had it been added unconditionally, a client re-posting an existing order without its
`order_id` would hash to a new id and duplicate the row instead of matching it.

ONE KIND FOR BOTH VACCINATION AND DEWORMING, because one doctype covers both:
`Preventive Care Record` derives which it is from its catalogue category, so a client cannot
say it wrongly and does not have to say it at all.
"""

from __future__ import annotations

import frappe

TARGETS = (
	("visit_order", "Visit Order", "kind", "preventive"),
	("pet_billable_item", "Pet Billable Item", "item_type", "Preventive"),
)


def execute():
	ready = {}
	for module_path, doctype, fieldname, new_option in TARGETS:
		if not frappe.db.exists("DocType", doctype):
			frappe.log_error(
				title="PREVENTIVE_SELECT_OPTION_NO_DOCTYPE",
				message=f"{doctype} does not exist; nothing to extend.",
			)
			return

		frappe.reload_doc("pet_app", "doctype", module_path, force=True)

		options = frappe.db.get_value("DocField", {"parent": doctype, "fieldname": fieldname}, "options")
		available = [line.strip() for line in (options or "").splitlines() if line.strip()]
		if new_option not in available:
			frappe.log_error(
				title="PREVENTIVE_SELECT_OPTION_MISSING",
				message=(
					f"{doctype}.{fieldname} still does not offer {new_option!r} after the reload. "
					f"Options are: {available}. Preventive orders cannot be created from a visit."
				),
			)
			return
		ready[f"{doctype}.{fieldname}"] = available

	frappe.logger("pet_app.migrate").info({"event": "PREVENTIVE_SELECT_OPTIONS_READY", "fields": ready})
