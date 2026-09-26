"""Where stock moves from, derived from the branch rather than from the item.

An item used to carry its own location through ``Item Default.default_warehouse``, and a
medication through ``Medication.default_warehouse``. Both were wrong in practice and
wrong in principle.

Wrong in practice: 1,824 items pointed at a warehouse holding 9 items of real stock and
196 pointed at one holding a single item, while 296 items and 3,702 units actually sat in
a third warehouse that nothing pointed at. Every sale that trusted those defaults tried
to relieve stock from an empty shelf.

Wrong in principle: the same tin of food sells from whichever store the seller stands in.
Location is a fact about the branch, not about the product - so a new branch should need
one row to configure, not a rewrite of 2,000 item rows.

The chain this module implements is::

    service (performing_branch) -> branch -> Branch.custom_wharehouse -> warehouse

``performing_branch`` comes first so the cross-branch case works: a main-clinic doctor
orders an X-ray, the machine lives at the hotel, so the hotel performs it, relieves its
own stock and bills it. That is the same rule ``order_billing`` already applies to
revenue - see :func:`pet_app.utils.branch.performing_branch_for`.
"""

from __future__ import annotations

import frappe
from frappe.utils import cint, cstr

from pet_app.utils.branch import get_current_branch


BRANCH_WAREHOUSE_FIELD = "custom_wharehouse"


def branch_warehouse(branch: str | None = None, user: str | None = None) -> str | None:
	"""The warehouse a branch sells and dispenses from, or ``None`` when unwired.

	``None`` is a real answer and never an error: an unbranched user (Administrator, a
	back-office account, a background job) has no branch to derive a location from, and a
	branch whose warehouse nobody has filled in yet must not silently borrow another
	branch's stock. Every caller treats ``None`` as "leave whatever is already there
	alone".

	The field is spelled ``custom_wharehouse`` on Branch. That typo is in the live
	schema and is not ours to correct here; it is pinned to a constant so a future
	rename is one edit.
	"""
	branch = cstr(branch).strip() or get_current_branch(user)
	if not branch:
		return None

	try:
		warehouse = frappe.db.get_value("Branch", branch, BRANCH_WAREHOUSE_FIELD)
	except Exception:
		# The field is a Custom Field. A site that has not had it applied yet must not
		# take the whole save down with it.
		return None

	return cstr(warehouse).strip() or None


def stamp_branch_and_warehouse(doc, method=None) -> None:
	"""``before_validate``/``before_submit`` on Sales Invoice - rows agree with the invoice.

	Two separate rules, and the branch one is the reason this runs unconditionally.

	**Branch.** A row must never contradict its own invoice. ``frappe.new_doc`` fills a
	link field with a user's User Permission value when they hold exactly one
	(defaults.py:24-27), and ``Document._set_defaults`` applies that to child rows as well
	(document.py:1006-1013). So an X-ray raised by a `main` user for a service the hotel
	performs came out with ``branch = hotel`` on the invoice and ``branch = main`` on every
	row. That is not cosmetic: ``has_user_permission`` walks child rows, so the hotel staff
	who own the invoice were refused when they opened it; and when GL entries are built the
	row's dimension *overrides* the parent's (accounts_controller.py:1337-1343), so the
	revenue would have posted to the branch that did not do the work.

	The ordering inside ``insert()`` is what makes correcting it here sufficient:
	``_set_defaults`` (document.py:435) runs before ``run_before_save_methods``
	(document.py:447), so the default lands first and this rewrites it, on every save.

	**Warehouse.** The same invariant as the branch, for the same reason. This used to fire
	only under ``update_stock``, on the argument that a pure service line has no location to
	speak of. That was wrong, because the row's warehouse is a permission dimension as well
	as a stock one: ``has_user_permission`` walks child rows for *every* restricted link, so
	a warehouse that disagrees with the invoice's branch locks the branch's own staff out of
	their own invoice, whether or not anything moves.

	It reached that state on its own. ``commit_order_billing`` sends a service line with no
	warehouse key - deliberately, since ``invoice_reuse`` reads that key's presence to decide
	``update_stock`` (invoice_reuse.py:249-251). The row is therefore still empty when this
	runs, the old rule declined to invent one, and ERPNext's ``set_missing_item_details``
	then filled it from ``Stock Settings.default_warehouse``. A hotel X-ray came out branded
	hotel and warehoused main, and the hotel cashier was refused on it.

	So: every row takes the branch's warehouse. ``set_warehouse`` on the invoice stays gated
	on ``update_stock`` - it decides where goods actually ship from, and claiming a shipping
	point for an invoice that ships nothing would be a lie in the header.

	Writing the warehouse here holds, and cannot turn a service invoice into a stock one.
	``set_missing_item_details`` calls ``get_item_details`` with ``overwrite_warehouse=False``
	and assigns only when the field ``is None``, and ``warehouse`` is not in
	``force_item_fields`` (accounts_controller.py:91-101), so what lands here survives
	validate. And ``update_stock`` is decided from the caller's ``items`` list before the doc
	exists, never re-read from the saved rows, so a hook cannot flip it.

	Re-run at ``before_submit`` rather than trusted from the draft: both values decide
	where money and goods land, so an edit made on the form between the last save and the
	submit must not survive.

	Does nothing about the warehouse when none resolves. An unbranched user (Administrator,
	a background job) keeps whatever ERPNext worked out; failing closed here would block
	every automated invoice.
	"""
	if not doc:
		return

	rows = doc.get("items") or []
	branch = cstr(doc.get("branch")).strip()

	# The invariant: the row's accounting dimension is the invoice's, always.
	if branch:
		for row in rows:
			if cstr(row.get("branch")).strip() != branch:
				row.branch = branch

	if doc.get("custom_driver_flow"):
		from pet_app.utils.driver_orders import fulfillment_warehouse
		warehouse = fulfillment_warehouse(doc)
	else:
		warehouse = branch_warehouse(branch)
	if not warehouse:
		return

	# Only a stock leg gets a shipping point on the header; the rows take the branch's
	# warehouse either way, because that is what the permission check reads.
	if cint(doc.get("update_stock")):
		doc.set_warehouse = warehouse

	for row in rows:
		if cstr(row.get("warehouse")).strip() != warehouse:
			row.warehouse = warehouse


# Former name of the function above, kept as an alias.
#
# A doc_event target is resolved by dotted string at call time, and a running worker holds
# the hook list it loaded at boot. Renaming the function while a process was live made every
# Sales Invoice save fail with "module has no attribute stamp_warehouse_from_branch" until a
# restart - the rename, not the logic, broke it. The alias makes the old and new names
# resolve to the same function, so a rename can never again depend on restart ordering.
#
# Safe to delete once every process has been restarted past this commit.
stamp_warehouse_from_branch = stamp_branch_and_warehouse
