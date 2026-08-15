"""Add and backfill ``Vet Visit.branch`` for clinic separation.

Backfill is a correctness requirement, not cleanup. Frappe's User Permission filter is
``ifnull(branch,'')='' or branch in (...)`` (``frappe/model/db_query.py``
``add_user_permissions``), so a NULL branch is visible to *every* clinic rather than
none. Any visit left unstamped is a leak.

Resolution order for existing rows:
1. the visit doctor's mirrored ``Healthcare Practitioner.clinic_branch``
2. the default (oldest) Branch, as a catch-all

With a single Branch on site both resolve to the same value, so this patch is inert --
it only starts mattering once a second Branch exists.
"""

from __future__ import annotations

import frappe


DOCTYPE = "Vet Visit"
FIELDNAME = "branch"


def execute():
	if not frappe.db.exists("DocType", "Branch"):
		return

	ensure_branch_field()
	backfill_branch()
	frappe.clear_cache(doctype=DOCTYPE)


def ensure_branch_field():
	from frappe.custom.doctype.custom_field.custom_field import create_custom_field

	if frappe.db.has_column(DOCTYPE, FIELDNAME):
		return

	create_custom_field(
		DOCTYPE,
		{
			"fieldname": FIELDNAME,
			"label": "Branch",
			"fieldtype": "Link",
			"options": "Branch",
			"read_only": 1,
			"in_standard_filter": 1,
			"description": "Clinic that owns this visit. Set automatically from the creating user.",
		},
	)


def backfill_branch():
	if not frappe.db.has_column(DOCTYPE, FIELDNAME):
		return

	default_branch = _default_branch()
	if not default_branch:
		return

	# 1. Inherit from the visit's own doctor where that practitioner has a branch.
	frappe.db.sql(
		f"""
		update `tab{DOCTYPE}` v
		join `tabHealthcare Practitioner` p on p.name = v.doctor
		set v.`{FIELDNAME}` = p.clinic_branch
		where ifnull(v.`{FIELDNAME}`, '') = ''
		  and ifnull(p.clinic_branch, '') != ''
		"""
	)

	# 2. Anything still unstamped -- no doctor, or a doctor with no branch -- falls back
	#    to the default branch. Leaving these NULL would make them globally visible.
	frappe.db.sql(
		f"""
		update `tab{DOCTYPE}`
		set `{FIELDNAME}` = %s
		where ifnull(`{FIELDNAME}`, '') = ''
		""",
		(default_branch,),
	)


def _default_branch() -> str | None:
	rows = frappe.get_all("Branch", pluck="name", order_by="creation asc", limit_page_length=1)
	return rows[0] if rows else None
