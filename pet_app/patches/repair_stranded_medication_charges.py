"""Put back medication charges stranded by a cancelled invoice or a draft that lost its lines.

Two records, both audited on 2026-09-16, and both shaped the same way: the Pet Billable
Item says Billed, and the invoice it names can no longer collect the money. A Billed row is
skipped by every billing path, so until the row is released nothing will ever charge it.

1. CANCELLED INVOICE. `on_sales_invoice_cancel` did not reset medication rows, because a
   prescription records its invoice on the row and never on `Vet Visit.sales_invoice`.
   Fixed in the same change as this patch; the rows cancelled before the fix are released
   here, through the fixed function itself (`release_billable_rows_billed_to_invoice`).
   On this site that is exactly:

       VVT-2026-02482  Cefalexin 50 ml                 15,000  ACC-SINV-2026-02283
       VVT-2026-02482  test_medecaton_for_deywarming    5,000  ACC-SINV-2026-02283

   Released only - Billable, invoice cleared. They are raised again by the visit's next
   save (medication trigger `on_request`) or at visit close. This patch does not raise a
   new invoice for a guardian who has none open.

2. DRAFT WITH NO LINES. The visit page's front-end "Save as draft" sync deleted the
   medication lines of ACC-SINV-2026-02601 and never inserted its replacements, leaving
   0 item rows under a 60,000 header (VVT-2026-02536, two "Examine" at 5,000).

   The rows are released and then billed again through the REAL path,
   `bill_visit_medications_at(visit, "on_request")` - the same call a visit save makes. It
   finds the guardian's newest open draft, which is this one, appends the lines with the
   stock context `medication_invoice_context` resolves, and saves, and ERPNext recomputes
   the header from the lines that actually exist: 10,000. The 50,000 dental procedure in
   the old header was a phantom line; that procedure is already on submitted
   ACC-SINV-2026-02411 and must not be charged again, and recomputing drops it.

   Not rebuilt by hand. A hand-written line would have to reproduce the warehouse, UOM and
   conversion the billing path resolves, and get any of it wrong and the stock relief on
   submit is wrong. If the re-bill does not happen - a different trigger, a failure it
   logs - the rows stay Billable (collectable at close) and a draft that is still empty is
   deleted, which is safe only because it is a draft with no lines. That delete is
   refused if anything still points at the draft.

Gated on predicates, not names: a cancelled invoice that Billable rows still name, and a
draft whose header disagrees with its lines. Only medication rows on Vet Visits are acted
on. Anything else found in either state is reported and left alone, because no other kind
can be re-billed from here without its order document.

NOT covered, on purpose: 591 rows on 294 visits still Billed against the 298 drafts
deleted on 2026-09-14. That deletion was requested by the owner; whether those charges
are raised again or written off is theirs to decide.

Idempotent. A released row no longer names the invoice, and a repaired draft's header
matches its lines, so a second run selects nothing.
"""

from __future__ import annotations

import frappe
from frappe.utils import cstr, flt

from pet_app.utils.visit_billing import release_billable_rows_billed_to_invoice

MEDICATION_PREFIX = "medication::"


def execute():
	if not frappe.db.has_column("Pet Billable Item", "sales_invoice"):
		return

	report: list[str] = []
	_release_rows_on_cancelled_invoices(report)
	_repair_emptied_drafts(report)

	message = "\n".join(report) if report else "No stranded medication charges found."
	print(message)
	if report:
		frappe.logger("pet_app.billing").info({"event": "STRANDED_MEDICATION_CHARGES_REPAIRED", "report": report})


def _rows_naming(invoice_name: str) -> list[frappe._dict]:
	return frappe.get_all(
		"Pet Billable Item",
		filters={"sales_invoice": invoice_name},
		fields=["name", "parenttype", "parent", "item_code", "item_type", "status", "amount", "linked_service_id"],
		ignore_permissions=True,
	)


def _is_visit_medication(row) -> bool:
	return row.parenttype == "Vet Visit" and cstr(row.linked_service_id).startswith(MEDICATION_PREFIX)


def _label(row) -> str:
	return f"{row.parent} / {row.name} / {row.item_code} / {flt(row.amount):g}"


