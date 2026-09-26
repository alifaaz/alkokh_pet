"""Bill one clinical order at its configured moment, to the branch that performed it.

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

WHEN a charge is raised is a clinic-wide setting - two of them, see
utils/billing_trigger.py. `billing_trigger` governs the four order types and is read by
`plan_order_billing_at(doc, moment)`; `medication_billing_trigger` governs prescriptions
and is read by `bill_visit_medications_at`, which passes it to `should_bill_now`
explicitly. Both return None / False unless `moment` is that kind's configured trigger, so
no call site decides policy for itself. `on_visit_close` stands the relevant call sites
down and the charge rides the visit exactly as it did before per-order billing existed.

WHERE it lands is unchanged and independent of the trigger. `performing_branch` decides;
an order without one bills to the visit's branch - the same branch the visit invoice would
have used at close.

Scope, and what is deliberately left alone:

- An order with no `visit` is NOT billed here, with one exception: a PetCareService that
  has neither a `visit` nor `source_doctype == "Pet Boarding"` is a walk-in counter
  booking - there is no visit to bill it onto and no boarding checkout to catch it either,
  so plan_order_billing bills it directly, resolving its branch to whichever branch the
  staff member closing it is stationed at (see `get_current_branch` in its branch
  resolution). Boarding-sourced orders still never bill here; they already bill onto
  Pet Boarding.billable_items at checkout, and billing them again here would double-charge.
  This is also what keeps the boarding `Included` rule intact - see `_medication_included`.
- Manually-added billable rows are NOT billed here. They have no order and no event.
- Medication IS billed here, through `plan_medication_billing`, a parallel entry point for
  child rows, on its own trigger. NOTE: once a prescription's row is Billed, an edit to the
  prescription's qty is neither applied nor refused - `upsert_visit_billable_item` returns
  early on a Billed row (utils/visit_billing.py:122), so the billable row keeps the billed
  quantity and `_validate_billed_billable_rows_unchanged` sees nothing changed. Under
  `medication_billing_trigger = on_request` that is reachable on any visit save.
- An order whose visit has no branch either is left on the visit, rather than guessing.
- Nothing is ever written back to Vet Visit.sales_invoice. That field stays a single Link
  holding the visit's own branch's invoice; an order billed elsewhere records its invoice
  on itself, and a prescription records it on its billable row.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, getdate

from pet_app.utils.billing_trigger import (
	get_medication_billing_trigger,
	include_visit_medication_in_boarding,
	should_bill_now,
)
from pet_app.utils.branch import BRANCH_DOCTYPE, get_current_branch
from pet_app.utils.guardian_customer import resolve_customer_for_clinical_record
from pet_app.utils.invoice_reuse import get_or_create_open_invoice
from pet_app.utils.medication_stock import medication_invoice_context
from pet_app.utils.visit_billing import (
	_clinical_record_care_service,
	_clinical_record_item_name,
	_clinical_record_rate,
	get_care_service_doc,
	log_visit_billing_event,
	set_visit_billable_item_status,
)

# doctype -> the Pet Billable Item `item_type` its charge occupies on the visit.
#
# Medication is still absent, and still on purpose - but the reason has narrowed. It is
# not a doctype. A prescription is a `Vet Visit Medication Item` child row with no
# document of its own, so it cannot be planned by `plan_order_billing`, cannot carry
# `sales_invoice`/`billed` columns, and is not swept by any loop that walks this map.
# Medication is billed by `plan_medication_billing` below, which is a parallel entry
# point, not a member of this map. Anything iterating ORDER_ITEM_TYPES - the release
# sweep, `summarise_visit_billing` - is asking "which ORDER DOCTYPES exist", and the
# answer is still these four.
#
# The Included rule that used to guard this map is honoured by scope rather than by a
# check here: medications absorbed by the medical boarding rate are ordered THROUGH a
# boarding and live on `Pet Boarding.billable_items`, which no path in this module
# touches - `plan_medication_billing` refuses a record with no `visit` unconditionally
# (medication has no walk-in path), and `plan_order_billing` refuses one too except for a
# walk-in PetCareService (no visit, not boarding-sourced). See `_medication_included` for
# the full argument and the one open question.
ORDER_ITEM_TYPES = {
	"Lab": "Lab",
	"Imaging": "Imaging",
	"Pet Procedure": "Procedure",
	"PetCareService": "Service",
	"Preventive Care Record": "Preventive",
}

# The child doctype a prescription row belongs to. Recorded on the plan so the commit
# path can tell a medication plan from an order plan without inspecting `item_type`.
MEDICATION_ROW_DOCTYPE = "Vet Visit Medication Item"

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

	# Boarding-sourced orders are billed by the boarding flow at checkout, never here. Every
	# other order type only bills when tied to a visit - except a PetCareService with
	# neither a visit nor a boarding source, which is a walk-in counter booking: there is no
	# visit to bill it onto and no boarding checkout to catch it, so it bills here instead.
	visit_name = cstr(doc.get("visit")).strip()
	is_boarding_sourced = cstr(doc.get("source_doctype")).strip() == "Pet Boarding"
	if not visit_name and (doc.doctype != "PetCareService" or is_boarding_sourced):
		return None

	branch = cstr(doc.get("performing_branch")).strip()
	branch_source = "performing_branch"
	if not branch and visit_name:
		# The catalogue says nothing about where this is performed, which means "wherever
		# it was ordered" - the visit's branch. This is the SAME branch
		# _create_sales_invoice_for_visit would have used at close, so nothing about where
		# the money lands changes here; only when it is raised.
		branch = cstr(frappe.db.get_value("Vet Visit", visit_name, "branch")).strip()
		branch_source = "visit"
	if not branch and not visit_name:
		# A walk-in PetCareService has no visit and no catalogue branch to fall back on.
		# Resolve to wherever the staff member closing it is stationed - the same fallback
		# already used for counter-sale branch/warehouse resolution elsewhere (see
		# branch.get_current_branch's other callers in branch_warehouse and invoice_reuse).
		branch = cstr(get_current_branch(frappe.session.user)).strip()
		branch_source = "current_user"
	if not branch:
		# Nothing can answer. Leave it unbilled rather than guess - a visit-tied order stays
		# on the visit for close to resolve as today; a walk-in surfaces in
		# unbilled_care_services for staff to bill by hand.
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


def plan_order_billing_at(doc, moment: str, *, item_type: str | None = None):
	"""`plan_order_billing`, but only when `moment` is the configured billing trigger.

	The gate goes here rather than at each call site so that adding a call site cannot
	forget it, and so the plan/commit split survives: a caller still plans BEFORE its
	status transition and commits after, and simply gets None at the moments that are not
	its clinic's trigger.
	"""
	if not should_bill_now(moment):
		return None
	return plan_order_billing(doc, item_type=item_type)


def _medication_included(state: dict, visit_name: str, row) -> bool:
	"""Whether a rate somewhere else absorbs this prescription.

	Two ways to be Included, and they are not the same thing:

	1. The row already says so. Set by the boarding flow for medication ordered THROUGH a
	   boarding, and by this function on a previous pass. Always honoured - the invariant
	   the three boarding exclusion points exist to protect is that an Included row never
	   becomes a line, whichever flow marked it.

	2. `Pet App Access Settings.include_visit_medication_in_boarding` is on and this pet is
	   on an active Treatment stay. This is the switch this module previously refused to
	   throw on its own, and it is a deliberate reversal of the split documented at
	   api/healthcare/boarding.py:3227 - which reads the rate as covering "what the
	   boarding facility gives, not what a consultation prescribes". With the setting on,
	   the clinic's answer is that a Treatment rate covers both, so a doctor prescribing
	   mid-stay does not put a second charge on a guardian who is already paying for
	   medical boarding.

	The Treatment test is `_medication_is_included`, the boarding module's own function, so
	the two paths cannot drift on what "Treatment" means: it reads the OCCUPANT's boarding
	type, not the booking's, because two pets on one booking may board on different terms
	and it is the animal receiving the drug whose terms decide.

	Imported inside the function. api/healthcare/boarding is a large module that imports
	this one; a module-level import would be a cycle and would pull the boarding API into
	every visit save.
	"""
	if cstr(state.get("status")).strip() == "Included":
		return True

	if not include_visit_medication_in_boarding():
		return False

	pet = cstr(row.get("pet")).strip() or cstr(
		frappe.db.get_value("Vet Visit", visit_name, "animal_patient") or ""
	).strip()
	if not pet:
		return False

	from pet_app.utils.boarding_occupancy import find_active_boarding_for_pet

	active = find_active_boarding_for_pet(pet)
	if not active:
		return False

	from pet_app.api.healthcare.boarding import _medication_is_included

	boarding = frappe.get_doc("Pet Boarding", active["name"])
	return bool(_medication_is_included(boarding, pet))


def plan_medication_billing(visit, row, *, warehouse_required: bool = True):
	"""Everything needed to bill one prescription row, or None when it is not billed here.

	A prescription is a child row, not a document, so this is a parallel entry point to
	`plan_order_billing` rather than a case inside it. What it shares is the contract:
	resolves and validates everything, mutates nothing, returns None for the ordinary
	skips and throws for what somebody has to fix.

	Stock is the reason `warehouse` is resolved here at all. A medication line that
	carries a warehouse makes the invoice deduct it; one that carries an explicit empty
	string does not, and an ABSENT key is worse than either because ERPNext backfills it
	from Item Default (see vet_visit.py:1039-1047). Exactly one of the two deduction
	points may fire:

	  - already issued at dispense  -> empty warehouse, the goods have already left
	  - opted in to dispense-time stock -> empty warehouse, dispense will deduct later
	  - neither -> the invoice is the stock document, as it has always been for these

	The middle case is new and is what makes billing before dispense safe: under
	`on_request` the goods are still on the shelf when the charge is raised, and deducting
	them then would double-count against the dispense that follows.
	"""
	visit_name = cstr(getattr(visit, "name", visit)).strip()
	if not visit_name:
		return None
	if cstr(row.get("dispense_status")).strip() == "Cancelled":
		return None
	if not row.get("medication_item"):
		return None

	linked_service_id = f"medication::{row.name}"
	state = _medication_billing_state(visit_name, linked_service_id)
	already_included = cstr(state.get("status")).strip() == "Included"
	if _medication_included(state, visit_name, row):
		# Absorbed by a rate somewhere else. Keeps its row, its real price and its
		# schedule; never becomes an invoice line. Already-Included rows need no write.
		if already_included or not state.get("row"):
			return None
		return frappe._dict(
			doctype=MEDICATION_ROW_DOCTYPE,
			name=row.name,
			visit=visit_name,
			item_type="Medication",
			linked_service_id=linked_service_id,
			billable_row=state.get("row"),
			included=True,
		)
	if state.get("billed"):
		return None
	if not state.get("row"):
		# The billable row is created by _sync_prescribed_medications_billables on the visit
		# save. Until it exists there is nothing to mark Billed, and billing without being
		# able to record it would re-charge on the next save.
		return None

	item = frappe.db.get_value(
		"Item", row.medication_item, ["name", "item_name", "is_stock_item"], as_dict=True
	)
	if not item:
		frappe.throw(_("Medication Item {0} was not found.").format(frappe.bold(row.medication_item)))

	qty = flt(row.get("qty"))
	if qty <= 0:
		frappe.throw(
			_("Prescribed medication row {0} must have Qty greater than zero before it can be billed.").format(
				row.idx
			)
		)
	rate = flt(row.get("rate"))
	if rate <= 0:
		frappe.throw(
			_("Prescribed medication row {0} must have a positive rate before it can be billed.").format(row.idx)
		)

	branch = cstr(frappe.db.get_value("Vet Visit", visit_name, "branch")).strip()
	if not branch:
		frappe.logger("pet_app.billing").info(
			{"event": "MEDICATION_BILLING_SKIPPED_NO_BRANCH", "visit": visit_name, "row": row.name}
		)
		return None
	if not frappe.db.exists(BRANCH_DOCTYPE, branch):
		frappe.throw(
			_("Vet Visit {0} is stamped with branch {1}, which no longer exists.").format(
				frappe.bold(visit_name), frappe.bold(branch)
			)
		)

	customer = cstr(frappe.db.get_value("Vet Visit", visit_name, "customer")).strip()
	if not customer:
		frappe.throw(
			_("Vet Visit {0} has no Customer, so its medication cannot be billed.").format(
				frappe.bold(visit_name)
			)
		)

	stock_context = medication_invoice_context(row, branch=branch, warehouse_required=warehouse_required)

	return frappe._dict(
		doctype=MEDICATION_ROW_DOCTYPE,
		name=row.name,
		branch=branch,
		branch_source="visit",
		customer=customer,
		item_code=item.name,
		item_name=cstr(row.get("medication") or item.item_name),
		rate=rate,
		qty=qty,
		warehouse=stock_context.get("warehouse", ""),
		stock_context=stock_context,
		item_type="Medication",
		visit=visit_name,
		order_id=None,
		linked_service_id=linked_service_id,
		billable_row=state.get("row"),
		included=False,
	)


def _medication_billing_state(visit_name: str, linked_service_id: str) -> dict:
	"""Whether this prescription's billable row has already been invoiced.

	Read from the row rather than from a column on the prescription, because a
	prescription has no document to carry one. `Pet Billable Item.sales_invoice` is the
	per-row equivalent of `Lab.sales_invoice`.
	"""
	# Same fail-safe as _has_billing_state, for the same window. Between deploying this
	# code and running migrate the column does not exist, and selecting it is a SQL error
	# rather than a None. Reporting "already billed" stands medication billing down
	# entirely for that window, which leaves every charge riding the visit exactly as it
	# did before - the safe direction. Not reachable today, because should_bill_now cannot
	# return True while the setting field is also absent, but the two guards are
	# independent and this one should not depend on that.
	if not frappe.get_meta("Pet Billable Item").has_field("sales_invoice"):
		frappe.logger("pet_app.billing").info(
			{"event": "MEDICATION_BILLING_SKIPPED_NO_STATE_FIELD", "visit": visit_name}
		)
		return {"billed": True, "status": None}

	row = frappe.db.get_value(
		"Pet Billable Item",
		{
			"parenttype": "Vet Visit",
			"parent": visit_name,
			"parentfield": "billable_items",
			"linked_service_id": linked_service_id,
		},
		["name", "status", "sales_invoice"],
		as_dict=True,
	)
	if not row:
		return {"billed": False, "status": None}
	status = cstr(row.get("status")).strip()
	return {
		"billed": status in {"Billed", "Included"} or bool(cstr(row.get("sales_invoice")).strip()),
		"status": status,
		"row": row.name,
		"sales_invoice": row.get("sales_invoice"),
	}


def _commit_medication_billing(plan):
	"""Raise the charge for one prescription row and record it ON the row.

	The state write is the one real difference from the order path. A Lab records its
	invoice on itself; a prescription has no document, so `Pet Billable Item.sales_invoice`
	is where it goes. Without that column a cancellation could tell that a row had been
	billed but not onto which invoice, and the line could not be found to remove.

	Stock moves on invoice submission with the prescribed dose conversion on the line.
	"""
	if plan.included:
		frappe.db.set_value(
			"Pet Billable Item", plan.billable_row, {"status": "Included"}, update_modified=False
		)
		log_visit_billing_event(
			"MEDICATION_INCLUDED_IN_BOARDING_RATE", row=plan.name, visit=plan.visit
		)
		return frappe._dict(included=True, sales_invoice=None, amount=0.0, created=False)

	item = {
		"item_code": plan.item_code,
		"qty": plan.qty,
		"rate": plan.rate,
		"description": plan.item_name,
		**(plan.get("stock_context") or {}),
	}

	result = get_or_create_open_invoice(
		customer=plan.customer,
		items=[item],
		source_doctype="Vet Visit",
		source_name=plan.visit,
		branch=plan.branch,
		posting_date=getdate(),
		due_date=getdate(),
		ignore_pricing_rule=1,
		remarks=_("Vet Visit {0} medication billed.").format(plan.visit),
		# Same authorise-then-elevate as the order path. `branch` here is the visit's own
		# stamped branch, never a request parameter, and the caller has already proved
		# write access to the visit.
		branch_authorised=True,
	)
	invoice = result.invoice

	# db.set_value on the child row, not set_visit_billable_item_status.
	#
	# The medication call sites all run DURING or immediately after a Vet Visit save -
	# prescribing is a visit save, dispensing saves the visit. Marking the row through a
	# document write would re-fetch and re-save the visit from inside that save: straight
	# recursion on the prescribe path, and a stale in-memory copy on the dispense one.
	# The row already exists in the database by the time this runs, and the write is two
	# columns with no validation of its own to run.
	#
	# `total_billable_amount` deliberately does NOT need recomputing: only "Cancelled"
	# rows are excluded from it (apply_billable_item_amounts), and this row stays counted.
	if plan.billable_row:
		frappe.db.set_value(
			"Pet Billable Item",
			plan.billable_row,
			{"status": "Billed", "sales_invoice": invoice.name},
			update_modified=False,
		)

	invoice.add_comment(
		"Comment",
		_("Medication from Vet Visit {0} billed by {1} to branch {2}.").format(
			plan.visit, frappe.session.user, plan.branch
		),
	)
	log_visit_billing_event(
		"MEDICATION_BILLED",
		row=plan.name,
		visit=plan.visit,
		sales_invoice=invoice.name,
		branch=plan.branch,
		customer=plan.customer,
		qty=plan.qty,
		amount=flt(plan.qty) * flt(plan.rate),
		warehouse=plan.warehouse or None,
	)

	return frappe._dict(
		sales_invoice=invoice.name,
		branch=plan.branch,
		branch_source=plan.branch_source,
		customer=plan.customer,
		amount=flt(plan.qty) * flt(plan.rate),
		created=result.created,
	)


def commit_order_billing(doc, plan):
	"""Raise the charge for a completed order and record where it went."""
	if not plan:
		return None

	if plan.doctype == MEDICATION_ROW_DOCTYPE:
		return _commit_medication_billing(plan)

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
			sales_invoice=invoice.name,
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


def bill_visit_medications_at(visit, moment: str) -> list[dict]:
	"""Bill every prescription on this visit whose moment has come.

	Medication is the one billable kind with no order document, so it has no start and no
	release of its own. Its moments are the two events it does have:

	    on_request  -> prescribed. The row is created; the charge follows it.
	    on_start    -> first dispense. Goods begin moving.
	    on_release  -> fully dispensed.

	on_start and on_release both land on the dispense path because that is the only later
	event a prescription has; which of the two fires is decided by `moment`, passed by the
	caller from the row's dispense status.

	Called AFTER the visit is saved, never during validate. The row must exist before it
	can be marked, and a prescription that cannot be billed - no customer on the visit, no
	rate yet - must not take the clinical record down with it. Failures are logged and the
	charge falls through to visit close, which is what happened to every medication before
	this setting existed.
	"""
	visit_name = cstr(getattr(visit, "name", visit)).strip()
	if not visit_name:
		return []

	# The Included pass is NOT gated on the moment. "This never becomes a line" is not a
	# billing-moment decision - it is a pricing one - and gating it would leave the setting
	# doing nothing at all under on_visit_close, where no per-order moment ever fires and
	# the row would reach get_billable_invoice_items still Billable.
	#
	# plan_medication_billing returns an Included plan only for a row that should become
	# Included and has not yet, so a row already marked is not rewritten on every save.
	# The MEDICATION trigger, not the order one. A prescription is not an order and has its
	# own setting; passing the trigger explicitly is what keeps this call site from silently
	# following `billing_trigger` when somebody changes it for the four order types.
	raising = should_bill_now(moment, trigger=get_medication_billing_trigger())

	results = []
	for row in visit.get("prescribed_medications") or []:
		if not row.name:
			continue
		try:
			plan = plan_medication_billing(visit, row)
			if plan and not plan.included and not raising:
				continue
			billing = commit_order_billing(None, plan)
		except Exception:
			# One unbillable prescription must not refuse the dispense or the save that
			# carried it. Logged with the row so it can be found, and left for visit close.
			frappe.log_error(
				frappe.get_traceback(),
				f"Medication billing failed: {visit_name} row {row.name}",
			)
			continue
		if billing:
			results.append(billing)
	return results


def release_orders_billed_to_invoice(invoice_name: str, *, skip_visit: str | None = None):
	"""Undo per-order billing when the invoice it landed on is cancelled.

	`on_sales_invoice_cancel` walks INVOICE_LINKED_DOCTYPES and saves each parent. That
	loop cannot be reused here: saving a Lab or Imaging re-runs `set_values_from_visit`,
	which throws BILLED_VISIT_LOCK_MESSAGE whenever the visit has its own invoice - which
	is precisely the situation per-order billing creates. So the state is cleared with
	db.set_value and the visit row is put back by itself.
	"""
	released = []
	skip_visit = cstr(skip_visit).strip()
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
			# `skip_visit` is the visit the CALLER is holding open in memory and will save
			# itself. Putting its row back here would fetch a second copy of that visit,
			# save it, and leave the caller's copy stale - which is a TimestampMismatch on
			# the caller's own save, not a silent overwrite. Its row is the caller's to set.
			if row.visit and row.visit != skip_visit:
				set_visit_billable_item_status(
					row.visit,
					status="Billable",
					sales_invoice="",
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
