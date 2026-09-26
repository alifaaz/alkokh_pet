"""Bind a template category to a send surface (a dialog or button), not only to a page.

p1_29 gave each category a ``page_key``. One page can host several send dialogs - the pet
profile carries the death certificate, lab, preventive and guardian dialogs - but a page
can hold only one category, and three categories (death_certificate, reminder,
general_guardian) have no page at all. ``surface_key`` names the dialog itself, so those
can be bound too. A category may carry a page, a surface, or both.

RESOLUTION ORDER (``notifications.template_categories.resolve_category``): a surface_key
match wins; failing that, a page_key match; failing both, no category and the screen gets
the full catalogue. The narrower binding wins because it is the more deliberate one: a
dialog-level assignment is made for that dialog, a page-level one is a default for
everything on the page that did not say otherwise.

Same rules as page_key: optional, trimmed, blank stored as NULL, unique - two categories
cannot claim one dialog - with a refusal (SURFACE_KEY_TAKEN, naming the holder) from the
category's validate hook and the unique index as the backstop.

NOTHING IS SEEDED. The surface keys the frontend uses for this (its own constant map) are
not in the deployed bundle, so none could be confirmed, and a guessed key binds nothing
while looking configured. The owner sets them in desk.

NOT THE SEND-DEFAULTS KEY. ``Pet App WhatsApp Send Default.surface_key`` is a different
namespace (``send_targets.SEND_SURFACES``: ``boarding.check_in``, ``pet.death``, ...) that
picks a surface's default template. This field only names which dialog a category serves.
The two share a name and nothing else; neither reads the other.

Additive and idempotent: ``ensure_doctype`` adds the field only if it is missing. No row
is read or written.
"""

from __future__ import annotations

import frappe

from pet_app.patches.p1_5_notification_engine_schema import ensure_doctype
from pet_app.patches.p1_28_template_surface_category import CATEGORY_DOCTYPE

SURFACE_KEY_FIELD = {
	"fieldname": "surface_key",
	"fieldtype": "Data",
	"unique": 1,
	"in_list_view": 1,
	"in_standard_filter": 1,
}


def execute():
	if not frappe.db.exists("DocType", CATEGORY_DOCTYPE):
		return
	ensure_doctype(CATEGORY_DOCTYPE, {"fields": [SURFACE_KEY_FIELD]})
	frappe.clear_cache(doctype=CATEGORY_DOCTYPE)
