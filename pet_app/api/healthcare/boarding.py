from __future__ import annotations

from contextlib import contextmanager
from math import ceil

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, cstr, flt, get_datetime, getdate, now_datetime, nowdate

from erpnext.accounts.party import get_party_account
from pet_app.api.accounting.cashier import (
	_default_cash_mode_of_payment,
	_get_default_company,
	_get_settings_doc,
	_is_cash_mode,
	_validate_account,
)
from pet_app.api.permissions import (
	get_user_roles,
	require_doctype_permission,
	require_restriction_value,
	user_has_full_access,
)
from pet_app.api.response import fail, standardize_response
from pet_app.api.sales import _set_optional_guardian_reference
from pet_app.pet_app.doctype.pet_boarding.pet_boarding import (
	ACTIVE_BOARDING_STATUSES,
	CLOSED_BOARDING_STATUSES,
	PENDING_ROOM_STATUS,
	ROOM_ASSIGNED_ACTIVE_BOARDING_STATUSES,
	get_active_boarding_for_room,
)
from pet_app.pet_app.doctype.pet_care_episode.pet_care_episode import ACTIVE_EPISODE_STATUSES
from pet_app.utils.case_assignment import DIRECT_ASSIGN_ROLES, visit_practitioner
from pet_app.utils.practitioner import get_practitioner_for_user
from pet_app.utils.guardian_customer import get_guardian_record, get_or_create_customer_from_guardian
from pet_app.utils.invoice_reuse import get_or_create_open_invoice
from pet_app.utils.boarding_occupancy import (
	ACTIVE_OCCUPANT_STATUS,
	OCCUPANT_DOCTYPE,
	active_occupant_count,
	active_occupants,
	assert_capacity,
	assert_room_available,
	active_boardings_for_pets,
	billable_occupants,
	boarding_lock,
	close_all_active_occupants,
	close_occupant,
	close_open_stint,
	find_active_boarding_for_pet,
	find_occupant,
	get_max_pets_per_booking,
	boarding_pets,
	is_last_active_occupant,
	occupant_nights,
	open_stint,
	pet_is_on_boarding,
	pet_locks,
	room_locks,
)
from pet_app.utils.boarding_pricing import resolve_boarding_rate
from pet_app.utils.mortality import assert_pet_not_deceased
from pet_app.utils.medication_stock import (
	assert_row_can_record_stock,
	is_opted_in,
	issue_for_dispense,
	receive_for_return,
	returnable_stock_qty,
)
from pet_app.utils.price_list import get_veterinary_selling_price_list


ROOM_STAY_SERVICE_PREFIX = "boarding_room_stay"
LOCK_TIMEOUT_SECONDS = 15
BOARDING_TYPES = ("Travel", "Treatment")
BILLABLE_ITEM_STATUSES = ("Draft", "Billable", "Billed", "Cancelled", "Included")
# Charges that are real but deliberately not invoiced. Kept separate from Cancelled: a
# cancelled charge did not happen, an included one did and its row is the dispensing record.
NON_INVOICED_STATUSES = ("Cancelled", "Included")
# Charges whose GOODS never leave the shelf. Only Cancelled - and this is the whole
# point of the distinction. Included and Cancelled sat in one tuple, and because the
# invoice was the only thing that had ever moved stock, "not invoiced" silently meant
# "never issued". A medical boarding rate absorbs the CHARGE, not the goods: the
# medication is given to the animal either way and the vial has to come off the shelf
# either way, or the clinic's inventory says it still owns a drug it injected. Stock now
# moves at dispense, which consults this tuple and not NON_INVOICED_STATUSES, so an
# Included row issues stock exactly like a Billable one and simply never reaches an
# invoice line.
NON_ISSUED_STATUSES = ("Cancelled",)
BILLABLE_ITEM_TYPES = ("Room Stay", "Service", "Medication", "Lab", "Imaging", "Procedure", "Product", "Other")
MEDICATION_PLAN_TYPES = {"Medication", "Injection"}
DISPENSE_STATUSES = ("Prescribed", "Pending Dispense", "Dispensed", "Partially Dispensed", "Cancelled", "Returned")
DISPENSE_FINAL_STATUSES = {"Dispensed", "Cancelled", "Returned"}

# Clinical orders that can be raised directly from a checked-in boarding (no Vet Visit).
ORDER_KINDS = ("lab", "radiology", "service", "medication")
ORDER_PRIORITIES = ("Routine", "Normal", "High", "Urgent")
DEFAULT_ORDER_PRIORITY = "Routine"
DUPLICATE_ORDER_WINDOW_SECONDS = 120
ORDER_KIND_DOCTYPE = {"lab": "Lab", "radiology": "Imaging", "service": "PetCareService"}
ORDER_KIND_BILLABLE_TYPE = {"lab": "Lab", "radiology": "Imaging", "service": "Service", "medication": "Medication"}
ORDER_TERMINAL_STATUSES = {
	"Lab": {"Released", "Completed", "Cancelled"},
	"Imaging": {"Released", "Completed", "Cancelled"},
	"PetCareService": {"completed", "cancelled", "Completed", "Cancelled"},
}
BOARDING_READ_ROLES = ("System Manager", "Healthcare Practitioner", "Doctor", "Accounts User", "Healthcare", "Accounting")
BOARDING_WRITE_ROLES = ("System Manager", "Healthcare Practitioner", "Doctor", "Accounts User", "Healthcare", "Accounting")
VISIT_BOARDING_ACTIVE_STATUSES = ACTIVE_BOARDING_STATUSES

# A stay started from a visit is almost always medical - every visit-linked stay on record is.
# That makes Treatment a sound DEFAULT and an unsound assumption. It used to be written into
# the record with no way for the operator to see or change it; now it is only what they get
# when they express no preference, and it is surfaced so the UI can show it as a choice.
VISIT_BOARDING_SUGGESTED_TYPE = "Treatment"
CANCELLABLE_BOARDING_STATUSES = (PENDING_ROOM_STATUS, "Reserved")

# Which visit statuses cannot start a stay. Two flows are supported, and they are the same
# code path: the animal goes to a kennel mid-case (visit still Draft / In Progress /
# Follow-up Needed), or the case is finished and invoiced first and the animal is handed to
# boarding afterwards (visit Completed). Both create the stay at PENDING_ROOM_STATUS with
# no room, so reception assigns and checks in identically and nothing downstream has to
# know which door was used.
#
# Completed is safe here because this path never saves the Vet Visit: it reads pet,
# guardian and customer off it and inserts a Pet Boarding. So Vet Visit's billing locks
# (_validate_sales_invoice_lock, _validate_billing_lock) are never reached, and
# set_values_from_visit does not exist on Pet Boarding. The stay's branch comes from
# Pet Boarding Settings.boarding_branch via utils.branch.stamp_boarding_branch, never from
# the visit, so a closed visit cannot misattribute it.
#
# Cancelled stays refused: a cancelled visit is a visit that did not happen, so there is no
# clinical event to board off the back of.
VISIT_BOARDING_BLOCKED_VISIT_STATUSES = {"Cancelled"}


def _has_boarding_role(*allowed_roles, user=None):
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	user_roles = set(frappe.get_roles(user) or [])
	return bool(user_roles & set(allowed_roles))


def _has_doctype_permission(doctype: str, ptype: str) -> bool:
	try:
		return bool(frappe.has_permission(doctype, ptype=ptype))
	except Exception:
		return False


def _require_boarding_role(*allowed_roles):
	if not _has_boarding_role(*allowed_roles):
		frappe.throw(_("Not permitted"), frappe.PermissionError)


def _require_boarding_read_access():
	if _has_boarding_role(*BOARDING_READ_ROLES):
		return
	if _has_doctype_permission("Pet Boarding", "read") or _has_doctype_permission("Service Room", "read"):
		return
	frappe.throw(_("Not permitted"), frappe.PermissionError)


def _require_boarding_write_access():
	if _has_boarding_role(*BOARDING_WRITE_ROLES) or _has_doctype_permission("Pet Boarding", "write"):
		return
	frappe.throw(_("Not permitted"), frappe.PermissionError)


def _require_boarding_invoice_access():
	if not (_has_boarding_role(*BOARDING_WRITE_ROLES) or _has_doctype_permission("Pet Boarding", "write")):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	require_doctype_permission("Sales Invoice", "create")


def _log_boarding_event(event: str, **context):
	frappe.logger("pet_app.boarding").info({"event": event, **context})


@frappe.whitelist()
@standardize_response
def list_boarding_units(search=None, occupancy=None, date=None):
	_require_boarding_read_access()
	rooms = _get_active_service_rooms(search=search)
	# One occupant-grained query for the whole page, then one card per room from its slice.
	state = _get_room_occupancy_state([room.name for room in rooms])

	data = []
	for room in rooms:
		row = _serialize_room(room, entry=state.get(room.name))
		if _matches_occupancy(row.get("occupancy"), occupancy):
			data.append(row)

	return {"data": data, "total": len(data)}


@frappe.whitelist()
@standardize_response
def get_boarding_detail(boarding_id=None, room_id=None, name=None):
	_require_boarding_read_access()
	boarding_name = boarding_id
	room_name = room_id

	if not boarding_name and name and frappe.db.exists("Pet Boarding", name):
		boarding_name = name
	elif not room_name and name and frappe.db.exists("Service Room", name):
		room_name = name

	if boarding_name:
		boarding = frappe.get_doc("Pet Boarding", boarding_name)
		boarding.check_permission("read")
		room = _get_service_room(boarding.service_room) if boarding.service_room else None
		return _serialize_detail(room, boarding)

	if room_name:
		room = _get_service_room(room_name)
		active = get_active_boarding_for_room(room.name)
		boarding = frappe.get_doc("Pet Boarding", active.name) if active else None
		if boarding:
			boarding.check_permission("read")
		return _serialize_detail(room, boarding)

	frappe.throw(_("boarding_id, room_id, or name is required."))


@frappe.whitelist()
@standardize_response
def list_boarding_records(
	search=None,
	status=None,
	pet_id=None,
	guardian_id=None,
	date_from=None,
	date_to=None,
	limit_start=0,
	limit_page_length=10,
	order_by="modified desc",
):
	_require_boarding_read_access()
	conditions = ["pb.docstatus < 2"]
	values = {}

	if status:
		conditions.append("(pb.record_status = %(status)s OR pb.status = %(status)s)")
		values["status"] = status
	if pet_id:
		conditions.append("pb.pet = %(pet_id)s")
		values["pet_id"] = pet_id
	if guardian_id:
		guardian_id = _resolve_guardian_id_from_any_identity(guardian_id)
		conditions.append("pb.guardian = %(guardian_id)s")
		values["guardian_id"] = guardian_id
	if date_from:
		conditions.append("DATE(COALESCE(pb.check_in, pb.reserved_at, pb.creation)) >= %(date_from)s")
		values["date_from"] = getdate(date_from)
	if date_to:
		conditions.append("DATE(COALESCE(pb.check_in, pb.reserved_at, pb.creation)) <= %(date_to)s")
		values["date_to"] = getdate(date_to)
	if search:
		values["search"] = f"%{search}%"
		conditions.append(
			"""(
				pb.name LIKE %(search)s
				OR pb.record_status LIKE %(search)s
				OR pb.customer LIKE %(search)s
				OR sr.room_code LIKE %(search)s
				OR sr.room_name LIKE %(search)s
				OR p.pet_name LIKE %(search)s
				OR g.full_name LIKE %(search)s
				OR g.phone LIKE %(search)s
			)"""
		)

	where_clause = " AND ".join(conditions)
	order_clause = _sanitize_order_by(order_by)
	values["limit_start"] = cint(limit_start)
	values["limit_page_length"] = cint(limit_page_length or 10)

	total = frappe.db.sql(
		f"""
		SELECT COUNT(*)
		FROM `tabPet Boarding` pb
		LEFT JOIN `tabService Room` sr ON sr.name = pb.service_room
		LEFT JOIN `tabPet` p ON p.name = pb.pet
		LEFT JOIN `tabGuardian` g ON g.name = pb.guardian
		WHERE {where_clause}
		""",
		values,
	)[0][0]

	rows = frappe.db.sql(
		f"""
		SELECT
			pb.name,
			pb.service_room,
			sr.room_code,
			sr.room_name,
			pb.pet,
			p.pet_name,
			p.pet_image,
			pb.guardian,
			g.full_name AS guardian_name,
			g.phone AS guardian_phone,
			pb.customer,
			pb.boarding_type,
			pb.record_status,
			pb.status,
			pb.workflow_state,
			pb.reserved_at,
			pb.check_in,
			pb.check_out,
			pb.stay_days,
			pb.stay_hours,
			pb.total_cost,
			pb.deposit,
			pb.deposit_payment_entry,
			pb.balance,
			pb.billing_status,
			pb.sales_invoice,
			pb.note,
			pb.boarding_note,
			pb.boarded_by,
				pb.cancelled_by,
				pb.cancellation_note,
			pb.visit,
			pb.expected_check_out,
			pb.notes,
			pb.docstatus,
			pb.creation,
			pb.modified
		FROM `tabPet Boarding` pb
		LEFT JOIN `tabService Room` sr ON sr.name = pb.service_room
		LEFT JOIN `tabPet` p ON p.name = pb.pet
		LEFT JOIN `tabGuardian` g ON g.name = pb.guardian
		WHERE {where_clause}
		ORDER BY {order_clause}
		LIMIT %(limit_start)s, %(limit_page_length)s
		""",
		values,
		as_dict=True,
	)

	return {"data": [_serialize_boarding_record(row) for row in rows], "total": total}



@frappe.whitelist(methods=["POST"])
@standardize_response
def start_visit_boarding(visit=None, note=None, expected_check_out=None, boarding_type=None, data=None, **kwargs):
	"""Start a stay from a visit.

	`boarding_type` is the operator's choice. Omitted, it falls to
	VISIT_BOARDING_SUGGESTED_TYPE - which is what every existing caller gets, so behaviour
	is unchanged for them - but it is now a stated default rather than a hardcoded value
	the operator never saw.
	"""
	payload = _coerce_payload(data, kwargs)
	visit_name = cstr(payload.get("visit") or visit).strip()
	boarding_note = cstr(payload.get("note") if "note" in payload else note).strip()
	expected = payload.get("expected_check_out") if "expected_check_out" in payload else expected_check_out
	chosen_type = payload.get("boarding_type") if "boarding_type" in payload else boarding_type

	savepoint = f"start_visit_boarding_{frappe.generate_hash(length=10)}"
	frappe.db.savepoint(savepoint)
	try:
		boarding = _start_visit_boarding_atomic(visit_name, boarding_note, expected, boarding_type=chosen_type)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise
	else:
		frappe.db.release_savepoint(savepoint)

	return {
		"success": True,
		"boarding_id": boarding.name,
		"record_status": boarding.record_status,
		"status": boarding.status,
		"boarding_type": boarding.boarding_type,
		"suggested_boarding_type": VISIT_BOARDING_SUGGESTED_TYPE,
		"boarding": _serialize_boarding_doc(boarding),
	}


@frappe.whitelist(methods=["POST"])
@standardize_response
def cancel_boarding(boarding=None, boarding_id=None, name=None, note=None, data=None, **kwargs):
	payload = _coerce_payload(data, kwargs)
	boarding_name = cstr(payload.get("boarding") or payload.get("boarding_id") or boarding or boarding_id or name).strip()
	cancel_note = cstr(payload.get("note") if "note" in payload else note).strip()

	savepoint = f"cancel_boarding_{frappe.generate_hash(length=10)}"
	frappe.db.savepoint(savepoint)
	try:
		cancelled = _cancel_boarding_atomic(boarding_name, cancel_note)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise
	else:
		frappe.db.release_savepoint(savepoint)

	credit = cancelled.flags.get("deposit_credit")
	return {
		"success": True,
		"boarding_id": cancelled.name,
		"record_status": cancelled.record_status,
		"status": cancelled.status,
		"sales_invoice": cancelled.sales_invoice,
		# Surfaced so the counter sees the money did not vanish and is not owed back.
		"deposit_credit_remaining": flt(credit.unallocated_amount) if credit else 0.0,
		"deposit_payment_entry": credit.payment_entry if credit else None,
		"boarding": _serialize_boarding_doc(cancelled),
	}


def _cancel_boarding_atomic(boarding_name: str, cancel_note: str):
	if not boarding_name:
		frappe.throw(_("Pet Boarding is required."))
	if not cancel_note:
		frappe.throw(_("Cancellation note is required."))
	if not frappe.db.exists("Pet Boarding", boarding_name):
		frappe.throw(_("Pet Boarding {0} was not found.").format(frappe.bold(boarding_name)))

	boarding = frappe.get_doc("Pet Boarding", boarding_name)
	if boarding.docstatus != 0:
		frappe.throw(_("Only draft/open Pet Boarding records can be cancelled."))
	if boarding.record_status not in CANCELLABLE_BOARDING_STATUSES:
		frappe.throw(_("Only Pending Room or Reserved boarding records can be cancelled before check-in."))
	if boarding.sales_invoice:
		frappe.throw(_("Pet Boarding {0} already has Sales Invoice {1}.").format(frappe.bold(boarding.name), frappe.bold(boarding.sales_invoice)))
	if not _user_can_cancel_boarding(boarding):
		frappe.throw(_("Only the visit doctor or a Coordinator/Admin can cancel boarding."), frappe.PermissionError)

	def apply_cancel():
		boarding.set("billable_items", [])
		boarding.sales_invoice = None
		boarding.billing_status = "Unbilled"
		boarding.total_cost = 0
		boarding.balance = 0
		boarding.record_status = "Cancelled"
		boarding.status = "Cancelled"
		boarding.workflow_state = "Cancelled"
		boarding.cancelled_by = frappe.session.user
		boarding.cancellation_note = cancel_note
		# Same reason as check-out: an occupant left Active holds its room against
		# `room_occupancy` no matter what the booking says. One cancelled booking on this
		# site is holding a room that way today.
		for closed in close_all_active_occupants(boarding, status="Cancelled", note=cancel_note):
			close_open_stint(boarding.name, cstr(closed.pet).strip(), now_datetime())
		boarding.save(ignore_permissions=True)
		boarding.add_comment("Comment", _("Boarding cancelled by {0}. Reason: {1}").format(frappe.session.user, cancel_note))
		_log_boarding_event("BOARDING_CANCELLED", boarding=boarding.name, user=frappe.session.user)
		# The deposit is not refunded and not reversed - it stays as customer credit. Said
		# out loud on the stay and on the entry so it is not left dangling silently.
		boarding.flags.deposit_credit = _surface_deposit_credit_on_cancel(boarding)

	if boarding.service_room:
		with _service_room_lock(boarding.service_room):
			apply_cancel()
	else:
		apply_cancel()

	return boarding


def _start_visit_boarding_atomic(visit_name: str, boarding_note: str, expected_check_out=None, *, boarding_type=None):
	if not visit_name:
		frappe.throw(_("Visit is required."))
	if not boarding_note:
		frappe.throw(_("Boarding note is required."))
	if not frappe.db.exists("Vet Visit", visit_name):
		frappe.throw(_("Vet Visit {0} was not found.").format(frappe.bold(visit_name)))

	visit_doc = frappe.get_doc("Vet Visit", visit_name)
	_assert_can_start_visit_boarding(visit_doc)

	pet = visit_doc.get("animal_patient") or visit_doc.get("pet")
	guardian = visit_doc.get("guardian")
	if not pet:
		frappe.throw(_("Visit {0} has no pet to board.").format(frappe.bold(visit_doc.name)))
	if not guardian:
		frappe.throw(_("Visit {0} has no guardian to board.").format(frappe.bold(visit_doc.name)))

	customer = visit_doc.get("customer") or get_or_create_customer_from_guardian(guardian)
	resolved_type = _resolve_visit_boarding_type(boarding_type)
	operator_chose_type = bool(cstr(boarding_type).strip())
	boarding = frappe.get_doc(
		{
			"doctype": "Pet Boarding",
			"visit": visit_doc.name,
			"pet": pet,
			"guardian": guardian,
			"customer": customer,
			"boarding_type": resolved_type,
			"record_status": PENDING_ROOM_STATUS,
			"status": "Open",
			"workflow_state": PENDING_ROOM_STATUS,
			"reserved_at": now_datetime(),
			"expected_check_out": getdate(expected_check_out) if expected_check_out else None,
			"boarding_note": boarding_note,
			"note": boarding_note,
			"boarded_by": frappe.session.user,
			"billing_status": "Unbilled",
		}
	)
	# The visit's animal becomes an occupant HERE, before the insert, not after it.
	#
	# This path used to set the legacy `pet` scalar and stop, and it was the only
	# booking-creation path in the app that did. Everything downstream reads occupants:
	# `room_occupancy` and the hub's per-room count, the booking's own roster, the
	# deceased gate at check-in, per-pet pricing. All of them iterate a list, so on a
	# booking with no rows they iterate nothing and report nothing wrong - the room drew
	# Available with an animal asleep in it, the stay read "0 pets", and the room-stay
	# charge still appeared because `_ensure_room_stay_billable_item` falls back to
	# `_synthetic_occupant` off the scalar. Six bookings reached that state; BRD-00117
	# was the one somebody finally noticed.
	#
	# ORDERING: append, then price, then ONE insert. Do not move the append below
	# `insert()` and re-save - that is the shape this codebase has now been bitten by
	# three times, and it is the same shape every time. See
	# `pet_app/utils/sales_invoice_guard.py:50` (`_refresh_payment_schedule`): a stale
	# Payment Schedule CHILD row silently overwrote the parent's `due_date` through
	# `AccountsController.set_due_date()`, and 562 of 713 open drafts could not be saved.
	# A parent and its children are one document. Writing the parent first leaves a
	# committed record that is already wrong, and every later path that "just adds the
	# child" is reading a document that has moved underneath it.
	#
	# `service_room` and `joined_at` are deliberately empty: this booking is Pending Room,
	# so there is no kennel yet, and reserving one is not the animal arriving in it. The
	# room-assignment path stamps the room and check-in stamps the arrival - both of them
	# loops over `active_occupants`, which is exactly why the row has to exist by then.
	_append_reservation_occupants(boarding, [pet], None, resolved_type, None)
	boarding.insert(ignore_permissions=True)
	# The type and where it came from are recorded on the record itself: when a stay is
	# later queried at the travel or medical rate, the trail says whether a person picked
	# that or the system suggested it.
	#
	# The visit's status goes in for the same reason. VISIT_BOARDING_SUGGESTED_TYPE is
	# "Treatment" because a stay started mid-case is almost always medical; a stay started
	# after the case closed and invoiced is a weaker inference - it is as likely to be the
	# guardian leaving a well pet - and Travel and Treatment are priced differently. The
	# suggestion is deliberately not split by status (that would change what an existing
	# caller silently gets), so the trail has to say which door the stay came through.
	boarding.add_comment(
		"Comment",
		_("Visit boarding started from {0} ({1}) by {2} as {3} ({4}).").format(
			visit_doc.name,
			cstr(visit_doc.get("status")).strip() or _("Unknown"),
			frappe.session.user,
			resolved_type,
			_("operator choice") if operator_chose_type else _("suggested default"),
		),
	)
	_log_boarding_event(
		"VISIT_BOARDING_STARTED",
		boarding=boarding.name,
		visit=visit_doc.name,
		visit_status=cstr(visit_doc.get("status")).strip(),
		boarding_type=resolved_type,
		boarding_type_source="operator" if operator_chose_type else "suggested",
		user=frappe.session.user,
	)
	return boarding


