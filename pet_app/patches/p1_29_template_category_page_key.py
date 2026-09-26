"""Which screen each template category belongs to, as data the owner can edit in desk.

Until now the screen -> category mapping lived in the frontend (``surfaceCategories.ts``),
so a new category needed a deploy before any screen could use it. ``page_key`` moves that
mapping onto the category record: create a category in desk, set its ``page_key`` to a
screen's page key (the same ``page.*`` key Pet App Access Settings -> Page Access uses),
and ``get_surface_category_for_page`` resolves it with no deploy.

ONE SCREEN, ONE CATEGORY. ``page_key`` is unique: two categories claiming the same screen
would make the lookup ambiguous, and picking either silently would file templates under a
screen nobody chose. The field carries a unique index as the backstop, and
``notifications.template_categories.validate_template_category`` refuses a duplicate first
with a message naming the category that already holds the key. Empty is legal and common -
a category with no screen is still assignable to templates - and Frappe stores an empty
unique Data field as NULL, so any number of categories may leave it blank.

Additive and idempotent. ``ensure_doctype`` adds the field only if it is missing. The seed
writes a key only where the category exists, its ``page_key`` is still empty, and no other
category already holds that key - a value set in desk is never overwritten, and a second
run changes nothing. No template row is read or written, and ``list_template_options`` is
untouched: a screen that passes no category still gets the full catalogue.
"""

from __future__ import annotations

import frappe

from pet_app.patches.p1_5_notification_engine_schema import ensure_doctype
from pet_app.patches.p1_28_template_surface_category import CATEGORY_DOCTYPE

PAGE_KEY_FIELD = {
	"fieldname": "page_key",
	"fieldtype": "Data",
	"unique": 1,
	"in_list_view": 1,
	"in_standard_filter": 1,
}

# Only the pairings where the category and the page registry name the same screen one to
# one. Every other category is left blank for the owner to set in desk, because the page
# it belongs to is a choice, not a lookup: invoice could be POS Sell or Sales Invoices,
# treatment_followup could be Visits, Case Follow-up or CRM Follow-ups, and reminder,
# general_guardian and death_certificate have no single page of their own.
PAGE_KEYS = {
	"boarding": "page.healthcare.boarding",
	"lab": "page.healthcare.labs",
	"radiology": "page.healthcare.radiology",
	"preventive": "page.healthcare.preventive-care",
}


def execute():
	if not frappe.db.exists("DocType", CATEGORY_DOCTYPE):
		# p1_28 creates the doctype and runs before this in patches.txt; nothing to extend.
		return
	ensure_doctype(CATEGORY_DOCTYPE, {"fields": [PAGE_KEY_FIELD]})
	frappe.clear_cache(doctype=CATEGORY_DOCTYPE)
	seed_page_keys()


def seed_page_keys():
	for category, page_key in PAGE_KEYS.items():
		if not frappe.db.exists(CATEGORY_DOCTYPE, category):
			continue
		if frappe.db.get_value(CATEGORY_DOCTYPE, category, "page_key"):
			# Set by the owner, or by an earlier run: theirs, not ours.
			continue
		if frappe.db.exists(CATEGORY_DOCTYPE, {"page_key": page_key}):
			# Another category already claims this screen. Seeding here would trip the
			# unique index; the owner's assignment stands.
			continue
		frappe.db.set_value(CATEGORY_DOCTYPE, category, "page_key", page_key, update_modified=False)
