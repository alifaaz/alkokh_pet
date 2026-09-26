"""Mark a File as the reviewed report a record can deliver over WhatsApp.

A template that promises "we will send you the report" needs a file to send, and the
system has no way to render one - there is no server-side PDF renderer here. So the bytes
come from the client, and something has to record which of a record's attachments is the
one a human reviewed and meant to send.

It cannot be inferred. Lab and Imaging carry over a thousand attachments on this site
(1047 as of 2026-09-03, and growing - this is live intake, not a fixed count): nearly all
are photos of paper results, a handful are PDFs somebody uploaded that nobody reviewed
for sending, and LAB-00538 - the record behind the one real button tap - has a single `.webp`
and no PDF at all. "Newest attachment" and "newest .pdf" both send the wrong file to a
customer. A marker is the only honest filter.

A Check on File rather than a field on Lab and Imaging, because the clinic will add a
radiology template and more after it. File is where the doctype-agnostic answer lives:
`(attached_to_doctype, attached_to_name, pet_app_reviewed_report)` works for a source
doctype nobody has thought of yet, with no schema change and no code change.

This patch is the field's only owner. It is deliberately NOT in the Custom Field fixture
allow-list in hooks.py - tests/test_fixture_allowlist.py fails a field that is shipped by
both a patch and a fixture, because migrate runs the patch and sync_fixtures then
overwrites it with whatever JSON was last exported.
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from pet_app.notifications.reports import REVIEWED_REPORT_FIELD


def execute():
	create_custom_fields(
		{
			"File": [
				{
					"fieldname": REVIEWED_REPORT_FIELD,
					"fieldtype": "Check",
					"label": "Reviewed Report",
					"default": "0",
					"insert_after": "custom_is_default",
					# Set by pet_app.notifications.reports.save_reviewed_report and by
					# nothing else. A staff member ticking this by hand in Desk would be
					# declaring a file fit to send to a customer through a screen built
					# for none of that.
					"read_only": 1,
					"description": (
						"Set by the app when a reviewed report PDF is attached. "
						"The WhatsApp report delivery sends the newest file carrying this flag."
					),
				}
			]
		},
		update=True,
	)
	frappe.clear_cache(doctype="File")
