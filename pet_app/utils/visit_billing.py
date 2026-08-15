from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt

from pet_app.utils.care_service import get_care_service_doc as resolve_care_service_doc
from pet_app.utils.invoice_source import has_marker, strip_markers

# Records that carry a sales_invoice back-link. Under invoice reuse many of these point
# at one invoice, so anything that clears or cancels an invoice has to sweep all of them
# rather than the single row a get_value() happens to return.
INVOICE_LINKED_DOCTYPES = ("Vet Visit", "Pet Boarding")


# Billable rows that must never become an invoice line on the parent's own invoice.
# "Cancelled" is the original member. "Billed" joined it when per-order billing arrived:
# a row billed on its own, at its own branch, is already paid for and must not be emitted
# a second time when the visit closes.
SKIP_INVOICE_ROW_STATUSES = frozenset({"Cancelled", "Billed"})

STRICT_MODE = True
BILLED_VISIT_LOCK_MESSAGE = "This visit is already billed and cannot be modified."
# Says what is actually locked. Since orders bill at their own completion, a single charge
# can be invoiced while the visit around it is still open and fully editable - and the
# operator reading "this visit is already billed" about an open visit has no way to act on
# it. Names the charge instead.
BILLED_ROW_LOCK_MESSAGE = "This charge has already been invoiced and cannot be changed: {0}."

# Compared as numbers, not as text. A client that serialises a whole float as a JSON
# integer sends 1 where the database holds 1.0; comparing those with cstr() reports a
# change that never happened. See _billed_row_field_changed.
NUMERIC_BILLABLE_FIELDS = frozenset({"qty", "rate", "amount"})


def billable_row_label(row) -> str:
	return cstr(row.get("item_name") or row.get("item_code") or row.get("linked_service_id") or "").strip() or _("this item")


def billed_row_field_changed(before, after, fieldname: str) -> bool:
	"""Whether the client actually changed a locked field on an invoiced charge.

	Absent means unchanged. A full-document save may simply omit a field, and for these
	fields clearing is never a legitimate edit - so `None` is read as "not supplied" rather
	than as "set it to blank". A payload that drops one field while genuinely changing
	another is still caught on the other.
	"""
	after_value = after.get(fieldname)
	if after_value is None:
		return False
	before_value = before.get(fieldname)
	if fieldname in NUMERIC_BILLABLE_FIELDS:
		return flt(before_value) != flt(after_value)
	return cstr(before_value) != cstr(after_value)
ALL_CHARGES_ALREADY_BILLED_MESSAGE = (
	"Every charge on Vet Visit {0} was already billed when its orders completed. "
	"There is nothing left to invoice - do not add items to raise one."
)
ISSUED_INVOICE_CANCEL_MESSAGE = (
	"This order is already on submitted Sales Invoice {0}. The invoice has been issued, "
	"so cancellation is an accounting matter. Please contact the cashier."
)


def log_visit_billing_event(event: str, **context):
	frappe.logger("pet_app.visit_billing").info({"event": event, **context})


def get_care_service_doc(care_service: str):
	return resolve_care_service_doc(care_service)


def apply_billable_item_amounts(visit) -> float:
	total = 0
	for row in visit.billable_items or []:
		row.qty = flt(row.qty or 1)
		row.rate = flt(row.rate or 0)
		row.amount = row.qty * row.rate
		if row.item_code and not row.item_name:
			row.item_name = frappe.db.get_value("Item", row.item_code, "item_name")
		if not row.status:
			row.status = "Billable"
		if row.status != "Cancelled":
			total += flt(row.amount)
	return total


