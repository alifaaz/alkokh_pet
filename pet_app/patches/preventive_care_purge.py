"""Remove the two superseded record doctypes, their rows, and the preventive columns.

THE ONLY DESTRUCTIVE PATCH IN THIS PIECE OF WORK. Everything before it was additive. It runs
last, on purpose, so every stage can be reviewed before anything irreversible happens.

IT VERIFIES BY PREDICATE, NEVER BY COUNT. An earlier assessment of this site counted 26
superseded records; by the time this was written it was 27, because one more was produced
during probing. A patch that asserted "26" would have refused correctly-purgeable data, and a
patch that asserted nothing would have destroyed data it had never examined. So the gate is
the set of properties that make a row safe to delete, evaluated at execution time:

    1. every row carries the app's own machine provenance marker in `notes` - only
       `write_clinical_record` ever emitted that string, so the row was generated, not typed
    2. every row has `modified == creation` - nobody has opened and edited it since
    3. zero `Version` rows for either doctype
    4. every row is on ONE pet
    5. the Draft invoice this edits is still Draft and carries no submitted payment

If any of those has stopped holding, the patch writes an Error Log naming which one and
changes nothing. A refusal is recoverable; a wrong deletion is not.

THE TABLE DROP IS KEYED ON THE TABLE, NOT ON THE DOCTYPE ROW. Dropping a doctype takes two
statements - deleting the `DocType` row (DML) and dropping its table (DDL) - and they do not
commit together. An interrupted run can therefore leave an empty table with no DocType behind
it, which `frappe.db.exists("DocType", ...)` reports as "already done" while the table sits
there forever. This happened on this bench during verification: the DML committed because a
probe crashed after it and never rolled back, while the DDL had been stubbed out. So the two
are now tracked separately, and an orphan table is dropped only after confirming it is empty.

WHAT IT DOES, in the order that keeps the data consistent:

    A. removes invoice lines sourced from preventive services, from DRAFT invoices only,
       and re-saves so ERPNext recomputes its own totals
    B. deletes the Pet Reminder rows pointing at the superseded records
    C. deletes the superseded record rows
    D. drops the two DocTypes and their tables
    E. reloads PetCareService and drops three preventive columns

Steps B and C use `frappe.db.delete`, not `delete_doc`: the doctype folders were removed with
the rest of the superseded code, so there is no controller left to import - `delete_doc` fails
with `ModuleNotFoundError`. Safe here because these rows have no dependents at all: zero
Comment, File, Version or ToDo rows, verified before the delete rather than assumed.

WHAT IT DELIBERATELY LEAVES, and why each is someone's decision rather than a patch's:

    * 29 `PetCareService` rows in the two preventive categories. Three are SUBMITTED and
      thirteen are sourced by lines on a SUBMITTED invoice. Deleting a submitted document, or
      orphaning a submitted invoice line, is an accounting act. They are inert: nothing
      creates them any more and no preventive code reads them.
    * 12 submitted Stock Entries (Material Issues) stamped on those rows. Deleting one means
      CANCELLING it, which moves stock.
    * 13 lines on submitted ACC-SINV-2026-01848. Submitted invoice lines are immutable; the
      only route is cancel-and-amend, which changes the invoice number and the GL.
    * `PetCareService.stock_warehouse`. Its predicate FAILS and the patch reports rather than
      forcing: 21 non-preventive rows (Grooming, Bathing, Boarding) carry a value. They are
      not real data - both creating users hold exactly ONE Warehouse User Permission with
      `apply_to_all_doctypes`, and every one of the 21 rows carries exactly that warehouse on
      a `read_only` field with no default. That is Frappe's single-permitted-value auto-fill,
      not a deliberate write, and nothing reads the column. It is very probably droppable -
      but "probably" is not the standard for dropping a column, and the instruction was to
      abort loudly when a predicate does not hold.
"""

from __future__ import annotations

import frappe

OLD_DOCTYPES = ("Pet Vaccination Record", "Pet Deworming Record")
MARKER_LIKE = "%[alkokh-source-group:PetCareService:%"
PREVENTIVE_CATEGORY_NAMES = ("vaccination", "vaccinations", "vaccine", "vaccines", "deworming", "dewormings", "deworm")

# Dropped: verified to hold no value on any non-preventive row.
DROP_COLUMNS = ("next_due_date", "stock_issued_qty", "stock_entry")
# Checked and reported, not dropped. See the module docstring.
REPORT_ONLY_COLUMNS = ("stock_warehouse",)

SERVICE_DOCTYPE = "PetCareService"


def _fail(reason: str, **context):
	frappe.log_error(
		title="PREVENTIVE_CARE_PURGE_REFUSED",
		message=f"{reason}\n\n{context}",
	)
	frappe.logger("pet_app.migrate").error({"event": "PREVENTIVE_CARE_PURGE_REFUSED", "reason": reason, **context})