def can_start_visit_boarding(visit_doc) -> bool:
	try:
		if not visit_doc or cstr(visit_doc.get("status")) in VISIT_BOARDING_BLOCKED_VISIT_STATUSES:
			return False
		if active_boarding_for_visit(visit_doc.name):
			return False
		# The animal, not just this visit. A pet added to someone else's booking as an
		# occupant leaves `Pet Boarding.visit` null, so the check above sees nothing and
		# the action was offered - and it did not fail, it created a SECOND booking.
		# Pending Room and Reserved count as much as Checked In: a stay started from a
		# completed visit lands with no room, and offering the action again is exactly
		# how the duplicate appears.
		pet = cstr(visit_doc.get("animal_patient") or visit_doc.get("pet")).strip()
		if pet and find_active_boarding_for_pet(pet):
			return False
		return _user_can_start_visit_boarding(visit_doc)
	except Exception:
		return False


def can_cancel_visit_boarding(visit_doc) -> bool:
	try:
		if not visit_doc:
			return False
		boarding = active_boarding_for_visit(visit_doc.name)
		if not boarding or boarding.record_status not in CANCELLABLE_BOARDING_STATUSES:
			return False
		return _user_can_cancel_boarding(boarding)
	except Exception:
		return False


def active_boarding_for_visit(visit_name: str):
	visit_name = cstr(visit_name).strip()
	if not visit_name:
		return None
	if not frappe.get_meta("Pet Boarding").has_field("visit"):
		return None
	rows = frappe.get_all(
		"Pet Boarding",
		filters={
			"visit": visit_name,
			"record_status": ["in", VISIT_BOARDING_ACTIVE_STATUSES],
			"docstatus": ["<", 2],
		},
		fields=[
			"name",
			"record_status",
			"status",
			"boarding_type",
			"service_room",
			"expected_check_out",
			"boarding_note",
			"boarded_by",
			"visit",
			"cancelled_by",
			"cancellation_note",
			"creation",
		],
		order_by="creation desc",
		limit_page_length=1,
	)
	return rows[0] if rows else None


def checked_in_boarding_for_visit(visit_name: str):
	visit_name = cstr(visit_name).strip()
	if not visit_name:
		return None
	if not frappe.get_meta("Pet Boarding").has_field("visit"):
		return None
	rows = frappe.get_all(
		"Pet Boarding",
		filters={
			"visit": visit_name,
			"record_status": "Checked In",
			"docstatus": ["<", 2],
		},
		fields=[
			"name",
			"record_status",
			"status",
			"boarding_type",
			"service_room",
			"expected_check_out",
			"boarding_note",
			"boarded_by",
			"visit",
			"creation",
		],
		order_by="creation desc",
		limit_page_length=1,
	)
	return rows[0] if rows else None


def visit_boarding_payload(visit_doc) -> dict | None:
	row = active_boarding_for_visit(visit_doc.name)
	if not row:
		return None
	service_room_name = None
	if row.get("service_room"):
		service_room_name = frappe.db.get_value("Service Room", row.service_room, "room_name")
	boarded_by_name = frappe.db.get_value("User", row.boarded_by, "full_name") if row.get("boarded_by") else None
	return {
		"name": row.name,
		"status": row.record_status,
		"boarding_type": row.boarding_type,
		"service_room": row.service_room,
		"service_room_name": service_room_name,
		"expected_check_out": row.expected_check_out,
		"boarding_note": row.boarding_note,
		"boarded_by": row.boarded_by,
		"boarded_by_name": boarded_by_name,
		"created_at": row.creation,
	}


def _pet_already_boarding_message(pet: str, active: dict) -> str:
	"""THE refusal for "this animal is already on a booking".

	One string, used by the reserve/add-occupant gate and by the visit start gate, because
	the frontend surfaces it verbatim when its state arrives stale and two wordings for one
	rule would read as two different problems.
	"""
	return _("Pet {0} is already on boarding {1} ({2}).").format(
		frappe.bold(pet), frappe.bold(active["name"]), frappe.bold(active["record_status"])
	)


def visit_pet_boarding_payload(visit_doc) -> dict:
	"""Is this visit's PET on a booking - whoever created it.

	Distinct from `visit_boarding_payload`, and the pair is the point. That one answers
	"did THIS VISIT start a stay", resolved through `Pet Boarding.visit`, and it is null
	for a pet added to someone else's booking as an occupant - which is null on all but 26
	of 1043 visits here. This one answers "is the ANIMAL boarding right now", resolved from
	the pet's own active occupancy, which is what decides whether Send to Boarding can
	succeed.

	Both are needed and neither replaces the other: cancelling a visit's boarding, and
	refusing to complete a case mid-stay, must key on the stay this visit owns rather than
	on any booking that happens to hold the animal.

	Resolved through `find_active_boarding_for_pet`, so the field the client renders and
	the rule the server enforces are the same lookup and cannot disagree. Two queries;
	`active_boardings_for_pets` is the batched form for a list.
	"""
	pet = cstr(visit_doc.get("animal_patient") or visit_doc.get("pet")).strip() if visit_doc else ""
	active = find_active_boarding_for_pet(pet) if pet else None
	return _pet_boarding_fields(active)


def _pet_boarding_fields(active: dict | None) -> dict:
	"""The three keys, always present so the client never has to test for their absence."""
	if not active:
		return {"pet_boarding_id": None, "pet_boarding_status": None, "pet_boarding_room": None}
	room = cstr(active.get("service_room")).strip()
	return {
		"pet_boarding_id": active.get("name"),
		"pet_boarding_status": active.get("record_status"),
		# The room's own name, not its id - this exists to be shown. Falls back to the id
		# rather than to null: a Pending Room stay has no room at all, and "no room yet"
		# and "a room whose name is missing" must not render the same.
		"pet_boarding_room": (frappe.db.get_value("Service Room", room, "room_name") or room) if room else None,
	}


def pet_boarding_fields_for_visits(visit_rows) -> dict[str, dict]:
	"""The three keys for many visits at once, keyed by visit name. One pass, not N.

	For any list that shows the action. Each row needs `name` and the pet
	(`animal_patient`, or `pet`). Room names are looked up once for the distinct rooms
	involved rather than once per visit.
	"""
	rows = list(visit_rows or [])
	by_pet = {}
	for row in rows:
		pet = cstr(row.get("animal_patient") or row.get("pet")).strip()
		if pet:
			by_pet.setdefault(pet, []).append(cstr(row.get("name")).strip())

	active_by_pet = active_boardings_for_pets(by_pet.keys())
	rooms = {cstr(a.get("service_room")).strip() for a in active_by_pet.values() if a.get("service_room")}
	room_names = {}
	if rooms:
		room_names = {
			r.name: r.room_name
			for r in frappe.get_all(
				"Service Room", filters={"name": ["in", list(rooms)]}, fields=["name", "room_name"]
			)
		}

	result = {cstr(row.get("name")).strip(): _pet_boarding_fields(None) for row in rows}
	for pet, visit_names in by_pet.items():
		active = active_by_pet.get(pet)
		if not active:
			continue
		room = cstr(active.get("service_room")).strip()
		fields = {
			"pet_boarding_id": active.get("name"),
			"pet_boarding_status": active.get("record_status"),
			"pet_boarding_room": (room_names.get(room) or room) if room else None,
		}
		for visit_name in visit_names:
			result[visit_name] = dict(fields)
	return result


def _assert_can_start_visit_boarding(visit_doc):
	if cstr(visit_doc.get("status")) in VISIT_BOARDING_BLOCKED_VISIT_STATUSES:
		frappe.throw(_("Cancelled visits cannot start boarding."))
	existing = active_boarding_for_visit(visit_doc.name)
	if existing:
		frappe.throw(
			_("Visit {0} already has active boarding {1} ({2}).").format(
				frappe.bold(visit_doc.name), frappe.bold(existing.name), frappe.bold(existing.record_status)
			)
		)
	# Hiding the button is not the fix. This path never checked the ANIMAL - only whether
	# this visit had started a stay of its own - so a visit for a pet boarding as someone
	# else's occupant went straight through and inserted a duplicate booking. The insert
	# does not catch it either: a visit-started stay is Pending Room with no service_room,
	# and the room-level rule returns early for exactly that shape.
	pet = cstr(visit_doc.get("animal_patient") or visit_doc.get("pet")).strip()
	active_for_pet = find_active_boarding_for_pet(pet) if pet else None
	if active_for_pet:
		frappe.throw(_pet_already_boarding_message(pet, active_for_pet))
	if not _user_can_start_visit_boarding(visit_doc):
		frappe.throw(_("Only the visit doctor or a Coordinator/Admin can start boarding from a visit."), frappe.PermissionError)


def _user_can_start_visit_boarding(visit_doc) -> bool:
	user = frappe.session.user
	if user == "Administrator" or user_has_full_access(user):
		return True
	roles = set(get_user_roles(user) or [])
	if roles & DIRECT_ASSIGN_ROLES:
		return True
	current = visit_practitioner(visit_doc)
	return bool(current and get_practitioner_for_user(user, include_disabled=False) == current)


def _user_can_cancel_boarding(boarding) -> bool:
	user = frappe.session.user
	if user == "Administrator" or user_has_full_access(user):
		return True
	roles = set(get_user_roles(user) or [])
	if roles & DIRECT_ASSIGN_ROLES:
		return True
	if not boarding.get("visit"):
		return False
	visit_doc = frappe.get_doc("Vet Visit", boarding.visit)
	current = visit_practitioner(visit_doc)
	return bool(current and get_practitioner_for_user(user, include_disabled=False) == current)


def _coerce_payload(data, kwargs) -> dict:
	payload = {}
	if data:
		payload = frappe.parse_json(data) if isinstance(data, str) else data
	if payload is None:
		payload = {}
	if not isinstance(payload, dict):
		frappe.throw(_("Payload must be an object."))
	payload = dict(payload)
	payload.update({key: value for key, value in kwargs.items() if value is not None})
	return payload

@frappe.whitelist()
@standardize_response
def reserve_room(roomId, petId=None, petIds=None, guardianId=None, checkIn=None, checkOut=None, note=None, boardingType=None, boarding_id=None, boardingId=None, name=None):
	"""Reserve a room for one guardian's pets.

	`petIds` (a list) is the forward-looking parameter; `petId` (a single name) still works
	alone so no existing caller breaks. Supplying both is fine when they agree and refused
	when they do not - a caller in two minds gets told, not guessed at.

	Capacity is enforced, never truncated: while Pet Boarding Settings allows one pet, a
	two-pet request is REFUSED with the limit in the message rather than quietly reserving
	the first. When that setting rises to 7 this same code accepts seven, unchanged.
	"""
	_require_boarding_write_access()
	room_id = cstr(roomId).strip()
	boarding_name = cstr(boarding_id or boardingId or name).strip()
	if boarding_name:
		return _reserve_existing_pending_boarding(boarding_name, room_id, checkIn=checkIn, checkOut=checkOut, note=note)

	pets = _resolve_reservation_pets(petId, petIds)
	guardian_id = cstr(guardianId).strip()

	if not room_id:
		frappe.throw(_("Service Room is required."))

	_assert_reservation_capacity(len(pets))

	# Both rooms and pets are contended, and by different callers. The room lock stops two
	# guardians claiming one room; the pet locks stop one pet being reserved into two rooms
	# at once, which the room lock cannot see because the two callers hold different rooms.
	# There is no booking lock here because the booking does not exist yet - nothing else
	# can be adding pets to it. The booking lock belongs on the paths that mutate an
	# existing occupant set.
	with _service_room_lock(room_id), pet_locks(*pets):
		room = _get_service_room(room_id)
		if room.status != "Active":
			frappe.throw(_("Service Room {0} is inactive.").format(frappe.bold(room_id)))

		existing = get_active_boarding_for_room(room.name)
		if existing:
			frappe.throw(
				_("Service Room {0} already has active boarding {1} ({2}).").format(
					frappe.bold(room.name),
					frappe.bold(existing.name),
					frappe.bold(existing.record_status),
				)
			)

		# The first pet resolves the guardian when none was sent; every pet is then checked
		# against that guardian, so a mixed-guardian list cannot slip through on the
		# strength of its first entry.
		guardian_id = _resolve_guardian_for_pet(pets[0], guardian_id)
		_assert_pets_reservable(pets, guardian_id)
		customer_id = get_or_create_customer_from_guardian(guardian_id)

		boarding_type = _normalize_boarding_type(boardingType)
		joined_at = _coerce_datetime(checkIn)
		boarding = frappe.get_doc(
			{
				"doctype": "Pet Boarding",
				"service_room": room.name,
				# Legacy single-pet field, still mandatory until Stage 6. The first pet is
				# the booking's nominal pet; the occupant rows are the truth.
				"pet": pets[0],
				"guardian": guardian_id,
				"customer": customer_id,
				"boarding_type": boarding_type,
				"record_status": "Reserved",
				"status": "Open",
				"workflow_state": "Reserved",
				"reserved_at": now_datetime(),
				"check_in": joined_at,
				"check_out": _coerce_datetime(checkOut),
				"note": note,
				"billing_status": "Unbilled",
			}
		)
		_append_reservation_occupants(boarding, pets, room.name, boarding_type, joined_at)
		_ensure_room_stay_billable_item(boarding)
		boarding.insert()
		_open_reservation_stints(boarding, pets, room.name)
		boarding.add_comment(
			"Comment",
			_("Boarding reserved by {0} for {1}.").format(frappe.session.user, ", ".join(pets)),
		)
		_log_boarding_event(
			"BOARDING_RESERVED",
			boarding=boarding.name,
			room=room.name,
			pets=pets,
			pet_count=len(pets),
			user=frappe.session.user,
		)

	return {
		"success": True,
		"boarding_id": boarding.name,
		"room_id": room.name,
		"record_status": boarding.record_status,
		"occupancy": "Reserved",
		"customer": boarding.customer,
		"pets": pets,
		"boarding": _serialize_boarding_doc(boarding),
	}


def _resolve_reservation_pets(pet_id, pet_ids) -> list[str]:
	"""One ordered list of pets from either parameter, or a refusal.

	Order is preserved from `petIds` because the first entry becomes the booking's legacy
	`pet` field, and a caller that lists them in a particular order should get that order
	back rather than something sorted behind its back.
	"""
	single = cstr(pet_id).strip()
	listed = _coerce_pet_list(pet_ids)

	if listed and single and single not in listed:
		frappe.throw(
			_("petId {0} is not in petIds ({1}). Send one or the other, or the same pets in both.").format(
				frappe.bold(single), frappe.bold(", ".join(listed))
			)
		)
	if listed and single and len(listed) > 1:
		# They agree as far as it goes, but petId cannot express the rest of the list and a
		# caller sending both plainly means different things by them.
		frappe.throw(
			_("petIds holds {0} pets while petId names only {1}. Send petIds alone.").format(
				len(listed), frappe.bold(single)
			)
		)

	pets = listed or ([single] if single else [])
	if not pets:
		frappe.throw(_("Pet is required. Send petIds (a list) or petId."))
	return pets


def _coerce_pet_list(value) -> list[str]:
	"""Accept a real list, a JSON array string, or a single name; refuse duplicates."""
	if value in (None, ""):
		return []
	if isinstance(value, str):
		text = value.strip()
		if not text:
			return []
		try:
			parsed = frappe.parse_json(text)
		except Exception:
			parsed = text
		value = parsed if isinstance(parsed, (list, tuple)) else [text]
	if not isinstance(value, (list, tuple)):
		frappe.throw(_("petIds must be a list of Pet names."))

	pets: list[str] = []
	for entry in value:
		pet = cstr(entry).strip()
		if not pet:
			continue
		if pet in pets:
			frappe.throw(_("Pet {0} appears twice in petIds.").format(frappe.bold(pet)))
		pets.append(pet)
	if not pets:
		frappe.throw(_("petIds must name at least one pet."))
	return pets


def _assert_reservation_capacity(count: int):
	limit = get_max_pets_per_booking()
	if count > limit:
		frappe.throw(
			_("This booking would hold {0} pets. The limit is {1}. Reserve a second room for the rest.").format(
				count, limit
			)
		)


def _assert_pets_reservable(pets: list[str], guardian_id: str):
	"""Every pet exists, belongs to this guardian, and is not already boarding.

	Each failure names the pet. A list that fails on its fourth entry should say so rather
	than report that "a pet" was wrong.
	"""
	for pet in pets:
		if not frappe.db.exists("Pet", pet):
			frappe.throw(_("Pet {0} was not found.").format(frappe.bold(pet)))
		# Checked before the guardian link and before the already-boarding check, because a
		# dead animal is refused on grounds that no other answer can change - reassigning a
		# guardian or ending another booking would satisfy those and still leave this one.
		#
		# The doc-level hook (mortality.validate_document_not_deceased) also covers the
		# reserve_room insert, but not add_occupant: that appends to a booking that already
		# exists, which is an update, and the hook is is_new() only. This is the shared
		# gate both paths run through, so it is where the rule has to live to cover both.
		assert_pet_not_deceased(pet, action=_("be boarded"))
		if not frappe.db.exists("PetGuardian", {"pet_id": pet, "guardian_id": guardian_id}):
			frappe.throw(
				_("Pet {0} is not linked to Guardian {1}.").format(frappe.bold(pet), frappe.bold(guardian_id))
			)
		active = find_active_boarding_for_pet(pet)
		if active:
			frappe.throw(_pet_already_boarding_message(pet, active))


def _append_reservation_occupants(boarding, pets: list[str], service_room: str, boarding_type: str, joined_at):
	"""One occupant per pet, all sharing the reserved room.

	Sharing is the normal case; a pet gets its own room only through a later per-pet
	transfer. `joined_at` stays empty unless a check-in time was supplied - reserving a
	room is not the animal arriving in it.
	"""
	if not boarding.meta.has_field("occupants"):
		# Pre-migrate: the table is not on the doctype yet. The booking is still created,
		# with its legacy `pet` field, and the backfill patch will give it an occupant.
		return
	for pet in pets:
		boarding.append(
			"occupants",
			{
				"pet": pet,
				"status": "Active",
				"boarding_type": boarding_type,
				"service_room": service_room,
				"joined_at": joined_at,
			},
		)


def _open_reservation_stints(boarding, pets: list[str], service_room: str):
	"""A stint per pet, opened at reservation - the room is held from that moment."""
	if not frappe.db.table_exists("Pet Boarding Room Stint"):
		return
	for pet in pets:
		open_stint(boarding.name, pet, service_room, boarding.reserved_at, reason=_("Room reserved."))


def _reserve_existing_pending_boarding(boarding_name: str, room_id: str, *, checkIn=None, checkOut=None, note=None):
	if not room_id:
		frappe.throw(_("Service Room is required."))
	if not boarding_name:
		frappe.throw(_("Pet Boarding is required."))

	boarding = frappe.get_doc("Pet Boarding", boarding_name)
	boarding.check_permission("write")
	if boarding.docstatus != 0:
		frappe.throw(_("Only draft/open Pet Boarding records can be assigned a room."))
	if boarding.record_status != PENDING_ROOM_STATUS:
		frappe.throw(_("Only Pending Room boarding records can be assigned a room from this action."))
	if boarding.service_room:
		frappe.throw(_("Pet Boarding {0} already has room {1}.").format(frappe.bold(boarding.name), frappe.bold(boarding.service_room)))

	# A Pending Room booking can sit for days before a room frees up - `start_visit_boarding`
	# creates it with no room at all - and the pet can die in that window. Assigning a room
	# is the moment the animal is committed to a kennel, so it is checked here too. The
	# nominal pet as well as the occupants: a Pending Room booking created from a visit has
	# a `pet` and may have no occupant rows yet.
	for pet in {cstr(boarding.pet).strip(), *(cstr(o.pet).strip() for o in active_occupants(boarding))} - {""}:
		assert_pet_not_deceased(pet, action=_("be assigned a room"))

	# Here the booking DOES exist, so its occupant set is contended: this path assigns a
	# room to every occupant, and something else could be adding one. The booking lock
	# spans that read-decide-write; the room lock spans the room's.
	with boarding_lock(boarding.name), _service_room_lock(room_id):
		room = _get_service_room(room_id)
		if room.status != "Active":
			frappe.throw(_("Service Room {0} is inactive.").format(frappe.bold(room_id)))
		existing = get_active_boarding_for_room(room.name)
		if existing:
			frappe.throw(
				_("Service Room {0} already has active boarding {1} ({2}).").format(
					frappe.bold(room.name), frappe.bold(existing.name), frappe.bold(existing.record_status)
				)
			)
		boarding.service_room = room.name
		# The occupants move with the booking. Nothing did this before, and the comment
		# above already claimed it did: a booking that reached Pending Room with its pets
		# already on it got a room while every occupant kept a blank `service_room`. That
		# matters because `room_occupancy`, the hub's per-room count and every write-side
		# availability check read the OCCUPANT's room and not the booking's - so the animals
		# sat in a kennel that no occupant row named, and the room read Available.
		#
		# Active only: a pet that has already departed or been cancelled keeps the room it
		# actually left from, which is what its history and any later audit should show.
		#
		# Overwritten unconditionally rather than blanks-only, because on this path an
		# active occupant cannot already hold a room: the guard above refuses a booking that
		# has one, `add_occupant` refuses a booking with no room, and
		# `transfer_occupant_room` refuses one that does not currently hold a room. Every
		# active occupant here is blank by construction.
		for occupant in active_occupants(boarding):
			occupant.service_room = room.name
		boarding.record_status = "Reserved"
		boarding.status = "Open"
		boarding.workflow_state = "Reserved"
		if checkIn:
			boarding.check_in = _coerce_datetime(checkIn)
		if checkOut:
			boarding.expected_check_out = getdate(checkOut)
		if note:
			boarding.note = cstr(note)
		_ensure_room_stay_billable_item(boarding)
		boarding.run_method("_apply_billable_item_amounts")
		boarding.run_method("_compute_totals")
		boarding.save()
		boarding.add_comment("Comment", _("Room {0} assigned by {1}.").format(room.name, frappe.session.user))
		_log_boarding_event("BOARDING_ROOM_ASSIGNED", boarding=boarding.name, room=room.name, user=frappe.session.user)

	return {
		"success": True,
		"boarding_id": boarding.name,
		"room_id": boarding.service_room,
		"record_status": boarding.record_status,
		"occupancy": "Reserved",
		"customer": boarding.customer,
		"boarding": _serialize_boarding_doc(boarding),
	}


