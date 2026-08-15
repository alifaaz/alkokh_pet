"""Add and backfill ``branch`` on PetCareService and Pet Procedure.

Both are workspace worklist items (``_service_items`` / ``_procedure_items`` in
``pet_app/api/workspace.py``), so they are separated for the same reason visits are.

Branch is inherited from the linked ``Vet Visit`` where there is one, then from the
record's own practitioner, then the default -- a NULL branch would be visible to every
clinic (``frappe/model/db_query.py``, ``add_user_permissions``).
"""

from __future__ import annotations

import frappe


DOCTYPES = ("PetCareService", "Pet Procedure")
PRACTITIONER_FIELDS = ("provider", "doctor")


def execute():
	if not frappe.db.exists("DocType", "Branch"):
		return

	default_branch = _default_branch()
	if not default_branch:
		return

	for doctype in DOCTYPES:
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
			"in_standard_filter": 1,
			"description": "Clinic that owns this record. Filled automatically from the linked visit or the creating user.",
		},
	)


def backfill(doctype: str, default_branch: str):
	if not frappe.db.has_column(doctype, "branch"):
		return

	if frappe.db.has_column(doctype, "visit") and frappe.db.has_column("Vet Visit", "branch"):
		frappe.db.sql(
			f"""
			update `tab{doctype}` c
			join `tabVet Visit` v on v.name = c.visit
			set c.branch = v.branch
			where ifnull(c.branch, '') = '' and ifnull(v.branch, '') != ''
			"""
		)

	for field in PRACTITIONER_FIELDS:
		if frappe.db.has_column(doctype, field):
			frappe.db.sql(
				f"""
				update `tab{doctype}` c
				join `tabHealthcare Practitioner` hp on hp.name = c.`{field}`
				set c.branch = hp.clinic_branch
				where ifnull(c.branch, '') = '' and ifnull(hp.clinic_branch, '') != ''
				"""
			)

	frappe.db.sql(
		f"update `tab{doctype}` set branch = %s where ifnull(branch, '') = ''",
		(default_branch,),
	)


def _default_branch() -> str | None:
	rows = frappe.get_all("Branch", pluck="name", order_by="creation asc", limit_page_length=1)
	return rows[0] if rows else None