def upsert_visit_billable_item(
	visit,
	*,
	linked_service_id: str,
	item_code: str,
	item_type: str,
	linked_doctype: str | None = None,
	linked_name: str | None = None,
	order_id: str | None = None,
	qty: float = 1,
	rate: float = 0,
	item_name: str | None = None,
	note: str | None = None,
	status: str = "Billable",
):
	if not linked_service_id:
		frappe.throw(_("Linked Service ID is required for billable items."))
	if not item_code:
		frappe.throw(_("Item Code is required for billable items."))
	if flt(qty) <= 0:
		frappe.throw(_("Billable item Qty must be greater than zero."))
	if flt(rate) < 0:
		frappe.throw(_("Billable item Rate must not be negative."))

	row = _find_billable_row(
		visit,
		linked_service_id,
		linked_doctype=linked_doctype,
		linked_name=linked_name,
		order_id=order_id,
		item_code=item_code,
		item_type=item_type,
	)
	if row:
		if row.status == "Billed":
			return row
		row.linked_service_id = linked_service_id
		_set_if_field(row, "linked_doctype", linked_doctype)
		_set_if_field(row, "linked_name", linked_name)
		_set_if_field(row, "order_id", order_id)
		row.item_code = item_code
		row.item_name = item_name or frappe.db.get_value("Item", item_code, "item_name")
		row.item_type = item_type
		row.qty = flt(qty or 1)
		row.rate = flt(rate or 0)
		row.amount = row.qty * row.rate
		row.note = note
		if row.status in (None, "", "Draft"):
			row.status = status
		return row

	row = visit.append("billable_items", {})
	row.linked_service_id = linked_service_id
	_set_if_field(row, "linked_doctype", linked_doctype)
	_set_if_field(row, "linked_name", linked_name)
	_set_if_field(row, "order_id", order_id)
	row.item_code = item_code
	row.item_name = item_name or frappe.db.get_value("Item", item_code, "item_name")
	row.item_type = item_type
	row.qty = flt(qty or 1)
	row.rate = flt(rate or 0)
	row.amount = row.qty * row.rate
	row.status = row.status or status
	row.note = note
	log_visit_billing_event(
		"BILLING_MODIFIED",
		visit=visit.name,
		linked_service_id=linked_service_id,
		item_code=item_code,
		item_type=item_type,
		amount=row.amount,
	)
	return row


def _clinical_record_care_service(doc) -> str | None:
	return cstr(doc.get("care_service") or doc.get("care_service_id")).strip() or None


def _clinical_record_rate(doc, service) -> float:
	for fieldname in ("rate", "price"):
		value = doc.get(fieldname)
		if value in (None, ""):
			continue
		value = flt(value)
		if value > 0:
			return value
	return flt(service.default_price or 0)


def _clinical_record_item_name(doc, service=None) -> str:
	service_name = cstr(service.service_name).strip() if service else ""
	service_doc_name = cstr(service.name).strip() if service else ""
	procedure_name = ""
	if doc.doctype == "Pet Procedure" and doc.get("procedure_template"):
		procedure_name = cstr(
			frappe.db.get_value("Procedure Template", doc.get("procedure_template"), "procedure_name")
		).strip()
	return (
		cstr(doc.get("item_name")).strip()
		or cstr(doc.get("pet_service_name")).strip()
		or procedure_name
		or service_name
		or service_doc_name
		or cstr(doc.get("item_code")).strip()
		or cstr(doc.name).strip()
	)


def _clinical_record_note(doc, item_name: str) -> str:
	return cstr(doc.get("note") or doc.get("description") or item_name).strip()