@frappe.whitelist()
@standardize_response
def check_in_boarding(boarding_id, deposit=None):
	"""Check a reserved stay in, optionally taking a deposit (عربون) at the counter.

	`deposit` is optional. Absent, blank or zero means no deposit and no Payment Entry -
	byte-for-byte the behaviour before it existed. A positive amount is written to
	Pet Boarding.deposit and _create_boarding_deposit_payment_entry raises the advance.
	"""
	_require_boarding_write_access()
	if not boarding_id:
		frappe.throw(_("Pet Boarding is required."))

	boarding = frappe.get_doc("Pet Boarding", boarding_id)
	boarding.check_permission("write")

	if boarding.docstatus != 0:
		frappe.throw(_("Only draft/open Pet Boarding records can be checked in."))
	if boarding.record_status in CLOSED_BOARDING_STATUSES:
		frappe.throw(_("Closed or cancelled boarding records cannot be checked in."))
	if boarding.record_status != "Reserved":
		frappe.throw(_("Only Reserved boarding records can be checked in."))

	# A stay is reserved days before the animal arrives, and a pet can die in that window -
	# at home, or on another booking. Check-in is the animal physically entering the
	# kennel, so it is checked again here rather than trusted from reservation time.
	# Every active occupant, not the nominal pet: that is the distinction nezoko slipped
	# through. Refused as a whole; one dead animal stops the check-in rather than the other
	# six arriving and the dead one being quietly skipped.
	#
	# Read through `boarding_pets`, which answers from the occupant rows when there are any
	# and from the legacy `pet` scalar when there are none. Iterating `active_occupants`
	# directly made this gate NO-OP on a booking with no rows: the loop had nothing to walk,
	# so no animal was checked at all and a dead pet would have been admitted in silence.
	# `_start_visit_boarding_atomic` produced exactly those bookings until the fix above,
	# and the room-assignment path already reads both shapes for this same reason. A gate
	# that fails open on an empty list is worse than no gate, because it reads as enforced.
	for pet_name in boarding_pets(boarding):
		assert_pet_not_deceased(pet_name, action=_("be checked in"))

	with _service_room_lock(boarding.service_room):
		existing = get_active_boarding_for_room(boarding.service_room, exclude_name=boarding.name)
		if existing:
			frappe.throw(
				_("Service Room {0} already has active boarding {1} ({2}).").format(
					frappe.bold(boarding.service_room),
					frappe.bold(existing.name),
					frappe.bold(existing.record_status),
				)
			)

		boarding.record_status = "Checked In"
		boarding.status = "Open"
		boarding.workflow_state = "Checked In"
		boarding.check_in = now_datetime()
		# Every animal that has not already been given an arrival time arrives NOW.
		#
		# Nothing stamped this before: a booking reserved without an explicit `checkIn`
		# kept `joined_at` empty through check-in and forever after, so `occupant_nights`
		# fell to its one-night placeholder and a week-long stay billed a single night.
		# Three Checked In bookings on this site are in that state. It also made the
		# published roster lie - a null `joined_at` is documented as "has not arrived yet",
		# which is exactly wrong for an animal standing in the kennel.
		#
		# Only blanks are filled: a reservation that supplied its own arrival time keeps it.
		#
		# The room is reconciled on the same pass, blanks only. Check-in does NOT assign a
		# room - it refuses anything that is not already Reserved, and a Reserved booking
		# has held one since it was reserved - so this is a net, not an assignment. It
		# catches an occupant that reached check-in with no room on it: a pre-Stage-2
		# backfilled row, or a booking that was roomed before the assignment path above
		# learned to stamp its occupants. Check-in is the moment the roster and the per-room
		# count start being read in earnest, so it is the right place to catch one.
		#
		# Blanks only, unlike the assignment path: a row that already names a room keeps it.
		# After a transfer that room is deliberately not the booking's, and overwriting
		# would drag a transferred pet back onto the booking's room on paper while the
		# animal sleeps elsewhere.
		for occupant in active_occupants(boarding):
			if not occupant.get("joined_at"):
				occupant.joined_at = boarding.check_in
			if not cstr(occupant.get("service_room")).strip():
				occupant.service_room = boarding.service_room
		boarding.customer = boarding.customer or get_or_create_customer_from_guardian(boarding.guardian)
		# Validated before it is written, so a rejected amount leaves the stay untouched
		# rather than checked in with a bad figure on it. Only a supplied value is honoured;
		# a deposit already set on the record (from Desk) keeps working as before.
		if deposit is not None and cstr(deposit).strip() != "":
			boarding.deposit = _validate_deposit_amount(boarding, deposit)
		deposit_payment_entry = _create_boarding_deposit_payment_entry(boarding)
		if deposit_payment_entry:
			boarding.deposit_payment_entry = deposit_payment_entry.name
		boarding.save()
		boarding.add_comment("Comment", _("Boarding checked in by {0}.").format(frappe.session.user))
		_log_boarding_event("BOARDING_CHECKED_IN", boarding=boarding.name, room=boarding.service_room, user=frappe.session.user)

	return {
		"success": True,
		"boarding_id": boarding.name,
		"room_id": boarding.service_room,
		"record_status": boarding.record_status,
		"occupancy": "Occupied",
		"boarding": _serialize_boarding_doc(boarding),
		"deposit": flt(boarding.get("deposit")),
		"deposit_payment_entry": boarding.get("deposit_payment_entry"),
	}


@frappe.whitelist()
@standardize_response
def check_out_boarding(boarding_id):
	_require_boarding_invoice_access()
	if not boarding_id:
		frappe.throw(_("Pet Boarding is required."))

	boarding = frappe.get_doc("Pet Boarding", boarding_id)
	boarding.check_permission("write")

	if boarding.record_status == "Checked Out" or boarding.sales_invoice:
		frappe.throw(_("Pet Boarding {0} is already checked out.").format(frappe.bold(boarding.name)))
	if boarding.record_status == "Cancelled":
		frappe.throw(_("Cancelled boarding records cannot be checked out."))
	if boarding.record_status != "Checked In":
		frappe.throw(_("Only Checked In boarding records can be checked out."))
	if boarding.docstatus != 0:
		frappe.throw(_("Only open Pet Boarding records can be checked out."))

	with _service_room_lock(boarding.service_room):
		boarding.check_out = now_datetime()
		boarding.customer = get_or_create_customer_from_guardian(boarding.guardian)

		# Close every pet still here, BEFORE the room-stay rows are reconciled, so each
		# one prices from its own joined_at to this check-out rather than to `now`. A pet
		# that departed earlier already carries its own departed_at and is untouched.
		#
		# This is also what actually frees the room. `room_occupancy` reads OCCUPANT
		# status, not booking status, so an occupant left Active after check-out keeps the
		# kennel occupied forever - four bookings on this site are in that state right now
		# because nothing at runtime closed them; only the backfill patch ever did.
		for closed in close_all_active_occupants(boarding, when=boarding.check_out):
			close_open_stint(boarding.name, cstr(closed.pet).strip(), boarding.check_out)

		boarding.run_method("_compute_stay_days")
		_ensure_room_stay_billable_item(boarding, add_if_missing=False)
		boarding.run_method("_apply_billable_item_amounts")
		boarding.run_method("_compute_totals")

		invoice_items = _build_sales_invoice_items(boarding)
		invoice_total = sum(flt(row.get("qty")) * flt(row.get("rate")) for row in invoice_items)
		invoice = None
		guardian_field = None

		if invoice_items and invoice_total > 0:
			# Appends to the customer's open Draft for this branch when one exists - an
			# eight-day stay and a same-week clinic visit land on one invoice.
			result = get_or_create_open_invoice(
				customer=boarding.customer,
				items=invoice_items,
				source_doctype="Pet Boarding",
				source_name=boarding.name,
				# The stay's own stamped branch, written at insert by
				# utils.branch.stamp_boarding_branch from Pet Boarding Settings. Read from
				# the record and never re-derived here, so a later change to the setting
				# cannot re-attribute a stay that already happened. Stays created before
				# that stamp was wired carry no branch and still fall through to the
				# acting user's branch in invoice_reuse.resolve_branch - they are not
				# retroactively re-attributed.
				branch=boarding.get("branch"),
				# Authorise-then-elevate. What is established before this line:
				# _require_boarding_invoice_access proved the caller holds boarding write
				# access AND Sales Invoice create; boarding.check_permission("write")
				# proved they may write this specific stay; and the four record_status /
				# docstatus guards above proved it is an open, checked-in stay that is not
				# already invoiced.
				#
				# The elevation cannot be steered. `branch` is not a parameter of this
				# endpoint and cannot be influenced by the request - it is either the
				# value stamped on the record from a single site setting, or blank, in
				# which case resolve_branch returns the caller's own branch and the
				# elevation is a no-op. There is exactly one cross-branch value it can
				# produce, and it is the same for every user.
				#
				# Without it the boarding facility's revenue posts to whichever clinic
				# processed the checkout, which reaches the ledger and not just a report.
				branch_authorised=True,
				posting_date=getdate(boarding.check_out),
				due_date=getdate(boarding.check_out),
				selling_price_list=get_veterinary_selling_price_list(),
				ignore_pricing_rule=1,
				remarks=_("Pet Boarding {0} checkout.").format(boarding.name),
				guardian=boarding.guardian,
			)
			invoice = result.invoice
			guardian_field = result.guardian_reference_field
			invoice.add_comment(
				"Comment",
				_("Boarding checkout invoice {0} from Pet Boarding {1} by {2}.").format(
					_("created") if result.created else _("extended"),
					boarding.name, frappe.session.user
				),
			)

		deposit_allocation = _allocate_boarding_deposit(boarding, invoice)

		for row in boarding.billable_items or []:
			# Included must survive check-out. Flipping it to Billed would claim the
			# customer was charged for a medication the medical rate absorbed, and would
			# destroy the only record of what that rate actually covered.
			if row.status not in NON_INVOICED_STATUSES:
				row.status = "Billed"

		boarding.sales_invoice = invoice.name if invoice else None
		boarding.billing_status = "Invoiced" if invoice else "Unbilled"
		boarding.record_status = "Checked Out"
		boarding.status = "Closed"
		boarding.workflow_state = "Closed"
		boarding.save()
		if invoice:
			boarding.add_comment(
				"Comment",
				_("Boarding checked out and invoiced with Sales Invoice {0} by {1}.").format(
					invoice.name, frappe.session.user
				),
			)
		else:
			boarding.add_comment(
				"Comment",
				_("Boarding checked out with no billable charges by {0}.").format(frappe.session.user),
			)
		_log_boarding_event(
			"BOARDING_CHECKED_OUT",
			boarding=boarding.name,
			sales_invoice=invoice.name if invoice else None,
			user=frappe.session.user,
		)

		if invoice and boarding.meta.is_submittable and boarding.docstatus == 0:
			boarding.submit()

	return {
		"success": True,
		"boarding_id": boarding.name,
		"room_id": boarding.service_room,
		"record_status": boarding.record_status,
		"status": boarding.status,
		"occupancy": "Available",
		"sales_invoice": invoice.name if invoice else None,
		"customer": boarding.customer,
		"guardian_reference_field": guardian_field,
		"total_cost": boarding.total_cost,
		# An ESTIMATE of what will be due (total_cost - deposit), not a ledger figure. The
		# invoice is shared under reuse, so its outstanding is the customer's, not this
		# stay's. The keys below are the ones that are actually true.
		"balance": boarding.balance,
		"invoice_grand_total": flt(invoice.grand_total) if invoice else 0.0,
		"deposit": flt(boarding.get("deposit")),
		"deposit_allocated": flt(deposit_allocation.allocated) if deposit_allocation else 0.0,
		"deposit_unallocated_remainder": (
			flt(deposit_allocation.unallocated_remainder) if deposit_allocation else 0.0
		),
		"deposit_payment_entry": boarding.get("deposit_payment_entry"),
		"boarding": _serialize_boarding_doc(boarding),
	}


def _surface_deposit_credit_on_cancel(boarding):
	"""A cancelled stay keeps its deposit as customer credit. Say so, in both places.

	The owner's rule is no refund path: the money stays on the customer's account as an
	unallocated advance and is consumed by their next invoice. That is defensible, but it
	must not be silent - an unallocated Payment Entry with a cancelled stay behind it is
	invisible unless somebody thinks to look at the customer ledger.

	Writes a comment on the stay and on the Payment Entry itself, and returns the figure
	so the cancel response can carry it. Deliberately does NOT cancel or amend the entry:
	the cash was genuinely received and the ledger should keep saying so.
	"""
	pe_name = cstr(boarding.get("deposit_payment_entry")).strip()
	if not pe_name:
		return None
	pe = frappe.db.get_value(
		"Payment Entry", pe_name, ["name", "docstatus", "party", "unallocated_amount"], as_dict=True
	)
	if not pe or cint(pe.docstatus) != 1:
		return None
	remaining = flt(pe.unallocated_amount)
	if remaining <= 0:
		return None

	note = _(
		"Pet Boarding {0} was cancelled. Deposit {1} remains as unallocated credit on {2} "
		"and will be applied to their next invoice. No refund was made."
	).format(
		boarding.name,
		frappe.format_value(remaining, {"fieldtype": "Currency"}),
		pe.party,
	)
	boarding.add_comment("Comment", note)
	frappe.get_doc("Payment Entry", pe_name).add_comment("Comment", note)
	_log_boarding_event(
		"BOARDING_DEPOSIT_CREDIT_RETAINED",
		boarding=boarding.name,
		payment_entry=pe_name,
		customer=pe.party,
		unallocated_amount=remaining,
	)
	return frappe._dict(payment_entry=pe_name, customer=pe.party, unallocated_amount=remaining)


def _allocate_boarding_deposit(boarding, invoice):
	"""Put the deposit against the invoice this checkout produced.

	Written to `Sales Invoice.advances`, not to `Payment Entry.references`. That is not a
	shortcut - it is the only mechanism that works here. The invoice is a DRAFT (invoice
	reuse keeps one open draft per customer per branch), and ERPNext's
	reconcile_against_document reconciles against posted vouchers: a draft has no GL and
	no outstanding to reconcile with. An advance row on the draft is exactly ERPNext's
	"deposit taken before invoicing" form, and on submit it becomes the Payment Entry
	Reference, with outstanding computed net of it.

	The invoice is still raised in full - nothing is deducted from the total. The advance
	records that the customer paid X on the day they paid it, which is the trail the owner
	asked for.

	Allocates only what fits. A deposit larger than the bill allocates up to the invoice
	total and the remainder stays unallocated on the customer, available to the next
	invoice. Idempotent: a Payment Entry already present on this invoice is left alone.
	"""
	pe_name = cstr(boarding.get("deposit_payment_entry")).strip()
	if not pe_name or not invoice:
		return None

	pe = frappe.db.get_value(
		"Payment Entry", pe_name,
		["name", "docstatus", "party", "unallocated_amount"], as_dict=True,
	)
	if not pe or cint(pe.docstatus) != 1:
		# Draft or cancelled: nothing to allocate, and re-raising it is not this path's job.
		return None
	if cstr(pe.party) != cstr(invoice.customer):
		# Would be somebody else's money. Refuse quietly rather than misapply it.
		frappe.log_error(
			f"Boarding {boarding.name}: deposit {pe_name} belongs to {pe.party}, "
			f"invoice {invoice.name} is for {invoice.customer}. Not allocated.",
			"Boarding deposit allocation",
		)
		return None

	available = flt(pe.unallocated_amount)
	if available <= 0:
		return None

	for row in invoice.get("advances") or []:
		if row.reference_type == "Payment Entry" and cstr(row.reference_name) == pe_name:
			return None

	already = sum(flt(row.allocated_amount) for row in invoice.get("advances") or [])
	room = flt(invoice.grand_total) - already
	if room <= 0:
		return None

	allocated = min(available, room)
	invoice.append("advances", {
		"reference_type": "Payment Entry",
		"reference_name": pe_name,
		"advance_amount": available,
		"allocated_amount": allocated,
		"remarks": _("Boarding deposit for Pet Boarding {0}.").format(boarding.name),
	})
	invoice.flags.from_custom_flow = True
	invoice.flags.ignore_permissions = True
	invoice.save(ignore_permissions=True)
	invoice.add_comment(
		"Comment",
		_("Boarding deposit {0} allocated ({1}) from Pet Boarding {2}.").format(
			pe_name, frappe.format_value(allocated, {"fieldtype": "Currency"}), boarding.name
		),
	)
	_log_boarding_event(
		"BOARDING_DEPOSIT_ALLOCATED",
		boarding=boarding.name,
		payment_entry=pe_name,
		sales_invoice=invoice.name,
		available=available,
		allocated=allocated,
		remainder=available - allocated,
	)
	return frappe._dict(
		payment_entry=pe_name,
		available=available,
		allocated=allocated,
		unallocated_remainder=flt(available - allocated),
	)


def _expected_stay_cost(boarding) -> float:
	"""Roughly what this stay is expected to cost, for sanity-checking a deposit.

	Sums ACROSS OCCUPANTS, each at its own catalogue rate. It used to resolve one settings
	item and multiply by nights, which priced a seven-pet booking as one and would have let
	a deposit seven times too large through the ceiling below without complaint.

	Read-only: it touches no billable row. Returns 0.0 when it cannot work anything out,
	which callers must read as "no opinion" rather than as "free" - so a catalogue the
	owner has not finished setting up relaxes the deposit check rather than blocking the
	counter.
	"""
	occupants = billable_occupants(boarding) or [_synthetic_occupant(boarding)]
	expected_out = boarding.get("expected_check_out")

	total = 0.0
	for occupant in occupants:
		pet = cstr(occupant.get("pet")).strip()
		if not pet:
			continue
		try:
			rate = resolve_boarding_rate(
				pet, cstr(occupant.get("boarding_type")).strip() or boarding.boarding_type
			).rate
		except Exception:
			# One unpriceable pet must not silence the whole estimate.
			continue

		nights = 1
		if expected_out:
			start = get_datetime(occupant.get("joined_at") or boarding.get("check_in") or now_datetime())
			hours = (get_datetime(expected_out) - start).total_seconds() / 3600
			if hours > 0:
				nights = max(ceil(hours / 24), 1)
		total += flt(rate) * nights

	return total


def _validate_deposit_amount(boarding, amount: float) -> float:
	"""Numeric and not negative. Returns the amount.

	There is deliberately NO upper bound. One used to live here - a deposit could not
	exceed twice `_expected_stay_cost` - and it was removed because it refused honest
	money far more often than it caught a typo. The estimate it compared against reads
	`expected_check_out`, which on a Reserved booking that has not been checked in yet
	resolves to a single night. A guardian booking five nights and handing over a real
	75,000 was measured against a 25,000 estimate and turned away at the counter.

	A ceiling that fires on the common legitimate case is worse than no ceiling: staff
	learn to work around it, and the amount it was protecting is entered anyway by some
	other route. Catching a mistyped zero is now the input's job - the check-in dialog
	groups thousands as the operator types - which is where a keying slip is visible at
	the moment it is made, rather than a refusal several fields later.

	`boarding` is unused and kept only so the call site does not have to change shape;
	it is the natural argument if a per-stay deposit rule is ever wanted again.
	"""
	amount = flt(amount)
	if amount < 0:
		frappe.throw(_("Deposit cannot be negative."))
	if amount == 0:
		return 0.0
	return amount


def _create_boarding_deposit_payment_entry(boarding):
	deposit_amount = flt(boarding.deposit)
	if deposit_amount <= 0:
		return None

	existing = boarding.get("deposit_payment_entry")
	if existing and frappe.db.exists("Payment Entry", existing):
		existing_doc = frappe.get_doc("Payment Entry", existing)
		if existing_doc.docstatus != 2:
			return existing_doc

	customer = boarding.customer or get_or_create_customer_from_guardian(boarding.guardian)
	if not customer:
		frappe.throw(_("Customer is required to capture boarding deposit."))

	company = _get_default_company()
	if not company:
		frappe.throw(_("Default company is required to capture boarding deposit."))

	mode_of_payment = _default_cash_mode_of_payment()
	paid_to = _resolve_boarding_deposit_account(company, mode_of_payment)
	party_account = get_party_account("Customer", customer, company)
	_validate_account(party_account, company=company, label="Customer Receivable Account")
	posting_date = getdate(boarding.check_in or nowdate())

	pe = frappe.new_doc("Payment Entry")
	pe.payment_type = "Receive"
	pe.company = company
	pe.posting_date = posting_date
	pe.mode_of_payment = mode_of_payment
	pe.party_type = "Customer"
	pe.party = customer
	pe.paid_from = party_account
	pe.paid_to = paid_to
	pe.paid_amount = deposit_amount
	pe.received_amount = deposit_amount
	pe.reference_no = boarding.name
	pe.reference_date = posting_date
	pe.remarks = _("Boarding deposit for Pet Boarding {0}.").format(boarding.name)
	pe.flags.ignore_permissions = True
	pe.insert(ignore_permissions=True)
	pe.submit()
	pe.add_comment(
		"Comment",
		_("Boarding deposit captured for Pet Boarding {0} by {1}.").format(boarding.name, frappe.session.user),
	)
	return pe


