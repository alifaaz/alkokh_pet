"""Type every existing Rating row: Internal, or Customer.

``rating_type`` arrives with the DocType (rating.json), so this patch adds no schema -
it only classifies the 1,530 rows that predate the column. It runs post_model_sync, so
the column is there by the time it does.

The rule, and why it is this rule
---------------------------------
``rated_by`` cannot classify these rows. Only 4 of the 1,791 Guardians on this site
have a linked User, so ``_create_rating``'s old fallback chain wrote every guardian's
rating as ``rated_by = "Administrator"`` - which resolves to practitioner HCP-00060.
A rule of the form "the rater is a practitioner, therefore staff" would mislabel every
customer rating on the site as Internal, which is precisely the blending this change
exists to stop.

Two independent predicates do identify them, and this patch trusts neither alone:

  A. reachable from a ``Pet App WhatsApp Action Request`` with
     ``result_doctype = 'Rating'`` - the executor recorded what it wrote
  B. ``owner = 'Guest'`` - the webhook runs unauthenticated, and nothing else does

They are derived from different columns written by different code, so agreement is
evidence and disagreement is a fact about the data that nobody has looked at yet. If
they select different row sets the patch stops and says so rather than picking a
winner: a backfill that guesses is worse than one that refuses, and this one cannot be
re-run against already-typed rows to correct itself.

Everything else on the four staff-rated doctypes is Internal. That is not an
assumption - every rater on those rows maps to a Healthcare Practitioner, not one of
them rated their own work, and the notes read as supervisor audits of app discipline
("did not press start and finish despite being told"), not as service feedback.

Product is deliberately untouched
---------------------------------
The 8 Product rows are seeded demo data - ``notes = "Seeded mobile catalog rating."``,
all 8 written by ``_ensure_rating`` in seed_mobile_catalog_demo_data.py, which has no
Patch Log entry and is not in patches.txt. There are no real customer product reviews
on this site. Whether that demo data should be typed Customer, or deleted, is Mostafa's
call and not a thing to decide inside a backfill. They stay untyped, and the summary
reports them separately so they cannot be mistaken for rows this patch missed.

Idempotent: it only ever writes rows whose ``rating_type`` is still empty.
"""

from __future__ import annotations

import frappe

from pet_app.utils.rating_entities import CUSTOMER_RATING, INTERNAL_RATING


# The doctypes whose ratings this patch classifies. Product is excluded on purpose;
# see the module docstring.
INTERNAL_REFERENCE_DOCTYPES = ("PetCareService", "Lab", "Imaging", "Vet Visit")


def execute():
	if not frappe.db.has_column("Rating", "rating_type"):
		# post_model_sync should have created it. Say so loudly rather than reporting a
		# clean run over a column that is not there.
		frappe.throw("Rating.rating_type does not exist; run this patch after the DocType sync.")

	customer = _customer_rows()

	# Customer first, and row by row - there are three of them, and each also needs the
	# guardian that the same Action Request chain identifies. Doing this before the
	# Internal sweep means a row that is customer-authored can never be caught by it.
	guardians = _customer_guardians(customer)
	guardians_written = 0
	for name in customer:
		frappe.db.set_value("Rating", name, "rating_type", CUSTOMER_RATING, update_modified=False)
		# Attribute the row to the guardian who actually wrote it. Without this it
		# carries a type but still no author, and the uniqueness rule that now keys on
		# the guardian would read all three as the same rater.
		if guardians.get(name):
			frappe.db.set_value(
				"Rating", name, "rated_by_guardian", guardians[name], update_modified=False
			)
			guardians_written += 1

	# Everything else on the four staff-rated doctypes, as one statement rather than a
	# 1,500-name IN list.
	internal = frappe.db.count(
		"Rating",
		{"reference_doctype": ("in", INTERNAL_REFERENCE_DOCTYPES), "rating_type": ("in", ("", None))},
	)
	frappe.db.sql(
		"""
		update `tabRating`
		set rating_type = %(internal)s
		where ifnull(rating_type, '') = ''
			and reference_doctype in %(doctypes)s
		""",
		{"internal": INTERNAL_RATING, "doctypes": tuple(INTERNAL_REFERENCE_DOCTYPES)},
	)

	frappe.db.commit()
	_report(len(customer), guardians_written, internal)


def _customer_rows():
	"""Rows both predicates agree are customer-authored. Raises if they disagree."""
	by_action_request = {
		name
		for name in frappe.get_all(
			"Pet App WhatsApp Action Request",
			filters={"result_doctype": "Rating", "result_name": ("is", "set")},
			pluck="result_name",
		)
		if name and frappe.db.exists("Rating", name)
	}
	by_owner = set(frappe.get_all("Rating", filters={"owner": "Guest"}, pluck="name"))

	if by_action_request != by_owner:
		only_action = sorted(by_action_request - by_owner)
		only_owner = sorted(by_owner - by_action_request)
		frappe.throw(
			"Rating backfill stopped: the two customer-row predicates disagree, so "
			"neither can be trusted on its own.\n"
			f"  linked to an Action Request but owner != Guest: {only_action or 'none'}\n"
			f"  owner = Guest but no Action Request: {only_owner or 'none'}\n"
			"Resolve which of these are customer ratings before running this patch."
		)

	# Only rows still untyped, so a re-run is a no-op rather than a second write.
	return [
		name
		for name in sorted(by_owner)
		if not frappe.db.get_value("Rating", name, "rating_type")
	]


def _customer_guardians(names):
	"""{rating name: guardian} from the Action Request's conversation."""
	if not names:
		return {}
	rows = frappe.db.sql(
		"""
		select ar.result_name as rating, c.guardian as guardian
		from `tabPet App WhatsApp Action Request` ar
		join `tabPet App WhatsApp Conversation` c on c.name = ar.conversation
		where ar.result_doctype = 'Rating'
			and ar.result_name in %(names)s
			and ifnull(c.guardian, '') != ''
		""",
		{"names": tuple(names)},
		as_dict=True,
	)
	return {row.rating: row.guardian for row in rows}


def _report(customer, guardians, internal):
	untyped = frappe.db.sql(
		"""
		select reference_doctype, count(*) as rows_left
		from `tabRating`
		where ifnull(rating_type, '') = ''
		group by reference_doctype
		order by rows_left desc
		""",
		as_dict=True,
	)
	print(f"Rating.rating_type backfill: {customer} Customer, {internal} Internal.")
	print(f"  rated_by_guardian set on {guardians} of the {customer} customer rows.")
	if untyped:
		for row in untyped:
			print(f"  still untyped: {row.reference_doctype} x{row.rows_left}")
		print("  (Product rows are excluded by design - see this patch's docstring.)")
	else:
		print("  no untyped rows remain.")
