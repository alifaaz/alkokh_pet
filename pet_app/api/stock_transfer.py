"""Stock Transfer contract v1. GET reads; authenticated POST commands only."""

import frappe

from pet_app.stock_transfer import access, inventory
from pet_app.stock_transfer.guards import lock
from pet_app.stock_transfer.rules import TransferError, fail, public_url, quantity
from pet_app.stock_transfer.service import detail, envelope, execute, load, snapshots


@frappe.whitelist(methods=["GET"])
@envelope
def get_capabilities():
	current = {
		"id": frappe.session.user,
		"label": frappe.db.get_value("User", frappe.session.user, "full_name") or frappe.session.user,
	}
	result = dict(
		enabled=False,
		contract_version=1,
		actions=[],
		current_user=current,
		requesters=[current],
		warehouses=[],
		quarantine_warehouses=[],
		public_app_url=None,
	)
	if not frappe.db.exists("DocType", access.ORDER) or not frappe.has_permission(access.ORDER, "read"):
		return result
	special = access.special_warehouses()
	configured = {}
	for r in frappe.get_list(
		"Warehouse",
		filters={"disabled": 0, "is_group": 0},
		fields=["name", "warehouse_name", "company"],
		limit_page_length=0,
	):
		try:
			access.warehouse(r.name, r.company)
		except (TransferError, frappe.PermissionError):
			continue
		choice = dict(id=r.name, label=r.warehouse_name, company=r.company)
		if r.name not in special:
			result["warehouses"].append(choice)
		cfg = access.settings(r.company, required=False)
		if cfg:
			configured[cfg.company] = cfg
			if r.name in {q.warehouse for q in cfg.quarantine_warehouses}:
				result["quarantine_warehouses"].append(choice)
	for cfg in configured.values():
		try:
			result["public_app_url"] = public_url(cfg.public_app_url)
			access.prerequisites(cfg)
			result["enabled"] = True
			result["public_app_url"] = public_url(cfg.public_app_url)
		except TransferError:
			continue
	if result["enabled"]:
		result["actions"] = access.allowed()
	if frappe.has_permission(access.ORDER, "submit") and frappe.has_permission("User", "read"):
		for u in frappe.get_list(
			"User",
			filters={"enabled": 1, "name": ["not in", ["Guest", frappe.session.user]]},
			fields=["name", "full_name"],
			limit_page_length=0,
		):
			result["requesters"].append(dict(id=u.name, label=u.full_name or u.name))
	return result


@frappe.whitelist(methods=["GET"])
@envelope
def get_transfer(transfer_id):
	lock()
	return detail(load(transfer_id))


@frappe.whitelist(methods=["GET"])
@envelope
def list_transfers(search=None, status=None, warehouse=None, offset=0, limit=20):
	access.permit(access.ORDER)
	states = ("Ordered", "Preparing", "Transferring", "Received", "Cancelled")
	if status and status not in states:
		fail("Unknown transfer status.")
	offset, limit = max(0, int(offset)), max(1, min(100, int(limit)))
	filters = {}
	if warehouse:
		access.warehouse(warehouse)
	or_filters = None
	if search:
		or_filters = {"name": ["like", f"%{search}%"], "notes": ["like", f"%{search}%"]}
	# Native query permissions run before document-level link scope checks. Both
	# checks run before any count or page slice, including cancelled historical rows.
	candidates = frappe.get_list(
		access.ORDER,
		filters=filters,
		or_filters=or_filters,
		pluck="name",
		order_by="modified desc, name desc",
		limit_page_length=0,
	)
	counts = {s: 0 for s in states[:-1]}
	matches = []
	for name in candidates:
		try:
			doc = load(name)
		except (TransferError, frappe.PermissionError):
			continue
		if warehouse and warehouse not in (doc.source_warehouse, doc.target_warehouse):
			continue
		if doc.status in counts:
			counts[doc.status] += 1
		if not status or status == doc.status:
			matches.append(doc)
	items = []
	for doc in matches[offset : offset + limit]:
		items.append(
			dict(
				id=doc.name,
				version=f"{doc.name}:{doc.revision}",
				status=doc.status,
				company=doc.company,
				source_warehouse=doc.source_warehouse,
				target_warehouse=doc.target_warehouse,
				requester=doc.requester,
				requested_date=str(doc.requested_date),
				priority=doc.priority,
				notes=doc.notes or "",
				item_count=len(doc.items),
				has_exceptions=bool(doc.has_exceptions),
				ready=bool(doc.ready),
				modified=str(doc.modified),
				allowed_actions=access.allowed(doc),
			)
		)
	return dict(items=items, total=len(matches), counts=counts)