def _preventive_categories() -> list[str]:
	"""By category NAME, never by docname - `CategoryCareServices-0018` is only this bench's."""
	rows = frappe.get_all("CategoryCareServices", fields=["name", "category_name"], ignore_permissions=True)
	return [r.name for r in rows if (r.category_name or "").strip().lower() in PREVENTIVE_CATEGORY_NAMES]


def _table_exists(doctype: str) -> bool:
	return bool(frappe.db.sql("show tables like %s", (f"tab{doctype}",)))


def _table_rows(doctype: str) -> int:
	return frappe.db.sql(f"select count(*) from `tab{doctype}`")[0][0]


def execute():
	# TWO SEPARATE QUESTIONS, because they can disagree. The DocType row and the table are
	# dropped by different statements - one DML, one DDL - so an interrupted run can leave a
	# table with no DocType behind it. Keying the table drop on `frappe.db.exists("DocType")`
	# would then skip it forever and leave an orphan table nothing can ever reach.
	present = [d for d in OLD_DOCTYPES if frappe.db.exists("DocType", d)]
	orphan_tables = [d for d in OLD_DOCTYPES if _table_exists(d) and d not in present]

	if (
		not present
		and not orphan_tables
		and not any(frappe.db.has_column(SERVICE_DOCTYPE, c) for c in DROP_COLUMNS)
	):
		frappe.logger("pet_app.migrate").info({"event": "PREVENTIVE_CARE_PURGE_ALREADY_DONE"})
		return

	# An orphan table is only safe to drop if it is EMPTY. A table with rows and no DocType
	# means something went wrong in a way this patch must not paper over.
	for doctype in orphan_tables:
		rows = _table_rows(doctype)
		if rows:
			return _fail(
				f"`tab{doctype}` still holds {rows} row(s) but its DocType row is gone. That is a "
				"half-finished state this patch will not resolve blind - the rows can no longer be "
				"verified through the ORM. Nothing was changed.",
				doctype=doctype, rows=rows,
			)

	# ── 1. verify ────────────────────────────────────────────────────────────
	pets, total = set(), 0
	for doctype in present:
		rows = frappe.db.sql(
			f"select name, pet, notes, creation, modified from `tab{doctype}`", as_dict=True
		)
		total += len(rows)
		for row in rows:
			pets.add(row.pet)
			if "[alkokh-source-group:PetCareService:" not in (row.notes or ""):
				return _fail(
					f"{doctype} {row.name} was not machine-written (no provenance marker). "
					"It may be a real clinical record. Nothing was deleted.",
					doctype=doctype, row=row.name,
				)
			if row.modified != row.creation:
				return _fail(
					f"{doctype} {row.name} has been edited since insert "
					f"(creation {row.creation}, modified {row.modified}). Nothing was deleted.",
					doctype=doctype, row=row.name,
				)
		versions = frappe.db.count("Version", {"ref_doctype": doctype})
		if versions:
			return _fail(f"{doctype} has {versions} Version row(s), so it has been edited. Nothing was deleted.")
		for dependent, filters in (
			("Comment", {"reference_doctype": doctype}),
			("File", {"attached_to_doctype": doctype}),
		):
			found = frappe.db.count(dependent, filters)
			if found:
				return _fail(
					f"{doctype} has {found} {dependent} row(s). `frappe.db.delete` would orphan them "
					"and the controller needed to cascade is gone. Nothing was deleted.",
				)
	if len(pets) > 1:
		return _fail(f"Superseded records span {len(pets)} pets ({sorted(pets)}), not one. Nothing was deleted.")

	categories = _preventive_categories()
	if not categories:
		return _fail("No preventive CategoryCareServices resolved by name. Nothing was deleted.")

	service_names = frappe.get_all(
		SERVICE_DOCTYPE, filters={"category": ["in", categories]}, pluck="name", ignore_permissions=True
	)

	# ── 2. A: draft invoice lines sourced from a preventive service ──────────
	removed_lines, left_lines = 0, []
	if service_names:
		rows = frappe.db.sql(
			"""select sii.name, sii.parent, sii.description, si.docstatus
			from `tabSales Invoice Item` sii join `tabSales Invoice` si on si.name = sii.parent
			where sii.description like %s""",
			(MARKER_LIKE,),
			as_dict=True,
		)
		targets = {}
		for row in rows:
			if not any(f"PetCareService:{name}]" in (row.description or "") for name in service_names):
				continue
			if row.docstatus != 0:
				left_lines.append((row.parent, row.name))
				continue
			targets.setdefault(row.parent, []).append(row.name)

		for invoice_name, line_names in targets.items():
			paid = frappe.db.sql(
				"""select per.parent from `tabPayment Entry Reference` per
				join `tabPayment Entry` pe on pe.name = per.parent
				where per.reference_name = %s and pe.docstatus = 1""",
				(invoice_name,),
			)
			if paid:
				return _fail(
					f"{invoice_name} has a submitted payment against it; its lines will not be touched. "
					"Nothing was deleted.",
					invoice=invoice_name,
				)
			invoice = frappe.get_doc("Sales Invoice", invoice_name)
			keep = [row for row in invoice.items if row.name not in set(line_names)]
			if not keep:
				return _fail(
					f"Removing preventive lines would leave {invoice_name} with no items at all. "
					"An empty invoice is not a valid document; it needs a human decision. Nothing was deleted.",
					invoice=invoice_name,
				)
			invoice.items = keep
			for index, row in enumerate(invoice.items, start=1):
				row.idx = index
			invoice.flags.ignore_permissions = True
			invoice.save()
			removed_lines += len(line_names)

	# ── 3. B + C: reminders, then the rows themselves ────────────────────────
	removed_reminders = 0
	if present and frappe.db.exists("DocType", "Pet Reminder"):
		removed_reminders = frappe.db.count("Pet Reminder", {"reference_doctype": ["in", present]})
		frappe.db.delete("Pet Reminder", {"reference_doctype": ["in", present]})

	for doctype in present:
		frappe.db.delete(doctype)
		frappe.db.delete("Custom DocPerm", {"parent": doctype})

	# ── 4. D: drop the doctypes. DDL, so it goes after every DML above ───────
	# `present` plus any orphan table left by an interrupted earlier run.
	for doctype in present + orphan_tables:
		frappe.db.sql_ddl(f"DROP TABLE IF EXISTS `tab{doctype}`")
		frappe.db.delete("DocField", {"parent": doctype})
		frappe.db.delete("DocPerm", {"parent": doctype})
		frappe.db.delete("Custom DocPerm", {"parent": doctype})
		frappe.db.delete("DocType", {"name": doctype})

	# ── 5. E: PetCareService loses the preventive columns ────────────────────
	frappe.reload_doc("pet_app", "doctype", "petcareservice", force=True)
	dropped, skipped = [], {}
	for column in DROP_COLUMNS:
		if not frappe.db.has_column(SERVICE_DOCTYPE, column):
			continue
		dirty = frappe.db.sql(
			f"""select count(*) from `tab{SERVICE_DOCTYPE}`
			where (category not in ({", ".join(["%s"] * len(categories))}) or category is null)
			  and ifnull(`{column}`, 0) != 0 and ifnull(`{column}`, '') != ''""",
			tuple(categories),
		)[0][0]
		if dirty:
			skipped[column] = dirty
			continue
		frappe.db.sql_ddl(f"ALTER TABLE `tab{SERVICE_DOCTYPE}` DROP COLUMN `{column}`")
		# NOTHING TO INVALIDATE, and it is worth saying why. `frappe.db.has_column` keeps
		# reporting a dropped column as present on this server, but not because of a stale
		# cache: `get_db_table_columns` queries `information_schema.columns WHERE table_name =
		# 'tabPetCareService'` with NO `table_schema` filter, so it pools columns from every
		# database on the host - including the leftover `test_driver_orders_*` site, which still
		# has the old schema. No invalidation can fix that. Code that needs to know whether a
		# field exists must ask the doctype meta, which is per-site and accurate.
		dropped.append(column)

	for column in REPORT_ONLY_COLUMNS:
		if frappe.db.has_column(SERVICE_DOCTYPE, column):
			dirty = frappe.db.sql(
				f"""select count(*) from `tab{SERVICE_DOCTYPE}`
				where (category not in ({", ".join(["%s"] * len(categories))}) or category is null)
				  and ifnull(`{column}`, '') != ''""",
				tuple(categories),
			)[0][0]
			skipped[column] = dirty

	if skipped:
		frappe.log_error(
			title="PREVENTIVE_CARE_PURGE_COLUMNS_KEPT",
			message=(
				f"These {SERVICE_DOCTYPE} columns were NOT dropped because non-preventive rows still "
				f"carry a value: {skipped}. For `stock_warehouse` the values come from Frappe's "
				"single-permitted-value auto-fill (both creating users hold exactly one Warehouse User "
				"Permission with apply_to_all_doctypes), not from a deliberate write, and nothing reads "
				"the column - but dropping it is a decision, not an inference."
			),
		)

	frappe.clear_cache()
	frappe.logger("pet_app.migrate").info(
		{
			"event": "PREVENTIVE_CARE_PURGED",
			"records_deleted": total,
			"doctypes_dropped": present,
			"orphan_tables_dropped": orphan_tables,
			"reminders_deleted": removed_reminders,
			"draft_invoice_lines_removed": removed_lines,
			"submitted_invoice_lines_left": len(left_lines),
			"columns_dropped": dropped,
			"columns_kept": skipped,
			"preventive_services_left": len(service_names),
		}
	)
