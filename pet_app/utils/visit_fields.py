"""Write a handful of Vet Visit fields as a side effect, without re-validating the visit.

Follow-up and care-plan actions change two to five follow-up fields on the source visit.
They used to do it with visit.save(), which re-runs every link check on the whole visit -
including its billable rows. On 2026-09-14 about 298 draft Sales Invoices were deleted
without releasing the rows that point at them; those rows are deliberately left as they
are, so a full save of any of the ~295 affected visits raises LinkValidationError
("Could not find Row #1: Sales Invoice: ...") and the follow-up action fails with it.

These values are the only ones the callers change, so they are written directly.
db_set still moves `modified`/`modified_by`, exactly as the save did. What it skips is
the visit's own validate/on_update, which for these fields only paired follow_up_date
with follow_up_preferred_date and required a date when a follow-up is flagged - both
done here - and the Version entry, which a field-level write does not create.
A save from the visit page is untouched and still validates every link.
"""

from __future__ import annotations

import frappe
from frappe import _


def write_visit_fields(visit, values: dict) -> dict:
	updates = {fieldname: value for fieldname, value in values.items() if visit.meta.has_field(fieldname)}
	if "follow_up_date" in updates and visit.meta.has_field("follow_up_preferred_date"):
		updates.setdefault("follow_up_preferred_date", updates["follow_up_date"])

	required = updates.get("follow_up_required", visit.get("follow_up_required"))
	follow_up_date = (
		updates.get("follow_up_preferred_date")
		or updates.get("follow_up_date")
		or visit.get("follow_up_preferred_date")
		or visit.get("follow_up_date")
	)
	if required and not follow_up_date:
		# Same rule, same message as Vet Visit._validate_follow_up.
		frappe.throw(_("Follow-up Preferred Date is required when Follow-up Required is checked."))

	updates = {fieldname: value for fieldname, value in updates.items() if visit.get(fieldname) != value}
	if updates:
		visit.db_set(updates)
	return updates
