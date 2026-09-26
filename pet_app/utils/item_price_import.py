"""Bulk update Item buying/selling prices from a price-review spreadsheet.

The shop reviews prices in a spreadsheet keyed by barcode, and on this site the
barcode *is* the ``Item.item_code`` (the Pre Define Item convention). This module
reads such a sheet and upserts the ``Item Price`` rows for the buying and selling
price lists.

It only ever touches ``Item Price``. ``Item.standard_rate`` and
``Item.valuation_rate`` are left alone on purpose: ``valuation_rate`` feeds stock
accounting and must come from real purchase transactions, not from a sheet.

Deliberately not whitelisted -- this runs under ``bench execute`` only:

    bench --site <site> execute pet_app.utils.item_price_import.run
    bench --site <site> execute pet_app.utils.item_price_import.run --kwargs "{'dry_run': 0}"

``dry_run`` defaults to 1: the sheet is classified row by row and a CSV report is
written, but nothing is saved.
"""

from __future__ import annotations

import csv
import os

import frappe
from frappe import _
from frappe.utils import cstr, flt, now_datetime

from pet_app.utils.item_price import RATE_TOLERANCE
from pet_app.utils.price_list import (
	DEFAULT_BUYING_PRICE_LIST as BUYING_PRICE_LIST,
	get_veterinary_selling_price_list,
)

DEFAULT_FILENAME = "مراجعة الأصناف.Xls"

ITEM_CODE_HEADER = "item code"
BUYING_HEADER = "buying"
SELLING_HEADER = "selling"

# Rows held back after review of the dry-run report. Keep the reason with the code:
# these are sheet errors, not policy, and each should be dropped once the sheet is fixed.
SKIP_ITEM_CODES = {
	# Row 1798 prices this at 15 IQD. Its sibling 4771317474728 (Araton chicken
	# 1.5 kg, same 7,750 cost) is 15,000 -- three zeros were dropped. Left
	# unpriced rather than sellable for 15 IQD.
	"4771317474780": "suspected missing zeros in the sheet (15 vs 15000)",
}

COMMIT_EVERY = 200
PROGRESS_EVERY = 250

REPORT_FIELDS = (
	"row",
	"item_code",
	"item_name",
	"buying",
	"selling",
	"buying_old",
	"selling_old",
	"buying_status",
	"selling_status",
	"flags",
	"error",
)


def run(
	file_path: str | None = None,
	dry_run=1,
	commit_every: int = COMMIT_EVERY,
	skip_item_codes=None,
) -> dict:
	"""Import buying and selling prices from ``file_path`` into Item Price.

	``skip_item_codes`` (list, or a comma-separated string) is added to the
	reviewed ``SKIP_ITEM_CODES`` hold-back list for this run.

	Returns a summary dict and writes a per-row CSV report next to the sheet.
	"""
	dry_run = _as_bool(dry_run)
	skipped_codes = set(SKIP_ITEM_CODES) | _as_code_set(skip_item_codes)
	path = _resolve_path(file_path)
	rows = _read_sheet(path)

	selling_price_list = get_veterinary_selling_price_list()
	_validate_price_list(BUYING_PRICE_LIST, buying=True)
	_validate_price_list(selling_price_list, buying=False)

	items = _load_items()
	prices = _load_existing_prices((BUYING_PRICE_LIST, selling_price_list))

	summary = {
		"file": path,
		"dry_run": dry_run,
		"sheet_rows": len(rows),
		"buying_price_list": BUYING_PRICE_LIST,
		"selling_price_list": selling_price_list,
		"unmatched": 0,
		"skipped_manual": 0,
		"sells_below_cost": 0,
		"buying": _empty_counts(),
		"selling": _empty_counts(),
	}
	report: list[dict] = []
	written = 0

	for index, (row_number, code, buying, selling) in enumerate(rows, start=1):
		item = items.get(code)
		entry = {
			"row": row_number,
			"item_code": code,
			"item_name": item["item_name"] if item else "",
			"buying": buying,
			"selling": selling,
			"buying_old": "",
			"selling_old": "",
			"buying_status": "",
			"selling_status": "",
			"flags": [],
			"error": "",
		}

		if not item:
			entry["buying_status"] = entry["selling_status"] = "unmatched"
			summary["unmatched"] += 1
			report.append(_finish_entry(entry))
			continue

		if code in skipped_codes:
			entry["buying_status"] = entry["selling_status"] = "skipped_manual"
			entry["flags"].append("held_back")
			entry["error"] = SKIP_ITEM_CODES.get(code, "held back by caller")
			summary["skipped_manual"] += 1
			report.append(_finish_entry(entry))
			continue

		if item["disabled"]:
			entry["flags"].append("item_disabled")
		if buying > 0 and 0 < selling < buying:
			entry["flags"].append("sells_below_cost")
			summary["sells_below_cost"] += 1

		targets = (
			("buying", BUYING_PRICE_LIST, buying),
			("selling", selling_price_list, selling),
		)
		for key, price_list, rate in targets:
			status, old_rate, error = _apply(
				item=item,
				price_list=price_list,
				rate=rate,
				prices=prices,
				dry_run=dry_run,
			)
			entry[f"{key}_status"] = status
			entry[f"{key}_old"] = "" if old_rate is None else old_rate
			summary[key][status] = summary[key].get(status, 0) + 1
			if status == "conflict" and "duplicate_price_rows" not in entry["flags"]:
				entry["flags"].append("duplicate_price_rows")
			if error:
				entry["error"] = error
			if status in ("created", "updated"):
				written += 1

		report.append(_finish_entry(entry))

		if not dry_run and commit_every and written and written % commit_every == 0:
			# bench execute owns this transaction, so partial progress can be made
			# durable. Never do this from a web request or a test.
			frappe.db.commit()

		if index % PROGRESS_EVERY == 0:
			print(f"  ... {index}/{len(rows)} rows")

	if not dry_run:
		frappe.db.commit()

	summary["report"] = _write_report(path, report, dry_run)
	_print_summary(summary)
	return summary


