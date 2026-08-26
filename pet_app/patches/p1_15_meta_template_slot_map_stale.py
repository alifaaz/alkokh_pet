"""Surface a slot map that no longer matches its template.

``upsert_mirror_row`` overwrites ``components_json`` on every sync and deliberately
leaves ``slot_map`` alone - that is why an operator's mapping survives a re-sync. The
cost is that a template edited from four variables to five on Meta comes back with new
components and a stale four-slot map, and nothing notices: the sync reports
``updated`` and moves on. The mismatch only surfaces later, when a send refuses in
front of somebody trying to message a customer.

This check field is set during the sync that causes the drift, so the row shows up in
a list view the moment it goes stale rather than at the next send attempt.

The map itself is never cleared. A declared-count change may be a typo fix, and
discarding an operator's work over one is worse than a row that refuses loudly.
"""

from __future__ import annotations

import frappe

from pet_app.patches.p1_5_notification_engine_schema import check, ensure_doctype


def execute():
	for doctype, spec in DOCTYPES.items():
		ensure_doctype(doctype, spec)
	frappe.clear_cache()


DOCTYPES = {
	"Pet App WhatsApp Meta Template": {
		"fields": [
			check("slot_map_stale", in_list_view=1, in_standard_filter=1),
		],
	},
}
