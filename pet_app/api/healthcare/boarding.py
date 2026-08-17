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
	billable_occupants,
	boarding_lock,
	find_active_boarding_for_pet,
	get_max_pets_per_booking,
	occupant_nights,
	open_stint,
	pet_locks,
)
from pet_app.utils.boarding_pricing import resolve_boarding_rate
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
	active_by_room = _get_active_boardings_by_room([room.name for room in rooms])

	data = []
	for room in rooms:
		boarding = active_by_room.get(room.name)
		row = _serialize_room(room, boarding)
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
		if not frappe.db.exists("PetGuardian", {"pet_id": pet, "guardian_id": guardian_id}):
			frappe.throw(
				_("Pet {0} is not linked to Guardian {1}.").format(frappe.bold(pet), frappe.bold(guardian_id))
			)
		active = find_active_boarding_for_pet(pet)
		if active:
			frappe.throw(
				_("Pet {0} is already on boarding {1} ({2}).").format(
					frappe.bold(pet), frappe.bold(active["name"]), frappe.bold(active["record_status"])
				)
			)


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


# How far above the expected cost of the stay a deposit may go before it is refused.
#
# Not a cap at the expected cost: the owner's rule is that a deposit MAY exceed the final
# bill - a guest who leaves early pays less than they put down, and the remainder stays as
# credit. Capping at the estimate would refuse the very case the design is built to handle.
#
# Doubling catches what is actually worth catching, which is a keying slip: an extra zero
# is 10x and is refused, while every plausible real deposit is allowed through. Expressed
# as a multiple of the stay rather than a fixed number of dinars so it keeps working when
# prices change.
DEPOSIT_MAX_MULTIPLE_OF_EXPECTED_STAY = 2


def _validate_deposit_amount(boarding, amount: float) -> float:
	"""Positive, and not wildly out of proportion to the stay. Returns the amount."""
	amount = flt(amount)
	if amount < 0:
		frappe.throw(_("Deposit cannot be negative."))
	if amount == 0:
		return 0.0

	expected = _expected_stay_cost(boarding)
	if expected > 0:
		ceiling = expected * DEPOSIT_MAX_MULTIPLE_OF_EXPECTED_STAY
		if amount > ceiling:
			frappe.throw(
				_(
					"Deposit {0} is more than {1}x the expected cost of this stay ({2}). "
					"Check the amount; a deposit larger than the final bill is fine, but this looks like a typing error."
				).format(
					frappe.bold(frappe.format_value(amount, {"fieldtype": "Currency"})),
					DEPOSIT_MAX_MULTIPLE_OF_EXPECTED_STAY,
					frappe.bold(frappe.format_value(expected, {"fieldtype": "Currency"})),
				)
			)
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
	seen_names = set()
	normalized_rows = []

	for idx, raw_row in enumerate(incoming_rows, start=1):
		normalized_row = _normalize_billable_item_row(raw_row, idx, existing_by_name, seen_names)
		normalized_rows.append(normalized_row)

	boarding.set("billable_items", normalized_rows)
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
):
	"""Raise a single lab / imaging / care-service order for a checked-in boarding.

	The visit order flow is visit-scoped; this is boarding's own entry point. The frontend
	sends one call per selected order. The created order is linked back to the boarding via
	``source_doctype``/``source_name`` and a matching ``billable_items`` row is appended so
	it settles through the existing checkout invoice path.

	``pet`` is REQUIRED and names the animal the order is for. A stay holds one pet today,
	so it can only be that pet - but see _resolve_order_pet for why it is not defaulted.
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
		boarding, kind, pet=order_pet, care_service=care_service, item_code=item_code, priority=priority, note=note
	)
	_append_boarding_order_billable(boarding, kind, order, care_service=care_service, item_code=item_code, note=note)
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

	Stage 2 replaces the membership test below with a lookup against the occupant table.
	Nothing else about this function changes.
	"""
	chosen = cstr(pet).strip()
	if not chosen:
		frappe.throw(_("Pet is required. Send the pet this order is for."))
	if chosen != cstr(boarding.pet).strip():
		frappe.throw(
			_("Pet {0} is not on Pet Boarding {1}.").format(
				frappe.bold(chosen), frappe.bold(boarding.name)
			)
		)
	return chosen


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


def _create_boarding_order_doc(boarding, kind, *, pet, care_service, item_code, priority, note):
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
	doc.insert(ignore_permissions=True)
	doc.add_comment(
		"Comment", _("Created from Pet Boarding {0} by {1}.").format(boarding.name, frappe.session.user)
	)
	return doc


