"""Medication settings API: list and save a Medication with its selling and buying price.

The medication settings page has always called `get_medications` and `upsert_medication`. This
module did not exist, so every load failed with ModuleNotFoundError and the page fell back to the
generic REST resource. That path cannot select `selling_price` / `buying_price` (not Medication
fields) - eight "Field not permitted in query" retries on every load - and it saves without
`buying_price`, silently dropping it while still reporting success.

SELLING PRICE is unchanged. `default_price` is written to the doc, and Medication.validate
(`_sync_item_defaults`) syncs Item.standard_rate and the veterinary selling Item Price exactly as
before. `selling_price` in the response is `get_item_effective_rate`, the doctype's own reader.

BUYING PRICE lives on the linked Item as an Item Price row on Standard Buying, the site's only
buying price list. Nothing is added to Medication. Purchase flows can leave several rows on that
list for one item (dated, per UOM, per supplier), so read and write share one selector,
`_buying_price_row`: the general row (no supplier, customer or batch) valid today, latest
`valid_from` first. A save updates that row's rate in place, keeping its UOM and dates, so what is
saved is what is read back. Only when no such row exists is one created, in stock UOM, as
`upsert_item_price` does for selling. The page sends 0 for an empty field, so 0 with no existing
row means "not entered" and creates nothing.

UNKNOWN PAYLOAD KEYS ARE IGNORED. Only `WRITABLE_FIELDS` the doctype actually has are set; the page
still sends species, route, contraindications, requires_prescription, controlled_drug and
active_ingredient, which are not Medication fields and are deliberately not added.

ERROR TEXT MUST NOT SAY "not found". The page's methodFallback classifies any error matching
/not found/i as a missing backend method, remembers it for the whole session and reverts to the
REST path that drops the buying price - so one bad save would reintroduce the bug until reload.
"""

from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, nowdate, strip_html

from pet_app.api.permissions import require_doctype_permission
from pet_app.api.response import fail, ok
from pet_app.pet_app.doctype.medication.medication import get_item_effective_rate
from pet_app.utils.price_list import DEFAULT_BUYING_PRICE_LIST as BUYING_PRICE_LIST

DOCTYPE = "Medication"
DEFAULT_LIMIT = 100
MAX_LIMIT = 500

WRITABLE_FIELDS = (
	"medication_name",
	"code",
	"linked_item",
	"default_warehouse",
	"default_price",
	"dosage_form_or_unit",
	"strength",
	"default_dosage",
	"default_frequency",
	"default_duration_days",
	"default_instructions",
	"disabled",
)
LIST_FIELDS = (
	"name",
	"medication_name",
	"code",
	"linked_item",
	"default_warehouse",
	"default_price",
	"dosage_form_or_unit",
	"strength",
	"default_dosage",
	"default_frequency",
	"default_duration_days",
	"default_instructions",
	"disabled",
	"reference_count",
	"given_count",
	"total_dispensed_amount",
	"modified",
)
SEARCH_FIELDS = ("name", "medication_name", "code", "linked_item", "default_warehouse")


