"""Atomic Stock Transfer Order command handler and complete contract responses."""

import inspect
import json
import uuid
from datetime import datetime, timezone
from functools import wraps

import frappe
from frappe.utils import getdate

from pet_app.stock_transfer import access
from pet_app.stock_transfer import inventory as inv
from pet_app.stock_transfer.guards import lock, native_reserved, reservations, stock_qty
from pet_app.stock_transfer.rules import (
	TransferError,
	digest,
	fail,
	quantity,
	rows,
	same,
	shipment,
	snapshot,
	stock_units,
)


def timestamp():
	return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def dumps(value):
	return json.dumps(value, default=str, allow_nan=False, sort_keys=True)


def record_refusal(fn, args, kwargs, code, message):
	"""Persist a refused workflow action to the Error Log.

	Refusals return ``ok:false`` instead of raising, so nothing else in Frappe records them.
	During the ST-2026-892CBAF517E54CF7 incident the operator's failed attempts left no trace
	anywhere and had to be inferred from what did not happen. Reads are chatty and excluded;
	deliberate workflow mutations are not.

	Called after the rollback. `tabError Log` is MyISAM, so the row outlives the rolled-back
	transaction it describes rather than vanishing with it.
	"""
	action = getattr(fn, "__name__", "")
	if action not in access.ACTIONS:
		return
	try:
		try:
			values = inspect.signature(fn).bind_partial(*args, **kwargs).arguments
		except TypeError:
			values = dict(kwargs)
		transfer = values.get("transfer_id")
		payload = dumps(values.get("payload"))
		if len(payload) > 4000:
			payload = f"{payload[:4000]}... [truncated, {len(payload)} chars]"
		frappe.log_error(
			title=f"Stock Transfer refused: {action} [{code}]",
			message="\n".join(
				(
					f"action: {action}",
					f"code: {code}",
					f"message: {message}",
					f"transfer: {transfer or '(new)'}",
					f"expected_version: {values.get('expected_version')}",
					f"idempotency_key: {values.get('idempotency_key')}",
					f"actor: {frappe.session.user}",
					f"payload: {payload}",
				)
			),
			**(dict(reference_doctype=access.ORDER, reference_name=transfer) if transfer else {}),
		)
	except Exception:
		# An audit trail must never turn a clean refusal into a server error.
		pass


def envelope(fn):
	@wraps(fn)
	def wrapped(*args, **kwargs):
		savepoint = "stock_transfer_" + uuid.uuid4().hex
		frappe.db.savepoint(savepoint)
		try:
			if frappe.session.user == "Guest":
				fail("Sign in to access stock transfers.", "PERMISSION_DENIED")
			return dict(ok=True, data=fn(*args, **kwargs), meta={}, errors=[])
		except frappe.QueryDeadlockError:
			# MariaDB snapshot conflicts may invalidate the transaction/savepoints.
			# A full rollback makes this a definitive refusal with no committed effects.
			frappe.db.rollback()
			message = "Stock changed concurrently. Reload the transfer before starting a new action."
			record_refusal(fn, args, kwargs, "RESERVATION_CONFLICT", message)
			return dict(ok=False, data={}, meta={"code": "RESERVATION_CONFLICT"}, errors=[message])
		except (
			TransferError,
			frappe.ValidationError,
			frappe.PermissionError,
			frappe.DoesNotExistError,
			ValueError,
			TypeError,
		) as exc:
			frappe.db.rollback(save_point=savepoint)
			code = getattr(exc, "code", None) or (
				"PERMISSION_DENIED"
				if isinstance(exc, frappe.PermissionError)
				else "NOT_FOUND"
				if isinstance(exc, frappe.DoesNotExistError)
				else "VALIDATION_ERROR"
			)
			record_refusal(fn, args, kwargs, code, str(exc))
			return dict(ok=False, data={}, meta={"code": code}, errors=[str(exc)])
		except Exception:
			# Unexpected transport/server failures remain failures, never an empty successful envelope.
			frappe.db.rollback(save_point=savepoint)
			raise

	return wrapped


def load(name):
	if not isinstance(name, str) or not frappe.db.exists(access.ORDER, name):
		fail("Stock transfer was not found.", "NOT_FOUND")
	doc = frappe.get_doc(access.ORDER, name, for_update=True)
	access.read_order(doc)
	return doc