# ─────────────────────────────────────────
# Upsert
# ─────────────────────────────────────────

# The general-row selector shared with Stock Price lives in `pet_app/utils/item_price.py`.
# This function deliberately keeps its own, stricter one: it matches every row for the pair
# regardless of validity dates so that `len(existing) > 1` surfaces as `conflict` in the CSV.
# An unattended bulk import must refuse an ambiguous item rather than pick one; Stock Price
# picks one, deterministically, and shows the user on screen which row it picked.
def _apply(
	*, item: dict, price_list: str, rate: float, prices: dict, dry_run: bool
) -> tuple[str, float | None, str]:
	"""Classify (and unless ``dry_run``, perform) one Item Price upsert.

	Returns ``(status, rate_being_replaced, error)``.
	"""
	existing = prices.get((item["name"], price_list), [])
	old_rate = flt(existing[0]["price_list_rate"]) if len(existing) == 1 else None

	if rate <= 0:
		# A zero in the sheet means "not priced yet", not "worth nothing". Writing
		# it would let the item sell for free, so leave any existing row alone.
		return "skipped_zero", old_rate, ""

	if len(existing) > 1:
		# Two authoritative rows and no way to tell which one wins. Report it.
		return "conflict", None, ""

	if existing:
		if abs(old_rate - rate) < RATE_TOLERANCE:
			return "unchanged", old_rate, ""
		status = "updated"
	else:
		status = "created"

	if dry_run:
		return status, old_rate, ""

	savepoint = "item_price_import_" + frappe.generate_hash(length=8)
	frappe.db.savepoint(savepoint)
	try:
		if existing:
			doc = frappe.get_doc("Item Price", existing[0]["name"])
		else:
			doc = frappe.new_doc("Item Price")
			doc.item_code = item["name"]
			doc.price_list = price_list
			# uom is required and ERPNext validates it against the item's UOM
			# Conversion Detail, so stock_uom is the only always-valid choice. It
			# also matches the shape of the rows already on the site, which keeps
			# ItemPrice.check_duplicates from spawning a twin.
			doc.uom = item["stock_uom"]
		# buying/selling/currency are overwritten from the Price List by
		# ItemPrice.update_price_list_details, so setting them here is pointless.
		doc.price_list_rate = rate
		doc.flags.ignore_permissions = True
		doc.save()
		frappe.db.release_savepoint(savepoint)
	except Exception as exc:
		frappe.db.rollback(save_point=savepoint)
		return "error", old_rate, _error_message(exc)

	return status, old_rate, ""


# ─────────────────────────────────────────
# Sheet reading
# ─────────────────────────────────────────

def _read_sheet(path: str) -> list[tuple]:
	"""Return ``[(row_number, item_code, buying, selling), ...]`` from the sheet."""
	extension = os.path.splitext(path)[1].lower()
	if extension == ".xls":
		table = _read_xls(path)
	elif extension in (".xlsx", ".xlsm"):
		table = _read_xlsx(path)
	else:
		frappe.throw(_("Unsupported spreadsheet format {0}. Expected .xls or .xlsx.").format(extension))

	if not table:
		frappe.throw(_("{0} is empty.").format(path))

	header = [cstr(cell).strip().lower() for cell in table[0]]
	columns = {}
	for label in (ITEM_CODE_HEADER, BUYING_HEADER, SELLING_HEADER):
		if label not in header:
			frappe.throw(
				_("Column {0} not found in {1}. Found: {2}").format(label, path, ", ".join(header))
			)
		columns[label] = header.index(label)

	rows = []
	for offset, raw in enumerate(table[1:], start=2):
		code = _normalise_code(_cell(raw, columns[ITEM_CODE_HEADER]))
		if not code:
			continue
		rows.append(
			(
				offset,
				code,
				flt(_cell(raw, columns[BUYING_HEADER])),
				flt(_cell(raw, columns[SELLING_HEADER])),
			)
		)
	return rows


