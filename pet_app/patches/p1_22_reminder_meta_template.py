"""Let a scheduled reminder name a Meta template.

`Pet App Reminder` could store only `template_key` - a local `Pet App WhatsApp
Template` slug. That made it the one live send path with no way to reach the mirror,
which matters more here than anywhere else: a reminder fires on a schedule, so it
almost always arrives into a closed 24-hour window, and a closed window is exactly the
case where only an approved Meta template can be delivered.

The two fields mirror the pair already carried by `Pet App Notification Queue`
(p1_13) and `Pet App WhatsApp Action Rule` (p1_16), with the same meanings, so the
three doctypes that store a template choice now describe it identically:
`meta_template` set is what makes the send mirror-direct, and `template_source` is the
same fact recorded explicitly so a list view or report can tell the paths apart.

Additive and inert for existing rows. `_resolve_meta_template_row` treats a blank pair
as "no mirror row wanted" (engine.py:590-592), so the seven live reminders keep taking
the local path with their `template_key` exactly as before - the new columns are simply
null on them.
"""

from __future__ import annotations

import frappe

from pet_app.patches.p1_5_notification_engine_schema import ensure_doctype, link


def execute():
	for doctype, spec in DOCTYPES.items():
		ensure_doctype(doctype, spec)
	frappe.clear_cache()


DOCTYPES = {
	"Pet App Reminder": {
		"fields": [
			# "local" or "meta". Data rather than Select, matching the queue and the
			# action rule - the vocabulary is ours and it is stored, not offered.
			{"fieldname": "template_source", "fieldtype": "Data", "in_standard_filter": 1},
			link("meta_template", "Pet App WhatsApp Meta Template"),
		],
	},
}