def _release_rows_on_cancelled_invoices(report: list[str]):
	invoices = frappe.db.sql_list(
		"""
		select distinct b.sales_invoice
		from `tabPet Billable Item` b
		join `tabSales Invoice` si on si.name = b.sales_invoice
		where si.docstatus = 2
		order by b.sales_invoice
		"""
	)
	for invoice in invoices:
		rows = _rows_naming(invoice)
		foreign = [row for row in rows if not _is_visit_medication(row)]
		if foreign:
			# An order row still naming a cancelled invoice means release_orders_billed_to_invoice
			# missed it too. Not this patch's kind; named so it is found.
			report.append(
				f"LEFT ALONE {invoice} (cancelled): non-medication rows still name it: "
				+ ", ".join(_label(row) for row in foreign)
			)
			continue
		released = release_billable_rows_billed_to_invoice(invoice)
		report.append(
			f"Released from cancelled {invoice}: "
			+ ", ".join(_label(row) for row in rows if row.name in released)
		)


def _emptied_drafts() -> list[str]:
	return frappe.db.sql_list(
		"""
		select si.name
		from `tabSales Invoice` si
		where si.docstatus = 0
		  and exists (select 1 from `tabPet Billable Item` b where b.sales_invoice = si.name)
		  and abs(ifnull(si.total, 0) - (
				select ifnull(sum(i.amount), 0) from `tabSales Invoice Item` i
				where i.parent = si.name and i.parenttype = 'Sales Invoice'
		  )) > 0.005
		order by si.name
		"""
	)


def _repair_emptied_drafts(report: list[str]):
	for invoice in _emptied_drafts():
		rows = _rows_naming(invoice)
		foreign = [row for row in rows if not _is_visit_medication(row)]
		if foreign:
			report.append(
				f"LEFT ALONE {invoice} (draft, header != lines): non-medication rows name it: "
				+ ", ".join(_label(row) for row in foreign)
			)
			continue
		if frappe.db.count("Sales Invoice Item", {"parent": invoice, "parenttype": "Sales Invoice"}):
			# Some lines survive. Which of the named charges are among them is a matching
			# question this patch does not guess at; the empty case is the one on this site.
			report.append(f"LEFT ALONE {invoice} (draft, header != lines): it still has lines, needs a manual look")
			continue

		release_billable_rows_billed_to_invoice(invoice)
		report.append(f"Released from emptied draft {invoice}: " + ", ".join(_label(row) for row in rows))

		# Imported here: order_billing pulls in invoice_reuse and the ERPNext selling chain.
		from pet_app.utils.order_billing import bill_visit_medications_at

		for visit_name in sorted({row.parent for row in rows}):
			billed = bill_visit_medications_at(frappe.get_doc("Vet Visit", visit_name), "on_request")
			for result in billed:
				report.append(
					f"  Re-billed {visit_name} -> {result.get('sales_invoice')} ({flt(result.get('amount')):g})"
				)

		_remove_if_still_empty(invoice, report)


def _remove_if_still_empty(invoice: str, report: list[str]):
	if not frappe.db.exists("Sales Invoice", invoice):
		return
	if frappe.db.count("Sales Invoice Item", {"parent": invoice, "parenttype": "Sales Invoice"}):
		total = frappe.db.get_value("Sales Invoice", invoice, "grand_total")
		report.append(f"  {invoice} now has its lines back; grand_total {flt(total):g}")
		return
	# Still empty: the re-bill went elsewhere or did not happen. An empty draft is only
	# safe to remove when nothing refers to it any more.
	still_named = frappe.db.count("Pet Billable Item", {"sales_invoice": invoice})
	linked = [
		dt
		for dt in ("Vet Visit", "Pet Boarding")
		if frappe.db.count(dt, {"sales_invoice": invoice})
	]
	referenced = frappe.db.count("Payment Entry Reference", {"reference_name": invoice})
	if still_named or linked or referenced:
		report.append(f"  {invoice} is still empty and still referenced - LEFT for a manual look")
		return
	frappe.delete_doc("Sales Invoice", invoice, ignore_permissions=True)
	report.append(f"  {invoice} was still empty with nothing pointing at it - deleted (the charges stay Billable)")