def _read_xls(path: str) -> list[list]:
	import xlrd

	sheet = xlrd.open_workbook(path).sheet_by_index(0)
	return [[sheet.cell_value(r, c) for c in range(sheet.ncols)] for r in range(sheet.nrows)]


def _read_xlsx(path: str) -> list[list]:
	import openpyxl

	workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
	return [list(row) for row in workbook.worksheets[0].iter_rows(values_only=True)]


def _cell(row: list, index: int):
	return row[index] if index < len(row) else None


def _normalise_code(value) -> str:
	"""Barcodes are text. A sheet saved with numeric codes must not become ``123.0``."""
	if value is None:
		return ""
	if isinstance(value, float) and value.is_integer():
		return str(int(value))
	return cstr(value).strip()


# ─────────────────────────────────────────
# Lookups
# ─────────────────────────────────────────

def _load_items() -> dict:
	rows = frappe.get_all(
		"Item",
		fields=["name", "item_name", "stock_uom", "disabled", "has_variants"],
		limit_page_length=0,
	)
	return {row.name: dict(row) for row in rows if not row.has_variants}


def _load_existing_prices(price_lists) -> dict:
	rows = frappe.get_all(
		"Item Price",
		filters={"price_list": ["in", list(price_lists)]},
		fields=["name", "item_code", "price_list", "price_list_rate"],
		limit_page_length=0,
	)
	prices: dict = {}
	for row in rows:
		prices.setdefault((row.item_code, row.price_list), []).append(dict(row))
	return prices


def _validate_price_list(price_list: str, *, buying: bool) -> None:
	row = frappe.db.get_value("Price List", price_list, ["enabled", "buying", "selling"], as_dict=True)
	if not row:
		# Standard Buying is enabled by enforce_iqd_defaults but never created by
		# seed_initial_master_data, so on a fresh site it can genuinely be missing.
		frappe.throw(_("Price List {0} does not exist.").format(frappe.bold(price_list)))
	if not row.enabled:
		frappe.throw(_("Price List {0} is disabled.").format(frappe.bold(price_list)))
	if buying and not row.buying:
		frappe.throw(_("Price List {0} is not a buying price list.").format(frappe.bold(price_list)))
	if not buying and not row.selling:
		frappe.throw(_("Price List {0} is not a selling price list.").format(frappe.bold(price_list)))


# ─────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────

def _empty_counts() -> dict:
	return {"created": 0, "updated": 0, "unchanged": 0, "skipped_zero": 0, "conflict": 0, "error": 0}


def _finish_entry(entry: dict) -> dict:
	entry["flags"] = ",".join(entry["flags"])
	return entry


def _write_report(source_path: str, report: list[dict], dry_run: bool) -> str:
	stamp = now_datetime().strftime("%Y%m%d-%H%M%S")
	suffix = "dryrun" if dry_run else "applied"
	path = os.path.join(os.path.dirname(source_path), f"item-price-import-{stamp}-{suffix}.csv")
	with open(path, "w", newline="", encoding="utf-8-sig") as handle:
		writer = csv.DictWriter(handle, fieldnames=list(REPORT_FIELDS))
		writer.writeheader()
		writer.writerows(report)
	return path


def _print_summary(summary: dict) -> None:
	mode = "DRY RUN (nothing saved)" if summary["dry_run"] else "APPLIED"
	print(f"\n{mode} -- {summary['sheet_rows']} sheet rows from {summary['file']}")
	for key in ("buying", "selling"):
		label = summary[f"{key}_price_list"]
		counts = ", ".join(f"{name}={value}" for name, value in summary[key].items() if value)
		print(f"  {label:<18} {counts or 'nothing to do'}")
	print(f"  unmatched item codes: {summary['unmatched']}")
	print(f"  rows held back for review: {summary['skipped_manual']}")
	print(f"  rows selling below cost: {summary['sells_below_cost']}")
	print(f"  report: {summary['report']}\n")


# ─────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────

def _resolve_path(file_path: str | None) -> str:
	path = file_path or frappe.get_site_path("private", "files", DEFAULT_FILENAME)
	path = os.path.abspath(path)
	if not os.path.isfile(path):
		frappe.throw(_("Spreadsheet not found: {0}").format(path))
	return path


def _as_code_set(value) -> set:
	if not value:
		return set()
	if isinstance(value, str):
		value = value.split(",")
	return {cstr(item).strip() for item in value if cstr(item).strip()}


def _as_bool(value) -> bool:
	if isinstance(value, str):
		return value.strip().lower() not in ("0", "", "false", "no")
	return bool(value)


def _error_message(exc: Exception) -> str:
	return cstr(getattr(exc, "message", None) or exc)[:500].replace("\n", " ")