def _append_boarding_order_billable(boarding, kind, order, *, care_service, item_code, note):
	resolved_item, rate, item_name = _resolve_order_billing(order, care_service, item_code)
	boarding.append(
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
	if plan.pet != boarding.pet:
		return _boarding_validation_error(_("Plan item does not belong to the boarded pet."))

	# The plan item's own pet, not the stay's. Identical today - the guard above just
	# proved they are equal - but it is the plan item that names the animal, and that
	# stays true once a stay holds several.
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


def _get_active_service_rooms(search=None):
	values = {}
	conditions = ["status = 'Active'"]
	if search:
		values["search"] = f"%{search}%"
		conditions.append("(name LIKE %(search)s OR room_code LIKE %(search)s OR room_name LIKE %(search)s OR room_type LIKE %(search)s)")

	return frappe.db.sql(
		f"""
		SELECT name, room_code, room_name, room_type, image, status, notes
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

	return frappe.db.get_value(
		"Service Room",
		room_name,
		["name", "room_code", "room_name", "room_type", "image", "status", "notes"],
		as_dict=True,
	)


def _get_active_boardings_by_room(room_names: list[str]) -> dict:
	if not room_names:
		return {}

	rows = frappe.get_all(
		"Pet Boarding",
		filters={
			"service_room": ["in", room_names],
			"record_status": ["in", ROOM_ASSIGNED_ACTIVE_BOARDING_STATUSES],
			"docstatus": ["<", 2],
		},
		fields=[
			"name",
			"service_room",
			"pet",
			"guardian",
			"customer",
			"boarding_type",
			"record_status",
			"status",
			"workflow_state",
			"reserved_at",
			"check_in",
			"check_out",
			"stay_days",
			"stay_hours",
			"total_cost",
			"deposit",
			"deposit_payment_entry",
			"balance",
			"billing_status",
			"sales_invoice",
			"note",
			"boarding_note",
			"boarded_by",
			"cancelled_by",
			"cancellation_note",
			"visit",
			"expected_check_out",
			"notes",
			"docstatus",
			"modified",
		],
		order_by="modified desc",
	)

	active_by_room = {}
	for row in rows:
		active_by_room.setdefault(row.service_room, row)
	return active_by_room


def _serialize_room(room, boarding=None) -> dict:
	occupancy = _occupancy_from_record_status(boarding.record_status if boarding else None)
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
		"occupancy": occupancy,
		"active_boarding": None,
		"boarding_id": None,
	}
	if boarding:
		boarding_data = _serialize_boarding_record(boarding)
		row.update(
			{
				"active_boarding": boarding_data,
				"boarding_id": boarding.name,
				"record_status": boarding.record_status,
				"pet": boarding.pet,
				"pet_id": boarding.pet,
				"pet_name": boarding_data.get("pet_name"),
				"guardian": boarding.guardian,
				"guardian_id": boarding.guardian,
				"guardian_name": boarding_data.get("guardian_name"),
				"customer": boarding.customer,
				"boarding_type": boarding.boarding_type,
				"reserved_at": boarding.reserved_at,
				"check_in": boarding.check_in,
				"check_out": boarding.check_out,
				"billing_status": boarding.billing_status,
				"sales_invoice": boarding.sales_invoice,
			}
		)
	return row


def _serialize_detail(room, boarding=None) -> dict:
	detail = _serialize_room(room, boarding) if room else {}
	detail["billable_items"] = []
	detail["permissions"] = _boarding_permissions(boarding)

	if boarding:
		boarding_data = _serialize_boarding_doc(boarding)
		detail.update(boarding_data)
		detail["room"] = _serialize_room(room, boarding) if room else None
		detail["boarding"] = boarding_data
		detail["occupancy"] = _occupancy_from_record_status(boarding.record_status)
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
		episode = _active_episode_name_for_pet(boarding.pet)
		if not episode:
			return False
		return _boarding_has_open_medication_plan_item(boarding.pet, episode)
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
		"billable_items": [_serialize_billable_item(row) for row in boarding.billable_items or []],
	}


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
		"stock_issued_qty": row.get("stock_issued_qty"),
		"stock_entry": row.get("stock_entry"),
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


def _normalize_billable_item_row(raw_row, idx: int, existing_by_name: dict, seen_names: set) -> dict:
	if not isinstance(raw_row, dict):
		frappe.throw(_("Billable item row {0} must be an object.").format(idx))

	row_name = cstr(raw_row.get("name")).strip()
	existing_row = None
	if row_name:
		existing_row = existing_by_name.get(row_name)
		if not existing_row:
			frappe.throw(_("Billable item row {0} does not belong to this Pet Boarding.").format(row_name))
		if row_name in seen_names:
			frappe.throw(_("Duplicate billable item row {0}.").format(row_name))
		seen_names.add(row_name)

	item_code = cstr(raw_row.get("item_code")).strip()
	if not item_code:
		frappe.throw(_("Billable item row {0} is missing Item Code.").format(idx))

	item = _get_item_details(item_code)
	qty = _coerce_positive_float(
		raw_row.get("qty") if raw_row.get("qty") is not None else (existing_row.qty if existing_row else 1),
		_("Billable item {0} has invalid quantity.").format(item_code),
	)
	rate_value = raw_row.get("rate") if raw_row.get("rate") is not None else (
		existing_row.rate if existing_row else item.get("rate")
	)
	rate = _coerce_non_negative_float(
		rate_value,
		_("Billable item {0} has invalid rate.").format(item_code),
	)

	status = cstr(raw_row.get("status")).strip()
	if not status:
		status = cstr(existing_row.status if existing_row else "").strip() or "Billable"
	if status not in BILLABLE_ITEM_STATUSES:
		frappe.throw(_("Invalid billable item status {0}.").format(frappe.bold(status)))

	item_type = cstr(raw_row.get("item_type")).strip()
	if not item_type:
		item_type = cstr(existing_row.item_type if existing_row else "").strip() or "Service"
	if item_type not in BILLABLE_ITEM_TYPES:
		frappe.throw(_("Invalid billable item type {0}.").format(frappe.bold(item_type)))

	row = {
		"item_name": cstr(raw_row.get("item_name")).strip() or item.get("item_name"),
		"item_code": item.get("item_code"),
		"item_type": item_type,
		"qty": qty,
		"rate": rate,
		"amount": flt(raw_row.get("amount") if raw_row.get("amount") is not None else qty * rate),
		"status": status,
		"note": raw_row.get("note") if "note" in raw_row else (existing_row.note if existing_row else ""),
		"linked_service_id": _raw_or_existing(raw_row, existing_row, "linked_service_id"),
		"linked_doctype": _raw_or_existing(raw_row, existing_row, "linked_doctype"),
		"linked_name": _raw_or_existing(raw_row, existing_row, "linked_name"),
		"order_id": _raw_or_existing(raw_row, existing_row, "order_id"),
		"care_episode": _raw_or_existing(raw_row, existing_row, "care_episode"),
	}
	if item_type == "Medication":
		dispense_status = cstr(existing_row.get("dispense_status") if existing_row else "").strip() or "Pending Dispense"
		if dispense_status not in DISPENSE_STATUSES:
			frappe.throw(_("Invalid dispense status {0}.").format(frappe.bold(dispense_status)))
		row.update(
			{
				"dispense_status": dispense_status,
				"dispensed_qty": flt(existing_row.get("dispensed_qty")) if existing_row else 0,
				"dispensed_by": existing_row.get("dispensed_by") if existing_row else None,
				"dispensed_at": existing_row.get("dispensed_at") if existing_row else None,
			}
		)
	if row_name:
		row["name"] = row_name

	return row


def _raw_or_existing(raw_row: dict, existing_row, fieldname: str):
	if fieldname in raw_row:
		return cstr(raw_row.get(fieldname)).strip()
	return cstr(existing_row.get(fieldname)).strip() if existing_row else ""


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


def _room_stay_service_id_for(pet: str) -> str:
	"""Identity of an occupant's room-stay row.

	Keyed on the PET, not the boarding type. The old `boarding_room_stay:travel` key
	assumed one room-stay row per booking; with several pets on one booking two of them
	would collide on it.
	"""
	return f"{ROOM_STAY_SERVICE_PREFIX}:{pet}"


def _find_occupant_room_stay_row(boarding, pet: str, claimed: set):
	"""This occupant's room-stay row, adopting a legacy one where that is unambiguous.

	Three ways a row is recognised, in order:
	  1. its `pet` column - the only one that works once a booking holds several
	  2. its per-pet `linked_service_id`
	  3. a single un-owned legacy row, adopted only when this booking has exactly one
	     occupant, so an old `boarding_room_stay:travel` row is inherited rather than
	     duplicated

	Cancelled rows are never returned. Reviving a charge somebody deliberately removed is
	worse than adding a new one they can see.
	"""
	legacy = None
	legacy_count = 0
	for row in boarding.billable_items or []:
		if row.item_type != "Room Stay" or row.name in claimed:
			continue
		if cstr(row.status).strip() == "Cancelled":
			continue
		if cstr(row.get("pet")).strip() == pet:
			return row
		if row.linked_service_id == _room_stay_service_id_for(pet):
			return row
		if not cstr(row.get("pet")).strip():
			legacy = legacy or row
			legacy_count += 1
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

	for occupant in occupants:
		pet = cstr(occupant.get("pet")).strip()
		if not pet:
			continue

		row = _find_occupant_room_stay_row(boarding, pet, claimed)
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
			row.linked_service_id = _room_stay_service_id_for(pet)
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
				"linked_service_id": _room_stay_service_id_for(pet),
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
