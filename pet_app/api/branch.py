"""Branch (clinic) context for the frontend.

Backing API for the Vue branch picker and for showing the user their current clinic.
Access control lives in ``pet_app.utils.branch``; this module only exposes it.
"""

from __future__ import annotations

import frappe

from pet_app.api.response import ok
from pet_app.api.response import standardize_response
from pet_app.utils.branch import (
	BRANCH_DOCTYPE,
	get_current_branch,
	get_user_branches,
	user_sees_all_branches,
)


@frappe.whitelist()
@standardize_response
def get_branch_context():
	"""Everything Vue needs to render the branch picker and header.

	``must_select`` is the important flag: when true the user covers more than one
	branch, so the backend cannot infer which clinic a new record belongs to and the
	frontend must present a choice. When false, omit the field entirely -- the backend
	fills it in.
	"""
	user = frappe.session.user
	sees_all = user_sees_all_branches(user)

	if sees_all:
		available = frappe.get_all(
			BRANCH_DOCTYPE,
			fields=["name", "branch"],
			order_by="creation asc",
			limit_page_length=0,
		)
	else:
		names = get_user_branches(user)
		available = [
			{"name": row.name, "branch": row.branch}
			for row in frappe.get_all(
				BRANCH_DOCTYPE,
				filters={"name": ["in", names]} if names else {"name": ["in", [""]]},
				fields=["name", "branch"],
				order_by="creation asc",
				limit_page_length=0,
			)
		]

	current = get_current_branch(user)
	if not current and len(available) == 1:
		current = available[0]["name"]

	return ok(
		{
			"current": current,
			"available": available,
			"sees_all_branches": bool(sees_all),
			"must_select": bool(len(available) > 1 and not current),
		}
	)
