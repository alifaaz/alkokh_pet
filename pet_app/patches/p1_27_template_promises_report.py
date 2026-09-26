"""Let a Meta template declare that it promises the record's reviewed report.

`lab_result_ready` ends "press the button below and we will send you the full report".
That sentence is a promise, and on 2026-09-03 the system could not keep it: the template
went out against LAB-00538, the guardian tapped 19 seconds later, and nothing happened -
because no reviewed PDF was attached to the lab, and there is no renderer here that could
have made one.

The refusal has to key on something, and it may not be a template name. The clinic will
add a radiology template and more after it, and every one of them has to work with no code
change. So the template says what it is, once, as configuration:

  tick this box  ->  this send is refused unless its source record has a reviewed report
                     waiting, and a tap on its button delivers that report.

Nothing in code lists which templates those are, and nothing needs editing when the next
one appears.

Why the mirror row and not the local Pet App WhatsApp Template: `lab_result_ready` has no
local row. Neither of its two rows - en_US and ar - is bound to one, and the send that
started this addressed the mirror directly with template_key empty. A flag on a row that
does not exist cannot be read.

Local-only metadata, in the same class as `local_template`, `slot_map_stale` and
`source_doctype` (p1_17): Meta neither knows nor cares, and `upsert_mirror_row` assigns
only its own named fields, so a sync leaves this alone. `apply_status_update` writes
status and rejected_reason by name for the same reason.

A Check rather than a kind: one record carries one reviewed report, which is true for Lab
and Imaging because they are separate records. If a single record ever has to deliver two
different documents, this becomes a kind and the mirror row names which one it promises -
that is a different decision, with per-template configuration behind it, and it is not
this one.
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
			# Off is the default and means the template promises nothing, which is what
			# every row on this site means today. Behaviour for all 23 of them is
			# unchanged until somebody ticks a box.
			check("delivers_reviewed_report", default=0, in_standard_filter=1),
		],
	},
}