def execute(action, payload, idempotency_key, transfer_id=None, expected_version=None):
	if not isinstance(payload, dict):
		fail("payload must be a JSON object.")
	if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 140:
		fail("Provide an idempotency_key of at most 140 characters.")
	if not lock():
		fail("Stock transfer schema has not been installed.", "STOCK_TRANSFER_DISABLED")
	key = digest([frappe.session.user, action, idempotency_key])
	fingerprint = digest(dict(payload=payload, transfer_id=transfer_id, expected_version=expected_version))
	# Current read is essential when another connection completed while this one waited.
	previous = frappe.db.sql(
		"select request_hash, transfer, result_json from `tabStock Transfer Request` where name=%s for update",
		key,
		as_dict=True,
	)
	if previous:
		record = previous[0]
		if record.request_hash != fingerprint:
			fail("This idempotency key was already used for a different request.", "IDEMPOTENCY_CONFLICT")
		load(record.transfer)  # Revocation still applies to replayed result disclosure.
		return json.loads(record.result_json)
	doc = None
	if action != "create_transfer":
		if not transfer_id or not isinstance(expected_version, str):
			fail("transfer_id and expected_version are required.")
		# Refresh the order through a current read before Document hydration.
		frappe.db.sql("select name from `tabStock Transfer Order` where name=%s for update", transfer_id)
		doc = load(transfer_id)
		if expected_version != version(doc):
			fail("The transfer changed. Reload it before starting a new action.", "VERSION_CONFLICT")
	elif transfer_id or expected_version:
		fail("Create must omit transfer_id and expected_version.")
	access.authorize(action, doc)
	if doc:
		cfg = access.prerequisites(access.settings(doc.company))
	else:
		source = access.regular(payload.get("source_warehouse"))
		cfg = access.prerequisites(access.settings(source.company))
	before = doc.as_dict() if doc else None
	internal = getattr(frappe.local, "stock_transfer_internal", False)
	frappe.local.stock_transfer_internal = True
	try:
		doc = apply(action, doc, payload, cfg)
		doc.revision = int(doc.revision or 0) + 1
		if doc.is_new():
			doc.insert(ignore_permissions=True, set_name=doc.name)
		else:
			doc.save(ignore_permissions=True)
		note = event_note(action, payload, before, doc)
		frappe.get_doc(
			dict(
				doctype="Stock Transfer Event",
				transfer=doc.name,
				action=action,
				actor=frappe.session.user,
				timestamp=timestamp(),
				note=note,
				audit_json=dumps(dict(payload=payload, before=before, after=doc.as_dict())),
			)
		).insert(ignore_permissions=True)
		result = detail(doc)
		frappe.get_doc(
			dict(
				doctype="Stock Transfer Request",
				name=key,
				request_hash=fingerprint,
				actor=frappe.session.user,
				method=action,
				transfer=doc.name,
				result_json=dumps(result),
				documents_json=dumps(result["documents"]),
			)
		).insert(ignore_permissions=True, set_name=key)
		return result
	finally:
		frappe.local.stock_transfer_internal = internal


def version(doc):
	return f"{doc.name}:{int(doc.revision or 0)}"


def order_fields(doc, payload, cfg):
	source = access.regular(payload.get("source_warehouse"), cfg.company)
	target = access.regular(payload.get("target_warehouse"), cfg.company)
	if source.name == target.name:
		fail("Source and target warehouses must differ.", "WAREHOUSE_MISMATCH")
	requester = payload.get("requester")
	# On-behalf requests require the manager's native submit permission and readable User record.
	if requester != frappe.session.user:
		access.permit(access.ORDER, "submit", doc if not doc.is_new() else None)
		access.permit("User", doc=frappe.get_doc("User", requester))
	if not frappe.db.get_value("User", requester, "enabled"):
		fail("Choose an enabled requester.")
	try:
		requested_date = getdate(payload.get("requested_date")) if payload.get("requested_date") else None
	except Exception:
		fail("requested_date must be a valid date.")
	if not requested_date:
		fail("requested_date is required.")
	if payload.get("priority") not in ("Low", "Normal", "High", "Urgent"):
		fail("Choose a valid priority.")
	if not isinstance(payload.get("notes", ""), str):
		fail("notes must be text.")
	old = {r.line_id: r for r in doc.items or []}
	new_rows = []
	for r in rows(payload.get("items")):
		foreign = frappe.db.get_value(
			"Stock Transfer Line", {"line_id": r["line_id"], "parent": ["!=", doc.name or ""]}, "name"
		)
		if foreign:
			fail("A line_id belongs to a different transfer.")
		item = inv.item(r.get("item_code"))
		if r["line_id"] in old and old[r["line_id"]].item_code != item.name:
			fail("Use a new line_id when replacing an item.")
		factor = inv.conversion(item, r.get("uom"), r.get("conversion_factor"))
		row = frappe._dict(
			line_id=r["line_id"],
			item_code=item.name,
			uom=r.get("uom"),
			stock_uom=item.stock_uom,
			conversion_factor=factor,
		)
		qty = inv.line_quantity(row, r.get("requested_qty"))
		row.update(
			requested_qty=qty,
			approved_qty=qty,
			prepared_qty=0,
			dispatched_qty=0,
			accepted_qty=0,
			damaged_qty=0,
			lost_qty=0,
			allocations_json="[]",
		)
		new_rows.append(row)
	if not sum(r.requested_qty for r in new_rows):
		fail("An order must request a positive total quantity.")
	doc.update(
		dict(
			company=cfg.company,
			source_warehouse=source.name,
			target_warehouse=target.name,
			requester=requester,
			requested_date=requested_date,
			priority=payload["priority"],
			notes=payload.get("notes", ""),
		)
	)
	doc.set("items", new_rows)