def sync_clinical_record_billable_item(doc, item_type: str, *, visit=None, save: bool = True):
	if not doc.visit:
		frappe.throw(_("Visit is required."))
	care_service = _clinical_record_care_service(doc)

	visit = visit or frappe.get_doc("Vet Visit", doc.visit)
	if visit.sales_invoice:
		frappe.throw(_(BILLED_VISIT_LOCK_MESSAGE))
	if visit.billed:
		frappe.throw(_(BILLED_VISIT_LOCK_MESSAGE))

	if care_service:
		service = get_care_service_doc(care_service)
		item_code = cstr(doc.get("item_code") or service.item_code).strip()
		if not item_code:
			frappe.throw(
				_("Care Service {0} is missing Item Code and cannot be billed.").format(
					frappe.bold(care_service)
				)
			)
		rate = _clinical_record_rate(doc, service)
		if rate <= 0:
			frappe.throw(
				_("Care Service {0} must have a positive billing rate before it can be billed.").format(
					frappe.bold(care_service)
				)
			)
		item_name = _clinical_record_item_name(doc, service)
	else:
		if item_type != "Procedure":
			frappe.throw(_("Care Service is required."))
		item_code = cstr(doc.get("item_code")).strip()
		if not item_code:
			frappe.throw(
				_("Pet Procedure {0} must have an Item Code before it can be billed.").format(
					frappe.bold(doc.name)
				)
			)
		rate = flt(doc.get("rate") or doc.get("price"))
		if rate <= 0:
			frappe.throw(
				_("Pet Procedure {0} must have a positive billing rate before it can be billed.").format(
					frappe.bold(doc.name)
				)
			)
		item_name = _clinical_record_item_name(doc)

	upsert_visit_billable_item(
		visit,
		linked_service_id=f"{doc.doctype}::{doc.name}",
		linked_doctype=doc.doctype,
		linked_name=doc.name,
		order_id=doc.get("order_id"),
		item_code=item_code,
		item_type=item_type,
		qty=1,
		rate=rate,
		item_name=item_name,
		note=_clinical_record_note(doc, item_name),
	)
	visit.total_billable_amount = apply_billable_item_amounts(visit)
	if save:
		visit.save(ignore_permissions=True)
	return visit


def summarise_visit_billing(visit) -> frappe._dict:
	"""What this visit is worth, what has been billed, and what is actually still owed.

	Needed because a visit's charges no longer arrive together. Each order bills at its own
	completion onto its own branch's invoice, and per-order billing never writes back to
	`Vet Visit.sales_invoice` - so a visit can be fully billed and fully paid while that
	field is still empty. Reading "no visit invoice" as "nothing has been paid" is what
	this replaces.

	Settlement is decided ROW BY ROW, not by summing the invoices. An invoice may carry
	several visits' charges (invoice reuse merges by customer and branch), so its
	`grand_total` and `outstanding_amount` say nothing about this visit's share. What can
	be said honestly is whether the invoice a given row landed on has been submitted and
	settled, and that is what is used.

	Returns totals in the visit's own currency terms plus the invoices its charges reached,
	so a caller can show them without inventing an apportionment.
	"""
	from pet_app.utils.order_billing import ORDER_ITEM_TYPES

	visit_invoice = cstr(visit.get("sales_invoice")).strip() or None

	# Which invoice each per-order-billed row went to. Rows that are not linked to one of
	# the order doctypes (medication, manual, legacy) belong to the visit's own invoice.
	order_invoices: dict[str, str] = {}
	for doctype in ORDER_ITEM_TYPES:
		if not frappe.get_meta(doctype).has_field("sales_invoice"):
			continue
		for row in frappe.get_all(
			doctype,
			filters={"visit": visit.name, "sales_invoice": ["is", "set"]},
			fields=["name", "sales_invoice"],
			ignore_permissions=True,
		):
			order_invoices[f"{doctype}::{row.name}"] = row.sales_invoice

	invoice_cache: dict[str, dict] = {}

	def _invoice(name):
		if not name:
			return None
		if name not in invoice_cache:
			invoice_cache[name] = frappe.db.get_value(
				"Sales Invoice",
				name,
				["name", "branch", "status", "docstatus", "grand_total", "outstanding_amount",
				 "paid_amount", "currency"],
				as_dict=True,
			)
		return invoice_cache[name]

	total = billed = unbilled = settled = 0.0
	for row in visit.get("billable_items") or []:
		if row.status == "Cancelled":
			continue
		amount = flt(row.amount) or flt(row.qty or 1) * flt(row.rate or 0)
		total += amount
		if row.status != "Billed":
			unbilled += amount
			continue
		billed += amount
		invoice = _invoice(order_invoices.get(cstr(row.linked_service_id)) or visit_invoice)
		# Settled means submitted with nothing outstanding. A draft invoice is a charge
		# raised, not money received, and must not read as paid.
		if invoice and cint(invoice.get("docstatus")) == 1 and flt(invoice.get("outstanding_amount")) <= 0:
			settled += amount

	invoices = [inv for inv in (_invoice(name) for name in {*order_invoices.values(), visit_invoice}) if inv]
	invoices.sort(key=lambda inv: cstr(inv.get("name")))

	return frappe._dict(
		total=total,
		billed=billed,
		unbilled=unbilled,
		settled=settled,
		outstanding=max(total - settled, 0.0),
		invoices=invoices,
		visit_invoice=visit_invoice,
		currency=next((inv.get("currency") for inv in invoices if inv.get("currency")), None),
	)


