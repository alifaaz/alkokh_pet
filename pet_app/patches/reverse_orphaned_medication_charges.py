"""Take back medication charges whose prescription row was deleted before the fix landed.

Until `_sync_prescribed_medications_billables` learned to reverse them, deleting a
prescription row from a Vet Visit left its `Pet Billable Item` behind with `status =
Billed`, its `sales_invoice` still set, and the invoice line untouched - so the guardian
was still being asked to pay for a medication the visit no longer prescribes. The visit
page only ever deleted rows; the `cancel_medication` action, which always reversed
correctly, was last used on 2026-07-25. Every orphan in this site was made that way.

Twenty-two orphans exist at the time of writing. Twenty are already `Cancelled` with no
invoice - the deletion reconciler caught them because they had not been billed yet - and
this patch does not touch them; they are bookkeeping, not money. Two are `Billed`:

    VVT-2026-01982  Ceftriaxone 1g Injection  5,000  ACC-SINV-2026-01652  Draft
    VVT-2026-01997  Penicillin-Streptomycin   5,000  ACC-SINV-2026-01716  SUBMITTED, Paid

Only the first is reversed here.

WHY THE SUBMITTED ONE IS LEFT ALONE. A line may never be removed from an issued document.
`ACC-SINV-2026-01716` has been submitted and paid; taking 5,000 off it is a Credit Note,
which is an accounting decision and not one a migration may make on the clinic's behalf.
The row is named in the log so it can be found, and it is left exactly as it is. The
side effect the owner has to know about is that VVT-2026-01997 cannot be saved at all
while this orphan stands - the reversal is attempted on every save of that visit and
correctly refuses - so the accounting decision is also what unblocks the visit.

WHY IT REUSES `cancel_visit_billable_item_by_link` RATHER THAN WRITING THE ROWS. That is
the same call the `cancel_medication` action makes and the same one the deletion path now
makes. It removes the line from the draft invoice, deletes the invoice outright when the
line was its last one (releasing every backlink first, so nothing is left pointing at a
document that no longer exists), clears `sales_invoice` on the row, marks it `Cancelled`
and recomputes the visit's total. A second implementation of "take a charge back" is
exactly what this app does not need, and the risk it would carry - a line removed from a
submitted invoice - is the one the shared assertion already refuses.

Saving the visit re-runs its normal validation, which under the `on_request` medication
trigger will raise charges for any prescription on that visit that is not billed yet.
That is the configured behaviour of any save of that visit, not something this patch
introduces, and a row that is already `Billed` is never billed twice.

Idempotent and re-runnable: a reversed row is no longer `status = Billed`, so a second run
does not select it. Written generally rather than against the two names above, so it also
catches anything that reached this state between the fix landing and the workers being
restarted. Scoped to Vet Visit parents because every orphan on this site has one; a
boarding equivalent would need `cancel_boarding_billable_item_by_link` and there is
nothing for it to do.

Nothing is skipped silently. Every row this patch declines to reverse is named, with the
reason, in an Error Log entry and in the migrate output.
"""

from __future__ import annotations

import frappe
from frappe.utils import cstr

from pet_app.utils.visit_billing import cancel_visit_billable_item_by_link

MEDICATION_PREFIX = "medication::"


def execute():
	if not frappe.db.has_column("Pet Billable Item", "sales_invoice"):
		# Pre-dates the column that records which invoice a prescription was billed onto.
		# Without it an orphan cannot be traced to a line, and guessing is how a wrong line
		# gets removed.
		return

	reversed_rows: list[str] = []
	skipped: list[str] = []

	for row in _orphaned_billed_medication_rows():
		invoice = cstr(row.sales_invoice).strip()
		label = f"{row.parent} / {row.name} / {row.item_code} / {row.amount}"

		if not invoice:
			# Billed, but nothing records onto what. The line cannot be located, so it
			# cannot be removed; cancelling the row here would hide a charge the guardian
			# is still going to be asked to pay.
			skipped.append(f"{label}: no sales_invoice recorded on the row")
			continue

		if not frappe.db.exists("Sales Invoice", invoice):
			skipped.append(f"{label}: invoice {invoice} no longer exists")
			continue

		docstatus = frappe.db.get_value("Sales Invoice", invoice, "docstatus")
		if docstatus != 0:
			state = "submitted" if docstatus == 1 else "cancelled"
			skipped.append(
				f"{label}: invoice {invoice} is {state} - left untouched, needs a Credit Note "
				f"or an accounting decision. NOTE: {row.parent} cannot be saved until this is resolved."
			)
			continue

		try:
			cancel_visit_billable_item_by_link(
				row.parent,
				linked_service_id=row.linked_service_id,
				item_type="Medication",
			)
		except Exception:
			# One unreversible orphan must not abort the migration, but it must not pass
			# unnoticed either: a charge left standing is the whole point of this patch.
			frappe.log_error(
				frappe.get_traceback(),
				f"Orphaned medication charge could not be reversed: {label}",
			)
			skipped.append(f"{label}: reversal failed, see Error Log")
			continue

		reversed_rows.append(f"{label} (invoice {invoice})")

	_report(reversed_rows, skipped)


def _orphaned_billed_medication_rows():
	"""Billed medication charges whose prescription row has been deleted."""
	rows = frappe.db.sql(
		"""
		select name, parent, linked_service_id, item_code, amount, sales_invoice
		from `tabPet Billable Item`
		where parenttype = 'Vet Visit'
		  and parentfield = 'billable_items'
		  and status = 'Billed'
		  and linked_service_id like %(prefix)s
		""",
		{"prefix": MEDICATION_PREFIX + "%"},
		as_dict=True,
	)

	orphans = []
	for row in rows:
		medication_row = cstr(row.linked_service_id).split("::", 1)[-1].strip()
		if not medication_row:
			continue
		if frappe.db.exists("Vet Visit Medication Item", medication_row):
			# The prescription is still there. Whatever else may be true of this charge,
			# it is not an orphan and is none of this patch's business.
			continue
		if not frappe.db.exists("Vet Visit", row.parent):
			continue
		orphans.append(row)
	return orphans


def _report(reversed_rows: list[str], skipped: list[str]):
	if reversed_rows:
		message = "Reversed orphaned medication charges:\n" + "\n".join(reversed_rows)
		print(message)
		frappe.logger("pet_app.billing").info(
			{"event": "ORPHANED_MEDICATION_CHARGES_REVERSED", "rows": reversed_rows}
		)

	if skipped:
		message = "Orphaned medication charges NOT reversed:\n" + "\n".join(skipped)
		print(message)
		frappe.log_error(message, "Orphaned medication charges left for the owner")

	if not reversed_rows and not skipped:
		print("No orphaned medication charges found.")