def _resolve_boarding_deposit_account(company: str, mode_of_payment: str | None) -> str:
	"""Where a deposit's cash lands: one site-wide treasury account.

	Deliberate and temporary. The app already has the better answer -
	cashier.resolve_session_cashier_till resolves the acting user's own POS Profile and
	till, and refuses to guess, which is what the driver cash handover uses. Boarding does
	NOT use it, because no user on this site holds a POS Profile: wiring deposits to the
	till would make every check-in throw "has no cashier profile" on the first day.

	What it costs, plainly: the deposit records that cash was received, but not WHO
	received it. There is no till attribution and nothing for a cashier settlement to
	reconcile against, so a shortfall at the end of a shift cannot be traced to a person.
	Assigning POS Profiles is the prerequisite for fixing it; once every counter user has
	one, this should become resolve_session_cashier_till(company).cash_account and the
	setting below becomes the fallback rather than the rule.
	"""
	if _is_cash_mode(mode_of_payment):
		settings = _get_settings_doc()
		account = settings.get("treasury_cash_account")
		_validate_account(account, company=company, account_type="Cash", label="Treasury Cash Account")
		return account

	if mode_of_payment and not frappe.db.exists("Mode of Payment", mode_of_payment):
		frappe.throw(_("Mode of Payment {0} does not exist.").format(frappe.bold(mode_of_payment)))

	account = frappe.db.get_value(
		"Mode of Payment Account",
		{"parent": mode_of_payment, "company": company},
		"default_account",
	)
	_validate_account(account, company=company, label="Mode of Payment Account")
	return account


@frappe.whitelist()
@standardize_response
def sync_billable_items(boarding_id=None, billable_items=None, name=None, boardingId=None, billableItems=None):
	_require_boarding_write_access()
	boarding_name = cstr(boarding_id or boardingId or name).strip()
	if not boarding_name:
		frappe.throw(_("Pet Boarding is required."))

	incoming_rows = _parse_billable_items_payload(
		billable_items if billable_items is not None else billableItems
	)

	boarding = frappe.get_doc("Pet Boarding", boarding_name)
	boarding.check_permission("write")

	if boarding.docstatus != 0:
		frappe.throw(_("Billable items can only be synced on open Pet Boarding records."))
	if boarding.sales_invoice:
		frappe.throw(_("Pet Boarding {0} is already invoiced.").format(frappe.bold(boarding.name)))

	existing_by_name = {row.name: row for row in boarding.billable_items or [] if row.name}
	existing_by_order_id = {}
	for row in boarding.billable_items or []:
		order_id = cstr(row.get("order_id")).strip()
		if order_id and order_id not in existing_by_order_id:
			existing_by_order_id[order_id] = row

	claimed_names = set()
	synced_rows = []

	for idx, raw_row in enumerate(incoming_rows, start=1):
		synced_rows.append(
			_sync_billable_item_row(boarding, raw_row, idx, existing_by_name, existing_by_order_id, claimed_names)
		)

	# Still a whole-table set, and deliberately so: a row the payload does not name is
	# a row the operator deleted, and Frappe's update_child_table is what deletes it.
	# What changed is WHAT is in the list - the existing child objects themselves,
	# carrying every column they were loaded with, instead of dicts rebuilt from the
	# payload. Re-seating an existing Document through set() leaves it untouched, so
	# omission still deletes a ROW while no longer emptying a FIELD.
	boarding.set("billable_items", synced_rows)
	# set() keeps each re-seated row's stored idx, so payload order is imposed here.
	# The client orders the editor's lines and that order is the record's.
	for position, row in enumerate(boarding.billable_items or [], start=1):
		row.idx = position
	boarding.run_method("_apply_billable_item_amounts")
	boarding.run_method("_compute_totals")
	boarding.save()
	boarding.add_comment("Comment", _("Boarding billable items updated by {0}.").format(frappe.session.user))
	_log_boarding_event("BOARDING_BILLABLES_SYNCED", boarding=boarding.name, user=frappe.session.user)

	return {
		"success": True,
		"boarding_id": boarding.name,
		"total_cost": boarding.total_cost,
		"balance": boarding.balance,
		"billable_items": [_serialize_billable_item(row) for row in boarding.billable_items or []],
		"boarding": _serialize_boarding_doc(boarding),
	}


@frappe.whitelist(methods=["POST"])
@standardize_response
def record_medication_given(boarding=None, plan_item=None, note=None, given_at=None, data=None, **kwargs):
	payload = _coerce_payload(data, kwargs)
	boarding_name = cstr(payload.get("boarding") or payload.get("boarding_id") or boarding).strip()
	plan_item_name = cstr(payload.get("plan_item") or payload.get("name") or plan_item).strip()
	given_note = cstr(payload.get("note") if "note" in payload else note).strip()
	given_at_value = payload.get("given_at") if "given_at" in payload else given_at

	if not boarding_name:
		return _boarding_validation_error(_("Pet Boarding is required."))
	if not plan_item_name:
		return _boarding_validation_error(_("Plan item is required."))

	_require_boarding_write_access()
	require_doctype_permission("Pet Care Plan Item", "write")

	if not frappe.db.exists("Pet Boarding", boarding_name):
		return _boarding_validation_error(_("Pet Boarding {0} was not found.").format(frappe.bold(boarding_name)))
	if not frappe.db.exists("Pet Care Plan Item", plan_item_name):
		return _boarding_validation_error(_("Plan item {0} was not found.").format(frappe.bold(plan_item_name)))

	boarding_doc = frappe.get_doc("Pet Boarding", boarding_name)
	boarding_doc.check_permission("write")
	if boarding_doc.record_status != "Checked In" or boarding_doc.docstatus != 0:
		return _boarding_validation_error(_("Only checked-in Pet Boarding records can record medication as given."))

	plan = frappe.get_doc("Pet Care Plan Item", plan_item_name)
	validation_error = _validate_boarding_medication_plan_item(boarding_doc, plan)
	if validation_error:
		return validation_error

	if cstr(plan.get("status")).strip() == "Done":
		return {
			"success": True,
			"boarding_id": boarding_doc.name,
			"plan_item": _serialize_plan_item_for_boarding(plan),
			"idempotent": True,
		}
	if cstr(plan.get("status")).strip() in {"Cancelled", "Converted To Visit"}:
		return _boarding_validation_error(_("This medication plan item can no longer be marked as given."))

	try:
		completed_at = _coerce_given_at(given_at_value)
	except Exception:
		return _boarding_validation_error(_("given_at must be a valid datetime."))
	plan.status = "Done"
	plan.completed_on = completed_at
	plan.completed_by = frappe.session.user
	if given_note:
		plan.completion_note = given_note
	plan.save(ignore_permissions=True)
	plan.add_comment(
		"Comment",
		_("Medication marked given from Pet Boarding {0} by {1}.").format(boarding_doc.name, frappe.session.user),
	)
	_log_boarding_event(
		"BOARDING_MEDICATION_GIVEN",
		boarding=boarding_doc.name,
		plan_item=plan.name,
		care_episode=plan.get("care_episode"),
		user=frappe.session.user,
	)

	return {
		"success": True,
		"boarding_id": boarding_doc.name,
		"plan_item": _serialize_plan_item_for_boarding(plan),
		"idempotent": False,
	}


@frappe.whitelist()
@standardize_response
def create_order(
	boarding_id=None,
	kind=None,
	template_id=None,
	item_code=None,
	care_service_id=None,
	priority=None,
	note=None,
	pet=None,
	dose_option=None,
	warehouse=None,
	dosage=None,
	frequency=None,
	duration_days=None,
	scheduled_datetime=None,
	provider=None,
):
	"""Raise a single lab / imaging / care-service order for a checked-in boarding.

	The visit order flow is visit-scoped; this is boarding's own entry point. The frontend
	sends one call per selected order. The created order is linked back to the boarding via
	``source_doctype``/``source_name`` and a matching ``billable_items`` row is appended so
	it settles through the existing checkout invoice path.

	``pet`` is REQUIRED and names the animal the order is for. A stay holds one pet today,
	so it can only be that pet - but see _resolve_order_pet for why it is not defaulted.

	``provider`` names WHO PERFORMS the work, as a Healthcare Practitioner docname. It is
	optional and absence stays valid; see _coerce_provider for why it is never defaulted to
	the caller. It lands on the billable row for every kind, and additionally on the order
	document itself wherever that document has somewhere to put it - which today is
	PetCareService only. Lab and Imaging carry a `doctor`, not a `provider`, so a boarding
	lab or imaging order records its assignee on the billing row alone; that is the row the
	orders panel reads, so the assignment is visible either way.
	"""
	_require_boarding_write_access()

	boarding_name = cstr(boarding_id).strip()
	if not boarding_name:
		frappe.throw(_("Pet Boarding is required."))

	kind = cstr(kind).strip().lower()
	if kind not in ORDER_KINDS:
		frappe.throw(
			_("Invalid order kind {0}. Expected one of: {1}.").format(
				frappe.bold(kind or ""), ", ".join(ORDER_KINDS)
			)
		)

	template_id = cstr(template_id).strip()
	care_service_id = cstr(care_service_id).strip()
	item_code = cstr(item_code).strip()
	if not template_id:
		frappe.throw(_("template_id is required."))

	priority = _normalize_order_priority(priority)
	note = cstr(note).strip() or None
	# Parsed once, here, so the order document and the billable row cannot end up holding
	# different moments for the same order.
	scheduled = _coerce_scheduled_datetime(scheduled_datetime)
	# Resolved BEFORE anything is written, so an unresolvable practitioner refuses the call
	# instead of leaving a created Lab behind and failing on the boarding save.
	order_provider = _coerce_provider(provider)
	care_service = care_service_id or template_id

	boarding = frappe.get_doc("Pet Boarding", boarding_name)
	boarding.check_permission("write")
	_assert_boarding_orderable(boarding)
	order_pet = _resolve_order_pet(boarding, pet)

	if kind == "medication":
		return _create_boarding_medication_order(
			boarding,
			pet=order_pet,
			medication=template_id,
			item_code=item_code,
			note=note,
			dose_option=dose_option,
			warehouse=warehouse,
			dosage=dosage,
			frequency=frequency,
			duration_days=duration_days,
			scheduled_datetime=scheduled,
			provider=order_provider,
		)

	order_doctype = ORDER_KIND_DOCTYPE[kind]
	require_doctype_permission(order_doctype, "create")

	existing = _find_recent_duplicate_order(order_doctype, boarding.name, kind, care_service, order_pet)
	if existing:
		_log_boarding_event(
			"BOARDING_ORDER_DUPLICATE",
			boarding=boarding.name,
			kind=kind,
			pet=order_pet,
			order=existing.name,
			user=frappe.session.user,
		)
		return _boarding_order_response(boarding, kind, existing, reused=True)

	order = _create_boarding_order_doc(
		boarding, kind, pet=order_pet, care_service=care_service, item_code=item_code, priority=priority, note=note,
		scheduled_datetime=scheduled, provider=order_provider,
	)
	_append_boarding_order_billable(
		boarding, kind, order, care_service=care_service, item_code=item_code, note=note,
		scheduled_datetime=scheduled, provider=order_provider,
	)
	_log_boarding_event(
		"BOARDING_ORDER_CREATED",
		boarding=boarding.name,
		kind=kind,
		pet=order_pet,
		order=order.name,
		user=frappe.session.user,
	)

	return _boarding_order_response(boarding, kind, order)


@frappe.whitelist(methods=["POST"])
@standardize_response
def dispense_medication(boarding=None, item_id=None, qty=None, note=None, data=None, **kwargs):
	payload = _coerce_payload(data, kwargs)
	boarding_name = cstr(payload.get("boarding") or payload.get("boarding_id") or boarding).strip()
	row_id = cstr(payload.get("item_id") or payload.get("row_name") or item_id).strip()
	dispense_note = cstr(payload.get("note") if "note" in payload else note).strip()

	if not boarding_name:
		return _boarding_validation_error(_("Pet Boarding is required."))
	if not row_id:
		return _boarding_validation_error(_("Medication billable row is required."))

	_require_boarding_write_access()
	if not frappe.db.exists("Pet Boarding", boarding_name):
		return _boarding_validation_error(_("Pet Boarding {0} was not found.").format(frappe.bold(boarding_name)))

	boarding_doc = frappe.get_doc("Pet Boarding", boarding_name)
	boarding_doc.check_permission("write")
	_assert_boarding_orderable(boarding_doc)
	_require_billable_dispense_fields()

	row = _find_boarding_billable_row(boarding_doc, row_id)
	if not row:
		return _boarding_validation_error(_("Medication billable row was not found."))
	if cstr(row.get("item_type")).strip() != "Medication":
		return _boarding_validation_error(_("Only medication billable rows can be dispensed from this action."))
	if cstr(row.get("status")).strip() in {"Cancelled", "Billed"}:
		return _boarding_validation_error(_("Medication billable row cannot be dispensed from its current status."))

	current_dispense_status = cstr(row.get("dispense_status")).strip()
	if current_dispense_status == "Dispensed":
		return {
			"success": True,
			"boarding_id": boarding_doc.name,
			"item": _serialize_billable_item(row),
			"idempotent": True,
		}
	if current_dispense_status in {"Cancelled", "Returned"}:
		return _boarding_validation_error(_("Medication billable row cannot be dispensed from its current status."))

	dispense_qty = flt(qty if qty is not None else payload.get("qty"))
	if dispense_qty <= 0:
		dispense_qty = max(flt(row.get("qty")) - flt(row.get("dispensed_qty")), 0)
	if dispense_qty <= 0:
		return _boarding_validation_error(_("Dispense Qty must be greater than zero."))
	if flt(row.get("dispensed_qty")) + dispense_qty > flt(row.get("qty")):
		return _boarding_validation_error(_("Dispensed Qty cannot exceed requested Qty."))

	# Status decides billing, not whether the goods move. An Included row - a
	# medication the medical boarding rate absorbs - is dispensed and issued like
	# any other; only Cancelled stops the vial leaving the shelf.
	medication_name = cstr(row.get("linked_name")).strip() if cstr(row.get("linked_doctype")).strip() == "Medication" else ""
	issued = None
	try:
		if cstr(row.get("status")).strip() not in NON_ISSUED_STATUSES:
			if is_opted_in(medication_name):
				assert_row_can_record_stock(row.meta, medication_name)
			issued = issue_for_dispense(
				medication=medication_name,
				medication_item=row.get("item_code"),
				# Boarding orders are one dose per row, so `qty` is a dose count in
				# the same sense as the visit's - the conversion to stock units
				# happens once, inside issue_for_dispense, never here.
				dose_count=dispense_qty,
				dose_option=row.get("dose_option"),
				row_warehouse=row.get("warehouse"),
				reference=_("Pet Boarding {0} row {1}").format(boarding_doc.name, row.name),
				label=medication_name or row.get("item_name") or row.get("item_code"),
			)
		if issued:
			row.warehouse = issued["warehouse"]
			row.stock_issued_qty = flt(row.get("stock_issued_qty")) + flt(issued["qty"])
			row.stock_entry = issued["stock_entry"]

		row.dispensed_qty = flt(row.get("dispensed_qty")) + dispense_qty
		row.dispensed_by = frappe.session.user
		row.dispensed_at = now_datetime()
		row.dispense_status = "Dispensed" if flt(row.dispensed_qty) >= flt(row.get("qty")) else "Partially Dispensed"
		if dispense_note:
			row.note = _append_note(row.get("note"), dispense_note)

		boarding_doc.save(ignore_permissions=True)
	except Exception:
		# standardize_response turns a throw into an error response WITHOUT rolling
		# back, so a submitted Stock Entry plus a failed save would otherwise commit
		# the movement and lose its record. The visit path already does this.
		frappe.db.rollback()
		raise

	boarding_doc.add_comment(
		"Comment",
		_("Medication billable row {0} dispensed by {1}.").format(row.name, frappe.session.user),
	)
	_log_boarding_event(
		"BOARDING_MEDICATION_DISPENSED",
		boarding=boarding_doc.name,
		item=row.name,
		qty=dispense_qty,
		stock_entry=issued["stock_entry"] if issued else None,
		stock_qty=issued["qty"] if issued else 0,
		user=frappe.session.user,
	)

	return {
		"success": True,
		"boarding_id": boarding_doc.name,
		"item": _serialize_billable_item(row),
		"boarding": _serialize_boarding_doc(boarding_doc),
		"idempotent": False,
	}


@frappe.whitelist(methods=["POST"])
@standardize_response
def add_occupant(boarding=None, pet=None, boarding_type=None, service_room=None, note=None, data=None, **kwargs):
	"""A pet joins a booking that already exists.

	Until now the only way onto a booking was `reserve_room`, at creation - every pet had
	to be named before the first one arrived. A guardian bringing a second animal on day
	three had no path but a second booking, a second room and a second invoice.

	The pet pays from the day it arrives, not from the day the booking opened: `joined_at`
	is stamped HERE, at the server, and the room-stay reconciliation already prices each
	occupant from its own `joined_at`. A pet joining on day six of a ten-day stay owes one
	night on the day it arrives, not six.

	The other animals' rows are reconciled too, and that is not a side effect of the join:
	reconciliation refreshes every running occupant's nights to the present, which is what
	departure and check-out already do. Their RATES are never touched - a row that has a
	price keeps it - so the join adds a line and moves nobody's money.

	`joined_at` is stamped only when the booking is already Checked In. Joining a Reserved
	booking leaves it empty, exactly as reserving does - the room is held, the animal has
	not arrived - and check-in fills it for everyone at once.

	Capacity is counted against ACTIVE occupants, so a booking that held seven and saw one
	go home has room for another. Counting rows would make a departure permanently consume
	a slot.
	"""
	payload = _coerce_payload(data, kwargs)
	boarding_name = cstr(payload.get("boarding") or payload.get("boarding_id") or boarding).strip()
	pet_name = cstr(payload.get("pet") or payload.get("pet_id") or pet).strip()
	requested_type = cstr(payload.get("boarding_type") if "boarding_type" in payload else boarding_type).strip()
	requested_room = cstr(
		payload.get("service_room") or payload.get("room_id") or service_room
	).strip()
	join_note = cstr(payload.get("note") if "note" in payload else note).strip()

	if not boarding_name:
		return _boarding_validation_error(_("Pet Boarding is required."))
	if not pet_name:
		return _boarding_validation_error(_("Pet is required. Send the pet that is joining."))

	_require_boarding_write_access()
	if not frappe.db.exists("Pet Boarding", boarding_name):
		return _boarding_validation_error(_("Pet Boarding {0} was not found.").format(frappe.bold(boarding_name)))

	boarding_doc = frappe.get_doc("Pet Boarding", boarding_name)
	boarding_doc.check_permission("write")

	if boarding_doc.docstatus != 0:
		return _boarding_validation_error(_("Only open Pet Boarding records can take another pet."))
	if boarding_doc.sales_invoice:
		return _boarding_validation_error(
			_("Pet Boarding {0} is already invoiced.").format(frappe.bold(boarding_doc.name))
		)
	if boarding_doc.record_status == PENDING_ROOM_STATUS:
		return _boarding_validation_error(
			_("Pet Boarding {0} has no room yet. Assign a room before adding another pet.").format(
				frappe.bold(boarding_doc.name)
			)
		)
	if boarding_doc.record_status not in ("Reserved", "Checked In"):
		return _boarding_validation_error(
			_("Only Reserved or Checked In boarding records can take another pet.")
		)
	if not boarding_doc.meta.has_field("occupants"):
		return _boarding_validation_error(_("Run migrations before adding a pet to a booking."))

	target_room = requested_room or cstr(boarding_doc.service_room).strip()
	if not target_room:
		return _boarding_validation_error(_("Service Room is required."))
	room = _get_service_room(target_room)
	if room.status != "Active":
		return _boarding_validation_error(_("Service Room {0} is inactive.").format(frappe.bold(room.name)))
	if requested_room and requested_room != cstr(boarding_doc.service_room).strip():
		# A different room is allowed, and validated exactly as a transfer would be. It is
		# permitted rather than refused because `transfer_occupant_room` already makes
		# split-room bookings reachable - a refusal here would only mean join-then-transfer,
		# which reaches the same state while leaving a stint in a room the animal was never
		# in. Branch attribution is checked for the same reason it is on a transfer.
		_assert_same_branch_transfer(boarding_doc, room)

	occupant_type = requested_type or cstr(boarding_doc.boarding_type).strip()
	if occupant_type not in BOARDING_TYPES:
		return _boarding_validation_error(
			_("Invalid boarding type {0}. Expected one of: {1}.").format(
				frappe.bold(occupant_type or ""), ", ".join(BOARDING_TYPES)
			)
		)

	# Room, then booking, then pet - the same order transfer and reserve take, so the three
	# endpoints cannot deadlock against each other. The whole count -> decide -> insert
	# window is inside these: two staff adding a pet at the same instant would otherwise
	# both read six actives and both append, giving eight on a limit of seven.
	with room_locks(room.name), boarding_lock(boarding_doc.name), pet_locks(pet_name):
		boarding_doc.reload()

		if find_occupant(boarding_doc, pet_name, active_only=True) is not None:
			return _boarding_validation_error(
				_("Pet {0} is already an active occupant of Pet Boarding {1}.").format(
					frappe.bold(pet_name), frappe.bold(boarding_doc.name)
				)
			)

		# Exists, belongs to this guardian, and is not active on any other booking. Shared
		# with reserve_room so a pet joining late is refused for the same reasons, in the
		# same words, as a pet named at reservation.
		_assert_pets_reservable([pet_name], cstr(boarding_doc.guardian).strip())

		assert_capacity(boarding_doc, adding=1)
		assert_room_available(room.name, boarding_doc.guardian, exclude_boarding=boarding_doc.name, adding=1)

		joined_at = now_datetime() if boarding_doc.record_status == "Checked In" else None
		boarding_doc.append(
			"occupants",
			{
				"pet": pet_name,
				"status": "Active",
				"boarding_type": occupant_type,
				"service_room": room.name,
				"joined_at": joined_at,
			},
		)
		# add_if_missing default: this pet needs a room-stay row it does not have yet, and
		# it is priced from ITS OWN type and nights. Existing rows are refreshed, never
		# repriced - a rate already on a row is left alone.
		_ensure_room_stay_billable_item(boarding_doc)
		boarding_doc.run_method("_apply_billable_item_amounts")
		boarding_doc.run_method("_compute_totals")
		boarding_doc.save(ignore_permissions=True)

		open_stint(
			boarding_doc.name,
			pet_name,
			room.name,
			joined_at or boarding_doc.get("reserved_at") or now_datetime(),
			reason=join_note or _("Joined an existing booking."),
		)

	boarding_doc.add_comment(
		"Comment",
		_("Pet {0} joined Pet Boarding {1} in room {2} by {3}.").format(
			pet_name, boarding_doc.name, room.name, frappe.session.user
		),
	)
	_log_boarding_event(
		"BOARDING_OCCUPANT_ADDED",
		boarding=boarding_doc.name,
		pet=pet_name,
		room=room.name,
		boarding_type=occupant_type,
		joined_at=cstr(joined_at) if joined_at else None,
		active_occupants=len(active_occupants(boarding_doc)),
		user=frappe.session.user,
	)

	return {
		"success": True,
		"boarding_id": boarding_doc.name,
		"pet": pet_name,
		"boarding_type": occupant_type,
		"service_room": room.name,
		"joined_at": joined_at,
		"active_occupant_count": len(active_occupants(boarding_doc)),
		"total_cost": boarding_doc.total_cost,
		"boarding": _serialize_boarding_doc(boarding_doc),
	}


