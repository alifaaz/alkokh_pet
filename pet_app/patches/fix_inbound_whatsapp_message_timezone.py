"""Fix historical inbound WhatsApp message timestamps that were stored in UTC.

Inbound WhatsApp messages previously ran their epoch `timestamp` through a bare
``datetime.fromtimestamp`` (see ``pet_app.notifications.inbox._message_datetime``),
which used the server's OS timezone (UTC) instead of Frappe's system timezone.
That stored ``message_at`` shifted behind everything else by the system-timezone
offset (e.g. -3h for Asia/Baghdad).

This patch recomputes ``message_at`` for inbound rows from the original WhatsApp
``timestamp`` preserved in ``raw_json``, using the now-corrected helper. Because
it recomputes from the source epoch rather than shifting the current value, it is
idempotent and safe to run more than once.

NOT registered in patches.txt on purpose - run it manually:

    bench --site <site> execute \
        pet_app.patches.fix_inbound_whatsapp_message_timezone.execute
"""

from __future__ import annotations

import json

import frappe

from pet_app.notifications.inbox import _message_datetime


def execute():
	rows = frappe.get_all(
		"Pet App WhatsApp Message",
		filters={"direction": "Inbound"},
		fields=["name", "message_at", "raw_json"],
	)

	updated = 0
	skipped = 0
	for row in rows:
		timestamp = _extract_timestamp(row.get("raw_json"))
		if timestamp is None:
			skipped += 1
			continue

		corrected = _message_datetime(timestamp)
		# Compare on second precision; message_at has no sub-second component here.
		if str(row.get("message_at")) == corrected.strftime("%Y-%m-%d %H:%M:%S"):
			continue

		frappe.db.set_value(
			"Pet App WhatsApp Message",
			row["name"],
			"message_at",
			corrected,
			update_modified=False,
		)
		updated += 1

	frappe.db.commit()
	print(
		f"Inbound WhatsApp message_at fix: {updated} corrected, "
		f"{skipped} skipped (no timestamp in raw_json), {len(rows)} scanned."
	)


def _extract_timestamp(raw_json):
	if not raw_json:
		return None
	try:
		data = json.loads(raw_json)
	except (TypeError, ValueError):
		return None
	# raw_json may hold either the bare WhatsApp message or the wrapping event.
	timestamp = data.get("timestamp")
	if timestamp is None:
		raw_message = data.get("raw_message") or {}
		timestamp = raw_message.get("timestamp")
	return timestamp
