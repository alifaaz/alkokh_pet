"""Align `Delivery Partner Settlement` with the field names the frontend actually queries.

The settlements tab showed nothing for any partner. Not because the link was missing - the
doctype shipped with a required Link to Delivery Partner - but because it was called
`delivery_partner` and the client filters on `partner`:

    /api/resource/Delivery Partner Settlement?filters=[["partner","=","test"]]&fields=[...]

Frappe fails the ENTIRE list query when one requested field does not exist, so every
settlement was invisible and the tab read as "this partner has none". The same request
recorded the rest of the client's expected field list in the Error Log, which is where
`statement_reference`, `commission_rate`, `adjustment_account`, `invoice_count` and
`remarks` come from. This patch is that list, applied.

RENAMES, NOT ADDITIONS, for the two that already held data
----------------------------------------------------------
`delivery_partner` -> `partner` and `notes` -> `remarks` are done with
`frappe.db.rename_column`, following `rename_guardian_typo_fields`. A plain reload would
have added the new column and left the old one orphaned beside it, so the settlement
already on this site would have kept its partner in a column nothing reads and appeared
unlinked - the exact symptom this patch exists to remove, preserved through the fix.

Ordered: rename FIRST, then reload. The reload creates any column the rename did not
already produce, so a fresh site - where the doctype JSON already carries the new names and
there is nothing to rename - passes straight through and this patch is a no-op.

`adjustment_account` is a real modelling change, not just a name. A clawback or a rounding
difference is not commission, and booking both to one account makes the commission figure
useless for the reporting anyone actually looks at it for. Existing rows are backfilled
from the partner's commission account so nothing is left unbooked.
"""

from __future__ import annotations

import frappe
from frappe.utils import flt, get_table_name

DOCTYPE = "Delivery Partner Settlement"
RENAMES = (("delivery_partner", "partner"), ("notes", "remarks"))

# Left on `tabDelivery Partner` by the same correction applied to the partner doctype: the
# field names there were fixed by editing the JSON and reloading, which ADDS the new column
# and leaves the old one beside it. There were no partner rows at that moment, so these
# carry nothing - and `_drop_if_empty` re-establishes that at run time rather than trusting
# it, because a column with data in it is somebody's record and not this patch's to delete.
ORPHANED_PARTNER_COLUMNS = ("commission_account", "phone", "email")


def execute():
	if not frappe.db.table_exists(DOCTYPE):
		# The creating patch has not run on this site yet; it will ship the new names.
		return

	for old, new in RENAMES:
		_rename_column(DOCTYPE, old, new)

	frappe.reload_doc("pet_app", "doctype", "delivery_partner_settlement", force=True)

	missing = [
		f
		for f in (
			"partner", "partner_name", "statement_reference", "commission_rate",
			"adjustment_account", "invoice_count", "remarks",
		)
		if not frappe.db.has_column(DOCTYPE, f)
	]
	if missing:
		frappe.log_error(
			title="DELIVERY_PARTNER_SETTLEMENT_FIELDS_MISSING",
			message=f"{DOCTYPE} is missing {', '.join(missing)} after the reload.",
		)
		return

	_backfill()

	for column in ORPHANED_PARTNER_COLUMNS:
		_drop_if_empty("Delivery Partner", column)

	frappe.clear_cache(doctype="Delivery Partner")
	frappe.clear_cache(doctype=DOCTYPE)
	frappe.logger("pet_app.migrate").info(
		{"event": "DELIVERY_PARTNER_SETTLEMENT_ALIGNED", "rows": frappe.db.count(DOCTYPE)}
	)


def _rename_column(doctype: str, old_fieldname: str, new_fieldname: str):
	"""Same shape as `rename_guardian_typo_fields._rename_column`.

	If BOTH columns exist the new one already won - a reload ran before this patch did -
	so the old one is dropped rather than copied over the top of live data.
	"""
	if frappe.db.has_column(doctype, new_fieldname):
		if frappe.db.has_column(doctype, old_fieldname):
			frappe.db.sql_ddl(
				f"ALTER TABLE `{get_table_name(doctype)}` DROP COLUMN `{old_fieldname}`"
			)
		return
	if frappe.db.has_column(doctype, old_fieldname):
		frappe.db.rename_column(doctype, old_fieldname, new_fieldname)


def _backfill():
	"""Fill the columns the client reads, for settlements that predate them.

	Derived from what each settlement already stores, never recomputed from live partner
	data: `commission_rate` is this settlement's own commission over its own gross, so a
	partner who has since renegotiated cannot restate a settlement that is already banked.
	"""
	for row in frappe.get_all(
		DOCTYPE,
		fields=["name", "partner", "gross_amount", "commission_amount", "adjustment_amount"],
	):
		values = {}

		partner_name, commission_account = frappe.db.get_value(
			"Delivery Partner", row.partner, ["partner_name", "commission_expense_account"]
		) or (None, None)
		if partner_name:
			values["partner_name"] = partner_name
		if commission_account and flt(row.adjustment_amount):
			values["adjustment_account"] = commission_account

		if flt(row.gross_amount):
			values["commission_rate"] = flt(
				flt(row.commission_amount) * 100.0 / flt(row.gross_amount)
			)

		values["invoice_count"] = frappe.db.count(
			"Delivery Partner Settlement Invoice", {"parent": row.name}
		)

		if values:
			frappe.db.set_value(DOCTYPE, row.name, values, update_modified=False)


def _drop_if_empty(doctype: str, column: str):
	"""Drop a leftover column, but ONLY when every row in it is null.

	The guard is the point. A column that turns out to hold something is evidence the
	assumption behind this cleanup is wrong, and dropping it would destroy the only copy;
	leaving it costs nothing but a little untidiness. So the unsafe case does nothing and
	says so, rather than proceeding on the strength of a comment.
	"""
	if not frappe.db.has_column(doctype, column):
		return
	if column in {f.fieldname for f in frappe.get_meta(doctype).fields}:
		# Not an orphan at all - it is a live field. Never touch it.
		return

	table = get_table_name(doctype)
	filled = frappe.db.sql(
		f"select count(*) from `{table}` where `{column}` is not null and `{column}` != ''"
	)[0][0]
	if filled:
		frappe.log_error(
			title="DELIVERY_PARTNER_ORPHAN_COLUMN_NOT_EMPTY",
			message=(
				f"{doctype}.{column} was expected to be an empty leftover but holds "
				f"{filled} value(s). Left in place; migrate it by hand."
			),
		)
		return

	frappe.db.sql_ddl(f"ALTER TABLE `{table}` DROP COLUMN `{column}`")
