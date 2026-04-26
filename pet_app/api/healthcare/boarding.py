from __future__ import annotations

from contextlib import contextmanager

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, get_datetime, getdate, now_datetime

from pet_app.api.sales import _set_optional_guardian_reference
from pet_app.pet_app.doctype.pet_boarding.pet_boarding import (
	ACTIVE_BOARDING_STATUSES,
	CLOSED_BOARDING_STATUSES,
	get_active_boarding_for_room,
)
from pet_app.utils.guardian_customer import get_guardian_record, get_or_create_customer_from_guardian


ROOM_STAY_SERVICE_PREFIX = "boarding_room_stay"
LOCK_TIMEOUT_SECONDS = 15
BILLABLE_ITEM_STATUSES = ("Draft", "Billable", "Billed", "Cancelled")
BILLABLE_ITEM_TYPES = ("Room Stay", "Service", "Medication", "Lab", "Product", "Other")
BOARDING_READ_ROLES = ("System Manager", "Doctor", "Accounts User", "Healthcare", "Accounting")
BOARDING_WRITE_ROLES = ("System Manager", "Doctor", "Accounts User", "Healthcare", "Accounting")


def _require_boarding_role(*allowed_roles):
	if frappe.session.user == "Administrator":
		return
	user_roles = set(frappe.get_roles(frappe.session.user) or [])
	if user_roles.isdisjoint(set(allowed_roles)):
		frappe.throw(_("Not permitted"), frappe.PermissionError)


def _require_boarding_read_access():
	_require_boarding_role(*BOARDING_READ_ROLES)
	if not frappe.has_permission("Pet Boarding", ptype="read") and not frappe.has_permission("Service Room", ptype="read"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)


def _require_boarding_write_access():
	_require_boarding_role(*BOARDING_WRITE_ROLES)
	if not frappe.has_permission("Pet Boarding", ptype="write"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)


def _require_boarding_invoice_access():
	_require_boarding_role(*BOARDING_WRITE_ROLES)
	if not frappe.has_permission("Sales Invoice", ptype="create"):
		frappe.throw(_("Not permitted to create Sales Invoice."), frappe.PermissionError)


def _log_boarding_event(event: str, **context):
	frappe.logger("pet_app.boarding").info({"event": event, **context})


@frappe.whitelist()
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
			pb.total_cost,
			pb.deposit,
			pb.balance,
			pb.billing_status,
			pb.sales_invoice,
			pb.note,
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


@frappe.whitelist()
def reserve_room(roomId, petId, guardianId, checkIn=None, checkOut=None, note=None, boardingType=None):
	_require_boarding_write_access()
	room_id = cstr(roomId).strip()
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


@frappe.whitelist()
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
	}


@frappe.whitelist()
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
		_ensure_room_stay_billable_item(boarding)
		boarding.run_method("_apply_billable_item_amounts")
		boarding.run_method("_compute_stay_days")
		boarding.run_method("_compute_totals")

		invoice_items = _build_sales_invoice_items(boarding)
		if not invoice_items:
			frappe.throw(_("Add at least one billable item before checkout."))

		invoice = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"customer": boarding.customer,
				"posting_date": getdate(boarding.check_out),
				"due_date": getdate(boarding.check_out),
				"items": invoice_items,
				"remarks": _("Pet Boarding {0} checkout.").format(boarding.name),
			}
		)
		guardian_field = _set_optional_guardian_reference(invoice, boarding.guardian)
		invoice.flags.from_custom_flow = True
		invoice.insert()
		invoice.add_comment(
			"Comment",
			_("Boarding checkout invoice created from Pet Boarding {0} by {1}.").format(
				boarding.name, frappe.session.user
			),
		)

		for row in boarding.billable_items or []:
			if row.status != "Cancelled":
				row.status = "Billed"

		boarding.sales_invoice = invoice.name
		boarding.billing_status = "Invoiced"
		boarding.record_status = "Checked Out"
		boarding.status = "Closed"
		boarding.workflow_state = "Closed"
		boarding.save()
		boarding.add_comment(
			"Comment",
			_("Boarding checked out and invoiced with Sales Invoice {0} by {1}.").format(
				invoice.name, frappe.session.user
			),
		)
		_log_boarding_event(
			"BOARDING_CHECKED_OUT",
			boarding=boarding.name,
			sales_invoice=invoice.name,
			user=frappe.session.user,
		)

		if boarding.meta.is_submittable and boarding.docstatus == 0:
			boarding.submit()

	return {
		"success": True,
		"boarding_id": boarding.name,
		"room_id": boarding.service_room,
		"record_status": boarding.record_status,
		"status": boarding.status,
		"occupancy": "Available",
		"sales_invoice": invoice.name,
		"customer": boarding.customer,
		"guardian_reference_field": guardian_field,
		"total_cost": boarding.total_cost,
		"balance": boarding.balance,
		"boarding": _serialize_boarding_doc(boarding),
	}