@frappe.whitelist(methods=["POST"])
@standardize_response
def depart_occupant(boarding=None, pet=None, note=None, departed_at=None, data=None, **kwargs):
	"""One pet goes home while the booking runs on.

	Deliberately NOT part of `check_out_boarding`, and the separation is the point. If one
	animal leaving shared a verb with the booking ending, sooner or later someone would
	press it with three pets still in the room: the booking would close, the invoice would
	be raised, and the two animals still in the kennel would have no open record and no
	room. Two different events, two different endpoints, and this one cannot end a booking
	no matter what it is passed.

	What it does: closes the occupant, stamps `departed_at`, closes that pet's open room
	stint, and reconciles the room-stay rows so this pet's row prices to ITS OWN nights.
	The room is NOT released - the other animals are still in it - and nothing on the
	booking changes.
	"""
	payload = _coerce_payload(data, kwargs)
	boarding_name = cstr(payload.get("boarding") or payload.get("boarding_id") or boarding).strip()
	pet_name = cstr(payload.get("pet") or payload.get("pet_id") or pet).strip()
	departure_note = cstr(payload.get("note") if "note" in payload else note).strip()
	when = payload.get("departed_at") if "departed_at" in payload else departed_at

	if not boarding_name:
		return _boarding_validation_error(_("Pet Boarding is required."))
	if not pet_name:
		return _boarding_validation_error(_("Pet is required. Send the pet that is leaving."))

	_require_boarding_write_access()
	if not frappe.db.exists("Pet Boarding", boarding_name):
		return _boarding_validation_error(_("Pet Boarding {0} was not found.").format(frappe.bold(boarding_name)))

	boarding_doc = frappe.get_doc("Pet Boarding", boarding_name)
	boarding_doc.check_permission("write")

	if boarding_doc.docstatus != 0:
		return _boarding_validation_error(_("Only open Pet Boarding records can have a pet depart."))
	if boarding_doc.sales_invoice:
		return _boarding_validation_error(
			_("Pet Boarding {0} is already invoiced.").format(frappe.bold(boarding_doc.name))
		)
	if boarding_doc.record_status != "Checked In":
		return _boarding_validation_error(
			_("Only Checked In boarding records can have a pet depart.")
		)

	# The booking's occupant set is read, decided on, and written here - the same window
	# add-a-pet contends for.
	with boarding_lock(boarding_doc.name):
		boarding_doc.reload()
		occupant = find_occupant(boarding_doc, pet_name, active_only=True)
		if occupant is None:
			return _boarding_validation_error(
				_("Pet {0} is not an active occupant of Pet Boarding {1}.").format(
					frappe.bold(pet_name), frappe.bold(boarding_doc.name)
				)
			)

		# The rule that keeps a room from being held by nobody. Without it the last pet
		# departs, every occupant is closed, and the booking stays Checked In forever with
		# its stay length accruing against `now` and its room unreservable.
		if is_last_active_occupant(boarding_doc, pet_name):
			return _boarding_validation_error(
				_(
					"Pet {0} is the last pet on Pet Boarding {1}. Check the booking out instead - "
					"departing the last occupant individually would leave the booking open with an empty room."
				).format(frappe.bold(pet_name), frappe.bold(boarding_doc.name))
			)

		departed_when = get_datetime(when) if when else now_datetime()
		room_left = cstr(occupant.get("service_room")).strip() or cstr(boarding_doc.service_room).strip()

		# `occupant` is the row validated as Active above. Passing it means the row checked
		# and the row closed are the same object - the divergence that let a repeated pet
		# validate on its open row and mutate its departed one cannot recur here.
		close_occupant(
			boarding_doc,
			pet_name,
			status="Departed",
			when=departed_when,
			note=departure_note or None,
			row=occupant,
		)
		close_open_stint(boarding_doc.name, pet_name, departed_when)

		# Reprices every room-stay row from its own occupant's joined_at/departed_at. This
		# pet's row now ends at its departure; the pets still here keep accruing to now,
		# which is the same running figure check-out would show them.
		_ensure_room_stay_billable_item(boarding_doc, add_if_missing=False)
		boarding_doc.run_method("_apply_billable_item_amounts")
		boarding_doc.run_method("_compute_totals")
		boarding_doc.save(ignore_permissions=True)

	boarding_doc.add_comment(
		"Comment",
		_("Pet {0} departed from Pet Boarding {1} by {2}.").format(
			pet_name, boarding_doc.name, frappe.session.user
		),
	)
	_log_boarding_event(
		"BOARDING_OCCUPANT_DEPARTED",
		boarding=boarding_doc.name,
		pet=pet_name,
		room=room_left,
		departed_at=cstr(departed_when),
		remaining_active=active_occupant_count(boarding_doc),
		user=frappe.session.user,
	)

	return {
		"success": True,
		"boarding_id": boarding_doc.name,
		"pet": pet_name,
		"departed_at": departed_when,
		"remaining_active_occupants": active_occupant_count(boarding_doc),
		# The room is still held. Saying so explicitly because the booking-level checkout
		# response reports "Available" here, and a client that reuses that mapping would
		# show the kennel as empty with animals in it.
		"room_id": room_left,
		"room_released": False,
		"boarding": _serialize_boarding_doc(boarding_doc),
	}


@frappe.whitelist(methods=["POST"])
@standardize_response
def transfer_occupant_room(boarding=None, pet=None, service_room=None, note=None, data=None, **kwargs):
	"""Move one pet to another room. A stint closes and another opens.

	No financial effect, by construction. Rates resolve from the pet's `animal_type` and
	its boarding type and from nothing else, so both rooms cost the same and no figure
	may move. This function therefore does NOT call `_ensure_room_stay_billable_item`, and
	a test asserts the totals are byte-identical across a transfer. The 56 stint rows sit
	there with dates on them; nothing prices from them and nothing here starts.

	Release and claim are ATOMIC. Both rooms are held for the whole operation, in sorted
	name order - see `room_locks` - so two simultaneous A->B and B->A transfers serialise
	instead of deadlocking, and two bookings cannot each come to believe they hold the
	same room. That belief would be invisible otherwise: `get_active_boarding_for_room`
	takes `limit_page_length=1` and returns *a* booking, not *the* booking, so a room with
	two claimants looks exactly like a room with one.

	Cross-branch transfers are refused - see `_assert_same_branch_transfer`.
	"""
	payload = _coerce_payload(data, kwargs)
	boarding_name = cstr(payload.get("boarding") or payload.get("boarding_id") or boarding).strip()
	pet_name = cstr(payload.get("pet") or payload.get("pet_id") or pet).strip()
	target_room = cstr(payload.get("service_room") or payload.get("room_id") or service_room).strip()
	transfer_note = cstr(payload.get("note") if "note" in payload else note).strip()

	if not boarding_name:
		return _boarding_validation_error(_("Pet Boarding is required."))
	if not pet_name:
		return _boarding_validation_error(_("Pet is required. Send the pet being moved."))
	if not target_room:
		return _boarding_validation_error(_("Service Room is required."))

	_require_boarding_write_access()
	if not frappe.db.exists("Pet Boarding", boarding_name):
		return _boarding_validation_error(_("Pet Boarding {0} was not found.").format(frappe.bold(boarding_name)))

	boarding_doc = frappe.get_doc("Pet Boarding", boarding_name)
	boarding_doc.check_permission("write")
	if boarding_doc.docstatus != 0:
		return _boarding_validation_error(_("Only open Pet Boarding records can be transferred."))
	if boarding_doc.sales_invoice:
		return _boarding_validation_error(
			_("Pet Boarding {0} is already invoiced.").format(frappe.bold(boarding_doc.name))
		)
	if boarding_doc.record_status not in ROOM_ASSIGNED_ACTIVE_BOARDING_STATUSES:
		return _boarding_validation_error(
			_("Only a boarding that currently holds a room can be transferred.")
		)

	occupant = find_occupant(boarding_doc, pet_name, active_only=True)
	if occupant is None:
		return _boarding_validation_error(
			_("Pet {0} is not an active occupant of Pet Boarding {1}.").format(
				frappe.bold(pet_name), frappe.bold(boarding_doc.name)
			)
		)

	source_room = cstr(occupant.get("service_room")).strip() or cstr(boarding_doc.service_room).strip()
	if source_room == target_room:
		return _boarding_validation_error(
			_("Pet {0} is already in Service Room {1}.").format(frappe.bold(pet_name), frappe.bold(target_room))
		)

	room = _get_service_room(target_room)
	if room.status != "Active":
		return _boarding_validation_error(_("Service Room {0} is inactive.").format(frappe.bold(room.name)))
	_assert_same_branch_transfer(boarding_doc, room)

	moved_at = now_datetime()
	# Both rooms, sorted, for the whole read-decide-write. The destination is checked for
	# availability INSIDE this lock; checked outside it, the answer is only a suggestion.
	with room_locks(source_room, target_room), boarding_lock(boarding_doc.name):
		boarding_doc.reload()
		occupant = find_occupant(boarding_doc, pet_name, active_only=True)
		if occupant is None:
			return _boarding_validation_error(
				_("Pet {0} is no longer an active occupant of Pet Boarding {1}.").format(
					frappe.bold(pet_name), frappe.bold(boarding_doc.name)
				)
			)

		assert_room_available(
			room.name, boarding_doc.guardian, exclude_boarding=boarding_doc.name, adding=1
		)

		close_open_stint(boarding_doc.name, pet_name, moved_at)
		open_stint(
			boarding_doc.name,
			pet_name,
			room.name,
			moved_at,
			reason=transfer_note or _("Transferred from {0}.").format(source_room),
		)
		occupant.service_room = room.name

		# The booking-level `service_room` can only name ONE room, so it is only moved
		# when every remaining occupant is in the same place. With the booking's pets
		# split across two rooms it stays on the room the rest are in, and the occupant
		# rows are the truth - which is why room_occupancy reads occupants, not this.
		rooms_in_use = {cstr(row.service_room).strip() for row in active_occupants(boarding_doc) if cstr(row.service_room).strip()}
		if rooms_in_use == {room.name}:
			boarding_doc.service_room = room.name

		# Deliberately no repricing call here. See the docstring.
		boarding_doc.save(ignore_permissions=True)

	boarding_doc.add_comment(
		"Comment",
		_("Pet {0} moved from Service Room {1} to {2} by {3}.").format(
			pet_name, source_room, room.name, frappe.session.user
		),
	)
	_log_boarding_event(
		"BOARDING_OCCUPANT_TRANSFERRED",
		boarding=boarding_doc.name,
		pet=pet_name,
		from_room=source_room,
		to_room=room.name,
		user=frappe.session.user,
	)

	return {
		"success": True,
		"boarding_id": boarding_doc.name,
		"pet": pet_name,
		"from_room": source_room,
		"to_room": room.name,
		"booking_room": boarding_doc.service_room,
		"total_cost": boarding_doc.total_cost,
		"boarding": _serialize_boarding_doc(boarding_doc),
	}


def _assert_same_branch_transfer(boarding, room):
	"""A pet may not be transferred into a room belonging to another branch.

	Branch attribution is frozen at insert - `utils.branch.stamp_boarding_branch` writes it
	once, and both the checkout and the death-settlement invoice pass `boarding.branch`
	verbatim with an explicit note that a later change must not re-attribute a stay that
	already happened. `invoice_reuse` then keys open drafts on branch, so the branch is not
	a label on this record, it is which set of books the money lands in.

	A cross-branch transfer has no correct answer inside that design. Keeping the original
	branch bills a stay to a site the animal has left; re-stamping retroactively moves
	revenue between branches, which is precisely the failure that already cost a
	production revert. Splitting the charge would mean pricing from stints, which the
	whole room-stay design refuses to do.

	So it is refused, and the message names the supported alternative: two bookings, one
	per branch, each invoicing its own nights to its own books.
	"""
	room_branch = cstr(room.get("branch")).strip()
	boarding_branch = cstr(boarding.get("branch")).strip()
	if not room_branch or not boarding_branch or room_branch == boarding_branch:
		return
	frappe.throw(
		_(
			"Service Room {0} belongs to branch {1}, but Pet Boarding {2} is attributed to branch {3}. "
			"A stay cannot move between branches: check this booking out and create a new one at {1}, "
			"so each branch invoices the nights it actually provided."
		).format(
			frappe.bold(room.name), frappe.bold(room_branch), frappe.bold(boarding.name), frappe.bold(boarding_branch)
		)
	)


@frappe.whitelist(methods=["POST"])
@standardize_response
def return_boarding_medication(boarding=None, item_id=None, qty=None, note=None, data=None, **kwargs):
	"""Undo a boarding dispense, goods included.

	The visit path has had `return_dispensed_medication` since the pharmacy API was
	written; boarding had no counterpart at all, which was survivable only while
	dispensing moved nothing. Now that it does, a dose drawn up and not given would
	otherwise stay deducted forever.

	Mirrors the visit function deliberately - same guards, same accumulator, same
	rule that the reversal is sized from what was ISSUED rather than from the dose
	option, so editing the option afterwards cannot unbalance the two movements.
	"""
	payload = _coerce_payload(data, kwargs)
	boarding_name = cstr(payload.get("boarding") or payload.get("boarding_id") or boarding).strip()
	row_id = cstr(payload.get("item_id") or payload.get("row_name") or item_id).strip()
	return_note = cstr(payload.get("note") if "note" in payload else note).strip()

	if not boarding_name:
		return _boarding_validation_error(_("Pet Boarding is required."))
	if not row_id:
		return _boarding_validation_error(_("Medication billable row is required."))

	_require_boarding_write_access()
	if not frappe.db.exists("Pet Boarding", boarding_name):
		return _boarding_validation_error(_("Pet Boarding {0} was not found.").format(frappe.bold(boarding_name)))

	boarding_doc = frappe.get_doc("Pet Boarding", boarding_name)
	boarding_doc.check_permission("write")
	_require_billable_dispense_fields()

	row = _find_boarding_billable_row(boarding_doc, row_id)
	if not row:
		return _boarding_validation_error(_("Medication billable row was not found."))
	if cstr(row.get("item_type")).strip() != "Medication":
		return _boarding_validation_error(_("Only medication billable rows can be returned from this action."))
	if not row.meta.has_field("return_qty"):
		return _boarding_validation_error(_("Run migrations before returning boarding medication."))

	return_qty = flt(payload.get("qty") if payload.get("qty") is not None else qty)
	if return_qty <= 0:
		return_qty = max(flt(row.get("dispensed_qty")) - flt(row.get("return_qty")), 0)
	if return_qty <= 0:
		return _boarding_validation_error(_("Return Qty must be greater than zero."))
	if flt(row.get("return_qty")) + return_qty > flt(row.get("dispensed_qty")):
		return _boarding_validation_error(_("Return Qty cannot exceed dispensed Qty."))

	try:
		restore_qty = returnable_stock_qty(
			stock_issued_qty=row.get("stock_issued_qty"),
			dispensed_qty=row.get("dispensed_qty"),
			return_doses=return_qty,
		)
		if restore_qty > 0:
			medication_name = (
				cstr(row.get("linked_name")).strip()
				if cstr(row.get("linked_doctype")).strip() == "Medication"
				else ""
			)
			returned_entry = receive_for_return(
				medication_item=row.get("item_code"),
				qty=restore_qty,
				warehouse=row.get("warehouse"),
				medication=medication_name,
				reference=_("Pet Boarding {0} row {1}").format(boarding_doc.name, row.name),
				label=medication_name or row.get("item_name") or row.get("item_code"),
			)
			if returned_entry:
				row.stock_issued_qty = max(flt(row.get("stock_issued_qty")) - restore_qty, 0)
				row.stock_entry = returned_entry

		row.return_qty = flt(row.get("return_qty")) + return_qty
		row.returned_by = frappe.session.user
		row.returned_at = now_datetime()
		row.dispense_status = (
			"Returned" if flt(row.return_qty) >= flt(row.get("dispensed_qty")) else "Partially Dispensed"
		)
		if return_note:
			row.note = _append_note(row.get("note"), return_note)

		boarding_doc.save(ignore_permissions=True)
	except Exception:
		frappe.db.rollback()
		raise

	boarding_doc.add_comment(
		"Comment",
		_("Medication billable row {0} returned by {1}.").format(row.name, frappe.session.user),
	)
	_log_boarding_event(
		"BOARDING_MEDICATION_RETURNED",
		boarding=boarding_doc.name,
		item=row.name,
		qty=return_qty,
		user=frappe.session.user,
	)

	return {
		"success": True,
		"boarding_id": boarding_doc.name,
		"item": _serialize_billable_item(row),
		"boarding": _serialize_boarding_doc(boarding_doc),
	}


def _resolve_order_pet(boarding, pet) -> str:
	"""The pet a boarding order is for. Required, and it must be on this stay.

	Required rather than defaulted to `boarding.pet`, even though a stay holds one pet
	today and the default would therefore always be right. That is precisely the danger:
	a default lets a caller that was never updated keep working, filing every order
	against whichever pet the stay happens to name - silently, and correctly, until the
	day a booking holds seven. Then it is wrong with no error and no way to tell from the
	data which animal was meant.

	Membership is the OCCUPANT TABLE, not `Pet Boarding.pet`. That scalar is the legacy
	single-pet field: it names one animal for the life of the booking, so testing against
	it refused every occupant except whichever one it happened to hold - a booking with
	three pets could raise orders for one of them, and the other two were told they were
	"not on this boarding" while the roster showed them present. The roster was right.

	`pet_is_on_boarding` reads BOTH shapes - the occupant table where it has rows, the
	legacy scalar where it has none - because bookings with no occupant rows still exist
	and refusing them all would be a worse outage than the one being fixed. See that
	function; it is the single definition every gate here shares.
	"""
	chosen = cstr(pet).strip()
	if not chosen:
		frappe.throw(_("Pet is required. Send the pet this order is for."))
	if not pet_is_on_boarding(boarding, chosen, active_only=True):
		frappe.throw(_pet_not_on_boarding_message(chosen, boarding))
	return chosen


def _pet_not_on_boarding_message(pet: str, boarding) -> str:
	"""Refuse by saying where the animal actually is, not merely that it is not here.

	An operator who is told "not on this boarding" has to go looking. One who is told the
	booking it IS on can act on the sentence.
	"""
	elsewhere = None
	try:
		elsewhere = find_active_boarding_for_pet(pet)
	except Exception:
		elsewhere = None
	if elsewhere and cstr(elsewhere.get("name")).strip() != cstr(boarding.name).strip():
		return _("Pet {0} is not an active occupant of Pet Boarding {1}. It is currently on {2} ({3}).").format(
			frappe.bold(pet),
			frappe.bold(boarding.name),
			frappe.bold(elsewhere["name"]),
			frappe.bold(elsewhere["record_status"]),
		)
	return _("Pet {0} is not an active occupant of Pet Boarding {1}.").format(
		frappe.bold(pet), frappe.bold(boarding.name)
	)


def _boarding_order_id(boarding, kind: str, care_service: str, pet: str) -> str:
	"""Identity of a boarding order, per pet.

	The pet belongs in this key. Without it two pets in one booking ordering the same
	service produce an identical id, and _find_recent_duplicate_order reads the second as
	a repeat of the first: no order, no charge, no error, and a missing clinical record.
	"""
	return f"{boarding.name}-{kind}-{care_service}-{pet}"


def _assert_boarding_orderable(boarding):
	if boarding.docstatus != 0:
		frappe.throw(_("Orders can only be added to open Pet Boarding records."))
	if boarding.sales_invoice:
		frappe.throw(_("Pet Boarding {0} is already invoiced.").format(frappe.bold(boarding.name)))
	if boarding.record_status == "Reserved":
		frappe.throw(
			_("Pet Boarding {0} is reserved and has not been checked in yet.").format(frappe.bold(boarding.name))
		)
	if boarding.record_status == "Checked Out":
		frappe.throw(_("Pet Boarding {0} is already checked out.").format(frappe.bold(boarding.name)))
	if boarding.record_status == "Cancelled":
		frappe.throw(_("Pet Boarding {0} is cancelled.").format(frappe.bold(boarding.name)))
	if boarding.record_status != "Checked In":
		frappe.throw(_("Only checked-in Pet Boarding records can have orders."))


def _normalize_order_priority(priority) -> str:
	priority = cstr(priority).strip()
	if not priority:
		return DEFAULT_ORDER_PRIORITY
	normalized = priority.title()
	if normalized not in ORDER_PRIORITIES:
		frappe.throw(
			_("Invalid priority {0}. Expected one of: {1}.").format(
				frappe.bold(priority), ", ".join(ORDER_PRIORITIES)
			)
		)
	return normalized


