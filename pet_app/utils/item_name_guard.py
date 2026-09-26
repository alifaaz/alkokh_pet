"""Refuse to rename a linked Item whose name this master did not set.

A template or master - CareService template, Medication, Procedure Template, Care Service
Billing Option - may create an Item and name it, and may link to an Item that already
exists. What it must never do is rename an Item somebody else named. The Item is shared
with pharmacy, stock reports and every posted invoice line, so a silent rename on an
unrelated save (a price edit, a category change) rewrites the name everywhere the Item
appears, including on documents already issued to a customer.

Ownership is established by evidence rather than by a flag, because none of the four
doctypes records which Item it created. A master may write the name when either:

  - the Item's current name still equals its own `item_code` - the shape every
    auto-created Item has, since all four create-branches set `item_code` and `item_name`
    from the master's name in the same breath; or
  - the Item's current name still equals the master's name BEFORE this save - the master
    named it and is now renaming itself, so the Item follows.

Anything else means the name was set outside this master, and it is left alone. That
includes the case the fuzzy auto-link creates: `_resolve_item` / `_resolve_linked_item`
adopt an EXISTING Item found by `find_matching_item`, and without this guard the very
first save of the new master would rename the Item it just adopted.

The refusal is deliberately non-fatal. The link, the price and the rest of the save are
all legitimate; only the name write is dropped, and the operator is told so they can
rename the Item by hand if that was the intent.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr


def previous_master_values(master, fieldnames) -> dict:
	"""The master's own field values as they stand in the database before this save.

	`get_doc_before_save()` is the cheap answer and is populated by the time `validate`
	runs, but it is not contractually guaranteed to be - so a direct read backs it up
	rather than silently degrading the guard to "no previous name" and refusing a rename
	the master was entitled to make. Returns {} for a new document, which has no previous
	name by definition.
	"""
	fieldnames = tuple(fieldnames)
	if master.is_new():
		return {}

	before = master.get_doc_before_save()
	if before is not None:
		return {field: cstr(before.get(field)).strip() for field in fieldnames}

	row = frappe.db.get_value(master.doctype, master.name, list(fieldnames), as_dict=True) or {}
	return {field: cstr(row.get(field)).strip() for field in fieldnames}


def may_rename_linked_item(item, previous_master_name: str | None) -> bool:
	"""Whether this master may overwrite `item.item_name`. See the module docstring."""
	current = cstr(item.item_name).strip()

	# An Item with no name yet is being filled in, not renamed.
	if not current:
		return True

	if current == cstr(item.item_code).strip():
		return True

	previous = cstr(previous_master_name).strip()

	return bool(previous) and current == previous


def warn_linked_item_rename_skipped(item, master) -> None:
	"""Non-fatal notice: the Item keeps the name someone else gave it, the link is saved."""
	frappe.msgprint(
		_('Item {0} already has the name "{1}", which was set outside {2}. The name was left unchanged; the link was saved.').format(
			frappe.bold(cstr(item.item_code)),
			cstr(item.item_name),
			frappe.bold("{0} {1}".format(_(master.doctype), cstr(master.name))),
		),
		title=_("Linked Item name kept"),
		indicator="orange",
	)
