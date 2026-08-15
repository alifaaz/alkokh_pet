"""Bill one clinical order at the moment it completes, to the branch that performed it.

The visit path bills everything at close, to the visit's branch. That is right for work
done where the case is managed and wrong for work that is not: a main doctor may order
radiology, read the report and manage the case, but the machine is at the boarding
facility, so the money is the facility's. `branch` is a live accounting dimension, so
getting it from the ordering doctor rather than from the service reaches the ledger.

Two entry points, deliberately split:

    plan_order_billing(doc)     - pre-flight. Resolves and validates EVERYTHING, mutates
                                  nothing. Called BEFORE the status transition.
    commit_order_billing(doc, plan) - raises the charge. Called after.

The split is what makes a refusal safe. These endpoints catch their own exceptions and
return a `fail(...)` response rather than letting the error propagate, so Frappe commits
whatever the request already wrote. If billing validated after the release, a pet with no
guardian on file would leave the lab Released, uncharged, and reported as failed - the
worst of the three outcomes. Validating first means the release never happens unless the
charge can.

Every clinical order bills at completion, whether or not the catalogue places it at
another branch. `performing_branch` decides only WHERE the charge lands; an order without
one bills to the visit's branch - the same branch the visit invoice would have used at
close - so widening this changed when a charge is raised, never where.

Scope, and what is deliberately left alone:

- An order with no `visit` is NOT billed here. Boarding-sourced orders already bill onto
  Pet Boarding.billable_items at checkout; billing them again here would double-charge.
- Medications and manually-added rows are NOT billed here. They have no completion event
  of their own, no branch of their own, and their quantities stay editable until the visit
  closes (the prescription table is re-synced on every save). They keep riding the visit.
- An order whose visit has no branch either is left on the visit, rather than guessing.
- Nothing is ever written back to Vet Visit.sales_invoice. That field stays a single Link
  holding the visit's own branch's invoice; an order billed elsewhere records its invoice
  on itself.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, getdate

from pet_app.utils.branch import BRANCH_DOCTYPE
from pet_app.utils.guardian_customer import resolve_customer_for_clinical_record
from pet_app.utils.invoice_reuse import get_or_create_open_invoice
from pet_app.utils.visit_billing import (
	_clinical_record_care_service,
	_clinical_record_item_name,
	_clinical_record_rate,
	get_care_service_doc,
	log_visit_billing_event,
	set_visit_billable_item_status,
)

# doctype -> the Pet Billable Item `item_type` its charge occupies on the visit.
ORDER_ITEM_TYPES = {
	"Lab": "Lab",
	"Imaging": "Imaging",
	"Pet Procedure": "Procedure",
	"PetCareService": "Service",
}

STATE_FIELDS = ("sales_invoice", "billed")


def _has_billing_state(doc) -> bool:
	"""Whether this site has run the per-order billing state patch yet.

	Fail-safe, and the reason the code may be deployed before the patch is run. Billing a
	charge without being able to record that it was billed would re-charge it on the next
	release and re-emit it at visit close, so if the fields are absent this module does
	nothing at all and every charge keeps flowing through the visit.
	"""
	return all(doc.meta.has_field(fieldname) for fieldname in STATE_FIELDS)


def _order_rate(doc) -> float:
	for fieldname in ("rate", "price"):
		value = doc.get(fieldname)
		if value not in (None, ""):
			value = flt(value)
			if value > 0:
				return value
	return 0.0


def plan_order_billing(doc, *, item_type: str | None = None):
	"""Everything needed to bill this order, or None when it is not billed per-order.

	Returns None for the ordinary cases (no performing branch, no visit, already billed,
	state fields absent) and throws for the ones somebody has to fix - a branch that does
	not exist, a missing item or rate, a pet with no guardian. Never mutates.
	"""
	if doc.doctype not in ORDER_ITEM_TYPES:
		return None
	if not _has_billing_state(doc):
		frappe.logger("pet_app.billing").info(
			{
				"event": "PER_ORDER_BILLING_SKIPPED_NO_STATE_FIELDS",
				"doctype": doc.doctype,
				"name": doc.name,
			}
		)
		return None
	if cint(doc.get("billed")) or cstr(doc.get("sales_invoice")).strip():
		return None

	# Boarding-sourced orders are billed by the boarding flow at checkout.
	visit_name = cstr(doc.get("visit")).strip()
	if not visit_name:
		return None

	branch = cstr(doc.get("performing_branch")).strip()
	branch_source = "performing_branch"
	if not branch:
		# The catalogue says nothing about where this is performed, which means "wherever
		# it was ordered" - the visit's branch. This is the SAME branch
		# _create_sales_invoice_for_visit would have used at close, so nothing about where
		# the money lands changes here; only when it is raised.
		branch = cstr(frappe.db.get_value("Vet Visit", visit_name, "branch")).strip()
		branch_source = "visit"
	if not branch:
		# Neither the catalogue nor the visit can answer. Leave the charge on the visit and
		# let close resolve it exactly as it does today, rather than guess a branch here.
		frappe.logger("pet_app.billing").info(
			{
				"event": "PER_ORDER_BILLING_SKIPPED_NO_BRANCH",
				"doctype": doc.doctype,
				"name": doc.name,
				"visit": visit_name,
			}
		)
		return None

	if not frappe.db.exists(BRANCH_DOCTYPE, branch):
		frappe.throw(
			_("{0} {1} is performed at branch {2}, which no longer exists. Fix the service before releasing it.").format(
				_(doc.doctype), frappe.bold(doc.name), frappe.bold(branch)
			)
		)

	# Resolved exactly the way sync_clinical_record_billable_item resolves it for the visit
	# path - the order's own value first, then its CareService template's. Reading only the
	# document would be wrong for PetCareService, which never stamps item_code or rate onto
	# itself the way Lab, Imaging and Pet Procedure do; every care service would refuse to
	# bill. Same helpers, so the two paths cannot drift apart on price.
	care_service = _clinical_record_care_service(doc)
	service = get_care_service_doc(care_service) if care_service else None

	item_code = cstr(doc.get("item_code") or (service.get("item_code") if service else "")).strip()
	if not item_code:
		if care_service:
			frappe.throw(
				_("Care Service {0} is missing Item Code, so {1} {2} cannot be billed.").format(
					frappe.bold(care_service), _(doc.doctype), frappe.bold(doc.name)
				)
			)
		frappe.throw(
			_("{0} {1} has no Item Code and cannot be billed.").format(_(doc.doctype), frappe.bold(doc.name))
		)

	rate = _clinical_record_rate(doc, service) if service else _order_rate(doc)
	if rate <= 0:
		frappe.throw(
			_("{0} {1} must have a positive rate before it can be billed.").format(
				_(doc.doctype), frappe.bold(doc.name)
			)
		)

	# Throws loudly on a pet with no guardian rather than billing a blank customer.
	customer = resolve_customer_for_clinical_record(doc)

	return frappe._dict(
		doctype=doc.doctype,
		name=doc.name,
		branch=branch,
		branch_source=branch_source,
		customer=customer,
		item_code=item_code,
		item_name=_clinical_record_item_name(doc, service),
		rate=rate,
		item_type=item_type or ORDER_ITEM_TYPES[doc.doctype],
		visit=visit_name,
		order_id=doc.get("order_id"),
	)


def commit_order_billing(doc, plan):
	"""Raise the charge for a completed order and record where it went."""
	if not plan:
		return None

	result = get_or_create_open_invoice(
		customer=plan.customer,
		items=[
			{
				"item_code": plan.item_code,
				"qty": 1,
				"rate": plan.rate,
				"description": plan.item_name,
			}
		],
		source_doctype=plan.doctype,
		source_name=plan.name,
		branch=plan.branch,
		posting_date=getdate(),
		due_date=getdate(),
		ignore_pricing_rule=1,
		remarks=_("{0} {1} billed on completion.").format(_(plan.doctype), plan.name),
		# Authorise-then-elevate, the same shape boarding checkout uses.
		#
		# What is established before this line, on every one of the four paths that reach
		# it: the caller passed the endpoint's own authorisation - `check_permission
		# ("write")` on the record for the two diagnostics endpoints, `_assert_record_
		# access(..., write=True)` at the top of `perform_action` for the procedure and
		# service ones - and `clinical_state.assert_action_allowed` proved the order is in
		# a status from which this completion is legal, which is also what makes it
		# single-shot.
		#
		# The elevation cannot be steered. `branch` is not a request parameter on any of
		# these paths. It is either `performing_branch` - snapshotted onto the order at
		# insert from the catalogue and never rewritten - or the visit's own stamped
		# branch. A caller who wants a different branch would have to edit the CareService
		# template and place a NEW order; they cannot move an existing charge by asking.
		#
		# When the branch came from the visit the elevation is a no-op: the caller is
		# writing into the branch they were already working in. It is still passed rather
		# than made conditional, because the value that reaches here is trusted for the
		# same reason in both cases, and a conditional would invite someone to make the
		# untrusted case the default later.
		#
		# Without it a main-restricted user releasing a hotel X-ray would fail
		# `assert_can_write_to_branch` on the invoice insert and could not release at all,
		# which is exactly how the boarding attempt failed before Stage 1.
		branch_authorised=True,
	)
	invoice = result.invoice

	# db.set_value, not doc.save: saving the order re-runs its controller, which re-reads
	# the visit and re-syncs the billable row we are about to mark Billed. The state write
	# is two flags and has no validation of its own to run.
	frappe.db.set_value(
		plan.doctype,
		plan.name,
		{"sales_invoice": invoice.name, "billed": 1},
		update_modified=False,
	)
	doc.sales_invoice = invoice.name
	doc.billed = 1

	# The visit keeps the row, for the clinical record and for cancellation, but marked so
	# get_billable_invoice_items will not emit it again when the visit closes.
	if plan.visit:
		set_visit_billable_item_status(
			plan.visit,
			status="Billed",
			linked_service_id=f"{plan.doctype}::{plan.name}",
			linked_doctype=plan.doctype,
			linked_name=plan.name,
			order_id=plan.order_id,
			item_code=plan.item_code,
			item_type=plan.item_type,
		)

	invoice.add_comment(
		"Comment",
		_("{0} {1} billed on completion by {2} to branch {3}.").format(
			_(plan.doctype), plan.name, frappe.session.user, plan.branch
		),
	)
	log_visit_billing_event(
		"ORDER_BILLED_ON_COMPLETION",
		doctype=plan.doctype,
		order=plan.name,
		visit=plan.visit,
		sales_invoice=invoice.name,
		branch=plan.branch,
		branch_source=plan.branch_source,
		customer=plan.customer,
		amount=plan.rate,
	)

	return frappe._dict(
		sales_invoice=invoice.name,
		branch=plan.branch,
		branch_source=plan.branch_source,
		customer=plan.customer,
		amount=plan.rate,
		created=result.created,
	)


def release_orders_billed_to_invoice(invoice_name: str):
	"""Undo per-order billing when the invoice it landed on is cancelled.

	`on_sales_invoice_cancel` walks INVOICE_LINKED_DOCTYPES and saves each parent. That
	loop cannot be reused here: saving a Lab or Imaging re-runs `set_values_from_visit`,
	which throws BILLED_VISIT_LOCK_MESSAGE whenever the visit has its own invoice - which
	is precisely the situation per-order billing creates. So the state is cleared with
	db.set_value and the visit row is put back by itself.
	"""
	released = []
	for doctype in ORDER_ITEM_TYPES:
		if not frappe.get_meta(doctype).has_field("sales_invoice"):
			continue
		for row in frappe.get_all(
			doctype,
			filters={"sales_invoice": invoice_name},
			fields=["name", "visit", "order_id", "item_code"],
			ignore_permissions=True,
		):
			frappe.db.set_value(
				doctype, row.name, {"sales_invoice": None, "billed": 0}, update_modified=False
			)
			if row.visit:
				set_visit_billable_item_status(
					row.visit,
					status="Billable",
					linked_service_id=f"{doctype}::{row.name}",
					linked_doctype=doctype,
					linked_name=row.name,
					order_id=row.order_id,
					item_code=row.item_code,
					item_type=ORDER_ITEM_TYPES[doctype],
				)
			released.append(f"{doctype}::{row.name}")

	if released:
		log_visit_billing_event(
			"ORDER_BILLING_RELEASED", sales_invoice=invoice_name, orders=released
		)
	return released