def reason(payload):
	value = payload.get("reason")
	if not isinstance(value, str) or not value.strip():
		fail("A reason is required.")
	return value.strip()


def apply(action, doc, payload, cfg):
	if action == "create_transfer":
		doc = frappe.new_doc(access.ORDER)
		doc.name = "ST-" + datetime.now(timezone.utc).strftime("%Y-") + uuid.uuid4().hex[:16].upper()
		doc.status = "Ordered"
		order_fields(doc, payload, cfg)
		return doc
	lines = {r.line_id: r for r in doc.items}
	if action == "update_transfer":
		order_fields(doc, payload, cfg)
	elif action == "start_preparation":
		if payload:
			fail("Start preparation expects an empty payload.")
		doc.status = "Preparing"
	elif action == "save_preparation":
		if not isinstance(payload.get("ready"), bool):
			fail("ready must be a boolean.")
		usage = {}
		for r in snapshot(payload.get("items"), lines, complete=True):
			line = lines[r["line_id"]]
			inv.conversion(inv.item(line.item_code), line.uom, line.conversion_factor)
			qty = inv.line_quantity(line, r.get("prepared_qty"))
			if qty > float(line.approved_qty) or (
				payload["ready"] and not same(qty, float(line.approved_qty))
			):
				fail(
					f"{line.line_id}: ready requires the complete approved quantity; preparation cannot exceed it."
				)
			picks = inv.allocations(
				doc, line, r.get("allocations"), stock_units(qty, line.conversion_factor), "prepare", usage
			)
			line.prepared_qty, line.allocations_json = qty, dumps(picks)
		inv.reserve(doc)
		doc.ready = payload["ready"]
	elif action == "approve_reduction":
		reason(payload)
		changed = False
		for r in snapshot(payload.get("items"), lines, complete=True):
			line = lines[r["line_id"]]
			qty = inv.line_quantity(line, r.get("approved_qty"))
			if qty > float(line.approved_qty):
				fail(f"{line.line_id}: reduction cannot increase approved demand.")
			changed |= qty < float(line.approved_qty)
			line.approved_qty = qty
			line.prepared_qty = min(float(line.prepared_qty), qty)
			remaining = stock_units(float(line.prepared_qty), line.conversion_factor)
			trimmed = []
			for a in json.loads(line.allocations_json or "[]"):
				take = min(a["stock_qty"], remaining)
				if take > 1e-8:
					trimmed.append({**a, "stock_qty": take})
					remaining -= take
			line.allocations_json = dumps(trimmed)
		if not changed or not sum(float(r.approved_qty) for r in doc.items):
			fail("Reduce at least one line and retain positive approved demand; cancel zero-demand orders.")
		doc.ready, doc.has_exceptions = False, True
		inv.reserve(doc)
	elif action == "dispatch_transfer":
		doc.shipment_json = dumps(shipment(payload))
		doc.transit_warehouse = cfg.transit_warehouse
		inv.reserve(doc)
		usage, movements = {}, []
		for line in doc.items:
			inv.conversion(inv.item(line.item_code), line.uom, line.conversion_factor)
			qty = inv.line_quantity(line, float(line.prepared_qty))
			if not same(qty, float(line.approved_qty)):
				fail("Prepared quantities must equal approved demand.", "INVALID_STATE")
			picks = inv.allocations(
				doc,
				line,
				json.loads(line.allocations_json or "[]"),
				stock_units(qty, line.conversion_factor),
				"prepare",
				usage,
			)
			if qty:
				movements.append((line, qty, picks, doc.transit_warehouse))
			line.dispatched_qty = qty
		if not movements:
			fail("Cannot dispatch an empty transfer.")
		# Source holds are consumed immediately before posting; the global lock excludes competitors.
		frappe.db.delete("Stock Transfer Reservation", {"transfer": doc.name})
		inv.post(doc, cfg, "dispatch", movements)
		doc.status, doc.ready = "Transferring", False
	elif action == "update_shipment":
		doc.shipment_json = dumps(shipment(payload, json.loads(doc.shipment_json)))
	elif action == "record_arrival":
		if payload:
			fail("Record arrival expects an empty payload.")
		doc.arrived_at, doc.arrived_by = timestamp(), frappe.session.user
	elif action in ("receive_transfer", "resolve_exception"):
		settle(doc, cfg, payload, lines, action == "resolve_exception")
	elif action == "cancel_transfer":
		reason(payload)
		frappe.db.delete("Stock Transfer Reservation", {"transfer": doc.name})
		doc.status, doc.ready = "Cancelled", False
	else:
		fail("Unknown stock transfer action.")
	return doc


