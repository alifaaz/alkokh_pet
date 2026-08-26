"""Let a Meta template declare which record its variables come from.

A mirror row has no source of its own - Meta knows nothing about Guardians or
bookings - so the slot-map editor was offering all 94 allowlisted variables for
every template. An operator mapping an Arabic boarding template was shown
``invoice.grand_total``, which validated cleanly and then resolved to nothing at
send, because nothing ever puts an ``invoice`` namespace in a boarding context.

Declaring the source narrows the catalogue to the namespaces that can actually be
populated for that template, and lets the same narrowing be enforced on save.

Local-only metadata, in the same class as ``local_template`` and
``slot_map_stale``: Meta neither knows nor cares, and ``upsert_mirror_row``
assigns only its own named fields, so a sync leaves this alone.

Data rather than a Select on purpose. The set of sources is
``SOURCE_NAMESPACES`` in ``notifications/context.py`` and it grows by code change -
Pet Boarding was added only recently. Select options baked into a patch would be a
second copy of that list, wrong the day the first one moves. The value is validated
against the live registry at save time instead.
"""

from __future__ import annotations

import frappe

from pet_app.patches.p1_5_notification_engine_schema import ensure_doctype


def execute():
	for doctype, spec in DOCTYPES.items():
		ensure_doctype(doctype, spec)
	frappe.clear_cache()


DOCTYPES = {
	"Pet App WhatsApp Meta Template": {
		"fields": [
			# Empty is legal and means "no source declared" - the catalogue stays
			# unscoped and the save-time namespace check does not apply.
			{"fieldname": "source_doctype", "fieldtype": "Data", "in_standard_filter": 1},
		],
	},
}