def visit_billing_status(visit, summary=None) -> str:
	"""One word for the front desk, honest about per-order billing.

	The old version answered from `Vet Visit.sales_invoice` alone, so a visit whose every
	charge had been billed and paid on other invoices read "Unbilled".
	"""
	summary = summary if summary is not None else summarise_visit_billing(visit)

	if summary.total <= 0:
		return "Unbilled"
	if summary.unbilled > 0:
		# Something on this visit has not been raised yet.
		return "Partially Billed" if summary.billed > 0 else "Unbilled"
	if summary.settled >= summary.total:
		return "Paid"
	if summary.settled > 0:
		return "Partially Paid"
	return "Draft Invoice"


def set_visit_billable_item_status(
	visit_name: str,
	*,
	status: str,
	linked_service_id: str,
	linked_doctype: str | None = None,
	linked_name: str | None = None,
	order_id: str | None = None,
	item_code: str | None = None,
	item_type: str | None = None,
	visit=None,
	save: bool = True,
):
	"""Move one billable row to `status` without touching the rest of the visit.

	Used by per-order billing to mark a row Billed at the moment its order was invoiced to
	its own branch, so `get_billable_invoice_items` skips it when the visit later closes.

	`ignore_billing_lock` is set because the visit is legitimately mid-life here: it has
	not been invoiced, and per-order billing never sets `sales_invoice` on it, so the lock
	that exists to freeze an invoiced visit has nothing to protect against on this path.
	"""
	visit = visit or frappe.get_doc("Vet Visit", visit_name)
	row = _find_billable_row(
		visit,
		linked_service_id,
		linked_doctype=linked_doctype,
		linked_name=linked_name,
		order_id=order_id,
		item_code=item_code,
		item_type=item_type,
	)
	if not row:
		return None
	row.status = status
	visit.total_billable_amount = apply_billable_item_amounts(visit)
	if save:
		visit.flags.ignore_billing_lock = True
		visit.save(ignore_permissions=True)
	return row


def assert_visit_billable_item_can_cancel(
	visit_name: str,
	*,
	linked_service_id: str | None = None,
	linked_doctype: str | None = None,
	linked_name: str | None = None,
	order_id: str | None = None,
	item_code: str | None = None,
	item_type: str | None = None,
	allow_legacy_service_fallback: bool = False,
	require_match: bool = False,
):
	if not visit_name or not (linked_service_id or (linked_doctype and linked_name) or order_id):
		return
	visit = frappe.get_doc("Vet Visit", visit_name)
	row = _find_billable_row(
		visit,
		linked_service_id,
		linked_doctype=linked_doctype,
		linked_name=linked_name,
		order_id=order_id,
		item_code=item_code,
		item_type=item_type,
	)
	if not row and allow_legacy_service_fallback:
		row = _find_legacy_service_billable_row(
			visit,
			linked_doctype=linked_doctype,
			linked_name=linked_name,
			item_type=item_type,
			raise_on_failure=require_match,
		)
	if row:
		_assert_parent_billable_row_can_cancel(visit, row, sales_invoice=visit.sales_invoice, billed=visit.billed)
	elif require_match:
		_raise_billable_match_failure(
			"No matching billable item was found.",
			visit=visit,
			linked_doctype=linked_doctype,
			linked_name=linked_name,
			item_type=item_type,
			order_id=order_id,
			linked_service_id=linked_service_id,
		)
	elif visit.sales_invoice or visit.billed:
		_assert_parent_billing_state_can_cancel(visit.sales_invoice, visit.billed)


