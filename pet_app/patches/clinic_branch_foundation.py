"""Foundation for clinic (branch) separation.

Creates the initial ERPNext ``Branch``, assigns every enabled user to it, and mirrors
the value onto Healthcare Practitioner records. Retires the short-lived ``clinic branch``
DocType in favour of ERPNext ``Branch``, which was already wired into
``pet_app.api.permissions`` (``RESTRICTION_DOCTYPE_MAP["branch"] = "Branch"``) and into
the Vue ``restrictions.branch`` contract.

Assigning every user to one branch is what makes this inert: with a single branch,
nothing is filtered out. Separation only begins when a second Branch is created and
users are reassigned.
"""

from __future__ import annotations

import frappe


DEFAULT_BRANCH = "main"
LEGACY_DOCTYPE = "clinic branch"
PRACTITIONER_DOCTYPE = "Healthcare Practitioner"


def execute():
	branch = ensure_default_branch()
	if not branch:
		return

	assign_users_to_branch(branch)
	backfill_practitioner_branch()
	drop_legacy_clinic_branch()
	frappe.clear_cache()


def ensure_default_branch() -> str | None:
	if not frappe.db.exists("DocType", "Branch"):
		# ERPNext not installed; nothing to anchor separation to.
		return None

	existing = frappe.get_all("Branch", pluck="name", limit_page_length=1, order_by="creation asc")
	if existing:
		return existing[0]

	doc = frappe.get_doc({"doctype": "Branch", "branch": DEFAULT_BRANCH})
	doc.insert(ignore_permissions=True)
	return doc.name


def assign_users_to_branch(branch: str):
	"""Give every enabled non-system user a Branch User Permission.

	Skips users who already have one, so re-running the patch never clobbers a
	deliberate reassignment.
	"""
	users = frappe.get_all(
		"User",
		filters={"enabled": 1, "user_type": "System User", "name": ["not in", ("Administrator", "Guest")]},
		pluck="name",
	)
	for user in users:
		if frappe.db.exists("User Permission", {"user": user, "allow": "Branch"}):
			continue
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": user,
				"allow": "Branch",
				"for_value": branch,
				"apply_to_all_doctypes": 1,
			}
		).insert(ignore_permissions=True)


def backfill_practitioner_branch():
	"""Mirror each practitioner's resolved branch onto their record."""
	if not frappe.db.exists("DocType", PRACTITIONER_DOCTYPE):
		return
	if not frappe.db.has_column(PRACTITIONER_DOCTYPE, "clinic_branch"):
		return

	from pet_app.utils.branch import sync_practitioner_branch

	for user_id in frappe.get_all(
		PRACTITIONER_DOCTYPE,
		filters={"user_id": ["!=", ""]},
		pluck="user_id",
	):
		if user_id:
			sync_practitioner_branch(user_id)


def drop_legacy_clinic_branch():
	"""Remove the superseded ``clinic branch`` DocType and its rows.

	Safe to delete: its only consumer was ``Healthcare Practitioner.clinic_branch``,
	which now points at ``Branch``, and no practitioner row referenced it.
	"""
	if not frappe.db.exists("DocType", LEGACY_DOCTYPE):
		return

	frappe.delete_doc("DocType", LEGACY_DOCTYPE, force=True, ignore_permissions=True, delete_permanently=True)
