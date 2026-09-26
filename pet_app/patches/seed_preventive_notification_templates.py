"""The two preventive reminders get a body worth sending.

`Pet Notification Template` has never had a row, so `_legacy_template` returned None and
`_send_legacy_reminder` fell back to the reminder TYPE as both subject and body. Every
vaccination reminder ever queued would have been delivered as the literal string
"Vaccination Due" - no pet, no date, nothing a guardian could act on.

BILINGUAL IN ONE BODY, NOT TWO ROWS. The doctype has no language field - its fields are
template_name, reminder_type, channel, subject, body, active - and `_legacy_template`
selects on `{reminder_type, channel, active}` alone, so two rows for one type and channel
would be indistinguishable and one of them would win arbitrarily. Until the doctype gains
a language and the lookup learns to use it, English and Arabic ship in the same body,
Arabic first because that is what the counter reads. Recorded here so the person who adds
`language` later knows why these look like this.

The placeholders are rendered by `_render` in api/notifications.py against the context
`_legacy_context` builds - `pet`, `due_date`, `guardian`. Rendering had to be added in the
same change: before it, `template.body` was used verbatim and a placeholder would have
been delivered as raw Jinja.

Seeded by name, updated in place if the row exists and nobody has edited it, so re-running
is safe and an operator's wording is never overwritten.
"""

from __future__ import annotations

import frappe
from frappe.utils import cstr


DOCTYPE = "Pet Notification Template"

TEMPLATES = (
	{
		"template_name": "Vaccination Due - In App",
		"reminder_type": "Vaccination Due",
		"channel": "In App",
		"subject": "Vaccination due for {{ pet }}",
		"body": (
			"تذكير: لقاح {{ pet }} مستحق بتاريخ {{ due_date }}. "
			"يرجى التواصل مع العيادة لتحديد موعد.\n"
			"Reminder: {{ pet }}'s vaccination is due on {{ due_date }}. "
			"Please contact the clinic to book an appointment."
		),
	},
	{
		"template_name": "Deworming Due - In App",
		"reminder_type": "Deworming Due",
		"channel": "In App",
		"subject": "Deworming due for {{ pet }}",
		"body": (
			"تذكير: جرعة الديدان لـ {{ pet }} مستحقة بتاريخ {{ due_date }}. "
			"يرجى التواصل مع العيادة لتحديد موعد.\n"
			"Reminder: {{ pet }}'s deworming is due on {{ due_date }}. "
			"Please contact the clinic to book an appointment."
		),
	},
)


def execute():
	if not frappe.db.exists("DocType", DOCTYPE):
		return

	for row in TEMPLATES:
		ensure_template(**row)


def ensure_template(template_name: str, reminder_type: str, channel: str, subject: str, body: str) -> str:
	"""Create the template, or repair a stub, without overwriting an edited one."""
	template_name = cstr(template_name).strip()

	if not frappe.db.exists(DOCTYPE, template_name):
		doc = frappe.new_doc(DOCTYPE)
		doc.template_name = template_name
		doc.reminder_type = reminder_type
		doc.channel = channel
		doc.subject = subject
		doc.body = body
		doc.active = 1
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
		return doc.name

	# Only fill in what is empty. A body somebody rewrote is theirs, not ours.
	current = frappe.db.get_value(
		DOCTYPE, template_name, ["reminder_type", "channel", "subject", "body", "active"], as_dict=True
	)
	patch = {}
	if not cstr(current.get("reminder_type")).strip():
		patch["reminder_type"] = reminder_type
	if not cstr(current.get("channel")).strip():
		patch["channel"] = channel
	if not cstr(current.get("subject")).strip():
		patch["subject"] = subject
	if not cstr(current.get("body")).strip():
		patch["body"] = body
	if patch:
		frappe.db.set_value(DOCTYPE, template_name, patch, update_modified=False)

	return template_name