def assert_boarding_billable_item_can_cancel(
	boarding_name: str,
	*,
	linked_service_id: str | None = None,
	linked_doctype: str | None = None,
	linked_name: str | None = None,
	order_id: str | None = None,
	item_code: str | None = None,
	item_type: str | None = None,
):
	if not boarding_name or not (linked_service_id or (linked_doctype and linked_name) or order_id):
		return
	boarding = frappe.get_doc("Pet Boarding", boarding_name)
	row = _find_billable_row(
		boarding,
		linked_service_id,
		linked_doctype=linked_doctype,
		linked_name=linked_name,
		order_id=order_id,
		item_code=item_code,
		item_type=item_type,
	)
	if row:
		_assert_parent_billable_row_can_cancel(
			boarding,
			row,
			sales_invoice=boarding.sales_invoice,
			billed=boarding.billing_status == "Invoiced",
		)
	elif boarding.sales_invoice or boarding.billing_status == "Invoiced":
		_assert_parent_billing_state_can_cancel(boarding.sales_invoice, boarding.billing_status == "Invoiced")


def cancel_visit_billable_item(visit_name: str, linked_service_id: str):
	cancel_visit_billable_item_by_link(visit_name, linked_service_id=linked_service_id)


def cancel_visit_billable_item_by_link(
	visit_name: str,
	*,
	linked_service_id: str | None = None,
	linked_doctype: str | None = None,
	linked_name: str | None = None,
	order_id: str | None = None,
	item_code: str | None = None,
	item_type: str | None = None,
	visit=None,
	save: bool = True,
	allow_legacy_service_fallback: bool = False,
	require_match: bool = False,
):
	if not visit_name:
		return
	if not (linked_service_id or (linked_doctype and linked_name) or order_id):
		return

	visit = visit or frappe.get_doc("Vet Visit", visit_name)
	row = _find_billable_row(
		visit,
		linked_service_id,
		linked_doctype=linked_doctype,
		linked_name=linked_name,
		order_id=order_id,
		item_code=item_code,
		item_type=item_type,
	)
	if not row and allow_legacy_service_fallback:
		row = _find_legacy_service_billable_row(
			visit,
			linked_doctype=linked_doctype,
			linked_name=linked_name,
			item_type=item_type,
			raise_on_failure=require_match,
		)
	if not row:
		if require_match:
			_raise_billable_match_failure(
				"No matching billable item was found.",
				visit=visit,
				linked_doctype=linked_doctype,
				linked_name=linked_name,
				item_type=item_type,
				order_id=order_id,
				linked_service_id=linked_service_id,
			)
		return
	_cancel_parent_billable_row(
		visit,
		row,
		sales_invoice=visit.sales_invoice,
		billed=visit.billed,
		recalculate=lambda doc: setattr(doc, "total_billable_amount", apply_billable_item_amounts(doc)),
		save=save,
	)
	return visit


def cancel_boarding_billable_item_by_link(
	boarding_name: str,
	*,
	linked_service_id: str | None = None,
	linked_doctype: str | None = None,
	linked_name: str | None = None,
	order_id: str | None = None,
	item_code: str | None = None,
	item_type: str | None = None,
):
	if not boarding_name:
		return
	if not (linked_service_id or (linked_doctype and linked_name) or order_id):
		return

	boarding = frappe.get_doc("Pet Boarding", boarding_name)
	row = _find_billable_row(
		boarding,
		linked_service_id,
		linked_doctype=linked_doctype,
		linked_name=linked_name,
		order_id=order_id,
		item_code=item_code,
		item_type=item_type,
	)
	if not row:
		return

	def recalculate(doc):
		doc.run_method("_apply_billable_item_amounts")
		doc.run_method("_compute_totals")
		if not doc.sales_invoice and doc.meta.has_field("billing_status"):
			doc.billing_status = "Unbilled"

	_cancel_parent_billable_row(
		boarding,
		row,
		sales_invoice=boarding.sales_invoice,
		billed=boarding.billing_status == "Invoiced",
		recalculate=recalculate,
	)


