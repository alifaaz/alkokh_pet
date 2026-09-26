"""Correct the invoice surface's key to the one the frontend actually sends.

p1_23 seeded this surface as ``invoice.due_reminder``. That key was a guess: unlike the
other frontend surfaces it had never sent, so there was no queue row to read it off and
no server call site that mints it. The real client constant is ``manual.invoice_send``
(documents: Sales Invoice). The guessed key survives in the frontend only in a mock that
never sends.

Nothing is lost - the row was seeded blank, which is the no-default state - but the row
has to be addressable by the key the frontend sends, or an operator would configure a
default that no send ever looks up.

Separate from p1_23 rather than an edit to it: p1_23 is already recorded as applied on
at least one site, so a site that ran it would keep the wrong key forever. Worse, the
key is no longer in SEND_SURFACES, and Pet App Access Settings refuses to save a row for
an unregistered surface - so on those sites the settings Single would be unsaveable
until this runs.

Idempotent, and carries over whatever the row held rather than resetting it.
"""

from __future__ import annotations

import frappe
from frappe.utils import cstr

SETTINGS_DOCTYPE = "Pet App Access Settings"
DEFAULTS_FIELD = "send_defaults"
OLD_KEY = "invoice.due_reminder"
NEW_KEY = "manual.invoice_send"


def execute():
	if not frappe.db.exists("DocType", SETTINGS_DOCTYPE):
		return
	if not frappe.get_meta(SETTINGS_DOCTYPE).has_field(DEFAULTS_FIELD):
		return

	doc = frappe.get_single(SETTINGS_DOCTYPE)
	rows = doc.get(DEFAULTS_FIELD) or []
	old = next((r for r in rows if cstr(r.surface_key).strip() == OLD_KEY), None)
	new = next((r for r in rows if cstr(r.surface_key).strip() == NEW_KEY), None)

	changed = False
	if old and not new:
		# Rename in place: same surface, right key, and whatever it was set to comes
		# with it.
		old.surface_key = NEW_KEY
		changed = True
		print(f"  ~ {OLD_KEY} -> {NEW_KEY}")
	elif old and new:
		# Both present - the correct one wins and the guess goes, so the table cannot
		# hold two rows for one surface.
		doc.remove(old)
		changed = True
		print(f"  - {OLD_KEY} (superseded by {NEW_KEY})")

	if not next((r for r in doc.get(DEFAULTS_FIELD) or [] if cstr(r.surface_key).strip() == NEW_KEY), None):
		doc.append(DEFAULTS_FIELD, {"surface_key": NEW_KEY, "enabled": 1, "template_source": ""})
		changed = True
		print(f"  + {NEW_KEY} -> (blank)")

	if not changed:
		return
	doc.save(ignore_permissions=True)
	frappe.db.commit()
