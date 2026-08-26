"""Give each {{n}} slot on a Meta template a stored meaning.

Meta's Insert Variable button drops a bare ``{{1}}`` into the message text and
records nothing about what that slot is for. The only hint Meta keeps is
``example.body_text``, which is free text somebody typed at creation time - a
display hint, not a binding. So the meaning has to live on our side.

It lives in a child table on the mirror row, keyed by the mirror docname, which is
Meta's own template id and is stable across re-syncs. Deliberately **not** keyed by
``local_template`` or ``binding_state``: ``upsert_mirror_row`` recomputes both on
every sync, so a map hung off either would be orphaned the first time somebody
renamed a local template.

A child table is safe here. ``upsert_mirror_row`` loads the existing row with
``frappe.get_doc``, assigns twelve named fields and saves; it never deletes, never
re-inserts over an existing row, and never clears what it does not name. Verified
against the live WABA: a locally-set value survives a full 15-template sync.
"""

from __future__ import annotations

import frappe

from pet_app.patches.p1_5_notification_engine_schema import ensure_doctype


SLOT_DOCTYPE = "Pet App WhatsApp Meta Template Slot"


def execute():
	for doctype, spec in DOCTYPES.items():
		ensure_doctype(doctype, spec)
	frappe.clear_cache()


DOCTYPES = {
	SLOT_DOCTYPE: {
		"istable": 1,
		"fields": [
			# 1-based, matching Meta's own {{1}} numbering rather than a list index,
			# so a stored map reads the same way the template text does.
			{"fieldname": "slot", "fieldtype": "Int", "reqd": 1, "in_list_view": 1},
			# An allowlisted variable key - "guardian.display_name", "pet.pet_name".
			# Data rather than Select: the allowlist is 84 entries in VARIABLE_FIELDS
			# and grows by code change, not by a patch rewriting Select options.
			{"fieldname": "variable_key", "fieldtype": "Data", "reqd": 1, "in_list_view": 1},
			# Optional. Empty means "refuse the send if this slot cannot be resolved";
			# filled means the operator has opted into this text standing in for it.
			{"fieldname": "fallback", "fieldtype": "Data", "in_list_view": 1},
		],
	},
	"Pet App WhatsApp Meta Template": {
		"fields": [
			{"fieldname": "slot_map", "fieldtype": "Table", "options": SLOT_DOCTYPE},
		],
	},
}