@frappe.whitelist()
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
			"record_status": ["in", ACTIVE_BOARDING_STATUSES],
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
			"total_cost",
			"deposit",
			"balance",
			"billing_status",
			"sales_invoice",
			"note",
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
				"guardian": boarding.guardian,
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

	if boarding:
		boarding_data = _serialize_boarding_doc(boarding)
		detail.update(boarding_data)
		detail["room"] = _serialize_room(room, boarding) if room else None
		detail["boarding"] = boarding_data
		detail["occupancy"] = _occupancy_from_record_status(boarding.record_status)

	return detail


def _serialize_boarding_doc(boarding) -> dict:
	return {
		"name": boarding.name,
		"boarding_id": boarding.name,
		"service_room": boarding.service_room,
		"room_id": boarding.service_room,
		"pet": boarding.pet,
		"pet_name": frappe.db.get_value("Pet", boarding.pet, "pet_name") if boarding.pet else None,
		"guardian": boarding.guardian,
		"guardian_name": frappe.db.get_value("Guardian", boarding.guardian, "full_name") if boarding.guardian else None,
		"customer": boarding.customer,
		"boarding_type": boarding.boarding_type,
		"record_status": boarding.record_status,
		"status": boarding.status,
		"workflow_state": boarding.workflow_state,
		"reserved_at": boarding.reserved_at,
		"check_in": boarding.check_in,
		"check_out": boarding.check_out,
		"stay_days": boarding.stay_days,
		"total_cost": boarding.total_cost,
		"deposit": boarding.deposit,
		"balance": boarding.balance,
		"billing_status": boarding.billing_status,
		"sales_invoice": boarding.sales_invoice,
		"note": boarding.note,
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
		"pet_name": row.get("pet_name") or _get_pet_name(row.pet),
		"pet_image": row.get("pet_image"),
		"guardian": row.guardian,
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
		"stay_days": row.stay_days,
		"total_cost": row.total_cost,
		"deposit": row.deposit,
		"balance": row.balance,
		"billing_status": row.billing_status,
		"sales_invoice": row.sales_invoice,
		"note": row.note,
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
		"note": raw_row.get("note") or "",
		"linked_service_id": cstr(raw_row.get("linked_service_id")).strip(),
	}
	if row_name:
		row["name"] = row_name

	return row


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


def _ensure_room_stay_billable_item(boarding):
	boarding.run_method("_compute_stay_days")
	stay_days = flt(boarding.stay_days or 1)
	item_code = _get_room_stay_item_code(boarding.boarding_type)
	item = _get_item_details(item_code)
	if flt(item.get("rate")) <= 0:
		frappe.throw(_("Price is not configured for boarding item {0}.").format(frappe.bold(item_code)))

	linked_service_id = _room_stay_service_id(boarding.boarding_type)
	existing_row = None
	for row in boarding.billable_items or []:
		if row.linked_service_id == linked_service_id:
			existing_row = row
			break
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
			_("Configure {0} in Pet Boarding Settings before checkout.").format(
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
		{"item_code": item_code, "price_list": "Standard Selling", "selling": 1},
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