def outstanding(line):
	return max(
		0.0,
		float(line.dispatched_qty or 0)
		- float(line.accepted_qty or 0)
		- float(line.damaged_qty or 0)
		- float(line.lost_qty or 0),
	)


def settle(doc, cfg, payload, lines, loss):
	if loss:
		reason(payload)
	movements = {"receipt": [], "damage": [], "loss": []}
	usage, total = {}, 0
	for r in snapshot(payload.get("items"), lines):
		line = lines[r["line_id"]]
		inv.conversion(inv.item(line.item_code), line.uom, line.conversion_factor)
		good = 0 if loss else inv.line_quantity(line, r.get("accepted_qty", 0))
		damage = 0 if loss else inv.line_quantity(line, r.get("damaged_qty", 0))
		lost = inv.line_quantity(line, r.get("lost_qty")) if loss else 0
		if good + damage + lost > outstanding(line) + 1e-8:
			fail(f"{line.line_id}: settlement exceeds outstanding quantity.", "OVER_RECEIPT")
		total += good + damage + lost
		quarantine = None
		if damage:
			if not isinstance(r.get("notes"), str) or not r["notes"].strip():
				fail(f"{line.line_id}: describe the damaged goods.")
			quarantine = payload.get("quarantine_warehouse")
			if quarantine not in {q.warehouse for q in cfg.quarantine_warehouses}:
				fail("Choose a configured quarantine warehouse.", "QUARANTINE_NOT_CONFIGURED")
			access.warehouse(quarantine, doc.company)
		for kind, qty, key, target in (
			("receipt", good, "accepted_allocations", doc.target_warehouse),
			("damage", damage, "damaged_allocations", quarantine),
			("loss", lost, "allocations", None),
		):
			if (loss and kind != "loss") or (not loss and kind == "loss"):
				continue
			picks = inv.allocations(
				doc, line, r.get(key, []), stock_units(qty, line.conversion_factor), "receive", usage
			)
			if qty:
				if not inv.managed(line):
					# Unmanaged clients send []; still retain server shipment provenance.
					remaining = stock_units(qty, line.conversion_factor)
					picks = []
					for a in inv.options(doc, line, "receive"):
						take = min(remaining, a["available_stock_qty"] - usage.get(a["id"], 0))
						if take > 1e-8:
							picks.append(dict(allocation_id=a["id"], stock_qty=take))
							usage[a["id"]] = usage.get(a["id"], 0) + take
							remaining -= take
					if not same(remaining, 0):
						fail("Shipment allocations are inconsistent.", "INVALID_ALLOCATION")
				inv.consume_transit(doc, line, qty, picks)
				movements[kind].append((line, qty, picks, target))
		line.accepted_qty = float(line.accepted_qty) + good
		line.damaged_qty = float(line.damaged_qty) + damage
		line.lost_qty = float(line.lost_qty) + lost
		if damage or lost:
			doc.has_exceptions = True
	if not total:
		fail("Provide a positive receipt or loss delta.")
	for kind, plans in movements.items():
		inv.post(doc, cfg, kind, plans)
	if all(same(outstanding(line), 0) for line in doc.items):
		doc.status, doc.received_at, doc.received_by = "Received", timestamp(), frappe.session.user


