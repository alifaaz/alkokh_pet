"""A screen can show several template categories: the binding moves from the category to the screen.

Until now a category carried ``page_key`` and ``surface_key`` (p1_29 / p1_30), each unique, so
a page resolved to exactly one category and could never show lab + boarding + general_guardian
together. The binding is inverted: one ``Pet App Template Screen`` record per screen, listing
the categories that screen shows.

* ``Pet App Template Screen`` - ``page_key`` OR ``surface_key`` (exactly one, each unique: a
  screen has ONE record, and that record holds the whole list), ``label``, ``enabled``, and
  ``categories``, a Table MultiSelect over ``Pet App Template Category``. Many-to-many: a
  screen lists several categories, and a category may appear on several screens.
* ``Pet App Template Screen Category`` - the child row, one Link.

Templates are untouched: each still carries ONE ``surface_category``. The multiplicity is the
screen's.

MIGRATION, LOSSLESS AND IDEMPOTENT. Every category holding a ``page_key`` becomes a screen
record for that page listing that one category; likewise each ``surface_key``. The keys were
unique per category, so every screen starts with exactly the one category it resolved to
before, and every resolver answer is unchanged on the day this runs. A screen record that
already exists for a key is left alone - never overwritten.

The category's own ``page_key`` / ``surface_key`` are kept, read-only, with their values:
nothing is deleted, and the pre-migration answer stays inspectable. The resolver stops
reading them once screen records exist.

Additive DDL only: two new tables, and two DocField properties on the category. No template
row is read or written.
"""

from __future__ import annotations

import frappe

from pet_app.patches.p1_5_notification_engine_schema import MODULE, check, ensure_doctype, field_doc, link, txt
from pet_app.patches.p1_28_template_surface_category import CATEGORY_DOCTYPE, _permissions

SCREEN_DOCTYPE = "Pet App Template Screen"
SCREEN_CATEGORY_DOCTYPE = "Pet App Template Screen Category"

CHILD_SPEC = {
	"istable": 1,
	"fields": [link("category", CATEGORY_DOCTYPE, reqd=1, in_list_view=1)],
}

SCREEN_FIELDS = [
	{"fieldname": "label", "fieldtype": "Data", "in_list_view": 1},
	{"fieldname": "page_key", "fieldtype": "Data", "unique": 1, "in_list_view": 1, "in_standard_filter": 1},
	{"fieldname": "surface_key", "fieldtype": "Data", "unique": 1, "in_list_view": 1, "in_standard_filter": 1},
	check("enabled", default="1", in_list_view=1, in_standard_filter=1),
	{"fieldname": "categories", "fieldtype": "Table MultiSelect", "options": SCREEN_CATEGORY_DOCTYPE},
	txt("description"),
]

LEGACY_NOTE = (
	"Legacy (before p1_31). Screens now list their categories in Pet App Template Screen; "
	"this value is kept for reference and no longer read."
)


def execute():
	if not frappe.db.exists("DocType", CATEGORY_DOCTYPE):
		return
	ensure_doctype(SCREEN_CATEGORY_DOCTYPE, CHILD_SPEC)
	ensure_screen_doctype()
	frappe.clear_cache()
	migrate_bindings()
	retire_category_keys()


def ensure_screen_doctype():
	"""Create it with the category doctype's own grants, or only add missing fields.

	Same reason as p1_28: ensure_doctype builds permissions from its own role helper, which
	cannot express "full for two roles, read for a third".
	"""
	if frappe.db.exists("DocType", SCREEN_DOCTYPE):
		ensure_doctype(SCREEN_DOCTYPE, {"fields": SCREEN_FIELDS})
		return
	frappe.get_doc(
		{
			"doctype": "DocType",
			"name": SCREEN_DOCTYPE,
			"module": MODULE,
			"custom": 1,
			"issingle": 0,
			"istable": 0,
			"autoname": "hash",
			"title_field": "label",
			"track_changes": 1,
			"allow_rename": 0,
			"fields": [field_doc(field) for field in SCREEN_FIELDS],
			"permissions": _permissions(),
			"sort_field": "modified",
			"sort_order": "DESC",
		}
	).insert(ignore_permissions=True)


def migrate_bindings() -> list[tuple[str, str, str]]:
	"""One screen record per category-held key. Returns what it created: (field, key, category)."""
	created = []
	for fieldname in ("page_key", "surface_key"):
		if not frappe.db.has_column(CATEGORY_DOCTYPE, fieldname):
			continue
		rows = frappe.get_all(
			CATEGORY_DOCTYPE,
			filters={fieldname: ["is", "set"]},
			fields=["name", fieldname],
			order_by="name asc",
			ignore_permissions=True,
		)
		for row in rows:
			key = (row.get(fieldname) or "").strip()
			if not key or frappe.db.exists(SCREEN_DOCTYPE, {fieldname: key}):
				continue
			frappe.get_doc(
				{
					"doctype": SCREEN_DOCTYPE,
					fieldname: key,
					"enabled": 1,
					"categories": [{"category": row.name}],
				}
			).insert(ignore_permissions=True)
			created.append((fieldname, key, row.name))
	return created


def retire_category_keys():
	"""The category's own keys become read-only, values kept. Idempotent."""
	doc = frappe.get_doc("DocType", CATEGORY_DOCTYPE)
	changed = False
	for field in doc.fields:
		if field.fieldname in ("page_key", "surface_key") and (not field.read_only or field.description != LEGACY_NOTE):
			field.read_only = 1
			field.description = LEGACY_NOTE
			changed = True
	if changed:
		doc.save(ignore_permissions=True)
		frappe.clear_cache(doctype=CATEGORY_DOCTYPE)
