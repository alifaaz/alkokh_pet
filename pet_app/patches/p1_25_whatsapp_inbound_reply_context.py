"""Store which outbound message an inbound WhatsApp reply is answering.

Meta sends it on every reply as ``message.context.id`` - the wamid of the quoted
message. ``_message_event`` never read it, so it was stored inside the webhook row's
payload_json and dropped everywhere else.

Without it a quick-reply tap is unattributable. The tap carries the button's visible
label ("Send the report") and nothing about the record the template was about; two
guardians tapping the same button on two different lab results are indistinguishable
rows. The wamid resolves that in one indexed read, because provider_message_id has been
unique on Pet App WhatsApp Message since p1_6, and the outbound row already carries
source_doctype / source_name.

One field, a self-link. Not a Data column holding the raw wamid: the raw value already
survives verbatim in the inbound row's raw_json, and what every caller wants is the row,
not the string.
"""

from __future__ import annotations

import json

import frappe

from pet_app.patches.p1_5_notification_engine_schema import ensure_doctype, link


DOCTYPE = "Pet App WhatsApp Message"
FIELDNAME = "replied_to_message"


def execute():
	ensure_doctype(
		DOCTYPE,
		{"fields": [link(FIELDNAME, DOCTYPE, in_standard_filter=1)]},
	)
	frappe.clear_cache(doctype=DOCTYPE)
	backfill_reply_context()


def backfill_reply_context():
	"""Fill the new column for inbound rows whose raw_json already carries a context.

	Reads nothing the row does not already contain - raw_json is Meta's own message
	object, stored verbatim at insert time - and writes only rows where the column is
	still empty and the quoted wamid resolves. Idempotent: a second run matches nothing.

	Without this the column would read "no context" for every reply the site has already
	received, which is the one state it must never be confused with.
	"""
	rows = frappe.get_all(
		DOCTYPE,
		filters={"direction": "Inbound", FIELDNAME: ["in", (None, "")]},
		fields=["name", "raw_json"],
		ignore_permissions=True,
	)
	filled = 0
	for row in rows:
		try:
			raw = json.loads(row.raw_json or "{}")
		except (TypeError, ValueError):
			continue
		context = raw.get("context")
		if not isinstance(context, dict):
			continue
		quoted = frappe.db.get_value(DOCTYPE, {"provider_message_id": context.get("id")}, "name")
		# Self-reference would be a corrupt row rather than a reply, and a Link to
		# itself is worse than a blank.
		if not quoted or quoted == row.name:
			continue
		frappe.db.set_value(DOCTYPE, row.name, FIELDNAME, quoted, update_modified=False)
		filled += 1
	return filled