def _create_boarding_medication_order(
	boarding,
	*,
	pet: str,
	medication: str,
	item_code: str | None = None,
	note: str | None = None,
	dose_option: str | None = None,
	warehouse: str | None = None,
	dosage: str | None = None,
	frequency: str | None = None,
	duration_days=None,
	scheduled_datetime=None,
	provider=None,
) -> dict:
	require_doctype_permission("Medication", "read")
	medication_name = cstr(medication).strip()
	if not medication_name or not frappe.db.exists("Medication", medication_name):
		return _boarding_validation_error(_("Medication {0} was not found.").format(frappe.bold(medication_name or "")))

	existing = _find_recent_duplicate_medication_billable(boarding, medication_name, pet)
	if existing:
		_log_boarding_event(
			"BOARDING_MEDICATION_ORDER_DUPLICATE",
			boarding=boarding.name,
			pet=pet,
			medication=medication_name,
			item=existing.name,
			user=frappe.session.user,
		)
		return _boarding_medication_order_response(boarding, existing, reused=True)

	medication_doc = frappe.db.get_value(
		"Medication",
		medication_name,
		["name", "medication_name", "linked_item", "default_price"],
		as_dict=True,
	)
	resolved_item = cstr(item_code).strip() or cstr(medication_doc.get("linked_item")).strip()
	if not resolved_item:
		return _boarding_validation_error(
			_("Medication {0} is not linked to an Item and cannot be billed for boarding.").format(
				frappe.bold(medication_name)
			)
		)

	item = _get_item_details(resolved_item)
	rate = item.get("rate") if medication_doc.get("default_price") in (None, "") else flt(medication_doc.get("default_price"))
	order_id = f"{boarding.name}-medication-{frappe.generate_hash(length=10)}"
	included = _medication_is_included(boarding, pet)
	row = boarding.append(
		"billable_items",
		{
			"pet": pet,
			# Included, not zero-rated and not suppressed. The rate stays true so the owner
			# can see what the medical rate absorbed; the status keeps it off the invoice.
			"status": "Included" if included else "Billable",
			"item_name": medication_doc.get("medication_name") or item.get("item_name"),
			"item_code": item.get("item_code"),
			"item_type": "Medication",
			"qty": 1,
			"rate": rate,
			"amount": rate,
			"note": note or medication_doc.get("medication_name") or item.get("item_name"),
			"linked_service_id": f"boarding-medication::{order_id}",
			"linked_doctype": "Medication",
			"linked_name": medication_name,
			"order_id": order_id,
		},
	)
	_set_child_value_if_field(row, "care_episode", _active_episode_name_for_pet(pet))
	_set_child_value_if_field(row, "dispense_status", "Pending Dispense")
	_set_child_value_if_field(row, "dispensed_qty", 0)
	# Which dose, and from whose shelf. Both optional and both blank by default:
	# a medication with one dose option resolves it on its own at dispense, and a
	# blank warehouse falls through the shared chain to the Medication's default.
	# They exist so a stay CAN name its own, rather than always inheriting the
	# clinic's - the boarding facility is a distinct place with its own branch.
	if cstr(dose_option).strip():
		_set_child_value_if_field(row, "dose_option", cstr(dose_option).strip())
	if cstr(warehouse).strip():
		require_restriction_value("warehouse", cstr(warehouse).strip())
		_set_child_value_if_field(row, "warehouse", cstr(warehouse).strip())

	# The schedule: what dose, how often, for how long. Free text on the first two by
	# design - the visit path takes free text and clinics write frequencies in Arabic and
	# English, so a controlled vocabulary here would both reject real entries and split the
	# two paths.
	#
	# `dosage` is NOT `dose_option`. dose_option names a Medication Dose Option and is what
	# moves stock; dosage is the clinical instruction a human reads. A medication can carry
	# one, both or neither, and neither is derived from the other - deriving one would give
	# the stock deduction a second source of truth.
	#
	# `duration_days` is stored and nothing else. It does not multiply qty, rate or amount:
	# the dose count arrives in qty from the client and stays there. Deriving a total from
	# frequency x duration would silently change what a guardian is charged, which is a
	# pricing decision and not this function's to make.
	#
	# On the lifecycle question the client raised - these are clinical facts living on a
	# billing row, so they follow that row if billing cancels it. That is deliberate and it
	# is already true of dose_option, warehouse, dispense_status and the whole stock trail
	# sitting beside them: the billable row IS the boarding medication order, because a
	# boarding medication has no order document at all. A medication absorbed by a medical
	# boarding rate is marked `Included` and keeps its row and its schedule; only an order
	# somebody actually removes takes its schedule with it, which is the correct outcome.
	# Splitting the schedule onto a second document would need two writes with nothing
	# keeping them in step, which trades a clear rule for a silent divergence.
	if cstr(dosage).strip():
		_set_child_value_if_field(row, "dosage", cstr(dosage).strip())
	if cstr(frequency).strip():
		_set_child_value_if_field(row, "frequency", cstr(frequency).strip())
	if duration_days is not None and cstr(duration_days).strip() != "":
		_set_child_value_if_field(row, "duration_days", _coerce_duration_days(duration_days))
	# WHEN, as opposed to how often. `frequency` and `duration_days` describe a recurrence
	# with no anchor - "BID, 5 days" says nothing about which day it starts. This is the
	# anchor, and it is the only one of the four that a worklist can sort on.
	if scheduled_datetime:
		_set_child_value_if_field(row, "scheduled_datetime", _coerce_scheduled_datetime(scheduled_datetime))
	# WHO gives it. The frontend does not offer an assignee on medication today and is not
	# asked to; this stores one anyway if a caller sends it, because the row has the field
	# and dropping a value the caller supplied is the exact defect this change closes.
	if provider:
		_set_child_value_if_field(row, "provider", provider)

	boarding.run_method("_apply_billable_item_amounts")
	boarding.run_method("_compute_totals")
	boarding.save()
	boarding.add_comment(
		"Comment",
		_("Medication {0} added to boarding billing by {1}.").format(medication_name, frappe.session.user),
	)
	_log_boarding_event(
		"BOARDING_MEDICATION_ORDER_CREATED",
		boarding=boarding.name,
		pet=pet,
		medication=medication_name,
		item=row.name,
		user=frappe.session.user,
	)
	return _boarding_medication_order_response(boarding, row)


def _create_boarding_order_doc(boarding, kind, *, pet, care_service, item_code, priority, note, scheduled_datetime=None, provider=None):
	order_id = _boarding_order_id(boarding, kind, care_service, pet)
	if kind == "service":
		template = frappe.db.get_value(
			"CareService template", care_service, ["service_name", "default_price"], as_dict=True
		)
		doc = frappe.get_doc(
			{
				"doctype": "PetCareService",
				"pet_service_name": (template.service_name if template else None) or _("Care Service"),
				"pet_id": pet,
				"guardian_id": boarding.guardian,
				"care_service_id": care_service,
				"item_code": item_code or None,
				"price": template.default_price if template else None,
				"status": "pending",
				"due_date": nowdate(),
				"description": note,
				"source_doctype": "Pet Boarding",
				"source_name": boarding.name,
				"order_id": order_id,
			}
		)
	else:
		doc = frappe.get_doc(
			{
				"doctype": ORDER_KIND_DOCTYPE[kind],
				"pet": pet,
				"care_service": care_service,
				"item_code": item_code or None,
				"priority": priority,
				"status": "Ordered",
				"source_doctype": "Pet Boarding",
				"source_name": boarding.name,
				"order_id": order_id,
			}
		)
	# Set only when one was actually given, and only where the column exists. Assigning
	# None would be harmless but writing the key unconditionally invites a later `or
	# nowdate()` to be added beside the ones already on `due_date`.
	if scheduled_datetime:
		_set_child_value_if_field(doc, "scheduled_datetime", scheduled_datetime)
	# Same rule, same reason: written only when one was given, and only where the document
	# has the field. Lab and Imaging do not - they carry a `doctor` instead - so for those
	# kinds the assignment lives on the billable row alone.
	if provider:
		_set_child_value_if_field(doc, "provider", provider)
	doc.insert(ignore_permissions=True)
	doc.add_comment(
		"Comment", _("Created from Pet Boarding {0} by {1}.").format(boarding.name, frappe.session.user)
	)
	return doc


def _append_boarding_order_billable(boarding, kind, order, *, care_service, item_code, note, scheduled_datetime=None, provider=None):
	resolved_item, rate, item_name = _resolve_order_billing(order, care_service, item_code)
	row = boarding.append(
		"billable_items",
		{
			"pet": order.get("pet") or order.get("pet_id"),
			"item_name": item_name,
			"item_code": resolved_item,
			"item_type": ORDER_KIND_BILLABLE_TYPE[kind],
			"qty": 1,
			"rate": rate,
			"amount": rate,
			"status": "Billable",
			"note": note or item_name,
			"linked_service_id": f"{order.doctype}::{order.name}",
			"linked_doctype": order.doctype,
			"linked_name": order.name,
			"order_id": order.get("order_id"),
		},
	)
	# The schedule rides on the billing row too, because for a boarding medication the
	# billable row IS the order - there is no order document to carry it. Recorded only;
	# it does not touch qty, rate or amount, and never reaches the invoice.
	if scheduled_datetime:
		_set_child_value_if_field(row, "scheduled_datetime", scheduled_datetime)
	# The assignment rides on the billing row for the same reason the schedule does: it is
	# the row the caller reads back, and for kinds whose order document has no provider
	# field it is the only place the assignment can live. Recorded only - who does the work
	# does not change what is charged, so it never touches qty, rate or amount.
	if provider:
		_set_child_value_if_field(row, "provider", provider)
	boarding.run_method("_apply_billable_item_amounts")
	boarding.run_method("_compute_totals")
	boarding.save()
	boarding.add_comment(
		"Comment",
		_("{0} order {1} added to boarding billing by {2}.").format(
			kind.title(), order.name, frappe.session.user
		),
	)


def _resolve_order_billing(order, care_service, item_code):
	"""Resolve (item_code, rate, item_name) for the boarding billable row.

	Lab/Imaging stamp item_code + rate from the Care Service during validate, so
	we prefer those. PetCareService does not, so we fall back to the template.
	"""
	resolved_item = cstr(order.get("item_code")).strip() or cstr(item_code).strip()
	rate = order.get("rate")
	item_name = None
	if care_service and frappe.db.exists("CareService template", care_service):
		template = frappe.db.get_value(
			"CareService template", care_service, ["item_code", "default_price", "service_name"], as_dict=True
		)
		resolved_item = resolved_item or cstr(template.item_code)
		if rate in (None, ""):
			rate = template.default_price
		item_name = template.service_name
	if not resolved_item:
		frappe.throw(_("Unable to determine an Item Code to bill this order."))
	item = _get_item_details(resolved_item)
	if rate in (None, ""):
		rate = item.get("rate")
	return resolved_item, flt(rate or 0), item_name or item.get("item_name")


def _find_recent_duplicate_order(order_doctype, boarding_name, kind, care_service, pet):
	"""A double-click guard, not a uniqueness rule - hence the short window.

	The pet filter is what keeps it a double-click guard. Without it the window catches
	any two identical orders on the same stay, so the second pet to need the same test
	within two minutes gets the first pet's order handed back instead of one of its own.
	"""
	since = add_to_date(now_datetime(), seconds=-DUPLICATE_ORDER_WINDOW_SECONDS)
	pet_field = "pet_id" if kind == "service" else "pet"
	filters = {
		"source_doctype": "Pet Boarding",
		"source_name": boarding_name,
		"creation": [">=", since],
		pet_field: pet,
	}
	filters["care_service_id" if kind == "service" else "care_service"] = care_service
	rows = frappe.get_all(order_doctype, filters=filters, fields=["name", "status"], order_by="creation desc")
	terminal = ORDER_TERMINAL_STATUSES.get(order_doctype, set())
	for row in rows:
		if cstr(row.status).strip() not in terminal:
			return frappe.get_doc(order_doctype, row.name)
	return None


def _boarding_order_response(boarding, kind, order, reused=False) -> dict:
	return {
		"success": True,
		"order_id": order.name,
		"kind": kind,
		# Echoed so the caller can confirm which animal the order was filed against
		# rather than inferring it from the stay.
		"pet": order.get("pet") or order.get("pet_id"),
		"boarding_id": boarding.name,
		"linked_doctype": order.doctype,
		"reused": reused,
		"total_cost": boarding.total_cost,
		"balance": boarding.balance,
		"billable_items": [_serialize_billable_item(row) for row in boarding.billable_items or []],
	}


def _boarding_medication_order_response(boarding, row, reused=False) -> dict:
	return {
		"success": True,
		"order_id": row.get("order_id") or row.name,
		"item_id": row.name,
		"kind": "medication",
		# Present for the same reason as on _boarding_order_response: the caller should
		# read back which animal the order was filed against, not infer it from the stay.
		"pet": row.get("pet"),
		"boarding_id": boarding.name,
		"linked_doctype": row.get("linked_doctype"),
		"linked_name": row.get("linked_name"),
		"reused": reused,
		"total_cost": boarding.total_cost,
		"balance": boarding.balance,
		"billable_item": _serialize_billable_item(row),
		"billable_items": [_serialize_billable_item(item) for item in boarding.billable_items or []],
	}


def _boarding_validation_error(message) -> dict:
	return fail(message, code="VALIDATION_ERROR")


def _active_episode_name_for_pet(pet: str | None) -> str | None:
	pet = cstr(pet).strip()
	if not pet:
		return None
	return frappe.db.get_value(
		"Pet Care Episode",
		{"pet": pet, "episode_status": ["in", list(ACTIVE_EPISODE_STATUSES)]},
		"name",
		order_by="modified desc",
	)


def _validate_boarding_medication_plan_item(boarding, plan) -> dict | None:
	# Any ACTIVE occupant, not the booking's legacy `pet` scalar. That comparison refused
	# a plan item for every animal on the stay but one, and a booking holds several now.
	plan_pet = cstr(plan.pet).strip()
	if not pet_is_on_boarding(boarding, plan_pet, active_only=True):
		return _boarding_validation_error(
			_("Plan item belongs to {0}, which is not an active occupant of Pet Boarding {1}.").format(
				frappe.bold(plan_pet or _("no pet")), frappe.bold(boarding.name)
			)
		)

	# The plan item's own pet, not the stay's - it is the plan item that names the animal.
	# No longer the same value as the booking scalar, which is exactly why it is read here.
	active_episode = _active_episode_name_for_pet(plan.pet)
	if not active_episode:
		return _boarding_validation_error(_("The boarded pet does not have an open care episode."))
	if plan.get("care_episode") != active_episode:
		return _boarding_validation_error(_("Plan item does not belong to the boarded pet's open care episode."))
	if not _is_medication_plan_item(plan):
		return _boarding_validation_error(_("Only medication or injection plan items can be marked as given from boarding."))

	linked_doctype = cstr(plan.get("linked_doctype")).strip()
	linked_name = cstr(plan.get("linked_name")).strip()
	if linked_doctype and linked_doctype != "Vet Visit Medication Item":
		return _boarding_validation_error(_("Medication plan item must link to a prescribed medication row."))
	if linked_name:
		row = frappe.db.get_value(
			"Vet Visit Medication Item",
			linked_name,
			["parent", "parenttype", "parentfield"],
			as_dict=True,
		)
		if not row:
			return _boarding_validation_error(_("Linked medication row was not found."))
		if row.parenttype != "Vet Visit" or row.parentfield != "prescribed_medications":
			return _boarding_validation_error(_("Linked medication row is not a visit prescription."))
		if frappe.get_meta("Vet Visit").has_field("care_episode"):
			visit_episode = frappe.db.get_value("Vet Visit", row.parent, "care_episode")
			if visit_episode and visit_episode != plan.get("care_episode"):
				return _boarding_validation_error(_("Linked medication row belongs to a different case."))
	return None


def _is_medication_plan_item(plan) -> bool:
	return (
		cstr(plan.get("plan_type")).strip() in MEDICATION_PLAN_TYPES
		or cstr(plan.get("linked_doctype")).strip() == "Vet Visit Medication Item"
	)


def _coerce_given_at(value):
	if not value:
		return now_datetime()
	return get_datetime(value)


def _serialize_plan_item_for_boarding(plan) -> dict:
	return {
		"name": plan.name,
		"pet": plan.pet,
		"care_episode": plan.get("care_episode"),
		"source_visit": plan.get("source_visit"),
		"linked_doctype": plan.get("linked_doctype"),
		"linked_name": plan.get("linked_name"),
		"plan_type": plan.get("plan_type"),
		"title": plan.get("title"),
		"status": plan.get("status"),
		"completed_on": plan.get("completed_on"),
		"completed_by": plan.get("completed_by"),
		"completion_note": plan.get("completion_note"),
	}


def _medication_is_included(boarding, pet: str) -> bool:
	"""Whether the medical boarding rate absorbs this medication.

	Only for a pet boarding medically, and only for medication ordered THROUGH the
	boarding. A medication prescribed on the linked Vet Visit is billed by the visit and
	never reaches this table, so the rule cannot reach it - which is the settled split:
	the rate covers what the boarding facility gives, not what a consultation prescribes.

	Read from the OCCUPANT's boarding type, not the booking's. Two pets on one booking may
	board on different terms, and it is the animal receiving the drug whose terms decide.
	"""
	pet = cstr(pet).strip()
	for occupant in boarding.get("occupants") or []:
		if cstr(occupant.pet).strip() == pet:
			return cstr(occupant.boarding_type).strip() == "Treatment"
	# No occupant row - a pre-Stage-2 record. Fall back to the booking's own type rather
	# than silently charging for something the medical rate should have covered.
	return cstr(boarding.boarding_type).strip() == "Treatment"


def _find_recent_duplicate_medication_billable(boarding, medication: str, pet: str):
	"""Double-click guard for medication, now scoped to one animal.

	The pet filter is what keeps this a double-click guard rather than a rule that two
	pets may not receive the same drug within two minutes. It became possible only once
	Pet Billable Item gained its `pet` column; before that this function could not tell
	two animals apart and would have handed the second pet the first pet's row.

	A row with no pet is treated as NOT a match. Those are pre-backfill rows, and
	assuming they belong to whoever is asking is exactly the misattribution the column
	was added to prevent.
	"""
	since = add_to_date(now_datetime(), seconds=-DUPLICATE_ORDER_WINDOW_SECONDS)
	pet = cstr(pet).strip()
	for row in boarding.billable_items or []:
		if cstr(row.get("item_type")).strip() != "Medication":
			continue
		if cstr(row.get("linked_doctype")).strip() != "Medication":
			continue
		if cstr(row.get("linked_name")).strip() != medication:
			continue
		if cstr(row.get("pet")).strip() != pet:
			continue
		if cstr(row.get("status")).strip() in {"Cancelled", "Billed"}:
			continue
		created_at = row.get("creation")
		if not created_at or get_datetime(created_at) >= since:
			return row
	return None


def _find_boarding_billable_row(boarding, row_id: str):
	row_id = cstr(row_id).strip()
	for row in boarding.billable_items or []:
		if row.name == row_id or cstr(row.idx) == row_id or cstr(row.get("order_id")).strip() == row_id:
			return row
	return None


def _require_billable_dispense_fields() -> None:
	meta = frappe.get_meta("Pet Billable Item")
	missing = [
		fieldname
		for fieldname in ("dispense_status", "dispensed_qty", "dispensed_by", "dispensed_at")
		if not meta.has_field(fieldname)
	]
	if missing:
		frappe.throw(_("Run migrations before dispensing boarding medication. Missing fields: {0}").format(", ".join(missing)))


def _set_child_value_if_field(row, fieldname: str, value) -> None:
	if row.meta.has_field(fieldname):
		row.set(fieldname, value)


def _append_note(current, note: str) -> str:
	current = cstr(current).strip()
	note = cstr(note).strip()
	if not current:
		return note
	if not note or note in current:
		return current
	return "\n".join([current, note])


# What a room holds when `Service Room.capacity` has nothing useful on it. One, because it
# is the only number that is true of every room ever built: a room holds at least one
# animal. Zero would publish `3 / 0`, and guessing higher would publish a vacancy that may
# not physically exist.
#
# This is NOT the booking limit. `max_pets_per_booking` is a policy about one booking;
# capacity is a fact about a room, and the two are deliberately separate numbers.
DEFAULT_ROOM_CAPACITY = 1

ROOM_BASE_FIELDS = ["name", "room_code", "room_name", "room_type", "image", "status", "notes"]


def _room_capacity_available() -> bool:
	"""Whether `Service Room.capacity` is on the table yet.

	The field ships in this change, but the column only appears when the owner runs
	migrate, and the app server may be restarted before or after that. Selecting a column
	that does not exist yet would fail the query and take the whole hub down, so the read
	adapts instead of assuming. Once migrate has run this is permanently true and costs a
	cached lookup.
	"""
	try:
		return bool(frappe.db.has_column("Service Room", "capacity"))
	except Exception:
		return False


def _room_capacity(room) -> int:
	"""The capacity to publish for a room. Never zero, never negative.

	A denominator has to be safe to divide by and safe to render. An unset column, a room
	created before the field existed, and a hand-typed 0 all mean "nobody has said", and
	they all resolve to the same floor rather than to a card reading `2 / 0`.
	"""
	value = cint((room or {}).get("capacity"))
	return value if value > 0 else DEFAULT_ROOM_CAPACITY


def _get_active_service_rooms(search=None):
	values = {}
	conditions = ["status = 'Active'"]
	if search:
		values["search"] = f"%{search}%"
		conditions.append("(name LIKE %(search)s OR room_code LIKE %(search)s OR room_name LIKE %(search)s OR room_type LIKE %(search)s)")

	fields = list(ROOM_BASE_FIELDS)
	if _room_capacity_available():
		fields.append("capacity")

	return frappe.db.sql(
		f"""
		SELECT {", ".join(fields)}
		FROM `tabService Room`
		WHERE {" AND ".join(conditions)}
		ORDER BY room_code ASC, room_name ASC
		""",
		values,
		as_dict=True,
	)


def _get_service_room(room_name: str):
	if not room_name or not frappe.db.exists("Service Room", room_name):
		frappe.throw(_("Service Room {0} was not found.").format(frappe.bold(room_name or "")))

	# Same conditional select as the hub's: `capacity` is only asked for once the column
	# exists, so a detail fetch cannot fail between this code landing and migrate running.
	fields = list(ROOM_BASE_FIELDS)
	if _room_capacity_available():
		fields.append("capacity")

	return frappe.db.get_value("Service Room", room_name, fields, as_dict=True)


BOARDING_RECORD_FIELDS = [
	"name", "service_room", "pet", "guardian", "customer", "boarding_type", "record_status",
	"status", "workflow_state", "reserved_at", "check_in", "check_out", "stay_days", "stay_hours",
	"total_cost", "deposit", "deposit_payment_entry", "balance", "billing_status", "sales_invoice",
	"note", "boarding_note", "boarded_by", "cancelled_by", "cancellation_note", "visit",
	"expected_check_out", "notes", "docstatus", "modified",
]


