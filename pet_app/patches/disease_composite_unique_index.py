from __future__ import annotations

import frappe

CONSTRAINT_NAME = "unique_disease_name_species"


def execute():
	"""Replace the single-column UNIQUE on Disease.disease_name with a composite
	UNIQUE on (disease_name, species).

	The single-column index made it impossible to record the same disease for more
	than one species. The composite constraint is what makes
	`Disease._validate_duplicate` (which already checks disease_name + species) the
	operative rule instead of dead weight.

	Frappe 16 has no doctype-level unique-constraint declaration - a per-field
	`unique: 1` only ever produces a single-column index - so the composite index
	must be created directly. It survives `bench migrate`: the schema sync's drop
	logic only considers single-column indexes (see
	`frappe.database.mariadb.database.get_column_index`, which excludes any index
	having a `Seq_in_index = 2` row).

	Idempotent: `add_unique` no-ops when the constraint already exists.
	"""
	if not frappe.db.table_exists("Disease"):
		return

	# The composite index must exist BEFORE the single-column one is dropped.
	# Any window with neither allows duplicates in, and once a duplicate exists the
	# composite index cannot be created without deleting real data.
	duplicates = frappe.db.sql(
		"""
		SELECT disease_name, species, COUNT(*) AS c
		FROM `tabDisease`
		GROUP BY disease_name, species
		HAVING c > 1
		""",
		as_dict=True,
	)
	if duplicates:
		frappe.throw(
			"Cannot create {0}: duplicate (disease_name, species) pairs exist: {1}".format(
				CONSTRAINT_NAME, duplicates
			)
		)

	frappe.db.add_unique("Disease", ["disease_name", "species"], constraint_name=CONSTRAINT_NAME)

	# `species` must not be NULL: MariaDB treats NULL as always-distinct in a UNIQUE
	# index, so a NULL species would silently permit unlimited duplicates for that
	# name. The `not_nullable` flag on the DocField applies NOT NULL DEFAULT '' on
	# the next schema sync; this backfill makes that sync safe.
	frappe.db.sql("UPDATE `tabDisease` SET species = '' WHERE species IS NULL")

	frappe.reload_doctype("Disease", force=True)