def event_note(action, payload, before, doc):
	notes = [action.replace("_", " ")]
	if payload.get("reason"):
		notes.append(payload["reason"])
	for r in payload.get("items", []):
		changes = ", ".join(f"{k}={v}" for k, v in r.items() if k.endswith("_qty"))
		allocations = ", ".join(f"{k}={dumps(v)}" for k, v in r.items() if "allocations" in k)
		notes.append(f"{r['line_id']}: {changes} {allocations} {r.get('notes', '')}".strip())
	return "; ".join(notes)


def detail(doc):
	items = []
	# One source read per distinct item, shared by every line that names it: a long order
	# repeats item codes, and these reads take row locks.
	cache = stock_cache(doc)
	demanded = {}
	for line in doc.items:
		demanded[line.item_code] = demanded.get(line.item_code, 0) + stock_units(
			float(line.approved_qty or 0), line.conversion_factor
		)
	for line in doc.items:
		item = frappe.get_doc("Item", line.item_code)
		picks = json.loads(line.allocations_json or "[]")
		held = cache.get(line.item_code) if cache else None
		current = held["on_hand"] if held else None
		reserved = held["reserved"] if held else None
		values = {
			key: float(line.get(key) or 0)
			for key in (
				"requested_qty",
				"approved_qty",
				"prepared_qty",
				"dispatched_qty",
				"accepted_qty",
				"damaged_qty",
				"lost_qty",
			)
		}
		items.append(
			dict(
				line_id=line.line_id,
				item_code=line.item_code,
				item_name=item.item_name,
				image=item.image,
				uom=line.uom,
				stock_uom=line.stock_uom,
				conversion_factor=float(line.conversion_factor),
				current_stock=current,
				reserved_stock=reserved,
				requires_allocation=bool(item.has_batch_no or item.has_serial_no),
				outstanding_qty=outstanding(line),
				issues=inv.line_issues(
					doc, line, item, held["free"] if held else None, demanded[line.item_code]
				),
				allocations=[dict(allocation_id=a["allocation_id"], stock_qty=a["stock_qty"]) for a in picks],
				allocation_details=[
					inv.option(a["allocation_id"], a.get("batch_no"), a.get("serial_no"), a["stock_qty"])
					for a in picks
				],
				**values,
			)
		)
	documents = frappe.get_all(
		"Stock Entry",
		filters={"custom_stock_transfer_order": doc.name},
		fields=["name as id", "custom_stock_transfer_kind as kind"],
		order_by="creation asc, name asc",
	)
	history = frappe.get_all(
		"Stock Transfer Event",
		filters={"transfer": doc.name},
		fields=["name as id", "action", "actor", "timestamp", "note"],
		order_by="creation asc, name asc",
	)
	return dict(
		id=doc.name,
		version=version(doc),
		status=doc.status,
		company=doc.company,
		source_warehouse=doc.source_warehouse,
		target_warehouse=doc.target_warehouse,
		requester=doc.requester,
		requested_date=str(doc.requested_date),
		priority=doc.priority,
		notes=doc.notes or "",
		item_count=len(items),
		has_exceptions=bool(doc.has_exceptions),
		ready=bool(doc.ready),
		allowed_actions=access.allowed(doc),
		items=items,
		shipment=json.loads(doc.shipment_json) if doc.shipment_json else None,
		arrived_at=doc.arrived_at or None,
		arrived_by=doc.arrived_by or None,
		received_at=doc.received_at or None,
		received_by=doc.received_by or None,
		documents=documents,
		history=history,
	)


def stock_cache(doc):
	"""Source-warehouse stock per distinct item, or None when the warehouse is out of scope.

	``free`` excludes this order's own holds, so it answers what reserve() will compare
	the order's demand against rather than double-counting what this order already holds.
	"""
	warehouse = doc.source_warehouse
	if not access.scoped_warehouse(warehouse):
		return None
	result = {}
	for code in {line.item_code for line in doc.items}:
		try:
			on_hand = stock_qty(code, warehouse)
			held = reservations(code, warehouse)
			native = native_reserved(code, warehouse)
		except frappe.PermissionError:
			return None
		total = sum(float(r.stock_qty) for r in held)
		mine = sum(float(r.stock_qty) for r in held if r.transfer == doc.name)
		result[code] = dict(on_hand=on_hand, reserved=total + native, free=on_hand - (total - mine) - native)
	return result


def snapshots(code, warehouse):
	if not access.scoped_warehouse(warehouse):
		return None, None
	try:
		current = stock_qty(code, warehouse)
		reserved = sum(float(r.stock_qty) for r in reservations(code, warehouse))
		reserved += native_reserved(code, warehouse)
		return current, reserved
	except frappe.PermissionError:
		return None, None
