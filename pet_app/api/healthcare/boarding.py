from __future__ import annotations

from contextlib import contextmanager

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
from pet_app.api.permissions import get_user_roles, require_doctype_permission, user_has_full_access
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
from pet_app.utils.price_list import get_veterinary_selling_price_list


ROOM_STAY_SERVICE_PREFIX = "boarding_room_stay"
LOCK_TIMEOUT_SECONDS = 15
BILLABLE_ITEM_STATUSES = ("Draft", "Billable", "Billed", "Cancelled")
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
CANCELLABLE_BOARDING_STATUSES = (PENDING_ROOM_STATUS, "Reserved")


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
def start_visit_boarding(visit=None, note=None, expected_check_out=None, data=None, **kwargs):
	payload = _coerce_payload(data, kwargs)
	visit_name = cstr(payload.get("visit") or visit).strip()
	boarding_note = cstr(payload.get("note") if "note" in payload else note).strip()
	expected = payload.get("expected_check_out") if "expected_check_out" in payload else expected_check_out

	savepoint = f"start_visit_boarding_{frappe.generate_hash(length=10)}"
	frappe.db.savepoint(savepoint)
	try:
		boarding = _start_visit_boarding_atomic(visit_name, boarding_note, expected)
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

	return {
		"success": True,
		"boarding_id": cancelled.name,
		"record_status": cancelled.record_status,
		"status": cancelled.status,
		"sales_invoice": cancelled.sales_invoice,
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

	if boarding.service_room:
		with _service_room_lock(boarding.service_room):
			apply_cancel()
	else:
		apply_cancel()

	return boarding


def _start_visit_boarding_atomic(visit_name: str, boarding_note: str, expected_check_out=None):
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
	boarding = frappe.get_doc(
		{
			"doctype": "Pet Boarding",
			"visit": visit_doc.name,
			"pet": pet,
			"guardian": guardian,
			"customer": customer,
			"boarding_type": "Treatment",
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
	boarding.add_comment("Comment", _("Visit boarding started from {0} by {1}.").format(visit_doc.name, frappe.session.user))
	_log_boarding_event(
		"VISIT_BOARDING_STARTED",
		boarding=boarding.name,
		visit=visit_doc.name,
		user=frappe.session.user,
	)
	return boarding


def can_start_visit_boarding(visit_doc) -> bool:
	try:
		if not visit_doc or cstr(visit_doc.get("status")) in {"Completed", "Cancelled"}:
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
	if cstr(visit_doc.get("status")) in {"Completed", "Cancelled"}:
		frappe.throw(_("Completed or cancelled visits cannot start boarding."))
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
def reserve_room(roomId, petId=None, guardianId=None, checkIn=None, checkOut=None, note=None, boardingType=None, boarding_id=None, boardingId=None, name=None):
	_require_boarding_write_access()
	room_id = cstr(roomId).strip()
	boarding_name = cstr(boarding_id or boardingId or name).strip()
	if boarding_name:
		return _reserve_existing_pending_boarding(boarding_name, room_id, checkIn=checkIn, checkOut=checkOut, note=note)

	pet_id = cstr(petId).strip()
	guardian_id = cstr(guardianId).strip()

	if not room_id:
		frappe.throw(_("Service Room is required."))
	if not pet_id:
		frappe.throw(_("Pet is required."))

	with _service_room_lock(room_id):
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

		guardian_id = _resolve_guardian_for_pet(pet_id, guardian_id)
		customer_id = get_or_create_customer_from_guardian(guardian_id)

		boarding = frappe.get_doc(
			{
				"doctype": "Pet Boarding",
				"service_room": room.name,
				"pet": pet_id,
				"guardian": guardian_id,
				"customer": customer_id,
				"boarding_type": _normalize_boarding_type(boardingType),
				"record_status": "Reserved",
				"status": "Open",
				"workflow_state": "Reserved",
				"reserved_at": now_datetime(),
				"check_in": _coerce_datetime(checkIn),
				"check_out": _coerce_datetime(checkOut),
				"note": note,
				"billing_status": "Unbilled",
			}
		)
		_ensure_room_stay_billable_item(boarding)
		boarding.insert()
		boarding.add_comment("Comment", _("Boarding reserved by {0}.").format(frappe.session.user))
		_log_boarding_event("BOARDING_RESERVED", boarding=boarding.name, room=room.name, user=frappe.session.user)

	return {
		"success": True,
		"boarding_id": boarding.name,
		"room_id": room.name,
		"record_status": boarding.record_status,
		"occupancy": "Reserved",
		"customer": boarding.customer,
		"boarding": _serialize_boarding_doc(boarding),
	}


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

	with _service_room_lock(room_id):
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
def check_in_boarding(boarding_id):
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

		for row in boarding.billable_items or []:
			if row.status != "Cancelled":
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
		"balance": boarding.balance,
		"boarding": _serialize_boarding_doc(boarding),
	}


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
):
	"""Raise a single lab / imaging / care-service order for a checked-in boarding.

	The visit order flow is visit-scoped; boarding records only carry a pet +
	guardian, so this is their own entry point. The frontend sends one call per
	selected order. The created order is linked back to the boarding via
	``source_doctype``/``source_name`` and a matching ``billable_items`` row is
	appended so it settles through the existing checkout invoice path.
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

	if kind == "medication":
		return _create_boarding_medication_order(boarding, medication=template_id, item_code=item_code, note=note)

	order_doctype = ORDER_KIND_DOCTYPE[kind]
	require_doctype_permission(order_doctype, "create")

	existing = _find_recent_duplicate_order(order_doctype, boarding.name, kind, care_service)
	if existing:
		_log_boarding_event(
			"BOARDING_ORDER_DUPLICATE",
			boarding=boarding.name,
			kind=kind,
			order=existing.name,
			user=frappe.session.user,
		)
		return _boarding_order_response(boarding, kind, existing, reused=True)

	order = _create_boarding_order_doc(
		boarding, kind, care_service=care_service, item_code=item_code, priority=priority, note=note
	)
	_append_boarding_order_billable(boarding, kind, order, care_service=care_service, item_code=item_code, note=note)
	_log_boarding_event(
		"BOARDING_ORDER_CREATED",
		boarding=boarding.name,
		kind=kind,
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

	row.dispensed_qty = flt(row.get("dispensed_qty")) + dispense_qty
	row.dispensed_by = frappe.session.user
	row.dispensed_at = now_datetime()
	row.dispense_status = "Dispensed" if flt(row.dispensed_qty) >= flt(row.get("qty")) else "Partially Dispensed"
	if dispense_note:
		row.note = _append_note(row.get("note"), dispense_note)

	boarding_doc.save(ignore_permissions=True)
	boarding_doc.add_comment(
		"Comment",
		_("Medication billable row {0} dispensed by {1}.").format(row.name, frappe.session.user),
	)
	_log_boarding_event(
		"BOARDING_MEDICATION_DISPENSED",
		boarding=boarding_doc.name,
		item=row.name,
		qty=dispense_qty,
		user=frappe.session.user,
	)

	return {
		"success": True,
		"boarding_id": boarding_doc.name,
		"item": _serialize_billable_item(row),
		"boarding": _serialize_boarding_doc(boarding_doc),
		"idempotent": False,
	}


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


def _create_boarding_medication_order(boarding, *, medication: str, item_code: str | None = None, note: str | None = None) -> dict:
	require_doctype_permission("Medication", "read")
	medication_name = cstr(medication).strip()
	if not medication_name or not frappe.db.exists("Medication", medication_name):
		return _boarding_validation_error(_("Medication {0} was not found.").format(frappe.bold(medication_name or "")))

	existing = _find_recent_duplicate_medication_billable(boarding, medication_name)
	if existing:
		_log_boarding_event(
			"BOARDING_MEDICATION_ORDER_DUPLICATE",
			boarding=boarding.name,
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
	row = boarding.append(
		"billable_items",
		{
			"item_name": medication_doc.get("medication_name") or item.get("item_name"),
			"item_code": item.get("item_code"),
			"item_type": "Medication",
			"qty": 1,
			"rate": rate,
			"amount": rate,
			"status": "Billable",
			"note": note or medication_doc.get("medication_name") or item.get("item_name"),
			"linked_service_id": f"boarding-medication::{order_id}",
			"linked_doctype": "Medication",
			"linked_name": medication_name,
			"order_id": order_id,
		},
	)
	_set_child_value_if_field(row, "care_episode", _active_episode_name_for_pet(boarding.pet))
	_set_child_value_if_field(row, "dispense_status", "Pending Dispense")
	_set_child_value_if_field(row, "dispensed_qty", 0)

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
		medication=medication_name,
		item=row.name,
		user=frappe.session.user,
	)
	return _boarding_medication_order_response(boarding, row)


def _create_boarding_order_doc(boarding, kind, *, care_service, item_code, priority, note):
	order_id = f"{boarding.name}-{kind}-{care_service}"
	if kind == "service":
		template = frappe.db.get_value(
			"CareService template", care_service, ["service_name", "default_price"], as_dict=True
		)
		doc = frappe.get_doc(
			{
				"doctype": "PetCareService",
				"pet_service_name": (template.service_name if template else None) or _("Care Service"),
				"pet_id": boarding.pet,
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
				"pet": boarding.pet,
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


def _find_recent_duplicate_order(order_doctype, boarding_name, kind, care_service):
	since = add_to_date(now_datetime(), seconds=-DUPLICATE_ORDER_WINDOW_SECONDS)
	filters = {
		"source_doctype": "Pet Boarding",
		"source_name": boarding_name,
		"creation": [">=", since],
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

	active_episode = _active_episode_name_for_pet(boarding.pet)
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


def _find_recent_duplicate_medication_billable(boarding, medication: str):
	since = add_to_date(now_datetime(), seconds=-DUPLICATE_ORDER_WINDOW_SECONDS)
	for row in boarding.billable_items or []:
		if cstr(row.get("item_type")).strip() != "Medication":
			continue
		if cstr(row.get("linked_doctype")).strip() != "Medication":
			continue
		if cstr(row.get("linked_name")).strip() != medication:
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
	if boarding_type in ("Travel", "Treatment"):
		return boarding_type
	return frappe.db.get_single_value("Pet Boarding Settings", "default_boarding_type") or "Travel"


def _coerce_datetime(value):
	if not value:
		return None
	return get_datetime(value)


def _ensure_room_stay_billable_item(boarding, *, add_if_missing: bool = True):
	boarding.run_method("_compute_stay_days")
	stay_days = flt(boarding.get("stay_days") or 1)
	linked_service_id = _room_stay_service_id(boarding.boarding_type)
	existing_row = None
	for row in boarding.billable_items or []:
		if row.linked_service_id == linked_service_id:
			existing_row = row
			break

	if not existing_row and not add_if_missing:
		return

	item_code = _get_room_stay_item_code(boarding.boarding_type)
	item = _get_item_details(item_code)
	if flt(item.get("rate")) <= 0:
		frappe.throw(_("Price is not configured for boarding item {0}.").format(frappe.bold(item_code)))

	if not existing_row:
		for row in boarding.billable_items or []:
			if row.item_type == "Room Stay" and row.item_code == item_code:
				existing_row = row
				break

	if existing_row:
		existing_row.item_name = existing_row.item_name or item.get("item_name")
		existing_row.item_code = item_code
		existing_row.item_type = "Room Stay"
		existing_row.qty = stay_days
		existing_row.rate = flt(existing_row.rate or item.get("rate"))
		existing_row.status = "Billable" if existing_row.status == "Draft" else existing_row.status or "Billable"
		existing_row.linked_service_id = linked_service_id
		return

	boarding.append(
		"billable_items",
		{
			"item_name": item.get("item_name"),
			"item_code": item_code,
			"item_type": "Room Stay",
			"qty": stay_days,
			"rate": item.get("rate"),
			"status": "Billable",
			"note": _("Auto-added {0} boarding room stay.").format(boarding.boarding_type),
			"linked_service_id": linked_service_id,
		},
	)


def _get_room_stay_item_code(boarding_type: str) -> str:
	fieldname = "treatment_boarding_item" if boarding_type == "Treatment" else "travel_boarding_item"
	item_code = frappe.db.get_single_value("Pet Boarding Settings", fieldname)
	if not item_code:
		frappe.throw(
			_("Configure {0} in Pet Boarding Settings before creating room charges.").format(
				_("Treatment Boarding Item") if boarding_type == "Treatment" else _("Travel Boarding Item")
			)
		)
	if not frappe.db.exists("Item", item_code):
		frappe.throw(_("Configured boarding item {0} was not found.").format(frappe.bold(item_code)))
	return item_code


def _room_stay_service_id(boarding_type: str) -> str:
	return f"{ROOM_STAY_SERVICE_PREFIX}:{cstr(boarding_type).lower()}"


def _build_sales_invoice_items(boarding) -> list[dict]:
	items = []
	for row in boarding.billable_items or []:
		if row.status == "Cancelled":
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
