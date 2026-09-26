"""Per-screen categories for the unified template picker.

Every sending surface calls the same ``list_template_options`` and gets the whole
catalogue - 36 entries today, mirror rows and local rows together. A death-certificate
screen offers boarding templates, a lab screen offers reminders, and the operator picks
the wrong one by scrolling too fast. What is missing is not a filter in each screen but
one place to say which screen a template belongs to.

``category`` on both template doctypes is NOT that place and is not touched here: it
carries Meta's own MARKETING / UTILITY / AUTHENTICATION (mapped through
``Pet App WhatsApp Meta Category Map`` for display) and the picker shows it in the
subtitle. The new axis is ``surface_category``, and the two are independent: a boarding
reminder is UTILITY to Meta and ``boarding`` to us.

Three things, all additive:

* ``Pet App Template Category`` - the list of screens, as records rather than a Select,
  for the same reason ``source_doctype`` is Data: the set grows by configuration, and
  Select options baked into a patch would be a second copy that is wrong the day the
  first one moves. Desk-only; no route, no page, no whitelisted endpoint.
* ``surface_category`` on BOTH template doctypes. A user picks either kind - the mirror
  emits one entry per Meta row and 21 of the 23 mirror rows have no local row at all -
  so a field on the local doctype alone would leave most of the catalogue impossible to
  categorise. Meta never sends or reads this field, and ``upsert_mirror_row`` assigns
  only its own named fields, so a sync leaves it alone, exactly like ``source_doctype``
  and ``slot_map_stale``.
* The nine screen records, seeded blank-Arabic for the owner to fill in desk.

Idempotent by construction: ``ensure_doctype`` adds only missing fields and leaves an
existing doctype's permissions alone, and the seed inserts only what is absent - a
second run changes nothing, and a label edited in desk survives it. No existing row is
read or written: every template starts uncategorised, and an uncategorised template is
returned to every screen, so nothing disappears from any picker until someone sets a
value.
"""

from __future__ import annotations

import frappe

from pet_app.patches.p1_5_notification_engine_schema import MODULE, check, ensure_doctype, field_doc, link, txt

CATEGORY_DOCTYPE = "Pet App Template Category"

# (category_name, label_en, description). label_ar is deliberately left empty: the
# Arabic wording is the owner's, and a machine guess in a customer-facing picker is
# worse than a blank the desk list shows as missing.
CATEGORIES = [
	("general_guardian", "General / Guardian", "Free guardian messages that belong to no single clinical screen."),
	("treatment_followup", "Treatment & Follow-up", "Follow-up and treatment-plan screens on a visit."),
	("reminder", "Reminder", "The reminder surfaces - anything scheduled ahead of a due date."),
	("boarding", "Boarding", "Boarding check-in, stay updates and check-out."),
	("lab", "Lab", "Lab orders and lab result delivery."),
	("radiology", "Radiology", "Radiology orders and reviewed-report delivery."),
	("preventive", "Preventive Care", "Vaccination and deworming - the preventive care screens."),
	("death_certificate", "Death Certificate", "The death record screen and its certificate delivery."),
	("invoice", "Invoice", "Invoice and payment messages sent from billing."),
]

TEMPLATE_DOCTYPES = (
	"Pet App WhatsApp Template",
	"Pet App WhatsApp Meta Template",
)


def execute():
	ensure_category_doctype()
	for doctype in TEMPLATE_DOCTYPES:
		# Empty is legal and means "every screen": an uncategorised template is returned
		# to a filtered picker as well as an unfiltered one.
		ensure_doctype(doctype, {"fields": [link("surface_category", CATEGORY_DOCTYPE, in_standard_filter=1)]})
	seed_categories()
	frappe.clear_cache()


def ensure_category_doctype():
	"""Create it with the permissions below, or - if it is already there - only add fields.

	``ensure_doctype`` builds permissions from its own role helper and cannot express
	"full for two roles, read for a third", so the create path is written out here. Its
	update path is exactly what is wanted on a re-run: missing fields are added and the
	permissions are left as they stand, so a grant the owner changed in desk survives.
	"""
	if frappe.db.exists("DocType", CATEGORY_DOCTYPE):
		ensure_doctype(CATEGORY_DOCTYPE, CATEGORY_SPEC)
		return

	frappe.get_doc(
		{
			"doctype": "DocType",
			"name": CATEGORY_DOCTYPE,
			"module": MODULE,
			"custom": 1,
			"issingle": 0,
			"istable": 0,
			"autoname": CATEGORY_SPEC["autoname"],
			"title_field": CATEGORY_SPEC["title_field"],
			"track_changes": 1,
			"allow_rename": 0,
			"fields": [field_doc(field) for field in CATEGORY_SPEC["fields"]],
			"permissions": CATEGORY_SPEC["permissions"],
			"sort_field": CATEGORY_SPEC["sort_field"],
			"sort_order": CATEGORY_SPEC["sort_order"],
		}
	).insert(ignore_permissions=True)


def seed_categories():
	for category_name, label_en, description in CATEGORIES:
		if frappe.db.exists(CATEGORY_DOCTYPE, category_name):
			# Never overwrite: the labels are the owner's to edit, including the Arabic
			# one this patch cannot supply.
			continue
		frappe.get_doc(
			{
				"doctype": CATEGORY_DOCTYPE,
				"category_name": category_name,
				"label_en": label_en,
				"label_ar": "",
				"enabled": 1,
				"description": description,
			}
		).insert(ignore_permissions=True)


def _permissions():
	"""Written out rather than taken from the schema helper, which grants read/write/create
	to every role it is given. The two roles that administer templates get the doctype in
	full; Healthcare Administrator gets read only - it already reads the mirror rows this
	field annotates, and without read on the target doctype the desk link field offers it
	nothing to pick.
	"""
	full = ("System Manager", "Pet App Admin")
	read_only = ("Healthcare Administrator",)
	# Every flag is written out, including the zeros. `write`, `create` and `delete`
	# default to 1 on DocPerm, so a row that simply omits them is a full grant wearing
	# the word "read".
	flags = ("read", "write", "create", "delete", "submit", "cancel", "amend", "print", "email", "export", "report", "share")

	def row(role, **granted):
		return dict({"role": role}, **{flag: 1 if granted.get(flag) else 0 for flag in flags})

	rows = [
		row(role, read=1, write=1, create=1, delete=1, print=1, email=1, export=1, report=1, share=1)
		for role in full
	]
	rows += [row(role, read=1, print=1, export=1, report=1) for role in read_only]
	return rows


CATEGORY_SPEC = {
	"autoname": "field:category_name",
	"title_field": "label_en",
	"sort_field": "category_name",
	"sort_order": "ASC",
	"fields": [
		{"fieldname": "category_name", "fieldtype": "Data", "reqd": 1, "unique": 1, "in_list_view": 1},
		{"fieldname": "label_en", "fieldtype": "Data", "in_list_view": 1},
		{"fieldname": "label_ar", "fieldtype": "Data", "in_list_view": 1},
		check("enabled", default="1", in_list_view=1, in_standard_filter=1),
		txt("description"),
	],
	"permissions": _permissions(),
}
