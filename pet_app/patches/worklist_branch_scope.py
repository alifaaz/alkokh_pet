"""Add and backfill ``branch`` on the remaining clinic worklist doctypes.

Same contract as ``vet_visit_branch_scope``: a NULL branch is visible to every clinic
(``frappe/model/db_query.py`` ``add_user_permissions``), so every row must be stamped.

``Pet Queue Ticket`` is a special case -- it already had a free-text ``branch`` Data
field, so a typo silently created a phantom branch. The Data field is converted to a
Link and any value that is not a real Branch is discarded before the backfill.
"""

from __future__ import annotations

import frappe


DOCTYPE_SOURCES = {
	# doctype: link field -> parent doctype used to inherit the branch
	"Vet Case Sheet": ("vet_visit", "Vet Visit"),
	"Pet Queue Ticket": ("visit", "Vet Visit"),
	"Pet Boarding": ("visit", "Vet Visit"),
	"Appointment": (None, None),
}


def execute():
	if not frappe.db.exists("DocType", "Branch"):
		return

	default_branch = _default_branch()
	if not default_branch:
		return

	normalise_queue_ticket_branch()

	for doctype in DOCTYPE_SOURCES:
		if not frappe.db.exists("DocType", doctype):
			continue
		ensure_branch_field(doctype)
		backfill(doctype, default_branch)
		frappe.clear_cache(doctype=doctype)


def ensure_branch_field(doctype: str):
	from frappe.custom.doctype.custom_field.custom_field import create_custom_field

	if frappe.db.has_column(doctype, "branch"):
		return

	create_custom_field(
		doctype,
		{
			"fieldname": "branch",
			"label": "Branch",
			"fieldtype": "Link",
			"options": "Branch",
			"read_only": 1,
			"in_standard_filter": 1,
			"description": "Clinic that owns this record. Set automatically from the creating user.",
		},
	)


def normalise_queue_ticket_branch():
	"""Convert ``Pet Queue Ticket.branch`` from free-text Data to a Link.

	Existing values are kept only when they name a real Branch; anything else was a
	typo or an ad-hoc label and would otherwise become a broken link.
	"""
	doctype = "Pet Queue Ticket"
	if not frappe.db.exists("DocType", doctype) or not frappe.db.has_column(doctype, "branch"):
		return

	meta_field = frappe.db.get_value(
		"DocField", {"parent": doctype, "fieldname": "branch"}, ["name", "fieldtype"], as_dict=True
	)
	if meta_field and meta_field.fieldtype == "Data":
		frappe.db.set_value("DocField", meta_field.name, "fieldtype", "Link", update_modified=False)
		frappe.db.set_value("DocField", meta_field.name, "options", "Branch", update_modified=False)

	valid = set(frappe.get_all("Branch", pluck="name"))
	rows = frappe.db.sql(
		"select name, branch from `tabPet Queue Ticket` where ifnull(branch,'') != ''", as_dict=True
	)
	for row in rows:
		if row.branch not in valid:
			frappe.db.set_value(doctype, row.name, "branch", None, update_modified=False)


def backfill(doctype: str, default_branch: str):
	if not frappe.db.has_column(doctype, "branch"):
		return

	link_field, parent_doctype = DOCTYPE_SOURCES.get(doctype, (None, None))

	# 1. Inherit from the parent record where one is linked and already stamped.
	if link_field and parent_doctype and frappe.db.has_column(doctype, link_field):
		if frappe.db.has_column(parent_doctype, "branch"):
			frappe.db.sql(
				f"""
				update `tab{doctype}` c
				join `tab{parent_doctype}` p on p.name = c.`{link_field}`
				set c.branch = p.branch
				where ifnull(c.branch, '') = ''
				  and ifnull(p.branch, '') != ''
				"""
			)

	# 2. Then from the record's own practitioner, where it has one.
	for field in ("practitioner", "doctor"):
		if frappe.db.has_column(doctype, field):
			frappe.db.sql(
				f"""
				update `tab{doctype}` c
				join `tabHealthcare Practitioner` hp on hp.name = c.`{field}`
				set c.branch = hp.clinic_branch
				where ifnull(c.branch, '') = ''
				  and ifnull(hp.clinic_branch, '') != ''
				"""
			)

	# 3. Everything still unstamped falls back to the default branch. Leaving these
	#    NULL would make them globally visible.
	frappe.db.sql(
		f"update `tab{doctype}` set branch = %s where ifnull(branch, '') = ''",
		(default_branch,),
	)


def _default_branch() -> str | None:
	rows = frappe.get_all("Branch", pluck="name", order_by="creation asc", limit_page_length=1)
	return rows[0] if rows else None