@frappe.whitelist(methods=["GET"])
@envelope
def search_items(source_warehouse, search=None, limit=30):
	access.permit(access.ORDER, "read")
	access.regular(source_warehouse)
	lock()
	result = []
	candidates = frappe.get_list(
		"Item",
		filters={"disabled": 0, "is_stock_item": 1, "has_variants": 0},
		or_filters={"name": ["like", f"%{search}%"], "item_name": ["like", f"%{search}%"]}
		if search
		else None,
		fields=["name", "item_name", "image", "stock_uom", "has_batch_no", "has_serial_no"],
		order_by="item_name asc",
		limit_page_length=0,
	)
	for r in candidates:
		try:
			inventory.item(r.name)
		except (TransferError, frappe.PermissionError):
			continue
		current, reserved = snapshots(r.name, source_warehouse)
		result.append(
			dict(
				id=r.name,
				label=r.item_name,
				image=r.image,
				stock_uom=r.stock_uom,
				current_stock=current,
				reserved_stock=reserved,
				has_batch=bool(r.has_batch_no),
				has_serial=bool(r.has_serial_no),
			)
		)
		if len(result) >= max(1, min(100, int(limit))):
			break
	return dict(items=result)


@frappe.whitelist(methods=["GET"])
@envelope
def get_allocations(transfer_id, line_id, phase):
	lock()
	doc = load(transfer_id)
	line = next((r for r in doc.items if r.line_id == line_id), None)
	if not line:
		fail("The line does not belong to this transfer.", "INVALID_ALLOCATION")
	access.warehouse(doc.source_warehouse if phase == "prepare" else doc.target_warehouse, doc.company)
	if not inventory.managed(line):
		if phase not in ("prepare", "receive"):
			fail("phase must be prepare or receive.")
		return dict(items=[])
	return dict(items=inventory.options(doc, line, phase))


def _command(action, transfer_id, expected_version, idempotency_key, payload):
	return execute(action, payload, idempotency_key, transfer_id, expected_version)


@frappe.whitelist(methods=["POST"])
@envelope
def create_transfer(payload=None, idempotency_key=None, transfer_id=None, expected_version=None):
	return _command("create_transfer", transfer_id, expected_version, idempotency_key, payload)


@frappe.whitelist(methods=["POST"])
@envelope
def update_transfer(transfer_id=None, expected_version=None, idempotency_key=None, payload=None):
	return _command("update_transfer", transfer_id, expected_version, idempotency_key, payload)


@frappe.whitelist(methods=["POST"])
@envelope
def start_preparation(transfer_id=None, expected_version=None, idempotency_key=None, payload=None):
	return _command("start_preparation", transfer_id, expected_version, idempotency_key, payload)


@frappe.whitelist(methods=["POST"])
@envelope
def save_preparation(transfer_id=None, expected_version=None, idempotency_key=None, payload=None):
	return _command("save_preparation", transfer_id, expected_version, idempotency_key, payload)


@frappe.whitelist(methods=["POST"])
@envelope
def approve_reduction(transfer_id=None, expected_version=None, idempotency_key=None, payload=None):
	return _command("approve_reduction", transfer_id, expected_version, idempotency_key, payload)


@frappe.whitelist(methods=["POST"])
@envelope
def dispatch_transfer(transfer_id=None, expected_version=None, idempotency_key=None, payload=None):
	return _command("dispatch_transfer", transfer_id, expected_version, idempotency_key, payload)


@frappe.whitelist(methods=["POST"])
@envelope
def update_shipment(transfer_id=None, expected_version=None, idempotency_key=None, payload=None):
	return _command("update_shipment", transfer_id, expected_version, idempotency_key, payload)


@frappe.whitelist(methods=["POST"])
@envelope
def record_arrival(transfer_id=None, expected_version=None, idempotency_key=None, payload=None):
	return _command("record_arrival", transfer_id, expected_version, idempotency_key, payload)


@frappe.whitelist(methods=["POST"])
@envelope
def receive_transfer(transfer_id=None, expected_version=None, idempotency_key=None, payload=None):
	return _command("receive_transfer", transfer_id, expected_version, idempotency_key, payload)


@frappe.whitelist(methods=["POST"])
@envelope
def resolve_exception(transfer_id=None, expected_version=None, idempotency_key=None, payload=None):
	return _command("resolve_exception", transfer_id, expected_version, idempotency_key, payload)


@frappe.whitelist(methods=["POST"])
@envelope
def cancel_transfer(transfer_id=None, expected_version=None, idempotency_key=None, payload=None):
	return _command("cancel_transfer", transfer_id, expected_version, idempotency_key, payload)
