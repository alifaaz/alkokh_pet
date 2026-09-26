from __future__ import annotations

import frappe


def execute():
	"""Prepare `Product.barcode` for the UNIQUE index declared on the DocField.

	Runs in [pre_model_sync] on purpose. The index itself is created by the schema
	sync from `"unique": 1` in product.json - declaring it there rather than adding
	it here is what makes it survive `bench migrate`, because the sync drops any
	single-column index the DocField does not claim (see
	`frappe.database.schema.DbColumn.build_for_alter_table`, the `drop_unique`
	branch). A patch-created index would be reverted on the next migrate.

	But the sync creates that index BEFORE any post_model_sync patch runs, so a site
	holding duplicate or whitespace-variant barcodes would fail the migrate with a
	raw `Duplicate entry` error and no indication of which rows caused it. This
	patch runs first and either makes the index creatable or refuses with the
	offending values named.

	Empty and NULL stay allowed, and that is not an accident of MariaDB alone:
	  - MariaDB treats NULL as always-distinct in a UNIQUE index, so any number of
	    products may carry no barcode.
	  - Frappe normalises a blank value to NULL on write for any field flagged
	    unique (`frappe.model.base_document.BaseDocument.get_valid_dict`), so the
	    empty strings the storefront would otherwise send never collide with each
	    other.
	The second point only holds while `unique` stays on the DocField; drop that flag
	and empty strings start colliding on the index this patch prepared.

	Idempotent: re-running finds nothing to trim, nothing to null, no duplicates.
	"""
	if not frappe.db.table_exists("Product"):
		# Fresh site: the doctype is created with the constraint already on it.
		return
	if not frappe.db.has_column("Product", "barcode"):
		return

	# Trim BEFORE the duplicate check, never after: "ABC " and "ABC" are one barcode
	# to a scanner and to the lookup endpoint, but two distinct keys to the index.
	# Trimming second would report a clean site and then fail the sync.
	frappe.db.sql(
		"""
		UPDATE `tabProduct`
		SET barcode = TRIM(barcode)
		WHERE barcode IS NOT NULL AND barcode != TRIM(barcode)
		"""
	)

	# Whitespace-only becomes NULL, matching what Frappe now writes for a blank.
	# Left as '' these would be the one value that CANNOT repeat, which is the exact
	# opposite of "most products carry no barcode".
	frappe.db.sql("UPDATE `tabProduct` SET barcode = NULL WHERE barcode = ''")

	duplicates = frappe.db.sql(
		"""
		SELECT barcode, COUNT(*) AS c, GROUP_CONCAT(name) AS products
		FROM `tabProduct`
		WHERE barcode IS NOT NULL AND barcode != ''
		GROUP BY barcode
		HAVING c > 1
		""",
		as_dict=True,
	)
	if duplicates:
		# Refusing is the point. Two products answering one scan is a data error the
		# site owner has to resolve by choosing which product keeps the code - this
		# patch cannot guess, and silently dropping one barcode loses real data.
		frappe.throw(
			"Cannot make Product.barcode unique: {0} barcode(s) are used by more than "
			"one product. Clear the duplicates and re-run the migrate. {1}".format(
				len(duplicates),
				"; ".join(f"{d.barcode} -> {d.products}" for d in duplicates),
			)
		)
