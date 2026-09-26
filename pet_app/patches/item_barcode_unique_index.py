from __future__ import annotations

import frappe

FIELDNAME = "custom_barcode"


def execute():
	"""Make `Item.custom_barcode` safe to put a UNIQUE index on.

	Modelled on pet_app.patches.product_barcode_unique_index, but the ownership is the
	other way round and that is worth being explicit about, because it changes what
	this patch can and cannot do.

	Product.barcode is a field on OUR doctype, so the constraint is declared in
	product.json and applied by the schema sync - which runs after pre_model_sync
	patches, so the Product patch cleans data the sync is about to depend on, in the
	same migrate.

	Item belongs to ERPNext. The field is a Custom Field shipped in
	fixtures/custom_field.json, and `sync_fixtures()` runs in post_schema_updates,
	AFTER every patch (frappe/migrate.py: run_schema_updates then post_schema_updates).
	The field is deliberately NOT created here as well: a Custom Field owned by both a
	patch and a fixture drifts, because migrate runs the patch and then overwrites the
	result with the exported JSON - which is exactly what tests/test_fixture_allowlist
	fails the build for. A patch could not own it in any case, since install_app marks
	every patch complete without running it on a fresh site
	(frappe/installer.py: set_all_patches_as_completed), so a patch-created field would
	never exist on a new install.

	The consequence, stated plainly: on the migrate that first ships this field the
	column does not exist yet when this runs, so this patch no-ops. That is expected.
	What it guards is the site where the column ALREADY exists with rows in it - an
	admin who added the field by hand through Customize Form, or any site that acquired
	the fixture before this patch was written. There, sync_fixtures is about to apply a
	unique constraint to a populated column, and without this the migrate would fail on
	a raw `Duplicate entry` naming no rows.

	Ongoing protection is not this patch's job and never could be, since a patch runs
	once: duplicates cannot accumulate afterwards because the unique index rejects them
	at write time, and whitespace variants cannot accumulate because
	pet_app.utils.item_barcode trims on before_validate.

	Idempotent: re-running finds nothing to trim, nothing to null, no duplicates.
	"""
	if not frappe.db.table_exists("Item"):
		return
	if not frappe.db.has_column("Item", FIELDNAME):
		# The expected path on the migrate that introduces the field. See above.
		return

	# Trim BEFORE the duplicate check, never after: "ABC " and "ABC" are one barcode to
	# a scanner but two distinct keys to the index. Trimming second would report a clean
	# site and then let the constraint fail.
	frappe.db.sql(
		"""
		UPDATE `tabItem`
		SET `{0}` = TRIM(`{0}`)
		WHERE `{0}` IS NOT NULL AND `{0}` != TRIM(`{0}`)
		""".format(FIELDNAME)
	)

	# Whitespace-only becomes NULL, matching what the trim hook now writes for a blank.
	# Left as '' these would be the one value that CANNOT repeat, in a field whose whole
	# point is that most items will not carry it.
	frappe.db.sql("UPDATE `tabItem` SET `{0}` = NULL WHERE `{0}` = ''".format(FIELDNAME))

	duplicates = frappe.db.sql(
		"""
		SELECT `{0}` AS barcode, COUNT(*) AS c, GROUP_CONCAT(name) AS items
		FROM `tabItem`
		WHERE `{0}` IS NOT NULL AND `{0}` != ''
		GROUP BY `{0}`
		HAVING c > 1
		""".format(FIELDNAME),
		as_dict=True,
	)
	if duplicates:
		# Refusing is the point. Two items answering one scan is a data error the site
		# owner has to resolve by choosing which item keeps the code - this patch cannot
		# guess, and silently clearing one loses real data.
		frappe.throw(
			"Cannot make Item.{0} unique: {1} barcode(s) are used by more than one item. "
			"Clear the duplicates and re-run the migrate. {2}".format(
				FIELDNAME,
				len(duplicates),
				"; ".join(f"{d.barcode} -> {d.items}" for d in duplicates),
			)
		)