def _get_room_occupancy_state(room_names: list[str]) -> dict:
	"""Which pets are in each room, and which bookings they belong to.

	The hub's grain is the ROOM, not the booking, and this is where that changes. The old
	version asked "which booking names this room" - `Pet Boarding.service_room` - which
	stops being answerable the moment a per-pet transfer splits a booking across two rooms.
	The booking's own `service_room` is a single field that only moves when every occupant
	ends up in one place, so a split booking left one room showing the wrong animal and the
	other showing Available with a pet asleep in it.

	It now reads occupants, which is the same set every write-side availability check reads
	(`room_occupancy`), so the hub and the reservation gate can no longer disagree about
	whether a room is free.

	An occupant with a blank `service_room` falls back to its booking's - one live row is in
	that state, and without the coalesce that animal would vanish from the hub entirely.

	Returns `{room: {"occupants": [...], "bookings": [...]}}`, both ordered deterministically.
	"""
	if not room_names:
		return {}

	rows = frappe.db.sql(
		f"""
		SELECT
			COALESCE(NULLIF(o.service_room, ''), b.service_room) AS room,
			o.pet, o.pet_name, o.status, o.boarding_type, o.service_room,
			o.joined_at, o.departed_at, o.death_record, o.departure_note, o.idx,
			b.name AS boarding, b.record_status
		FROM `tab{OCCUPANT_DOCTYPE}` o
		JOIN `tabPet Boarding` b ON b.name = o.parent
		WHERE o.parenttype = 'Pet Boarding'
		  AND o.status = %(active)s
		  AND b.docstatus < 2
		  AND b.record_status IN %(room_holding)s
		  AND COALESCE(NULLIF(o.service_room, ''), b.service_room) IN %(rooms)s
		ORDER BY b.name ASC, o.idx ASC
		""",
		{
			"active": ACTIVE_OCCUPANT_STATUS,
			"room_holding": ROOM_ASSIGNED_ACTIVE_BOARDING_STATUSES,
			"rooms": tuple(room_names),
		},
		as_dict=True,
	)
	if not rows:
		return {}

	pet_details = _resolve_pet_details(rows)
	booking_names = sorted({row.boarding for row in rows})
	# One fetch for every booking on the page. Keyed so a room can name several.
	records = {
		r.name: r
		for r in frappe.get_all(
			"Pet Boarding", filters={"name": ["in", booking_names]}, fields=BOARDING_RECORD_FIELDS
		)
	}

	state: dict = {}
	for row in rows:
		entry = state.setdefault(row.room, {"occupants": [], "booking_names": []})
		payload = _occupant_payload(row, pet_details)
		# The room the hub filed this pet under, so a blank column does not read as null
		# on a card that is definitionally about that room.
		payload["service_room"] = row.room
		payload["boarding"] = row.boarding
		entry["occupants"].append(payload)
		if row.boarding not in entry["booking_names"]:
			entry["booking_names"].append(row.boarding)

	for room, entry in state.items():
		entry["bookings"] = [records[name] for name in entry["booking_names"] if name in records]
	return state


def _primary_booking_for_room(room: str, entry: dict):
	"""The booking a room card names as context, when more than one has pets in it.

	Several bookings in one room is reachable - the one-guardian rule permits a guardian's
	two bookings to share, and a transfer can move a pet into a room another booking holds.
	The old map used `setdefault`, so the second booking was silently dropped and its
	animals became invisible. They are all returned in `bookings` now; this only picks which
	one fills the compatibility fields.

	Preference: the booking that calls this room its own, then the one with the most pets
	here, then by name so the answer never depends on row order.
	"""
	bookings = entry.get("bookings") or []
	if not bookings:
		return None
	if len(bookings) == 1:
		return bookings[0]

	counts = {}
	for occupant in entry.get("occupants") or []:
		counts[occupant.get("boarding")] = counts.get(occupant.get("boarding"), 0) + 1

	return sorted(
		bookings,
		key=lambda row: (
			0 if cstr(row.service_room).strip() == room else 1,
			-counts.get(row.name, 0),
			cstr(row.name),
		),
	)[0]


def _room_occupancy_from_state(entry: dict | None) -> str:
	"""Free, held, or inhabited - decided by the pets in the room.

	`room_is_free` is the single definition and this adopts it rather than inventing a
	third: a room is free when NO PET REMAINS IN IT, not when a booking ends. One pet
	leaving a shared room frees nothing; the last one leaving frees it.

	Reserved stays distinct because a room held for animals that have not arrived is not
	free either - it just has nobody in it yet. The split is taken from the booking's
	record_status rather than from `joined_at`, because a reservation may legitimately
	carry a future arrival time and that is still a held room, not an occupied one.
	"""
	entry = entry or {}
	if not (entry.get("occupants") or []):
		return "Available"
	# Read from the BOOKINGS, not the occupant payloads. The payload is the published Part C
	# shape and deliberately carries no booking status; reading a key it does not have made
	# every occupied room report Reserved. Every booking in this list has at least one pet
	# in this room by construction, so checking them is checking the room.
	if any(cstr(record.record_status).strip() == "Checked In" for record in entry.get("bookings") or []):
		return "Occupied"
	return "Reserved"


def _serialize_room(room, boarding=None, entry: dict | None = None) -> dict:
	"""One room card. Room-scoped: the pets IN THIS ROOM, with the booking as context.

	`occupants` and `active_occupant_count` are the room's, never the booking's. A booking
	split across two rooms appears on both cards, each listing only its own animals - two
	pets on one and five on the other, not seven on both.

	`entry` is this room's slice of `_get_room_occupancy_state`. The hub passes it, having
	fetched every room in one query; a single-room caller may omit it and have it looked up.

	The legacy `pet` / `pet_id` / `pet_name` and `active_boarding` stay for compatibility,
	but their MEANING has moved with the grain: `pet` is now the first occupant OF THIS
	ROOM, not the booking's nominal pet. That was the bug worth fixing on its own - on a
	split booking the old fields named whichever pet was reserved first, which after a
	transfer is frequently an animal that is no longer in the room being drawn.
	"""
	if entry is None:
		entry = _get_room_occupancy_state([room.name]).get(room.name)

	occupants = (entry or {}).get("occupants") or []
	bookings = (entry or {}).get("bookings") or []
	primary = _primary_booking_for_room(room.name, entry or {}) or boarding

	row = {
		"name": room.name,
		"room_id": room.name,
		"room_code": room.room_code,
		"room_name": room.room_name,
		"room_type": room.room_type,
		"image": room.image,
		"status": room.status,
		"room_status": room.status,
		"notes": room.notes,
		"occupancy": _room_occupancy_from_state(entry),
		# The card's subject.
		"occupants": occupants,
		"active_occupant_count": len(occupants),
		# The denominator, so the hub can render `3 / 5` and have it be true. Pairs with
		# active_occupant_count and is scoped the same way - both are THIS ROOM's, never the
		# booking's, so a booking split across two rooms fills each card against its own
		# room's capacity rather than showing seven-of-something twice.
		#
		# Display only in this change: nothing refuses a pet for exceeding it. So
		# active_occupant_count CAN exceed capacity, and a client must render that rather
		# than assume the ratio is bounded - clamping it would hide the exact overfill the
		# number exists to reveal.
		"capacity": _room_capacity(room),
		# Every booking with a pet in this room, not just the first. `setdefault` used to
		# drop the second one and its animals with it.
		"bookings": [_serialize_boarding_record(record) for record in bookings],
		"booking_count": len(bookings),
		"active_boarding": None,
		"boarding_id": None,
	}

	if primary is not None:
		boarding_data = _serialize_boarding_record(primary)
		first = occupants[0] if occupants else None
		row.update(
			{
				"active_boarding": boarding_data,
				"boarding_id": primary.name,
				"record_status": primary.record_status,
				# First occupant OF THIS ROOM - see the docstring. Falls back to the
				# booking's nominal pet only when the room has no occupant rows at all.
				"pet": (first or {}).get("pet") or primary.pet,
				"pet_id": (first or {}).get("pet") or primary.pet,
				"pet_name": (first or {}).get("pet_name") or boarding_data.get("pet_name"),
				"guardian": primary.guardian,
				"guardian_id": primary.guardian,
				"guardian_name": boarding_data.get("guardian_name"),
				"customer": primary.customer,
				"boarding_type": primary.boarding_type,
				"reserved_at": primary.reserved_at,
				"check_in": primary.check_in,
				"check_out": primary.check_out,
				"billing_status": primary.billing_status,
				"sales_invoice": primary.sales_invoice,
			}
		)
	return row


def _serialize_detail(room, boarding=None) -> dict:
	"""The detail payload: the BOOKING at the top level, the ROOM under `room`.

	Both grains are present and they answer different questions, which matters once a
	booking is split. Top-level `occupants` / `active_occupant_count` come from
	_serialize_boarding_doc and are the booking's whole roster. `room.occupants` /
	`room.active_occupant_count` are only the pets in that one room.

	`occupancy` is deliberately NOT recomputed from `record_status` here any more. It is
	whatever _serialize_room decided, which is the same room_is_free definition the hub and
	every write-side availability check use - one definition, three readers.
	"""
	room_card = _serialize_room(room, boarding) if room else {}
	detail = dict(room_card)
	detail["billable_items"] = []
	detail["permissions"] = _boarding_permissions(boarding)

	if boarding:
		boarding_data = _serialize_boarding_doc(boarding)
		occupancy = detail.get("occupancy")
		detail.update(boarding_data)
		# Restored after the update, which would otherwise leave the key absent for a
		# roomless booking and stale for one whose room is now read from its occupants.
		detail["occupancy"] = occupancy or _occupancy_from_record_status(boarding.record_status)
		detail["room"] = room_card or None
		detail["boarding"] = boarding_data
		detail["permissions"] = _boarding_permissions(boarding)

	return detail


def _boarding_permissions(boarding=None) -> dict:
	return {
		"can_cancel_boarding": bool(boarding and _can_cancel_boarding_payload(boarding)),
		"can_give_medication": bool(boarding and _user_can_give_medication(boarding)),
		"can_dispense_medication": bool(boarding and _user_can_dispense_medication(boarding)),
	}


def _can_cancel_boarding_payload(boarding) -> bool:
	try:
		return boarding.record_status in CANCELLABLE_BOARDING_STATUSES and _user_can_cancel_boarding(boarding)
	except Exception:
		return False


def _user_can_give_medication(boarding) -> bool:
	try:
		if boarding.record_status != "Checked In" or boarding.docstatus != 0:
			return False
		if not (_has_boarding_role(*BOARDING_WRITE_ROLES) or _has_doctype_permission("Pet Boarding", "write")):
			return False
		if not _has_doctype_permission("Pet Care Plan Item", "write"):
			return False
		# Every active occupant, not just the scalar pet. On a multi-pet booking this flag
		# described one animal and hid the button for the rest, so a nurse could not mark
		# a medication given for two of three pets in the room.
		for pet in boarding_pets(boarding, active_only=True):
			episode = _active_episode_name_for_pet(pet)
			if episode and _boarding_has_open_medication_plan_item(pet, episode):
				return True
		return False
	except Exception:
		return False


def _user_can_dispense_medication(boarding) -> bool:
	try:
		if boarding.record_status != "Checked In" or boarding.docstatus != 0:
			return False
		if not (_has_boarding_role(*BOARDING_WRITE_ROLES) or _has_doctype_permission("Pet Boarding", "write")):
			return False
		return any(_is_pending_boarding_medication_billable(row) for row in boarding.billable_items or [])
	except Exception:
		return False


def _boarding_has_open_medication_plan_item(pet: str, episode: str) -> bool:
	return bool(
		frappe.get_all(
			"Pet Care Plan Item",
			filters={
				"pet": pet,
				"care_episode": episode,
				"plan_type": ["in", list(MEDICATION_PLAN_TYPES)],
				"status": ["not in", ["Done", "Cancelled", "Converted To Visit"]],
			},
			pluck="name",
			limit_page_length=1,
			ignore_permissions=True,
		)
	)


def _is_pending_boarding_medication_billable(row) -> bool:
	if cstr(row.get("item_type")).strip() != "Medication":
		return False
	if cstr(row.get("status")).strip() in {"Cancelled", "Billed"}:
		return False
	return cstr(row.get("dispense_status")).strip() not in DISPENSE_FINAL_STATUSES


def _serialize_boarding_doc(boarding) -> dict:
	return {
		"name": boarding.name,
		"boarding_id": boarding.name,
		"service_room": boarding.service_room,
		"room_id": boarding.service_room,
		"pet": boarding.pet,
		"pet_id": boarding.pet,
		"pet_name": frappe.db.get_value("Pet", boarding.pet, "pet_name") if boarding.pet else None,
		"guardian": boarding.guardian,
		"guardian_id": boarding.guardian,
		"guardian_name": frappe.db.get_value("Guardian", boarding.guardian, "full_name") if boarding.guardian else None,
		"customer": boarding.customer,
		"boarding_type": boarding.boarding_type,
		"record_status": boarding.record_status,
		"status": boarding.status,
		"workflow_state": boarding.workflow_state,
		"reserved_at": boarding.reserved_at,
		"check_in": boarding.check_in,
		"check_out": boarding.check_out,
		"expected_check_out": boarding.get("expected_check_out"),
		"stay_days": boarding.stay_days,
		"stay_hours": boarding.get("stay_hours"),
		"total_cost": boarding.total_cost,
		"deposit": boarding.deposit,
		"deposit_payment_entry": boarding.get("deposit_payment_entry"),
		"balance": boarding.balance,
		"billing_status": boarding.billing_status,
		"sales_invoice": boarding.sales_invoice,
		"note": boarding.note,
		"boarding_note": boarding.get("boarding_note"),
		"boarded_by": boarding.get("boarded_by"),
			"cancelled_by": boarding.get("cancelled_by"),
			"cancellation_note": boarding.get("cancellation_note"),
		"visit": boarding.get("visit"),
		"notes": boarding.notes,
		"docstatus": boarding.docstatus,
		# The roster. Every reader that shows more than one animal reads THIS, not the
		# `pet`/`pet_id`/`pet_name` fields above - those name the booking's first occupant
		# and describe one animal on a booking that may hold seven.
		"occupants": _serialize_occupants(boarding),
		"active_occupant_count": len(active_occupants(boarding)),
		"billable_items": [_serialize_billable_item(row) for row in boarding.billable_items or []],
	}


def _occupant_payload(row, pet_details: dict) -> dict:
	"""One occupant, in the shape Part C publishes. THE definition, used by every reader.

	Both the booking roster and the room-grained hub go through here, so the two cannot
	drift into describing the same animal differently. Accepts either a child-table row or
	a SQL row - both answer `.get`.

	`pet_name`, `animal_type` and `pet_image` are all resolved from the Pet rather than
	copied off the row. `pet_name` has a column that almost nothing writes, so a raw copy
	would return null for nearly every occupant and a roster would render record IDs; the
	other two have no column on the occupant table at all. `pet_details` is a pre-batched
	lookup so this stays one query for a whole page of rooms rather than one per animal.

	`animal_type` is the meaningful axis - Cat, Dog, Bird, Rabbit, Horse - and is 100%
	populated. `animal_species` is deliberately NOT sent: it is taxonomic class and reads
	`Mammal` for 2,216 of 2,224 pets, so an icon keyed on it would draw one identical glyph
	for 99.6% of the board. It is already load-bearing elsewhere - boarding rates resolve on
	`animal_type` - so this is a maintained field, not a decorative one.

	`pet_image` is nullable and is expected to be null often: 23% of all pets have a photo,
	though boarded animals are photographed far more (19 of the 24 here). A null is the
	client's cue to draw its fallback, which is correct behaviour rather than a gap.
	"""
	pet = cstr(row.get("pet")).strip()
	details = pet_details.get(pet) or {}
	return {
		"pet": row.get("pet"),
		"pet_name": cstr(row.get("pet_name")).strip() or details.get("pet_name") or None,
		"animal_type": details.get("animal_type") or None,
		"pet_image": details.get("pet_image") or None,
		"status": row.get("status"),
		"boarding_type": row.get("boarding_type"),
		"service_room": row.get("service_room"),
		"joined_at": row.get("joined_at"),
		"departed_at": row.get("departed_at"),
		"death_record": row.get("death_record"),
		"departure_note": row.get("departure_note"),
	}


def _resolve_pet_details(rows) -> dict:
	"""One query for EVERY pet on the page. `{pet: {pet_name, animal_type, pet_image}}`.

	Deliberately not filtered to rows missing a cached `pet_name`, which is what this did
	when a name was the only thing it fetched. `animal_type` and `pet_image` have no column
	on the occupant table, so they must be read for every animal - narrowing to the rows
	that lack a name would silently leave a photo and a type off exactly the occupants whose
	name happened to be cached, and those are the majority.

	Still one query per page, now four columns instead of two.
	"""
	pets = {cstr(row.get("pet")).strip() for row in rows if cstr(row.get("pet")).strip()}
	if not pets:
		return {}
	return {
		r.name: {"pet_name": r.pet_name, "animal_type": r.animal_type, "pet_image": r.pet_image}
		for r in frappe.get_all(
			"Pet",
			filters={"name": ["in", list(pets)]},
			fields=["name", "pet_name", "animal_type", "pet_image"],
		)
	}


def _serialize_occupants(boarding) -> list[dict]:
	"""The booking's whole roster, in table order - every pet on the booking.

	NOT the same question as the hub's. This is "who is on this booking"; the hub asks
	"who is in this room", and on a booking split across two rooms the two answers differ.
	"""
	rows = boarding.get("occupants") or []
	if not rows:
		return []
	pet_details = _resolve_pet_details(rows)
	return [_occupant_payload(row, pet_details) for row in rows]


def _serialize_boarding_record(row) -> dict:
	return {
		"name": row.name,
		"boarding_id": row.name,
		"service_room": row.service_room,
		"room_id": row.service_room,
		"room_code": row.get("room_code"),
		"room_name": row.get("room_name"),
		"pet": row.pet,
		"pet_id": row.pet,
		"pet_name": row.get("pet_name") or _get_pet_name(row.pet),
		"pet_image": row.get("pet_image"),
		"guardian": row.guardian,
		"guardian_id": row.guardian,
		"guardian_name": row.get("guardian_name") or _get_guardian_name(row.guardian),
		"guardian_phone": row.get("guardian_phone"),
		"customer": row.customer,
		"boarding_type": row.boarding_type,
		"record_status": row.record_status,
		"status": row.status,
		"workflow_state": row.workflow_state,
		"occupancy": _occupancy_from_record_status(row.record_status),
		"reserved_at": row.reserved_at,
		"check_in": row.check_in,
		"check_out": row.check_out,
		"expected_check_out": row.get("expected_check_out"),
		"stay_days": row.stay_days,
		"stay_hours": row.get("stay_hours"),
		"total_cost": row.total_cost,
		"deposit": row.deposit,
		"deposit_payment_entry": row.get("deposit_payment_entry"),
		"balance": row.balance,
		"billing_status": row.billing_status,
		"sales_invoice": row.sales_invoice,
		"note": row.note,
		"boarding_note": row.get("boarding_note"),
		"boarded_by": row.get("boarded_by"),
			"cancelled_by": row.get("cancelled_by"),
			"cancellation_note": row.get("cancellation_note"),
		"visit": row.get("visit"),
		"notes": row.notes,
		"docstatus": row.docstatus,
		"creation": row.get("creation"),
		"modified": row.get("modified"),
	}


def _serialize_billable_item(row) -> dict:
	return {
		"name": row.name,
		"pet": row.get("pet"),
		"item_name": row.item_name,
		"item_code": row.item_code,
		"item_type": row.item_type,
		"qty": row.qty,
		"rate": row.rate,
		"amount": row.amount,
		"status": row.status,
		"note": row.note,
		"linked_service_id": row.linked_service_id,
		"linked_doctype": row.get("linked_doctype"),
		"linked_name": row.get("linked_name"),
		"order_id": row.get("order_id"),
		"care_episode": row.get("care_episode"),
		"dispense_status": row.get("dispense_status"),
		"dispensed_qty": row.get("dispensed_qty"),
		"dispensed_by": row.get("dispensed_by"),
		"dispensed_at": row.get("dispensed_at"),
		"return_qty": row.get("return_qty"),
		"returned_by": row.get("returned_by"),
		"returned_at": row.get("returned_at"),
		"dose_option": row.get("dose_option"),
		"warehouse": row.get("warehouse"),
		# Echoed exactly as stored so the client can verify the write instead of assuming
		# it. Null until the columns exist - see the note on _serialize_billable_item.
		"dosage": row.get("dosage"),
		"frequency": row.get("frequency"),
		"duration_days": row.get("duration_days"),
		"stock_issued_qty": row.get("stock_issued_qty"),
		"stock_entry": row.get("stock_entry"),
		# Echoed under the same name the client sends, so nothing has to be inferred.
		"scheduled_datetime": row.get("scheduled_datetime"),
		# Read back from the stored row, never from the request, so an assignment that
		# failed to store reads as unassigned rather than as the name still in the form.
		# `provider_name` rides along so the orders panel does not need a second lookup.
		"provider": row.get("provider"),
		"provider_name": row.get("provider_name"),
	}


def _parse_billable_items_payload(payload) -> list[dict]:
	if payload is None:
		return []
	if isinstance(payload, str):
		payload = frappe.parse_json(payload) if payload.strip() else []
	if isinstance(payload, dict):
		for key in ("billable_items", "items", "data"):
			if key in payload:
				payload = payload.get(key)
				break

	if payload is None:
		return []
	if not isinstance(payload, list):
		frappe.throw(_("billable_items must be an array."))

	return payload


# The five link fields `sync_billable_items` writes through verbatim: present in the
# payload means "set to this", absent means "leave alone", and an empty value means
# "clear". Together with item_name, item_code, item_type, qty, rate, amount, status and
# note they are the WHOLE writable surface of this endpoint.
#
# Everything else on the row is server-owned - `pet`, `dose_option`, `warehouse`, the
# schedule (`dosage`, `frequency`, `duration_days`), the dispense trail and the stock
# trail (`stock_issued_qty`, `stock_entry`) - and is written only by the endpoint that
# establishes the fact: create_order, dispense_medication, return_dispensed_medication.
# Billing edits a price. It does not edit a clinical or a stock fact.
_SYNC_PASSTHROUGH_FIELDS = ("linked_service_id", "linked_doctype", "linked_name", "order_id", "care_episode")