def _assert_parent_billing_state_can_cancel(sales_invoice: str | None, billed: bool):
	invoice = frappe.get_doc("Sales Invoice", sales_invoice) if sales_invoice else None
	if invoice and invoice.docstatus == 1:
		frappe.throw(_(ISSUED_INVOICE_CANCEL_MESSAGE).format(frappe.bold(invoice.name)))
	if invoice and invoice.docstatus == 0:
		return
	if invoice:
		frappe.throw(_(ISSUED_INVOICE_CANCEL_MESSAGE).format(frappe.bold(invoice.name)))
	if billed:
		frappe.throw(_(BILLED_VISIT_LOCK_MESSAGE))


def _assert_parent_billable_row_can_cancel(parent, row, *, sales_invoice: str | None, billed: bool):
	if row.status == "Cancelled":
		return
	invoice = frappe.get_doc("Sales Invoice", sales_invoice) if sales_invoice else None
	if invoice and invoice.docstatus == 1:
		frappe.throw(_(ISSUED_INVOICE_CANCEL_MESSAGE).format(frappe.bold(invoice.name)))
	if invoice and invoice.docstatus == 0:
		if not _find_draft_invoice_item_for_billable(parent, invoice, row):
			frappe.throw(
				_(
					"Could not safely match this order to a line on draft Sales Invoice {0}. "
					"Please contact the cashier."
				).format(frappe.bold(invoice.name))
			)
		return
	if invoice:
		frappe.throw(_(ISSUED_INVOICE_CANCEL_MESSAGE).format(frappe.bold(invoice.name)))
	if row.status == "Billed":
		# The row is invoiced even though the parent may not be - per-order billing raises
		# a charge while its visit stays open. Saying "this visit is already billed" about
		# an open visit gives the operator nothing to act on; name the charge instead.
		frappe.throw(_(BILLED_ROW_LOCK_MESSAGE).format(frappe.bold(billable_row_label(row))))
	if billed:
		frappe.throw(_(BILLED_VISIT_LOCK_MESSAGE))


def _cancel_parent_billable_row(parent, row, *, sales_invoice: str | None, billed: bool, recalculate, save: bool = True):
	_assert_parent_billable_row_can_cancel(parent, row, sales_invoice=sales_invoice, billed=billed)
	if row.status == "Cancelled":
		return

	invoice = frappe.get_doc("Sales Invoice", sales_invoice) if sales_invoice else None
	if invoice and invoice.docstatus == 0:
		_remove_draft_invoice_item_for_billable(parent, invoice, row)

	row.status = "Cancelled"
	recalculate(parent)
	parent.flags.ignore_billing_lock = True
	if save:
		parent.save(ignore_permissions=True)


def _remove_draft_invoice_item_for_billable(parent, invoice, billable_row):
	invoice_item = _find_draft_invoice_item_for_billable(parent, invoice, billable_row)
	if not invoice_item:
		frappe.throw(
			_(
				"Could not safely match this order to a line on draft Sales Invoice {0}. "
				"Please contact the cashier."
			).format(frappe.bold(invoice.name))
		)

	if len(invoice.items or []) == 1:
		# Every record pointing at this invoice loses its link, not just `parent`.
		# Clearing only the caller left the others linked to a deleted document, and a
		# Vet Visit with sales_invoice set is permanently locked by
		# _validate_sales_invoice_lock - unreachable and uneditable.
		_clear_invoice_backlinks(invoice.name, skip=parent)
		frappe.delete_doc("Sales Invoice", invoice.name, force=True, ignore_permissions=True)
		_reset_parent_billing_state(parent)
		return

	invoice.remove(invoice_item)
	invoice.flags.ignore_permissions = True
	invoice.save(ignore_permissions=True)


def _invoice_lines_for_parent(parent, invoice):
	"""The lines on this invoice that came from THIS record.

	An invoice may now hold several visits' and boardings' charges, so matching has to be
	scoped before anything else is tried. Lines written before source markers existed
	carry none; for those the whole invoice is the scope, which is only safe because such
	an invoice necessarily predates merging.
	"""
	all_rows = list(invoice.items or [])
	marked = [
		row for row in all_rows
		if has_marker(row.get("description"), parent.doctype, parent.name)
	]
	return marked or all_rows