@frappe.whitelist(methods=["GET", "POST"])
def get_medications(search=None, disabled=None, limit=DEFAULT_LIMIT, cursor=0):
	try:
		require_doctype_permission(DOCTYPE, "read")

		filters = {}
		if disabled not in (None, ""):
			filters["disabled"] = cint(disabled)

		or_filters = None
		search = cstr(search).strip()
		if search:
			or_filters = [[DOCTYPE, field, "like", f"%{search}%"] for field in SEARCH_FIELDS]

		limit = min(max(cint(limit) or DEFAULT_LIMIT, 1), MAX_LIMIT)
		cursor = max(cint(cursor), 0)

		total = len(frappe.get_all(DOCTYPE, filters=filters, or_filters=or_filters, pluck="name"))
		rows = frappe.get_all(
			DOCTYPE,
			filters=filters,
			or_filters=or_filters,
			fields=list(LIST_FIELDS),
			order_by="modified desc",
			limit_start=cursor,
			limit_page_length=limit,
		)
		return ok({"items": _with_prices(rows), "total": total})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def upsert_medication(**payload):
	try:
		payload.pop("cmd", None)
		buying_price = payload.get("buying_price")
		if buying_price not in (None, "") and flt(buying_price) < 0:
			frappe.throw(_("Buying price cannot be negative."))

		name = cstr(payload.get("name")).strip()
		if name:
			if not frappe.db.exists(DOCTYPE, name):
				frappe.throw(_("Medication {0} does not exist.").format(frappe.bold(name)))
			require_doctype_permission(DOCTYPE, "write")
			doc = frappe.get_doc(DOCTYPE, name)
		else:
			require_doctype_permission(DOCTYPE, "create")
			doc = frappe.new_doc(DOCTYPE)

		values = {field: payload[field] for field in WRITABLE_FIELDS if field in payload and doc.meta.has_field(field)}
		selling_price = payload.get("selling_price", payload.get("default_price"))
		if selling_price not in (None, ""):
			values["default_price"] = flt(selling_price)
		# An empty linked_item never unlinks a saved Medication: validate would then match or
		# create a different Item for it.
		if not doc.is_new() and not cstr(values.get("linked_item")).strip():
			values.pop("linked_item", None)

		doc.update(values)
		doc.flags.ignore_permissions = True
		doc.save()

		_apply_buying_price(doc.linked_item, buying_price)
		return ok(_medication_row(doc.name))
	except Exception as exc:
		frappe.db.rollback()
		return _error_response(exc)


def _apply_buying_price(item_code: str | None, value) -> None:
	if not item_code or value in (None, ""):
		return

	rate = flt(value)
	row = _buying_price_row(item_code)
	if row:
		if flt(row.price_list_rate) == rate:
			return
		item_price = frappe.get_doc("Item Price", row.name)
	else:
		if rate <= 0:
			return
		item_price = frappe.new_doc("Item Price")
		item_price.item_code = item_code
		item_price.price_list = BUYING_PRICE_LIST
		item_price.uom = None

	item_price.buying = 1
	item_price.price_list_rate = rate
	item_price.flags.ignore_permissions = True
	item_price.save()


def _buying_price_row(item_code: str | None):
	if not item_code:
		return None
	rows = frappe.db.sql(
		"""
		select name, price_list_rate
		from `tabItem Price`
		where item_code = %(item_code)s
			and price_list = %(price_list)s
			and ifnull(customer, '') = ''
			and ifnull(supplier, '') = ''
			and ifnull(batch_no, '') = ''
			and (valid_from is null or valid_from <= %(today)s)
			and (valid_upto is null or valid_upto >= %(today)s)
		order by ifnull(valid_from, '1900-01-01') desc, modified desc
		limit 1
		""",
		{"item_code": item_code, "price_list": BUYING_PRICE_LIST, "today": nowdate()},
		as_dict=True,
	)
	return rows[0] if rows else None


def _medication_row(name: str) -> dict:
	return _with_prices(frappe.get_all(DOCTYPE, filters={"name": name}, fields=list(LIST_FIELDS)))[0]


def _with_prices(rows: list[dict]) -> list[dict]:
	items = sorted({row.linked_item for row in rows if row.linked_item})
	warehouses = sorted({row.default_warehouse for row in rows if row.default_warehouse})
	item_names = (
		dict(frappe.get_all("Item", filters={"name": ["in", items]}, fields=["name", "item_name"], as_list=True))
		if items
		else {}
	)
	warehouse_names = (
		dict(
			frappe.get_all(
				"Warehouse", filters={"name": ["in", warehouses]}, fields=["name", "warehouse_name"], as_list=True
			)
		)
		if warehouses
		else {}
	)

	out = []
	for row in rows:
		row = frappe._dict(row)
		buying = _buying_price_row(row.linked_item)
		row.linked_item_name = item_names.get(row.linked_item)
		row.default_warehouse_name = warehouse_names.get(row.default_warehouse)
		row.selling_price = get_item_effective_rate(row.linked_item)
		row.buying_price = flt(buying.price_list_rate) if buying else None
		out.append(row)
	return out


def _error_response(exc):
	if isinstance(exc, frappe.PermissionError):
		return fail(_("Not permitted"), code="PERMISSION_DENIED")
	message = re.sub(r"not found", "missing", strip_html(cstr(exc)), flags=re.IGNORECASE)
	return fail(message or _("Could not complete the medication request."), code=exc.__class__.__name__)