def _sync_billable_item_row(boarding, raw_row, idx: int, existing_by_name: dict, existing_by_order_id: dict, claimed_names: set):
	"""Match one payload row to its existing child row and write ONLY what it carries.

	The row is MUTATED IN PLACE. This used to build a fresh dict and hand that to
	`boarding.set()`, which is where the data loss lived: `_init_child` constructs the
	child from that dict alone, never reading the stored row, and `db_update` then writes
	EVERY column of the table. A field the dict did not mention was therefore set to NULL
	on every sync. `dosage`, `frequency` and `duration_days` were rescued one at a time by
	copying them back out of the existing row; `pet`, `dose_option`, `warehouse`, the
	return trail and the stock trail were not, and were being erased. Enumerating the
	survivors is the wrong shape - it fails silently every time a column is added, which
	is exactly how those five were missed. Preservation is now structural: the object
	that reaches `db_update` IS the object loaded from the database.

	The cost of losing them is not cosmetic. `returnable_stock_qty` divides by
	`stock_issued_qty`, so a zeroed one puts NOTHING back in the warehouse while the row
	still records the return - the pharmacy log and the shelf disagree, with no error. On
	the shared Vet Visit path the invoice-side suppression keys on the same number, and a
	zeroed one deducts the medication a second time at the till.

	Identity is `name`, falling back to `order_id` for a payload that omits it. Both are
	stable: a Frappe child row keeps its name for its whole life, and `order_id` is
	generated once when the order is raised. The regeneration seen today is not the name
	changing - it is this function deleting the row and inserting a new one whenever the
	client did not echo `name` back, which is precisely what mutating in place removes.
	A row matching neither key is new and is appended.
	"""
	if not isinstance(raw_row, dict):
		frappe.throw(_("Billable item row {0} must be an object.").format(idx))

	row_name = cstr(raw_row.get("name")).strip()
	existing_row = None
	if row_name:
		existing_row = existing_by_name.get(row_name)
		if not existing_row:
			frappe.throw(_("Billable item row {0} does not belong to this Pet Boarding.").format(row_name))
	else:
		# No name, but a generated order id still identifies the row it belongs to. Without
		# this a client that echoes only `order_id` would duplicate every order row and
		# strand the original's dispense trail on a row about to be deleted.
		existing_row = existing_by_order_id.get(cstr(raw_row.get("order_id")).strip()) or None

	if existing_row is not None:
		if existing_row.name in claimed_names:
			frappe.throw(_("Duplicate billable item row {0}.").format(existing_row.name))
		claimed_names.add(existing_row.name)

	item_code = cstr(raw_row.get("item_code")).strip()
	if not item_code and existing_row is not None:
		item_code = cstr(existing_row.item_code).strip()
	if not item_code:
		frappe.throw(_("Billable item row {0} is missing Item Code.").format(idx))

	# Looked up only for a new row or one being re-pointed at a different Item. An
	# unchanged item_code needs nothing from the Item master, and re-reading it would make
	# an unrelated price edit fail on a row whose Item was since disabled or deleted.
	item = None
	if existing_row is None or item_code != cstr(existing_row.item_code).strip():
		item = _get_item_details(item_code)
		item_code = item.get("item_code")

	status = cstr(raw_row.get("status")).strip()
	if not status:
		status = cstr(existing_row.status if existing_row is not None else "").strip() or "Billable"
	if status not in BILLABLE_ITEM_STATUSES:
		frappe.throw(_("Invalid billable item status {0}.").format(frappe.bold(status)))

	item_type = cstr(raw_row.get("item_type")).strip()
	if not item_type:
		item_type = cstr(existing_row.item_type if existing_row is not None else "").strip() or "Service"
	if item_type not in BILLABLE_ITEM_TYPES:
		frappe.throw(_("Invalid billable item type {0}.").format(frappe.bold(item_type)))

	qty = _coerce_positive_float(
		raw_row.get("qty") if raw_row.get("qty") is not None else (existing_row.qty if existing_row is not None else 1),
		_("Billable item {0} has invalid quantity.").format(item_code),
	)
	rate_value = raw_row.get("rate")
	if rate_value is None:
		rate_value = existing_row.rate if existing_row is not None else (item or {}).get("rate")
	rate = _coerce_non_negative_float(
		rate_value,
		_("Billable item {0} has invalid rate.").format(item_code),
	)

	row = existing_row if existing_row is not None else boarding.append("billable_items", {})

	row.item_code = item_code
	row.item_type = item_type
	row.status = status
	row.qty = qty
	row.rate = rate
	row.amount = flt(raw_row.get("amount") if raw_row.get("amount") is not None else qty * rate)

	item_name = cstr(raw_row.get("item_name")).strip()
	if item_name:
		row.item_name = item_name
	elif not cstr(row.get("item_name")).strip():
		row.item_name = (item or _get_item_details(item_code)).get("item_name")

	if "note" in raw_row:
		row.note = raw_row.get("note")
	elif existing_row is None:
		row.note = ""

	for fieldname in _SYNC_PASSTHROUGH_FIELDS:
		if fieldname in raw_row:
			_set_child_value_if_field(row, fieldname, cstr(raw_row.get(fieldname)).strip())
		elif existing_row is None:
			_set_child_value_if_field(row, fieldname, "")

	# A row that becomes a medication needs somewhere for the dispense to start. The
	# controller applies the same default on validate; setting it here keeps the echoed
	# response honest for a brand new row. An existing row's dispense_status is never
	# touched - it is the dispense path's to move.
	if item_type == "Medication" and not cstr(row.get("dispense_status")).strip():
		_set_child_value_if_field(row, "dispense_status", "Pending Dispense")

	return row


def _coerce_scheduled_datetime(value):
	"""A scheduled moment, or None. NEVER a default.

	Absence is the normal case and stays valid: most orders are "do it now", and the
	overwhelming majority of rows will hold null forever. That is why nothing here falls
	back to `now_datetime()` - a stamp written automatically is indistinguishable from one
	a person chose, and the moment every row carries one, nothing can tell a scheduled
	order from an unscheduled one. That failure is silent and permanent, which is exactly
	what this field exists to prevent.

	An empty string, whitespace and null all mean "not scheduled" and return None, so a
	client clearing the picker does not have to send a different shape than one that never
	set it. Anything present but unparseable throws rather than being dropped: a schedule
	that silently vanished would have the client showing a time the record does not hold.
	"""
	if value is None:
		return None
	raw = cstr(value).strip()
	if not raw:
		return None
	try:
		parsed = get_datetime(raw)
	except Exception:
		parsed = None
	if not parsed:
		frappe.throw(
			_("Scheduled Datetime must be a valid date and time. Got {0}.").format(frappe.bold(raw))
		)
	return parsed


def _coerce_provider(value):
	"""A Healthcare Practitioner docname, or None. NEVER a default.

	The canonical identifier is the Healthcare Practitioner NAME (`HCP-#####`), because
	that is what `PetCareService.provider` and `Pet Procedure.provider` are Links to and
	what every existing row on both holds. A User email is accepted as a convenience and
	resolved through `user_id`, so a client that has only the login of the person still
	gets the assignment stored rather than an error - but it is resolved, not stored, and
	what comes back is always the docname.

	Absence stays valid and is the ordinary case: an unassigned order means "whoever is
	free". Nothing here falls back to `frappe.session.user` - a defaulted assignee is
	indistinguishable from a real one, and the desk would lose the ability to see what
	still needs assigning. Same rule as `scheduled_datetime`, for the same reason.

	Anything present but unresolvable throws rather than being dropped. An assignment that
	silently vanished is the whole defect this parameter exists to close: the operator
	picked a name, the dialog accepted it, and the record never held it.
	"""
	if value is None:
		return None
	raw = cstr(value).strip()
	if not raw:
		return None
	if frappe.db.exists("Healthcare Practitioner", raw):
		return raw
	# Only ever consulted as a fallback, and only when it identifies exactly one
	# practitioner. Two practitioners sharing a login is a data fault, and guessing which
	# of them was meant would store an assignment nobody made.
	matches = frappe.get_all(
		"Healthcare Practitioner", filters={"user_id": raw}, pluck="name", limit=2
	)
	if len(matches) == 1:
		return matches[0]
	if len(matches) > 1:
		frappe.throw(
			_("{0} matches more than one Healthcare Practitioner. Send the practitioner ID instead.").format(
				frappe.bold(raw)
			)
		)
	frappe.throw(
		_("Provider {0} is not a Healthcare Practitioner.").format(frappe.bold(raw))
	)


def _coerce_duration_days(value) -> int:
	"""Whole days, positive. Floors a fractional value rather than refusing it.

	Floor and not round: "5.5 days" from a UI stepper means five full days and part of a
	sixth, and billing a sixth day nobody ordered is the worse error. Zero and negative are
	refused outright - a course cannot run for no days, and a caller sending 0 has a bug
	rather than an intention. Omitting the field entirely is the supported way to say "no
	duration", and stays valid.
	"""
	raw = cstr(value).strip()
	try:
		days = int(float(raw))
	except (TypeError, ValueError):
		frappe.throw(_("Duration (days) must be a whole number of days. Got {0}.").format(frappe.bold(raw)))
	if days <= 0:
		frappe.throw(_("Duration (days) must be greater than zero. Got {0}.").format(frappe.bold(raw)))
	return days


def _coerce_positive_float(value, message: str) -> float:
	value = flt(value)
	if value <= 0:
		frappe.throw(message)
	return value


def _coerce_non_negative_float(value, message: str) -> float:
	value = flt(value)
	if value < 0:
		frappe.throw(message)
	return value


def _matches_occupancy(current: str, requested: str | None) -> bool:
	if not requested:
		return True
	return cstr(current).lower() == cstr(requested).lower()


def _occupancy_from_record_status(record_status: str | None) -> str:
	if record_status == "Reserved":
		return "Reserved"
	if record_status == "Checked In":
		return "Occupied"
	return "Available"


def _resolve_guardian_for_pet(pet_id: str, guardian_id: str | None) -> str:
	if not frappe.db.exists("Pet", pet_id):
		frappe.throw(_("Pet {0} was not found.").format(frappe.bold(pet_id)))

	guardian_id = _resolve_guardian_id_from_any_identity(guardian_id) if guardian_id else _get_primary_guardian_id(pet_id)
	if not guardian_id:
		frappe.throw(_("Guardian is required for Pet {0}.").format(frappe.bold(pet_id)))

	if not frappe.db.exists("PetGuardian", {"pet_id": pet_id, "guardian_id": guardian_id}):
		frappe.throw(
			_("Pet {0} is not linked to Guardian {1}.").format(
				frappe.bold(pet_id), frappe.bold(guardian_id)
			)
		)

	return guardian_id


def _resolve_guardian_id_from_any_identity(identity: str | None) -> str | None:
	identity = cstr(identity).strip()
	if not identity:
		return None
	if frappe.db.exists("Guardian", identity):
		return identity
	if frappe.db.exists("Customer", identity):
		guardian = frappe.db.get_value("Guardian", {"customer_id": identity}, "name")
		if guardian:
			return guardian
	return get_guardian_record(identity).get("name")


def _get_primary_guardian_id(pet_id: str) -> str | None:
	guardian_id = frappe.db.get_value("PetGuardian", {"pet_id": pet_id, "role": "primary_owner"}, "guardian_id")
	if guardian_id:
		return guardian_id
	return frappe.db.get_value("PetGuardian", {"pet_id": pet_id}, "guardian_id")


def _normalize_boarding_type(boarding_type: str | None) -> str:
	if boarding_type in BOARDING_TYPES:
		return boarding_type
	return frappe.db.get_single_value("Pet Boarding Settings", "default_boarding_type") or "Travel"


def _resolve_visit_boarding_type(boarding_type=None) -> str:
	"""The type for a visit-started stay: the operator's choice, or the suggested default.

	Deliberately NOT _normalize_boarding_type, which silently falls back to the settings
	default for anything it does not recognise. Here that would turn a mistyped "Medical"
	into a Travel stay billed at the travel rate - the same silent switch this endpoint
	used to perform, only in the other direction and harder to spot. An unrecognised value
	is refused instead, so a bad caller fails loudly rather than cheaply.
	"""
	chosen = cstr(boarding_type).strip()
	if not chosen:
		return VISIT_BOARDING_SUGGESTED_TYPE
	if chosen not in BOARDING_TYPES:
		frappe.throw(
			_("Invalid boarding type {0}. Expected one of: {1}.").format(
				frappe.bold(chosen), ", ".join(BOARDING_TYPES)
			)
		)
	return chosen


def _coerce_datetime(value):
	if not value:
		return None
	return get_datetime(value)


def _room_stay_note(boarding_type: str) -> str:
	return _("Auto-added {0} boarding room stay.").format(boarding_type)


def _is_generated_room_stay_note(note) -> bool:
	"""Whether this note is still one we wrote, and therefore safe to rewrite.

	The note reaches the customer: _build_sales_invoice_items uses `row.note or
	row.item_name` as the invoice description. So a row re-pointed at the other boarding
	type has to have its description corrected too - but not at the cost of discarding a
	note a human typed through sync_billable_items.
	"""
	current = cstr(note).strip()
	return not current or any(current == _room_stay_note(known) for known in BOARDING_TYPES)


def _room_stay_service_id_for(pet: str, ordinal: int = 1) -> str:
	"""Identity of one occupant's room-stay row. Per OCCUPANT, not per pet.

	Keyed on the pet, then on which stay of that pet's this is. The old
	`boarding_room_stay:travel` key assumed one room-stay row per booking and broke when a
	booking held several pets; keying on the pet alone then broke again when `add_occupant`
	let a pet leave and come back, because both of its stays produced the identical string.
	Two rows with one key do not stay apart: on BRD-00103 they were held apart only by the
	order they happened to sit in the child table, so one froze at its reserve-time value
	while the other reconciled.

	The first stay keeps the bare `boarding_room_stay:PET-00042`, so every one of the 66
	room-stay rows on this site keeps the key it already has and nothing needs re-keying.
	Only a second or later stay takes a suffix - `boarding_room_stay:PET-00042#2`.

	The ordinal is the occupant's position among that pet's rows on this booking, counted
	in table order. It is stable because occupant rows are append-only: a pet that leaves
	is closed by status, never removed, so no later row can renumber an earlier one. It is
	deliberately not the occupant row's `name` - a row appended by `reserve_room` or
	`add_occupant` has no name yet at the moment this runs, because both reconcile the
	charges before the parent is saved.
	"""
	base = f"{ROOM_STAY_SERVICE_PREFIX}:{pet}"
	return base if cint(ordinal) <= 1 else f"{base}#{cint(ordinal)}"


def _find_occupant_room_stay_row(boarding, pet: str, claimed: set, service_id: str, ordinal: int = 1):
	"""THIS occupant's room-stay row, adopting a legacy one where that is unambiguous.

	Three ways a row is recognised, in order:
	  1. its `linked_service_id` equals this occupant's - an exact identity match, and the
	     only test that can tell one stay of a pet from another
	  2. its `pet` column, but ONLY for a pet's first stay, and only for a row that carries
	     no key of its own. This is the compatibility path for rows written before the key
	     existed; it is deliberately unavailable to a second stay
	  3. a single un-owned legacy row, adopted only when this booking has exactly one
	     occupant, so an old `boarding_room_stay:travel` row is inherited rather than
	     duplicated

	Matching on `pet` used to come FIRST, and that is what let two stays of one pet be
	paired with two charges purely by the order they sat in the child table - nothing linked
	a charge to the period it billed. Reordering the rows silently re-paired them. The
	identity match now decides, and `claimed` only guards against one row serving twice.

	Cancelled rows are never returned. Reviving a charge somebody deliberately removed is
	worse than adding a new one they can see.
	"""
	legacy = None
	legacy_count = 0
	fallback = None
	for row in boarding.billable_items or []:
		if row.item_type != "Room Stay" or row.name in claimed:
			continue
		if cstr(row.status).strip() == "Cancelled":
			continue
		if cstr(row.linked_service_id).strip() == service_id:
			return row
		if (
			cint(ordinal) <= 1
			and fallback is None
			and cstr(row.get("pet")).strip() == pet
			and not cstr(row.linked_service_id).strip()
		):
			fallback = row
		if not cstr(row.get("pet")).strip():
			legacy = legacy or row
			legacy_count += 1
	if fallback is not None:
		return fallback
	if legacy is not None and legacy_count == 1 and len(billable_occupants(boarding)) == 1:
		return legacy
	return None


def _synthetic_occupant(boarding):
	"""A stand-in occupant for a booking that has none.

	Pre-Stage-2 records and anything the backfill has not reached. Without it those
	bookings would silently stop being charged for their room, which is a worse failure
	than carrying a compatibility branch until `pet` is dropped in Stage 6.
	"""
	return frappe._dict(
		pet=boarding.get("pet"),
		status="Active",
		boarding_type=boarding.boarding_type,
		joined_at=boarding.get("check_in"),
		departed_at=boarding.get("check_out"),
	)


def _ensure_room_stay_billable_item(boarding, *, add_if_missing: bool = True):
	"""One room-stay row per occupant, each priced from the catalogue for its own pet.

	The room is not an input. Rates come from the pet's `animal_type` and the occupant's
	boarding type, and from nothing else - a transfer moves an animal between rooms that
	cost the same, so no figure may move. `Pet Boarding Room Stint` rows sit right there
	with dates on them and are deliberately not consulted.

	A rate already on a row is never overwritten. Staff correct charges by hand - deleting
	the auto row and entering their own qty and rate - and the settled model does not
	support a mid-stay type change, so there is no case in which this function should
	reprice something that already has a price.
	"""
	boarding.run_method("_compute_stay_days")

	occupants = billable_occupants(boarding) or [_synthetic_occupant(boarding)]
	claimed: set = set()
	# Which stay of this pet's we are on. Counted in table order, so a pet's first row is
	# ordinal 1 and keeps the bare key it has always had.
	seen_per_pet: dict = {}

	for occupant in occupants:
		pet = cstr(occupant.get("pet")).strip()
		if not pet:
			continue

		ordinal = seen_per_pet.get(pet, 0) + 1
		seen_per_pet[pet] = ordinal
		service_id = _room_stay_service_id_for(pet, ordinal)

		row = _find_occupant_room_stay_row(boarding, pet, claimed, service_id, ordinal)
		if row is None and not add_if_missing:
			# Check-out passes add_if_missing=False: it reconciles what is there and does
			# not invent a charge for an occupant nobody billed.
			continue

		nights = occupant_nights(occupant)
		boarding_type = cstr(occupant.get("boarding_type")).strip() or boarding.boarding_type

		if row is not None:
			claimed.add(row.name)
			row.item_type = "Room Stay"
			row.pet = pet
			row.qty = nights
			row.linked_service_id = service_id
			row.status = "Billable" if row.status == "Draft" else row.status or "Billable"
			if flt(row.rate) > 0:
				# Priced already - by us or by a human. Left exactly as it is.
				continue
			rate = resolve_boarding_rate(pet, boarding_type)
			row.item_code = rate.item_code
			row.item_name = row.item_name or rate.service_name
			row.rate = rate.rate
			if _is_generated_room_stay_note(row.get("note")):
				row.note = _room_stay_note(boarding_type)
			continue

		rate = resolve_boarding_rate(pet, boarding_type)
		created = boarding.append(
			"billable_items",
			{
				"pet": pet,
				"item_name": rate.service_name,
				"item_code": rate.item_code,
				"item_type": "Room Stay",
				"qty": nights,
				"rate": rate.rate,
				"status": "Billable",
				"note": _room_stay_note(boarding_type),
				"linked_service_id": service_id,
			},
		)
		claimed.add(created.name)


def _build_sales_invoice_items(boarding) -> list[dict]:
	items = []
	for row in boarding.billable_items or []:
		# Included rows are absorbed by the medical boarding rate. They keep their real
		# price on the record and never reach the customer's invoice.
		if row.status in NON_INVOICED_STATUSES:
			continue
		if not row.item_code:
			frappe.throw(_("Billable item row {0} is missing Item Code.").format(row.idx))
		if not frappe.db.exists("Item", row.item_code):
			frappe.throw(_("Item {0} was not found.").format(frappe.bold(row.item_code)))
		if flt(row.qty) <= 0:
			frappe.throw(_("Billable item {0} has invalid quantity.").format(row.item_code))

		items.append(
			{
				"item_code": row.item_code,
				"qty": flt(row.qty),
				"rate": flt(row.rate or 0),
				"description": row.note or row.item_name,
			}
		)
	return items


def _get_item_details(item_code: str) -> dict:
	item = frappe.db.get_value("Item", item_code, ["name", "item_name", "stock_uom", "standard_rate"], as_dict=True)
	if not item:
		frappe.throw(_("Item {0} was not found.").format(frappe.bold(item_code)))

	rate = frappe.db.get_value(
		"Item Price",
		{"item_code": item_code, "price_list": get_veterinary_selling_price_list(), "selling": 1},
		"price_list_rate",
	)
	if rate is None:
		rate = item.standard_rate or 0

	return {
		"item_code": item.name,
		"item_name": item.item_name,
		"uom": item.stock_uom,
		"rate": flt(rate or 0),
	}


def _sanitize_order_by(order_by: str) -> str:
	allowed = {
		"modified desc": "pb.modified DESC",
		"modified asc": "pb.modified ASC",
		"creation desc": "pb.creation DESC",
		"creation asc": "pb.creation ASC",
		"check_in desc": "pb.check_in DESC",
		"check_in asc": "pb.check_in ASC",
		"check_out desc": "pb.check_out DESC",
		"check_out asc": "pb.check_out ASC",
		"total_cost desc": "pb.total_cost DESC",
		"total_cost asc": "pb.total_cost ASC",
		"record_status asc": "pb.record_status ASC",
		"record_status desc": "pb.record_status DESC",
	}
	return allowed.get(cstr(order_by).strip().lower(), "pb.modified DESC")


def _get_pet_name(pet_id: str | None) -> str | None:
	if not pet_id:
		return None
	return frappe.db.get_value("Pet", pet_id, "pet_name")


def _get_guardian_name(guardian_id: str | None) -> str | None:
	if not guardian_id:
		return None
	return frappe.db.get_value("Guardian", guardian_id, "full_name")


@contextmanager
def _service_room_lock(service_room: str):
	lock_name = f"service_room_boarding::{service_room}"
	acquired = False
	try:
		result = frappe.db.sql("SELECT GET_LOCK(%s, %s)", (lock_name, LOCK_TIMEOUT_SECONDS))
		acquired = bool(result and result[0] and cint(result[0][0]) == 1)
		if not acquired:
			frappe.throw(_("Could not acquire room lock. Please retry."))
		yield
	finally:
		if acquired:
			frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_name,))