def _reset_parent_billing_state(parent):
	if parent.meta.has_field("sales_invoice"):
		parent.sales_invoice = None
	if parent.meta.has_field("billed"):
		parent.billed = 0
	if parent.meta.has_field("billing_status"):
		parent.billing_status = "Unbilled"


def _records_linked_to_invoice(invoice_name: str) -> list[tuple[str, str]]:
	linked = []
	for doctype in INVOICE_LINKED_DOCTYPES:
		for name in frappe.get_all(
			doctype, filters={"sales_invoice": invoice_name}, pluck="name", ignore_permissions=True
		):
			linked.append((doctype, name))
	return linked


def _clear_invoice_backlinks(invoice_name: str, *, skip=None):
	"""Drop the sales_invoice link on every record except `skip`.

	`skip` is the document the caller already holds in memory and will save itself;
	saving it here too would fight that write.
	"""
	skipped = (skip.doctype, skip.name) if skip is not None else None
	for doctype, name in _records_linked_to_invoice(invoice_name):
		if skipped and (doctype, name) == skipped:
			continue
		doc = frappe.get_doc(doctype, name)
		_reset_parent_billing_state(doc)
		doc.flags.ignore_billing_lock = True
		doc.save(ignore_permissions=True)


def _find_draft_invoice_item_for_billable(parent, invoice, billable_row):
	# Scope first. Everything below then reasons about one record's own lines, which is
	# what the ordinal and uniqueness assumptions were always relying on.
	scope = _invoice_lines_for_parent(parent, invoice)

	active_rows = [row for row in parent.billable_items or [] if row.status != "Cancelled"]
	for index, row in enumerate(active_rows):
		if row.name == billable_row.name and index < len(scope):
			candidate = scope[index]
			if _invoice_item_matches_billable(candidate, billable_row):
				return candidate
			break

	matches = [row for row in scope if _invoice_item_matches_billable(row, billable_row)]
	if len(matches) == 1:
		return matches[0]
	return None


def _invoice_item_matches_billable(invoice_item, billable_row) -> bool:
	if invoice_item.item_code != billable_row.item_code:
		return False
	if flt(invoice_item.qty) != flt(billable_row.qty):
		return False
	if flt(invoice_item.rate) != flt(billable_row.rate):
		return False
	if flt(invoice_item.amount) != flt(billable_row.amount):
		return False
	billable_description = cstr(billable_row.get("item_name") or billable_row.get("item_code")).strip()
	# The provenance marker is machine text appended to the description; comparing it
	# against a billable row's item name would never match.
	invoice_description = strip_markers(invoice_item.get("description"))
	return not invoice_description or invoice_description == billable_description


def on_sales_invoice_cancel(doc, method=None):
	"""Release EVERY record billed onto this invoice.

	This used to read a single Vet Visit via get_value(). Under invoice reuse an invoice
	carries several records' charges, so all but one were left with billed = 1 pointing
	at a cancelled document - and _validate_sales_invoice_lock then refuses every edit,
	so the record could never be re-billed or corrected. Pet Boarding had no handler at
	all and was always left stranded.
	"""
	# Orders billed on their own completion point at this invoice too, and they are not in
	# INVOICE_LINKED_DOCTYPES because they cannot go through the save() below: re-saving a
	# Lab or Imaging re-runs set_values_from_visit, which throws the billed-visit lock
	# whenever the visit has an invoice of its own - the normal state once per-order
	# billing is in use. Handled first, with db.set_value, so the loop below is unchanged.
	from pet_app.utils.order_billing import release_orders_billed_to_invoice

	release_orders_billed_to_invoice(doc.name)

	for doctype, name in _records_linked_to_invoice(doc.name):
		parent = frappe.get_doc(doctype, name)
		for row in parent.billable_items or []:
			if row.status == "Billed":
				row.status = "Billable"

		_reset_parent_billing_state(parent)
		if parent.meta.has_field("total_billable_amount"):
			parent.total_billable_amount = apply_billable_item_amounts(parent)
		parent.flags.ignore_billing_lock = True
		parent.save(ignore_permissions=True)
		log_visit_billing_event(
			"INVOICE_CANCELLED", visit=parent.name, doctype=doctype, sales_invoice=doc.name
		)


