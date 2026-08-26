"""Meta template mirror: a local, read-through copy of Meta's message templates.

Why a separate doctype rather than meta_* fields on Pet App WhatsApp Template
-----------------------------------------------------------------------------
The two objects have different owners and different lifecycles. Meta owns the
approval state, the rejection reason and the component structure; we own the
event binding, the delivery mode and the body preview. Merging them means a sync
job writes into rows a human is editing, and a template Meta has never seen looks
identical to one it approved. The mirror keeps Meta's copy verbatim and points at
the local row through `local_template`; the local doctype is not modified.

Why status and category are Data, not Select
--------------------------------------------
A Select is a hardcoded list living in a patch. Meta has added template statuses
and categories before and will again; a value outside the list would either throw
on save or be silently dropped. These columns store whatever Meta sent, verbatim,
with no validation and no case normalisation. `raw_json` carries the same
guarantee at the object level: keys we do not model survive a sync untouched, so
adopting a new Meta field later is a read, not a backfill.
"""

from __future__ import annotations

import frappe

from pet_app.patches.p1_5_notification_engine_schema import (
	check,
	code,
	dt,
	ensure_doctype,
	link,
	txt,
)


def execute():
	ensure_meta_template_schema()
	seed_category_map()
	frappe.clear_cache()


def ensure_meta_template_schema():
	for doctype, spec in DOCTYPES.items():
		ensure_doctype(doctype, spec)


# Local category -> Meta category, as data. The translation is a lookup so that a
# new local category is a row somebody adds, not a patch somebody writes. Service
# is seeded deliberately blank: Meta has no Service category, and a blank value
# means "no equivalent" - the runtime refuses to guess one.
CATEGORY_MAP_SEED = (
	("Authentication", "AUTHENTICATION", ""),
	("Utility", "UTILITY", ""),
	("Marketing", "MARKETING", ""),
	(
		"Service",
		"",
		"Meta has no Service category. Pick the Meta category this local category "
		"should map to (usually UTILITY) and fill meta_category before syncing "
		"templates in it.",
	),
)


def seed_category_map():
	"""Insert the four known mappings, never overwriting an edited row."""
	for local_category, meta_category, notes in CATEGORY_MAP_SEED:
		if frappe.db.exists("Pet App WhatsApp Meta Category Map", local_category):
			continue
		frappe.get_doc(
			{
				"doctype": "Pet App WhatsApp Meta Category Map",
				"local_category": local_category,
				"meta_category": meta_category,
				"notes": notes,
			}
		).insert(ignore_permissions=True)


DOCTYPES = {
	"Pet App WhatsApp Meta Template": {
		# Meta's own id is the docname, so a re-sync is a stable upsert and the
		# local_template binding survives a rename on Meta's side.
		"autoname": "field:meta_template_id",
		"title_field": "template_name",
		"roles": ("System Manager", "Healthcare Administrator"),
		"fields": [
			{"fieldname": "meta_template_id", "fieldtype": "Data", "reqd": 1, "unique": 1},
			{"fieldname": "template_name", "fieldtype": "Data", "reqd": 1, "in_list_view": 1, "in_standard_filter": 1},
			# Meta locale code (en_US, ar, ...). Data, not Select: Meta's locale
			# list is theirs to extend.
			{"fieldname": "language", "fieldtype": "Data", "reqd": 1, "in_list_view": 1, "in_standard_filter": 1},
			# Verbatim from Meta. Never validated against a fixed list.
			{"fieldname": "category", "fieldtype": "Data", "in_list_view": 1, "in_standard_filter": 1},
			{"fieldname": "status", "fieldtype": "Data", "in_list_view": 1, "in_standard_filter": 1},
			txt("rejected_reason"),
			code("components_json"),
			code("raw_json"),
			link("provider_account", "Pet App WhatsApp Account", in_standard_filter=1),
			link("local_template", "Pet App WhatsApp Template"),
			{"fieldname": "binding_state", "fieldtype": "Data", "in_standard_filter": 1},
			dt("last_synced_at", in_list_view=1),
			check("missing_on_meta", in_list_view=1),
		],
	},
	"Pet App WhatsApp Meta Category Map": {
		"autoname": "field:local_category",
		"title_field": "local_category",
		"roles": ("System Manager", "Healthcare Administrator"),
		"fields": [
			{"fieldname": "local_category", "fieldtype": "Data", "reqd": 1, "unique": 1, "in_list_view": 1},
			# Blank is meaningful: "this local category has no Meta equivalent".
			{"fieldname": "meta_category", "fieldtype": "Data", "in_list_view": 1},
			txt("notes"),
		],
	},
}
