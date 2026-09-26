"""Bill an administered vaccination or deworming to its guardian.

THIS IS `utils.care_service_billing` RE-POINTED, NOT A SECOND BILLER. The route, the
helpers, the elevation pattern and the plan-then-commit discipline are all that module's,
carried over verbatim; what changed is the doctype it reads and the fact that it no longer
has to re-derive anything. Every downstream call - `get_or_create_open_invoice`,
`resolve_customer_for_clinical_record`, `resolve_dispense_warehouse`, `find_billed_invoice`,
`build_marker` - is the same function the visit path and the Service Provider screen use, so
this cannot drift from them on price, payer, warehouse or idempotency.

WHY IT IS SHORTER THAN WHAT IT REPLACES. `plan_care_service_billing` had to resolve the item
and the rate at billing time, through `_clinical_record_item_name` / `_clinical_record_rate`
and a `CareService template` fetch, because a `PetCareService` created from a visit order
carried neither. `Preventive Care Record` carries both: the controller snapshots `item_code`,
`rate` and `medication_name` from the catalogue at every unbilled save (see
`utils.preventive_catalogue`). So the biller reads the record's own fields, the invoice line
is built from the same values the operator saw on screen, and there is no second resolution
path that could disagree with the first.

WHY A PLAN AND A COMMIT, for the reason `order_billing`'s module docstring gives: a dose that
cannot be billed must not reach `Administered`. `plan_preventive_billing` runs BEFORE the
status transition and throws naming the fix; `commit_preventive_billing` runs after the save.
So a vaccination with no price stays `Ordered`, the operator is told exactly what to correct,
and no administered-but-uncharged record is created. Neither function is idempotent by
accident - `find_billed_invoice` makes the planner return None for anything already on a
live invoice, which is what makes a retry safe.

SCOPE. Records with no `visit` only. A visit-linked dose is the visit's charge and is billed
through `Vet Visit.billable_items` at close, exactly as a visit-linked Lab is; billing it here
as well would charge the guardian twice. That path is wired in the stage that makes this
doctype orderable from a visit - until then `plan_preventive_billing` returns None for a
visit-linked record and it bills nowhere, which is the safe direction to be incomplete in.

STOCK IS NOT POSTED HERE. The warehouse this resolves goes onto the invoice LINE, so that
`invoice_stock` classifies the document correctly when the item is a stock item. Relieving
the shelf is a separate leg with its own plan and commit.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, getdate

from pet_app.utils.guardian_customer import resolve_customer_for_clinical_record
from pet_app.utils.invoice_reuse import get_or_create_open_invoice
from pet_app.utils.invoice_source import find_billed_invoice
from pet_app.utils.medication_stock import resolve_dispense_warehouse
from pet_app.utils.preventive_catalogue import resolve_medication

SOURCE_DOCTYPE = "Preventive Care Record"

# One vial, one line. Neither kind is dosed per weight on the INVOICE: the weight band moves
# the price (`Care Service Billing Option.default_rate`), not the billed quantity. How much of
# the drug leaves the shelf is a different number entirely - `qty` - and it belongs to the
# stock leg, not here.
BILLED_QTY = 1


def plan_preventive_billing(record):
	"""Everything needed to bill this dose, or None when it is not billed here.

	Returns None for the cases that are somebody else's job or already done, and throws for
	the ones a human has to fix. Never mutates the record.
	"""
	# A dose given on a visit is the visit's charge. See SCOPE above.
	if cstr(record.get("visit")).strip():
		return None

	if cint(record.get("billed")) or cstr(record.get("sales_invoice")).strip():
		return None

	# Stronger than the flag above and checked second on purpose: the marker is written by
	# every path that bills, the flag only by paths that know to write it.
	if find_billed_invoice(SOURCE_DOCTYPE, record.name):
		return None

	item_code = cstr(record.get("item_code")).strip()
	if not item_code:
		# Unreachable through the controller, which refuses to save a record whose catalogue
		# rows have no item. Checked anyway, because a direct `db_set` could produce it and
		# an invoice line with no item is not a recoverable error.
		frappe.throw(
			_(
				"{0} {1} has no Item Code, so it cannot be billed. Set Item Code on its Care Service "
				"or Service Option before administering it."
			).format(_(cstr(record.get("kind")) or "Preventive care"), frappe.bold(record.name))
		)

	rate = flt(record.get("rate"))
	if rate <= 0:
		frappe.throw(
			_(
				"{0} {1} has no price, so it cannot be billed. Set Default Price on its Care Service, "
				"or a rate on its Service Option, before administering it."
			).format(_(cstr(record.get("kind")) or "Preventive care"), frappe.bold(record.name))
		)

	# Throws by itself, naming the dead end, when no guardian or pet owner resolves.
	customer = resolve_customer_for_clinical_record(record)

	# `performing_branch` first, then the record's own `branch`: the branch that DID the work
	# owns both the revenue and the shelf the goods leave, which is the rule `order_billing`
	# applies to revenue. Exactly the precedence `care_service_billing` used.
	#
	# BOTH HALVES ARE LOAD-BEARING, and the first is empty. `performing_branch` is blank on
	# every catalogue row on this site, so in practice the answer always comes from `branch` -
	# which is why this record carries one at all. Dropping to None instead would make
	# `get_or_create_open_invoice` refuse for any user who is not restricted to a single
	# clinic ("Select a branch for this Sales Invoice"), which is every back-office account.
	branch = cstr(record.get("performing_branch") or record.get("branch")).strip() or None

	# Only a stock item needs a warehouse on its line; `invoice_stock` decides the document
	# flag from the item, not from us. Reused rather than reimplemented: one chain decides
	# where goods leave, whether they are dispensed on a visit, on a boarding, or billed here.
	# It throws by itself, naming the three places to fix, when nothing resolves - which is
	# the refusal we want, raised in the pre-flight window while the record is still `Ordered`.
	warehouse = None
	if cint(frappe.db.get_value("Item", item_code, "is_stock_item")):
		warehouse = resolve_dispense_warehouse(
			medication=None,
			medication_item=item_code,
			branch=branch,
			label=f"{record.name} ({item_code})",
			purpose=_("bill"),
		)

	return frappe._dict(
		kind=cstr(record.get("kind")),
		item_code=item_code,
		item_name=cstr(record.get("medication_name")).strip() or item_code,
		rate=rate,
		customer=customer,
		guardian=cstr(record.get("guardian")).strip() or None,
		branch=branch,
		warehouse=warehouse,
	)


def commit_preventive_billing(record, plan):
	"""Raise the Draft charge and record it on the record."""
	if not plan:
		return None

	line = {
		"item_code": plan.item_code,
		"qty": BILLED_QTY,
		"rate": plan.rate,
		"description": plan.item_name,
	}
	# Doses may share a branch's open Draft; a warehouse on the line does not by itself
	# classify the invoice as a stock document.
	if plan.warehouse:
		line["warehouse"] = plan.warehouse

	result = get_or_create_open_invoice(
		customer=plan.customer,
		items=[line],
		source_doctype=SOURCE_DOCTYPE,
		source_name=record.name,
		branch=plan.branch,
		posting_date=getdate(),
		due_date=getdate(),
		ignore_pricing_rule=1,
		remarks=_("{0} {1} billed on administration.").format(_(SOURCE_DOCTYPE), record.name),
		guardian=plan.guardian,
		# is_pos stays 0: this is a Draft the cashier settles, never an immediate-payment POS
		# invoice, and only is_pos = 0 participates in draft reuse.
		is_pos=0,
		# Authorise-then-elevate. The caller has already proved write access to this record,
		# and `branch` is the record's own stamped branch - not a request parameter, so the
		# elevation cannot be steered by whoever is calling.
		branch_authorised=True,
	)
	invoice = result.invoice

	# db.set_value, not doc.save: saving the record here would re-enter its controller from
	# inside the transition that is already running.
	# `stock_warehouse` records WHERE the goods leave from, and it is knowable here and only
	# here: it is the warehouse this line was billed against, resolved by the one chain every
	# dispense path uses. `stock_issued_qty` and `stock_entry` stay empty on purpose - nothing
	# is issued at administration, and a number written here would claim a movement that has
	# not happened. The invoice's own ledger entries are the record of what left.
	stamps = {"sales_invoice": invoice.name, "billed": 1}
	if plan.warehouse:
		stamps["stock_warehouse"] = plan.warehouse
	frappe.db.set_value(SOURCE_DOCTYPE, record.name, stamps, update_modified=False)
	record.sales_invoice = invoice.name
	record.billed = 1
	if plan.warehouse:
		record.stock_warehouse = plan.warehouse

	frappe.logger("pet_app.billing").info(
		{
			"event": "PREVENTIVE_CARE_BILLED_ON_ADMINISTRATION",
			"record": record.name,
			"kind": plan.kind,
			"sales_invoice": invoice.name,
			"customer": plan.customer,
			"amount": flt(plan.rate) * BILLED_QTY,
			"created": result.created,
		}
	)

	return frappe._dict(
		sales_invoice=invoice.name,
		customer=plan.customer,
		amount=flt(plan.rate) * BILLED_QTY,
		created=result.created,
	)


# ─────────────────────────────────────────────────────────────────────────────
# Stock
# ─────────────────────────────────────────────────────────────────────────────
#
# COMPLETION DOES NOT POST A MATERIAL ISSUE, and that is the settled decision this leg
# inherits from `care_service_billing.plan_care_service_stock` rather than revisiting. Stock
# follows the Sales Invoice: `invoice_stock.prepare_invoice_stock` sets `update_stock` from
# the Item and Product Bundle contents, and ERPNext writes the ledger entries on submit. A
# second, independent Material Issue at administration would deduct the same vial twice.
#
# SO WHAT IS THIS LEG FOR? Catching the half-configured catalogue, loudly, in the pre-flight
# window. A band that claims to consume a tablet while the invoice will relieve nothing
# drifts the count silently - which is exactly what `stock_deduction_qty`'s "0 means NOT
# CONFIGURED" rule exists to prevent. Both branches below are carried over verbatim.


def _report_stock_not_relieved(record, message: str, *, reason: str, **context):
	"""Say, on screen and in the log, that a configured deduction did not happen.

	LOUD, BUT NOT FATAL, and the asymmetry is deliberate. It is worth interrupting somebody
	over - a band that claims to consume a tablet and consumes nothing is a silent count
	drift. It is not worth voiding the charge over: the guardian was treated and owes for it
	either way, and refusing the sale to protect a stock count moves the damage rather than
	preventing it. Orange, not red, and the band to fix is named in the text.
	"""
	frappe.msgprint(message, title=_("Stock not relieved"), indicator="orange")
	frappe.logger("pet_app.billing").warning(
		{
			"event": "PREVENTIVE_STOCK_NOT_RELIEVED",
			"record": getattr(record, "name", None),
			"reason": reason,
			**context,
		}
	)


def plan_preventive_stock(record):
	"""Check that a configured consumable will actually be relieved. Never mutates.

	Returns None always - there is nothing to commit, because the invoice does the relieving.
	The return value exists so this reads like every other plan/commit pair and so a future
	leg that DOES post something has a place to put its plan.

	ONE REFINEMENT OVER THE PATH THIS REPLACES, and it is deliberate.
	`plan_care_service_stock` read `stock_deduction_qty` off the band at completion time. This
	reads `record.qty` - the same number, defaulted from the band when the record was created,
	and adjustable by the operator for a partial dose. The dose that was GIVEN is the dose
	that is checked, not the dose the band happens to configure today; a band edited between
	ordering and administering must not retroactively change what this record claims to have
	consumed.
	"""
	from pet_app.utils.invoice_stock import stock_items_for

	qty = flt(record.get("qty"))
	medication = resolve_medication(record.get("service_option"), record.get("care_service"))
	medication_item = frappe.db.get_value("Medication", medication, "linked_item") if medication else None
	item_code = cstr(record.get("item_code")).strip()

	if qty <= 0:
		# 0 STILL MEANS NOT CONFIGURED - but "not configured" is only harmless when there is
		# nothing configured to consume. Resolving the medication FIRST is what separates the
		# two cases that a bare `qty <= 0` used to collapse into one silent pass:
		#
		#   NO CONSUMABLE -> nothing was ever promised, so there is nothing to check and
		#                    nothing to report. A grooming-style band, or a history dose.
		#
		#   A CONSUMABLE, AND NO QUANTITY -> somebody said this dose consumes a named drug and
		#                    never said how much of it. The dose would be recorded and billed
		#                    claiming nothing was taken off the shelf while the invoice relieves
		#                    a unit, which is the drift this whole function exists to prevent.
		#                    Refused here, in the pre-flight window, so the record stays
		#                    `Ordered` and the band can be corrected.
		if not medication_item:
			return None
		band = cstr(record.get("service_option")).strip()
		if band:
			# The band is where the number belongs and the only place it CAN be set, so the
			# refusal names it rather than blaming the operator's input.
			frappe.throw(
				_(
					"Service Option {0} consumes {1} but has no Stock Deduction Qty, so {2} "
					"cannot record what it used. Set Stock Deduction Qty on that Service Option, "
					"then administer this dose."
				).format(frappe.bold(band), frappe.bold(medication_item), frappe.bold(record.name))
			)
		# No band to name. Reachable only for a record whose quantity was cleared by hand -
		# a catalogue-backed dose defaults to BILLED_QTY and a history dose resolves no
		# medication - so the fix is the record's own quantity.
		frappe.throw(
			_(
				"{0} consumes {1} but records a quantity of 0. Set Quantity on the dose before "
				"administering it."
			).format(frappe.bold(record.name), frappe.bold(medication_item))
		)

	if not medication_item or medication_item not in stock_items_for(item_code):
		_report_stock_not_relieved(
			record,
			_(
				"{0} has a configured consumable ({1}, quantity {2}) with no corresponding stock item on "
				"its invoice line. Administration does not issue stock; review the service catalogue. "
				"No extra charge was added."
			).format(record.name, medication_item or medication or _("unspecified"), qty),
			reason="consumable_not_in_invoice",
			service_option=cstr(record.get("service_option")) or None,
			item_code=item_code or None,
		)
	elif item_code == medication_item and qty != BILLED_QTY:
		# The invoice will relieve exactly BILLED_QTY of this item. If the band says a
		# different number, one of the two is wrong and a human has to say which - so this
		# throws in the pre-flight window, while the record is still `Ordered`.
		frappe.throw(
			_(
				"{0} records quantity {1}, but its invoice bills {2} stock unit(s) of the same item. "
				"Correct the quantity before administering it."
			).format(record.name, qty, BILLED_QTY)
		)

	return None


def commit_preventive_stock(record, plan):
	"""Compatibility no-op: administration cannot post stock independently of its invoice."""
	return None