def _find_billable_row(
	visit,
	linked_service_id: str | None,
	*,
	linked_doctype: str | None = None,
	linked_name: str | None = None,
	order_id: str | None = None,
	item_code: str | None = None,
	item_type: str | None = None,
):
	for row in visit.billable_items or []:
		if row.linked_service_id == linked_service_id:
			return row
		if row.get("linked_name") == linked_service_id:
			return row
		if linked_name and row.linked_service_id == linked_name:
			return row
		if linked_doctype and linked_name and row.get("linked_doctype") == linked_doctype and row.get("linked_name") == linked_name:
			return row
		if (
			order_id
			and item_code
			and item_type
			and row.get("order_id") == order_id
			and row.item_code == item_code
			and row.item_type == item_type
			and (not linked_doctype or not row.get("linked_doctype") or row.get("linked_doctype") == linked_doctype)
		):
			return row
	return None


def _find_legacy_service_billable_row(
	visit,
	*,
	linked_doctype: str | None,
	linked_name: str | None,
	item_type: str | None,
	raise_on_failure: bool = False,
):
	if linked_doctype != "PetCareService" or not linked_name or item_type != "Service":
		return None
	service = frappe.db.get_value(
		"PetCareService",
		linked_name,
		["name", "visit", "care_service_id"],
		as_dict=True,
	)
	if not service or service.visit != visit.name or not service.care_service_id:
		if raise_on_failure:
			_raise_billable_match_failure(
				"PetCareService does not match this visit or has no Care Service template.",
				visit=visit,
				linked_doctype=linked_doctype,
				linked_name=linked_name,
				item_type=item_type,
			)
		return None
	service_matches = frappe.get_all(
		"PetCareService",
		filters={"visit": visit.name, "care_service_id": service.care_service_id},
		pluck="name",
		ignore_permissions=True,
	)
	if service_matches != [linked_name]:
		if raise_on_failure:
			_raise_billable_match_failure(
				"Legacy service record match was not unique.",
				visit=visit,
				linked_doctype=linked_doctype,
				linked_name=linked_name,
				item_type=item_type,
				care_service=service.care_service_id,
				matches=len(service_matches),
			)
		return None
	legacy_rows = frappe.get_all(
		"custom services",
		filters={
			"parenttype": "Vet Visit",
			"parentfield": "care_services",
			"parent": visit.name,
			"care_service_id": service.care_service_id,
		},
		fields=["name"],
		order_by="creation asc",
		ignore_permissions=True,
	)
	if len(legacy_rows) != 1:
		if raise_on_failure:
			_raise_billable_match_failure(
				"Legacy service billable match was not unique.",
				visit=visit,
				linked_doctype=linked_doctype,
				linked_name=linked_name,
				item_type=item_type,
				care_service=service.care_service_id,
				matches=len(legacy_rows),
			)
		return None
	legacy_key = f"visit-care-service::{legacy_rows[0].name}"
	matches = [
		row
		for row in visit.billable_items or []
		if row.linked_service_id == legacy_key and row.item_type == "Service"
	]
	if len(matches) == 1:
		return matches[0]
	if raise_on_failure:
		_raise_billable_match_failure(
			"Legacy service billable row was not unique.",
			visit=visit,
			linked_doctype=linked_doctype,
			linked_name=linked_name,
			item_type=item_type,
			care_service=service.care_service_id,
			legacy_key=legacy_key,
			matches=len(matches),
		)
	return None


def _raise_billable_match_failure(message: str, **context):
	visit = context.pop("visit", None)
	if visit is not None:
		context["visit"] = getattr(visit, "name", visit)
	log_visit_billing_event(
		"LEGACY_SERVICE_BILLABLE_MATCH_FAILED",
		reason=message,
		**context,
	)
	frappe.throw(
		_(
			"Could not safely match this service to exactly one billable line. "
			"Cancellation was not applied. Please contact the cashier."
		)
	)


def _set_if_field(row, fieldname: str, value):
	if value in (None, ""):
		return
	if row.meta.has_field(fieldname):
		row.set(fieldname, value)
