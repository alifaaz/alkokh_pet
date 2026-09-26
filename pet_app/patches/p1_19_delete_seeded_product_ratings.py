"""Delete the eight seeded Product ratings that p1_18 deliberately left untyped.

p1_18 typed every real row - 3 Customer, 1,527 Internal - and stopped at Product,
because the eight rows there are demo data from ``_ensure_rating`` in
seed_mobile_catalog_demo_data.py and whether to keep them was not a backfill's decision
to make. Mostafa has ruled: delete, not type.

Selection is by identity, never by count and never by content
-------------------------------------------------------------
The predicate is ``reference_doctype = 'Product'`` and no type. That is the whole
definition of "seeded" now that p1_18 has run: every row a real writer produces is
typed at insert, so an untyped row is one that predates the column, and on Product the
only rows that predate the column are the seeded eight.

Matching on ``notes = "Seeded mobile catalog rating."`` or on a hardcoded name list was
rejected. Both would still match if a genuine customer review had landed in the window
between the migrate and this patch - it would be typed Customer and must survive - and
a name list would additionally go wrong the moment the seed is re-run with different
autonames. The type column is the fact; the note string is a coincidence.

The count is an assertion, not a selector. Eight is what this site has, so anything
else means the predicate is describing something other than what was surveyed, and the
patch stops with the names it found rather than deleting an unknown set.

``ifnull(rating_type, '') = ''`` rather than ``rating_type is null``: the two differ
only on rows holding an empty string, which are just as untyped as NULL ones and would
otherwise survive as the very thing the next verification step checks for.

Idempotent: a second run selects nothing and returns.
"""

from __future__ import annotations

import frappe


# Every row this site was surveyed to hold. See the docstring: an assertion on what the
# identity predicate returns, not the thing that selects them.
EXPECTED_ROWS = 8

# Child tables parented to a Rating. frappe.delete_doc removes these itself, but they
# are cleared explicitly first so the patch can report what it removed instead of
# trusting that something else did.
CHILD_DOCTYPES = ("Rating Answer", "Rating Tag Selection")


def execute():
	if not frappe.db.has_column("Rating", "rating_type"):
		frappe.throw(
			"Rating.rating_type does not exist. This patch must run after "
			"p1_18_rating_type_backfill, which is what makes an untyped Product row "
			"identifiable as seeded."
		)

	names = _untyped_product_ratings()
	if not names:
		print("Seeded Product ratings: none found; already deleted.")
		return

	if len(names) != EXPECTED_ROWS:
		frappe.throw(
			f"Rating cleanup stopped: expected {EXPECTED_ROWS} untyped Product ratings, "
			f"found {len(names)}.\n"
			f"  {', '.join(names)}\n"
			"Something other than the known seed data matches this predicate. Check what "
			"these rows are before deleting anything."
		)

	print(f"Seeded Product ratings: {len(names)} selected for deletion.")
	print(f"  {', '.join(names)}")

	children = _delete_children(names)
	for doctype, count in children.items():
		print(f"  {doctype}: {count} row(s) deleted.")

	for name in names:
		# force skips the link check - nothing links to a Rating, but a demo row is not
		# worth failing a migrate over. delete_permanently keeps this out of Deleted
		# Document: seed data does not belong in an audit trail of real deletions.
		frappe.delete_doc(
			"Rating",
			name,
			force=True,
			ignore_permissions=True,
			delete_permanently=True,
		)

	frappe.db.commit()
	_report(len(names), children)


def _untyped_product_ratings():
	return [
		row.name
		for row in frappe.db.sql(
			"""
			select name
			from `tabRating`
			where reference_doctype = 'Product'
				and ifnull(rating_type, '') = ''
			order by name
			""",
			as_dict=True,
		)
	]


def _delete_children(names):
	"""Clear child rows parented to these ratings. Returns {doctype: count}."""
	counts = {}
	for doctype in CHILD_DOCTYPES:
		table = doctype.replace("`", "``")
		count = frappe.db.sql(
			f"select count(*) from `tab{table}` where parenttype = 'Rating' and parent in %(names)s",
			{"names": tuple(names)},
		)[0][0]
		counts[doctype] = count
		if count:
			frappe.db.sql(
				f"delete from `tab{table}` where parenttype = 'Rating' and parent in %(names)s",
				{"names": tuple(names)},
			)
	return counts


def _report(deleted, children):
	remaining_product = frappe.db.count("Rating", {"reference_doctype": "Product"})
	untyped = frappe.db.sql(
		"select count(*) from `tabRating` where ifnull(rating_type, '') = ''"
	)[0][0]
	by_type = frappe.db.sql(
		"""
		select ifnull(rating_type, '(untyped)') as rating_type, count(*) as rows_held
		from `tabRating`
		group by rating_type
		order by rows_held desc
		""",
		as_dict=True,
	)
	child_total = sum(children.values())
	print(f"Deleted {deleted} Rating row(s) and {child_total} child row(s).")
	print(f"  Product ratings remaining: {remaining_product}")
	for row in by_type:
		print(f"  {row.rating_type}: {row.rows_held}")
	print(f"  untyped rows remaining: {untyped}")
