"""Let the notification queue carry a mirror-direct send.

Until now a queued WhatsApp template send always named a local
`Pet App WhatsApp Template` and resolved everything from it. After the Phase 2
binding gate, twelve of the thirteen local rows refuse at pre-flight while the
mirror holds fifteen genuinely APPROVED Meta templates the composer could not
reach at all.

These two fields let a queue row point straight at a mirror row instead.
`meta_template` being set is what makes a row mirror-direct; `template_source` is
the same fact stored explicitly, so the queue is self-describing in a list view
and a report can count which path a send took. Both follow the queue's existing
habit of snapshotting what it resolved rather than re-deriving it later.
"""

from __future__ import annotations

import frappe

from pet_app.patches.p1_5_notification_engine_schema import ensure_doctype, link


def execute():
	for doctype, spec in DOCTYPES.items():
		ensure_doctype(doctype, spec)
	frappe.clear_cache()


DOCTYPES = {
	"Pet App Notification Queue": {
		"fields": [
			# "local" or "meta". Our vocabulary, not Meta's - Data rather than Select
			# only for consistency with the neighbouring delivery_mode column, which
			# is also a Data snapshot of a Select.
			{"fieldname": "template_source", "fieldtype": "Data", "in_standard_filter": 1},
			link("meta_template", "Pet App WhatsApp Meta Template"),
		],
	},
}
