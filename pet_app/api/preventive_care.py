"""Driving a Preventive Care Record through its lifecycle, and billing it when it lands.

WHY THIS IS NOT IN `api/workspace.py`. `_update_service_status` lives there because
`perform_action` lives there, and that file is already 4,000 lines. The transition logic for
a new doctype is self-contained - one planner, one save, one commit - so it goes in its own
module and the dispatcher imports it. The stage that wires `perform_action` adds three lines
there rather than another hundred.

THE SAVEPOINT IS THE POINT. This mirrors `_update_service_status` (api/workspace.py) exactly,
and the reason is that module's, restated because it is easy to lose:

    `@standardize_response` catches every exception and returns an envelope, so nothing
    reaches Frappe's request handler and the request COMMITS ANYWAY. Without an explicit
    savepoint the status transition, the stamps and the save all land before the billing
    commit that follows them, and a throw there cannot undo any of it. The result is a record
    that reads `Administered` with `billed = 0` and no invoice, and no Error Log saying so.

    The administration and its charge are one act or they are neither.

Two details carried over deliberately:

  `frappe.log_error` AFTER the rollback, never before - the row has to survive it. Same
  ordering, and the same reason, as `api/item_barcode.py`.

  The re-throw composes the refusal with the original message instead of a bare `raise`, so
  the operator learns the transition was refused as well as why. The exception class is
  preserved when it is a ValidationError - every deliberate billing refusal is one,
  `NegativeStockError` included - so the `code` in the response envelope does not change.

PAYLOAD FIELDS ARE AN ALLOW-LIST, NOT AN UPDATE. `payload` is request data. Each action names
the fields it will accept and nothing else is read, so a caller cannot set `billed`,
`sales_invoice`, `rate` or `kind` by putting them in the body. The derived fields
(`item_code`, `rate`, `medication_name`, `kind`, `category`) are the controller's to write.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.database.query import Engine
from frappe.utils import cint, cstr, flt, getdate, now_datetime, nowdate

from pet_app.api.permissions import require_doctype_permission
from pet_app.api.response import ok, standardize_response
from pet_app.utils import order_billing, preventive_billing
from pet_app.workflows import clinical_state

DOCTYPE = "Preventive Care Record"

# The actions this doctype declares, and the status each drives it to. This map is the
# explicit guard `clinical_state.assert_action_allowed` does NOT provide: that function ends
# with `if not allowed: return`, so an action name it does not recognise passes silently from
# ANY status, terminal ones included. A typo would therefore not refuse - it would permit.
# Membership here is checked first, and refuses by name.
ACTION_TARGET_STATUS = {
	"start_preventive": "In Progress",
	"administer_preventive": "Administered",
	"cancel_preventive": "Cancelled",
}

# What each action will read off the request body. Everything else is ignored.
START_FIELDS = ("provider", "weight")
ADMINISTER_FIELDS = (
	"provider",
	"administered_on",
	"next_due_date",
	"reminder_enabled",
	"batch_no",
	"dose",
	"vaccine_type",
	"qty",
	"medication",
	"weight",
	"notes",
)
CANCEL_FIELDS = ("cancellation_reason",)


def _refusal(action: str) -> str:
	"""What the operator is told when a transition is refused.

	The REASON is never invented here - the biller already names the record and the fix
	("... has no price, so it cannot be billed. Set Default Price on its Care Service ...").
	That text is carried through verbatim. This adds only the half the operator could not
	otherwise know: that the transition did NOT happen and nothing was written.
	"""
	if action == "start_preventive":
		return _("This dose was not started and nothing was changed.")
	if action == "administer_preventive":
		return _("This dose was not recorded as administered and nothing was changed.")
	if action == "cancel_preventive":
		return _("This dose was not cancelled and nothing was changed.")
	return _("This action was not applied and nothing was changed.")


def update_preventive_status(record_name: str, action: str, payload: dict | None = None):
	"""Savepoint boundary for the whole transition. See the module docstring."""
	payload = payload or {}
	savepoint = f"update_preventive_{frappe.generate_hash(length=10)}"
	frappe.db.savepoint(savepoint)
	try:
		_update_preventive_status_atomic(record_name, action, payload)
	except Exception as exc:
		frappe.db.rollback(save_point=savepoint)
		frappe.log_error(
			title="PREVENTIVE_STATUS_TRANSITION_FAILED",
			message=f"{record_name} / {action}: {frappe.get_traceback()}",
		)
		reason = cstr(exc).strip()
		refusal = _refusal(action)
		message = _("{0} {1}").format(refusal, reason) if reason else refusal
		if isinstance(exc, frappe.ValidationError):
			frappe.throw(message, exc=exc.__class__, title=_("Dose not updated"))
		frappe.throw(message, title=_("Dose not updated"))
	else:
		frappe.db.release_savepoint(savepoint)


def _update_preventive_status_atomic(record_name: str, action: str, payload: dict):
	if action not in ACTION_TARGET_STATUS:
		frappe.throw(
			_("{0} is not an action on a {1}. Expected one of: {2}.").format(
				frappe.bold(action), frappe.bold(DOCTYPE), ", ".join(sorted(ACTION_TARGET_STATUS))
			)
		)

	record = frappe.get_doc(DOCTYPE, record_name)
	clinical_state.assert_action_allowed(record, action)

	if action == "start_preventive":
		_apply_payload(record, payload, START_FIELDS)
		record.start_at = record.start_at or now_datetime()
	elif action == "administer_preventive":
		_apply_payload(record, payload, ADMINISTER_FIELDS)
		record.end_at = record.end_at or now_datetime()
		# The operator's own date when they gave one, else today. Unlike `next_due_date`,
		# this may be defaulted: a dose being recorded happened, and the only question is
		# which day - whereas a next dose may genuinely have no date at all.
		record.administered_on = getdate(record.administered_on or nowdate())
		record.administered_by = frappe.session.user
		record.administered_at = now_datetime()
	elif action == "cancel_preventive":
		_apply_payload(record, payload, CANCEL_FIELDS)
		record.cancelled_by = frappe.session.user
		record.cancelled_at = now_datetime()

	# PRE-FLIGHT, before anything is WRITTEN - which is not the same as before anything is
	# SET. It runs after the payload has been copied onto the in-memory document and before
	# the transition and the save, because a planner that reads the record must read the dose
	# as the operator is recording it, not as it was ordered. `_apply_payload` touches nothing
	# but this in-memory copy, so the pre-flight window is unchanged: a plan that throws still
	# leaves a record that is `Ordered` and unbilled on disk.
	#
	# The ordering matters most to `plan_preventive_stock`, whose whole job is to compare the
	# quantity being administered against the quantity the invoice will relieve. Planned ahead
	# of the payload it read the quantity defaulted at creation, so a `qty` sent with the
	# administration was never checked against anything - the mismatch it exists to refuse
	# went through silently, and the record was left claiming a consumption its invoice had
	# not relieved. `medication` and `batch_no` arrive the same way and feed the same check.
	#
	# Each planner returns None for a record that is somebody else's to bill or is already
	# billed, and throws - naming the row to fix - for one that cannot be. A dose that cannot
	# be billed stays `Ordered`.
	# TWO BILLERS, AND THEY ARE MUTUALLY EXCLUSIVE BY CONSTRUCTION.
	#
	#   visit-linked  -> `order_billing`, the same path Lab and Imaging use. The charge is
	#                    already on `Vet Visit.billable_items` from `after_insert`; this
	#                    raises the invoice at the clinic's configured moment (`on_release`
	#                    on this site) and returns None at every other moment, leaving the
	#                    visit to bill it at close.
	#   no visit      -> `preventive_billing`, which bills the guardian directly.
	#
	# `plan_order_billing` returns None when there is no visit and `plan_preventive_billing`
	# returns None when there is one, so exactly one of the two can ever produce a plan. That
	# is not a coincidence to rely on quietly - it is why a dose cannot be charged twice.
	order_plan = None
	billing_plan = None
	stock_plan = None
	if action == "administer_preventive":
		order_plan = order_billing.plan_order_billing_at(
			record, "on_release", item_type=order_billing.ORDER_ITEM_TYPES[DOCTYPE]
		)
		billing_plan = preventive_billing.plan_preventive_billing(record)
		# The stock leg is independent of the billing leg: a dose can be already billed and
		# still owe its stock check, and a band with no quantity owes neither. Planned here,
		# before the transition, so a half-configured band is reported - or refused - while
		# the record is still `Ordered` rather than after it is administered and charged.
		stock_plan = preventive_billing.plan_preventive_stock(record)

	clinical_state.transition_status(record, ACTION_TARGET_STATUS[action], action=action)
	record.save(ignore_permissions=True)

	# After the save, so a record that failed to transition raises no charge; inside the
	# savepoint, so a billing failure takes the transition down with it.
	if order_plan:
		order_billing.commit_order_billing(record, order_plan)
	if billing_plan:
		preventive_billing.commit_preventive_billing(record, billing_plan)
	if stock_plan:
		preventive_billing.commit_preventive_stock(record, stock_plan)

	# LAST, and deliberately after the charge. The next dose is a consequence of this one
	# having been given, so it is raised only once the giving is recorded and billed. It
	# cannot fail the administration - see `_create_next_dose`.
	if action == "administer_preventive":
		_create_next_dose(record)

	if action == "cancel_preventive":
		reason = cstr(payload.get("cancellation_reason") or payload.get("reason") or payload.get("note")).strip()
		comment = _("Dose cancelled by {0}.").format(frappe.session.user)
		if reason:
			comment = _("{0} Reason: {1}").format(comment, reason)
		record.add_comment("Comment", comment)


# ─────────────────────────────────────────────────────────────────────────────
# The next dose
# ─────────────────────────────────────────────────────────────────────────────
#
# A `next_due_date` used to be a date and nothing else: it drove the reminder sweep and the
# compliance standing, but no record existed for the dose it named, so the worklist's
# upcoming count read 0 while doses were genuinely owed. Administering now raises that record.
#
# WHAT THE CHILD IS: an `Ordered` dose for the same pet and the same catalogue row, due on the
# day the parent named. It is a RECALL, not an order - nobody has yet decided who will give it
# or when in the day, and the pet has not been weighed for it.
#
# WHAT IT IS NOT: a copy. Only the identity of the protocol travels. Everything the parent
# observed - a substituted vial, a deliberate half dose, last year's weight, the batch that
# came out of the fridge - describes the dose that WAS given, and presenting any of it as the
# standing plan for a dose a year away would be asserting something nobody checked.

# Copied, because they say WHICH protocol recurs and FOR WHOM.
RECURRENCE_COPIED_FIELDS = ("pet", "guardian", "care_service", "service_option", "reminder_enabled")


def _plan_next_dose(record) -> dict | None:
	"""The child to raise, or None when this dose owes no next one.

	Every reason to decline is an existence question, never a count, so running this twice on
	the same dose raises the same one record - see the two guards below.
	"""
	due_date = record.get("next_due_date")
	if not due_date:
		# No next dose was promised. The overwhelming majority of doses on this site.
		return None

	care_service = cstr(record.get("care_service")).strip()
	service_option = cstr(record.get("service_option")).strip()
	if not care_service and not service_option:
		# HISTORY HAS NO NEXT DOSE TO RAISE. A dose recorded with `kind` alone has no
		# catalogue row, so a child built from it would have no Item Code - and
		# `plan_preventive_billing` refuses to administer a dose it cannot price. Creating one
		# would mean manufacturing a record that can never be completed. The date still drives
		# the reminder off the parent, exactly as it does today.
		return None

	# GUARD ONE: this dose has already raised its next one. Keyed on the parent link, so it is
	# exact rather than inferred - re-running the transition, or a retried request, finds the
	# child it made last time and makes nothing.
	if frappe.db.exists(DOCTYPE, {"source_doctype": DOCTYPE, "source_name": record.name}):
		return None

	# GUARD TWO: somebody is already holding that slot. Two doses of one protocol administered
	# on the same day - which this site has, PCR-00002 and PCR-00003, both due the same date -
	# would otherwise each raise a recall and the worklist would show the pet owing two of the
	# same vaccination. An open dose for this pet, this protocol and this day is that dose,
	# whoever raised it.
	open_same_slot = {
		"pet": record.get("pet"),
		"due_date": getdate(due_date),
		"status": ["in", ["Ordered", "In Progress"]],
		"care_service": care_service or ["is", "not set"],
		"service_option": service_option or ["is", "not set"],
	}
	if frappe.db.exists(DOCTYPE, open_same_slot):
		return None

	values = {fieldname: record.get(fieldname) for fieldname in RECURRENCE_COPIED_FIELDS}
	values.update(
		{
			# The parent's next becomes the child's own. The child gets NO `next_due_date` of
			# its own: section 1.2 makes that field operator-chosen and never computed, and
			# inventing one would both raise a reminder for a dose nobody has given and set
			# this chain running without a human in it. Whoever administers the child decides
			# whether there is another after it - so the series advances exactly one step per
			# dose actually given, and stops the moment somebody stops choosing.
			"due_date": getdate(due_date),
			# The chain, made traversable in the direction it was created.
			"source_doctype": DOCTYPE,
			"source_name": record.name,
			# `branch` is COPIED rather than left to `stamp_branch_on_insert`, which reads the
			# SESSION user. The clinic that owns the recall is the one that gave the dose, not
			# whichever branch the person clicking happens to belong to - and for an
			# unrestricted user that hook would refuse the insert outright.
			"branch": record.get("branch"),
		}
	)
	return values


def _create_next_dose(record):
	"""Raise the next dose. Never fails the administration that triggered it.

	LOUD, BUT NOT FATAL - the asymmetry `_report_stock_not_relieved` already draws on this
	path, for the same reason. The dose was given; that is a clinical fact and a charge, and
	both are already recorded. A recall that could not be raised is worth interrupting somebody
	over - a deceased pet, a catalogue row deleted since - but refusing to record the treatment
	because the reminder could not be filed would destroy the fact to protect the convenience.
	Its own savepoint, so a failure here leaves the transition and the invoice untouched.
	"""
	values = _plan_next_dose(record)
	if not values:
		return None

	savepoint = f"next_dose_{frappe.generate_hash(length=10)}"
	frappe.db.savepoint(savepoint)
	try:
		child = frappe.new_doc(DOCTYPE)
		child.update(values)
		# Never from the request, exactly as at creation: a raised dose starts `Ordered` and
		# moves only through the three actions.
		child.status = "Ordered"
		child.insert(ignore_permissions=True)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		frappe.log_error(
			title="PREVENTIVE_NEXT_DOSE_NOT_CREATED",
			message=f"{record.name} -> next dose: {frappe.get_traceback()}",
		)
		frappe.msgprint(
			_(
				"{0} was recorded, but the next dose due {1} could not be raised. "
				"Create it by hand from the pet's record."
			).format(frappe.bold(record.name), frappe.bold(cstr(values.get("due_date")))),
			title=_("Next dose not created"),
			indicator="orange",
		)
		return None
	frappe.db.release_savepoint(savepoint)

	record.add_comment(
		"Comment",
		_("Next dose {0} raised for {1}.").format(child.name, cstr(values.get("due_date"))),
	)
	frappe.logger("pet_app.clinical").info(
		{
			"event": "PREVENTIVE_NEXT_DOSE_CREATED",
			"parent": record.name,
			"child": child.name,
			"due_date": cstr(values.get("due_date")),
		}
	)
	return child.name


def _apply_payload(record, payload: dict, fields: tuple[str, ...]):
	"""Copy the named fields off the request body, and nothing else.

	Blank and absent are both "leave it alone". A caller that wants to CLEAR a field does it
	through a normal save on the document, not by driving a transition - a status action is
	not an editing endpoint, and treating an omitted key as a deletion would silently wipe a
	next-due date every time a client posted a partial body.
	"""
	for fieldname in fields:
		if fieldname not in payload:
			continue
		value = payload.get(fieldname)
		if value is None or cstr(value).strip() == "":
			continue
		if fieldname in ("qty", "weight"):
			value = flt(value)
			if value <= 0:
				continue
		record.set(fieldname, value)


# ─────────────────────────────────────────────────────────────────────────────
# Creating a dose directly for a pet
# ─────────────────────────────────────────────────────────────────────────────
#
# THE PATH THAT IS NOT AN ORDER. A vaccination given on a pet's record, with no visit and
# nothing that ordered it, is the majority case on this site - 28 of the 29 preventive rows
# have no visit. It is a first-class path, not a fallback, which is why `visit` and
# `source_name` are both optional on the doctype and `pet` alone is the floor.
#
# WHAT A CALLER MAY SET is an allow-list, for the same reason the transition payloads are:
# this is request data. Everything derived - `kind`, `category`, `item_code`, `rate`,
# `medication_name`, `performing_branch` - is the controller's to write, and `billed`,
# `sales_invoice`, `status`, `administered_*` and the stock stamps are written only by the
# paths that earn them.

CREATE_FIELDS = (
	"pet",
	"visit",
	"guardian",
	"doctor",
	"provider",
	"care_service",
	"service_option",
	# Accepted, but it cannot contradict the catalogue: the controller throws when a stated
	# kind disagrees with the one its Care Service or Service Option resolves to. Its real
	# purpose is the history case - a dose given elsewhere, with no catalogue row to derive
	# a kind from. See `preventive_catalogue.assert_selection`.
	"kind",
	"medication",
	"branch",
	"due_date",
	"scheduled_datetime",
	"priority",
	"weight",
	"qty",
	"dose",
	"vaccine_type",
	"batch_no",
	"next_due_date",
	"reminder_enabled",
	"notes",
	# Accepted so a boarding or another origin can attribute the dose to itself. Both are
	# required together and the target must exist - `_validate_source` refuses otherwise.
	"source_doctype",
	"source_name",
)


@frappe.whitelist(methods=["POST"])
@standardize_response
def create_preventive_care_record(payload=None, **kwargs):
	"""Create one vaccination or deworming for a pet, ready to be administered.

	Returns the same detail shape `perform_action` returns, so a client that creates a dose
	and one that drives an existing one are reading the same object.

	It is created at `Ordered` and NOTHING IS BILLED HERE. Creating a dose is not giving it:
	the charge, the stock check and the clinical stamps all belong to `administer_preventive`,
	and a record that is created and then cancelled must never have raised a charge. The one
	exception is a visit-linked dose, whose charge lands on `Vet Visit.billable_items` at
	insert exactly as a Lab's does - that is the visit's running total, not an invoice.
	"""
	require_doctype_permission(DOCTYPE, "create")

	data = dict(payload or {})
	data.update({k: v for k, v in (kwargs or {}).items() if k != "cmd"})

	values = {}
	for fieldname in CREATE_FIELDS:
		if fieldname not in data:
			continue
		value = data.get(fieldname)
		if value is None or cstr(value).strip() == "":
			continue
		if fieldname in ("qty", "weight"):
			value = flt(value)
			if value <= 0:
				continue
		values[fieldname] = value

	if not cstr(values.get("pet")).strip():
		frappe.throw(_("Pet is required to create a {0}.").format(_(DOCTYPE)))

	doc = frappe.new_doc(DOCTYPE)
	doc.update(values)
	# Status is never taken from the request: a dose starts Ordered and moves only through
	# the three actions, which is what makes the lifecycle auditable.
	doc.status = "Ordered"
	doc.insert()

	# Imported here, not at module scope: api.workspace imports THIS module for its action
	# dispatch, so a top-level import would be circular.
	from pet_app.api.workspace import _preventive_detail

	return ok({"record": _preventive_detail(doc.name)})


# ─────────────────────────────────────────────────────────────────────────────
# Correcting a dose that has not happened yet
# ─────────────────────────────────────────────────────────────────────────────
#
# EDITING IS FOR A DOSE STILL AHEAD OF YOU. Once a dose is `Administered` a charge exists, a
# clinical record says a named person gave it on a named day, and the invoice that relieves
# the stock has been raised; once `Cancelled`, the charge was reversed on that basis. Neither
# is a draft, so neither is edited - a correction is a new dose, and the wrong one stays
# visible, which is what an audit trail is.
#
# THIS ENDPOINT IS THE FRONT DOOR, NOT THE LOCK. The lock is
# `PreventiveCareRecord._refuse_edit_when_committed`, which refuses the same edits arriving
# through `frappe.client.save`, `frappe.client.set_value` or any other generic write. This
# function exists to refuse them EARLIER and in the caller's language - naming the status and
# the allowed fields - so the screen can grey a control out instead of showing a save that
# fails. A gate with no lock behind it is a suggestion; a lock with no gate in front of it is
# a stack trace where a sentence should be.

# What a caller may change on a dose that is still `Ordered` or `In Progress`.
#
# Everything derived (`kind`, `category`, `item_code`, `rate`, `medication_name`,
# `performing_branch`) and everything the lifecycle earns (`status`, `billed`,
# `sales_invoice`, `administered_*`, `cancelled_*`, the stock stamps) is absent, for the same
# reason it is absent from CREATE_FIELDS: those belong to the paths that write them. `pet`,
# `visit` and `source_*` are absent too - re-pointing a dose at a different pet or visit is
# not an edit, it is a different record, and the billing already hangs off the old answer.
UPDATE_FIELDS = (
	"provider",
	"due_date",
	"scheduled_datetime",
	"priority",
	"weight",
	"qty",
	"dose",
	"vaccine_type",
	"batch_no",
	"medication",
	"next_due_date",
	"reminder_enabled",
	"notes",
)


@frappe.whitelist(methods=["POST", "PUT"])
@standardize_response
def update_preventive_care_record(name=None, payload=None, **kwargs):
	"""Correct a dose that has not been given or cancelled yet.

	Returns the same record shape the read returns - `_preventive_detail`, all 49 fields with
	the four labels resolved - so a screen that saved can re-render from the response without
	a second call.

	BLANK CLEARS, ABSENT LEAVES ALONE, and that is the opposite of what the three status
	actions do. `_apply_payload` treats blank as "leave it" because a status action is not an
	editing endpoint and a partial body must not wipe a `next_due_date`. This IS the editing
	endpoint: it is the only way a client can say "there is no next dose after all", so a key
	present with an empty value means exactly that. A key that is absent is untouched.
	"""
	require_doctype_permission(DOCTYPE, "write")

	data = dict(payload or {})
	data.update({k: v for k, v in (kwargs or {}).items() if k not in ("cmd", "name")})

	record_name = cstr(name or data.pop("name", "")).strip()
	if not record_name:
		frappe.throw(_("Record name is required."))

	unknown = sorted(set(data) - set(UPDATE_FIELDS))
	if unknown:
		# Same rule, same reason as the worklist's unknown-filter refusal: a silently dropped
		# field on an editing endpoint is a change the caller believes it made.
		frappe.throw(
			_("{0} cannot be changed on a {1}. Editable: {2}.").format(
				frappe.bold(", ".join(unknown)), _(DOCTYPE), ", ".join(UPDATE_FIELDS)
			),
			title=_("Field is not editable"),
		)

	record = frappe.get_doc(DOCTYPE, record_name)
	record.check_permission("write")

	if clinical_state.is_terminal_status(cstr(record.status), DOCTYPE):
		frappe.throw(
			_(
				"{0} is {1} and can no longer be edited. Record a correction as a new dose."
			).format(frappe.bold(record_name), frappe.bold(_(cstr(record.status)))),
			title=_("Dose is closed"),
		)

	for fieldname in UPDATE_FIELDS:
		if fieldname not in data:
			continue
		value = data.get(fieldname)
		if value is None or cstr(value).strip() == "":
			value = None
		elif fieldname in ("qty", "weight"):
			value = flt(value)
		record.set(fieldname, value)

	record.save()

	from pet_app.api.workspace import _preventive_detail

	return ok({"record": _preventive_detail(record.name)})


# ─────────────────────────────────────────────────────────────────────────────
# The worklist
# ─────────────────────────────────────────────────────────────────────────────

# What a list row carries. Deliberately the same field NAMES the detail shape uses, so a
# client can render a row and a detail panel from one type.
LIST_FIELDS = (
	"name",
	"kind",
	"status",
	"pet",
	"guardian",
	"visit",
	"order_id",
	"doctor",
	"provider",
	"care_service",
	"service_option",
	"medication",
	"medication_name",
	"vaccine_type",
	"dose",
	"batch_no",
	"weight",
	"qty",
	"priority",
	"due_date",
	"scheduled_datetime",
	"start_at",
	"end_at",
	"administered_on",
	"administered_by",
	"next_due_date",
	"reminder_enabled",
	"reminder_status",
	"item_code",
	"rate",
	"branch",
	"performing_branch",
	"billed",
	"sales_invoice",
	"creation",
	"modified",
)

# Filters a caller may set, each an exact match on the field of the same name.
#
# `name` IS ONE OF THEM, and it is how a caller reads a single dose out of the worklist. It
# was absent until a detail screen needed it, and its absence did not fail - it returned the
# WHOLE worklist, because an unrecognised key was silently dropped. A client that believed it
# had filtered rendered whatever row happened to be first.
LIST_EQ_FILTERS = (
	"name",
	"pet",
	"kind",
	"status",
	"guardian",
	"visit",
	"branch",
	"care_service",
	"service_option",
	"provider",
	"doctor",
)

# Every other key this endpoint reads. Together with LIST_EQ_FILTERS this is the COMPLETE set
# of things a request may carry, which is what makes refusing the rest safe to do.
LIST_RANGE_FILTERS = ("administered_from", "administered_to", "due_before", "due_after")
LIST_FLAG_FILTERS = ("overdue", "open_only")
LIST_PAGING_KEYS = ("limit", "start", "cursor")

RECOGNISED_LIST_KEYS = frozenset(
	LIST_EQ_FILTERS + LIST_RANGE_FILTERS + LIST_FLAG_FILTERS + LIST_PAGING_KEYS
)

MAX_PAGE_LENGTH = 100


@frappe.whitelist()
@standardize_response
def list_preventive_care_records(payload=None, **kwargs):
	"""The /healthcare/preventive-care worklist, and every filtered view of it.

	ONE QUERY PLUS FOUR LOOKUPS, not one query per row. Labels are resolved in batch after
	the rows come back: a worklist of 50 doses would otherwise issue 250 extra reads, which is
	the mistake `_preventive_detail` is allowed to make (it handles one record) and a list
	endpoint is not.

	`frappe.get_list`, not `get_all`. `get_all` sets `ignore_permissions=True`, which would
	make this a way to read doses the caller cannot read on the generic route - and since
	`Preventive Care Record` is in `utils.branch.SCOPED_DOCTYPES`, it is `get_list` that
	applies the reader's Branch User Permission and keeps one clinic out of another's worklist.

	PAGING IS EXPLICIT AND HAS NO SILENT CAP. `limit` defaults to 50 and is clamped to 100;
	one extra row is fetched to answer `has_more` honestly rather than making the client
	infer it from a short page. Frappe's own default of 20 is never relied on - a default
	page length applied invisibly is what made the endpoint this replaces drop rows.
	"""
	require_doctype_permission(DOCTYPE, "read")

	data = dict(payload or {})
	data.update({k: v for k, v in (kwargs or {}).items() if k != "cmd"})

	# AN UNKNOWN KEY IS REFUSED, NOT IGNORED, and a read endpoint is exactly where that rule
	# has to differ from the write paths. Dropping an unrecognised field on a write changes
	# nothing and the caller sees that nothing changed. Dropping an unrecognised FILTER widens
	# the question instead of narrowing it, and hands back rows the caller never asked for with
	# `ok: true` on them - so `{"name": "PCR-00005"}` returned the entire worklist and a screen
	# that trusted it displayed the wrong dose. There is no answer this endpoint can give to a
	# filter it does not understand that is safer than saying so.
	#
	# Safe to refuse because the set above is closed: it is every key the body below reads, and
	# every key section 3.4 of the contract documents. A caller within the contract cannot trip
	# it; a caller outside it was already getting an answer to a different question.
	unknown = sorted(set(data) - RECOGNISED_LIST_KEYS)
	if unknown:
		frappe.throw(
			_("{0} is not a filter on this worklist. Accepted: {1}.").format(
				frappe.bold(", ".join(unknown)), ", ".join(sorted(RECOGNISED_LIST_KEYS))
			),
			title=_("Unknown filter"),
		)

	# FILTERS ARE A LIST OF TRIPLES, NOT A DICT, and that is not a style choice. A dict holds
	# one condition per field, so two bounds on `administered_on` would silently replace each
	# other, and the `is set` guard below could not coexist with a `<` on the same field.
	conditions = []
	for fieldname in LIST_EQ_FILTERS:
		value = cstr(data.get(fieldname) or "").strip()
		if value:
			conditions.append([fieldname, "=", value])

	by_date = False
	if data.get("administered_from"):
		conditions.append(["administered_on", ">=", getdate(data["administered_from"])])
		by_date = True
	if data.get("administered_to"):
		conditions.append(["administered_on", "<=", getdate(data["administered_to"])])
		by_date = True

	# `["next_due_date", "is", "set"]` IS LOAD-BEARING, every time `next_due_date` is compared.
	# Frappe's query builder renders a comparison on a Date field through `ifnull(...)`, so a
	# NULL date becomes a very old one and matches `< today`. Without this guard, every dose
	# that has no next due date at all - which is most of them, and means "no recurrence" -
	# is reported as overdue. Raw SQL does not behave this way; `frappe.get_list` does.
	if data.get("due_before"):
		conditions += [["next_due_date", "is", "set"], ["next_due_date", "<", getdate(data["due_before"])]]
	if data.get("due_after"):
		conditions += [["next_due_date", "is", "set"], ["next_due_date", ">", getdate(data["due_after"])]]

	# `overdue` is a named question, not a date the client computes - so "overdue" means the
	# same thing on every screen that asks it.
	if cint(data.get("overdue")):
		conditions += [
			["next_due_date", "is", "set"],
			["next_due_date", "<", getdate(nowdate())],
			["status", "!=", "Cancelled"],
			["reminder_status", "!=", "Cancelled"],
		]

	# `open_only` is the worklist's default question: what still has to be done.
	if cint(data.get("open_only")):
		conditions.append(["status", "in", ["Ordered", "In Progress"]])

	limit = max(1, min(cint(data.get("limit") or 50), MAX_PAGE_LENGTH))
	start = max(0, cint(data.get("start") or data.get("cursor") or 0))
	order_by = "administered_on desc, creation desc" if by_date else "creation desc"
	filters = conditions

	# A PET'S PREVENTIVE HISTORY IS READABLE ACROSS BRANCHES; THE WORKLIST IS NOT. See
	# `_pet_history_read`. `pet_scope` is the pet when the exemption applies, else None, and
	# with None this is exactly the `frappe.get_list` call the worklist has always made.
	pet_scope = _pet_history_read(data)
	if pet_scope:
		rows = _BranchExemptEngine().get_query(
			DOCTYPE,
			fields=list(LIST_FIELDS),
			filters=filters,
			order_by=order_by,
			offset=start,
			limit=limit + 1,
			ignore_permissions=False,
			db_query_compat=True,
		).run(as_dict=True)
	else:
		rows = frappe.get_list(
			DOCTYPE,
			filters=filters,
			fields=list(LIST_FIELDS),
			order_by=order_by,
			limit_start=start,
			# One more than asked for, so `has_more` is a fact rather than a guess.
			limit_page_length=limit + 1,
		)
	has_more = len(rows) > limit
	rows = rows[:limit]

	meta = {
		"count": len(rows),
		"has_more": has_more,
		"next_cursor": (start + limit) if has_more else None,
		"start": start,
		"limit": limit,
		"filters_applied": [[c[0], c[1], str(c[2])] for c in conditions],
		"branch_scope_lifted": bool(pet_scope),
	}
	if pet_scope:
		meta.update(_pet_history_counts(pet_scope, conditions))

	return ok({"records": _with_labels(rows)}, meta=meta)


def _pet_history_read(data: dict) -> str | None:
	"""The pet whose history this request reads across branches, or None.

	THE EXEMPTION IS PET-SCOPED AND NOTHING WIDER. It applies only when the request carries a
	non-empty `pet`, which `LIST_EQ_FILTERS` turns into `pet = <value>`, so every row returned
	belongs to that one animal. A request without `pet` - the /healthcare/preventive-care
	worklist, the visit screen's `name` lookup - never reaches `_BranchExemptEngine`.

	Also only for a pet the caller can read. The Pet doctype carries no branch of its own, so
	this does not re-impose branch scoping. It keeps the exemption from reading the history of
	an animal that the caller's other restrictions put out of reach. Failing it does not
	refuse: the request falls back to the ordinary branch-scoped read.
	"""
	pet = cstr(data.get("pet") or "").strip()
	if not pet or not frappe.db.exists("Pet", pet) or not frappe.has_permission("Pet", "read", doc=pet):
		return None
	return pet


def _pet_history_counts(pet: str, conditions: list) -> dict:
	"""How many of this pet's records exist, and how many of them the caller cannot see.

	So the pet tab can tell "this pet has no preventive care" (`pet_total == 0`) from "it has
	records this user may not read" (`hidden > 0`). Counts only, never row data. `hidden`
	answers for this request's own filters, so a kind or status filter does not report
	records the filter itself excluded.
	"""
	matching = frappe.get_all(DOCTYPE, filters=conditions, pluck="name")
	visible = _BranchExemptEngine().get_query(
		DOCTYPE, fields=["name"], filters=conditions, ignore_permissions=False, db_query_compat=True
	).run(as_dict=True, pluck="name")
	return {
		"pet_total": frappe.db.count(DOCTYPE, {"pet": pet}),
		"hidden": len(set(matching) - set(visible)),
	}


class _BranchExemptEngine(Engine):
	"""Frappe's own query engine, blind to ONE user-permission condition: the `branch` field.

	Frappe builds user-permission conditions from `get_doctype_link_fields`, one condition per
	link field whose target doctype the user is restricted on. Dropping the Preventive Care
	Record field named `branch` from that list removes exactly one condition,
	`(IFNULL(branch,'')='' OR branch IN (<user's branches>))`, and leaves the rest of the
	engine unchanged:

	- the doctype read check (no read or select on the doctype still raises or returns nothing)
	- every other User Permission, on every other link field, including a Branch permission's
	  effect on any other Branch-linked field
	- if_owner constraints, permission query hooks and server scripts, and shared documents
	- permitted-field filtering on the selected columns

	Used only by `list_preventive_care_records`, and only through `_pet_history_read`.
	"""

	def get_doctype_link_fields(self, doctype: str | None = None):
		fields = super().get_doctype_link_fields(doctype)
		if (doctype or self.permission_doctype) != DOCTYPE:
			return fields
		return [df for df in fields if df.get("fieldname") != "branch"]


def _with_labels(rows: list) -> list[dict]:
	"""Attach the resolved labels, in four batched reads regardless of row count."""
	practitioners, services, options, pets, guardians = set(), set(), set(), set(), set()
	for row in rows:
		practitioners.update(x for x in (row.get("doctor"), row.get("provider")) if x)
		if row.get("care_service"):
			services.add(row["care_service"])
		if row.get("service_option"):
			options.add(row["service_option"])
		if row.get("pet"):
			pets.add(row["pet"])
		if row.get("guardian"):
			guardians.add(row["guardian"])

	def _map(doctype, names, field):
		if not names:
			return {}
		return {
			r.name: r.get(field)
			for r in frappe.get_all(
				doctype, filters={"name": ["in", sorted(names)]}, fields=["name", field], ignore_permissions=True
			)
		}

	practitioner_labels = _map("Healthcare Practitioner", practitioners, "practitioner_name")
	service_labels = _map("CareService template", services, "service_name")
	option_labels = _map("Care Service Billing Option", options, "service_title")
	pet_labels = _map("Pet", pets, "pet_name")
	guardian_labels = _map("Guardian", guardians, "full_name") if guardians else {}

	out = []
	for row in rows:
		item = dict(row)
		item["doctor_label"] = practitioner_labels.get(row.get("doctor"))
		item["provider_label"] = practitioner_labels.get(row.get("provider"))
		item["care_service_label"] = service_labels.get(row.get("care_service"))
		item["service_option_label"] = option_labels.get(row.get("service_option"))
		# List-only conveniences. A worklist shows whose pet this is; the detail panel does
		# not need them because it is opened from a pet or a visit that already says.
		item["pet_name"] = pet_labels.get(row.get("pet"))
		item["guardian_name"] = guardian_labels.get(row.get("guardian"))
		item["billed"] = cint(row.get("billed"))
		item["reminder_enabled"] = cint(row.get("reminder_enabled"))
		out.append(item)
	return out
