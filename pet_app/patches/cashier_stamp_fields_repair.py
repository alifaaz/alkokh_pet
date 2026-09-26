"""Re-create the cashier stamp fields that cashier_backend_setup declares but never made.

`cashier_backend_setup` is recorded in Patch Log as having run, yet none of the five
Payment Entry fields or the two Sales Invoice fields it declares exist on this site - only
the four unrelated reporting fields that arrive as fixtures. The likely cause is the usual
one: a patch is logged the first time it runs and never runs again, so fields added to its
body afterwards are declared but never created.

The consequence was silent, because every writer guards with `_set_if_field` / `_has_field`
and simply skips a missing field. So payments were stamped with nothing, and the two parts
of the settlement snapshot that read Payment Entry by `custom_pos_profile` -
`activity_summary` and `recent_payment_entries` - returned empty for every profile, as did
the `last_settlement` lookup. Cash still reconciled, because `expected_cash_on_hand` reads
GL Entry by account, but no figure could be traced to a cashier.

Calls the original function rather than restating the field list, so the two cannot drift.
`create_custom_fields` skips fields that already exist, which makes this safe to re-run and
harmless on a site where the original patch did complete.
"""

from __future__ import annotations

import frappe

from pet_app.patches.cashier_backend_setup import _create_cashier_custom_fields


def execute():
	_create_cashier_custom_fields()
	for doctype in ("POS Profile", "Payment Entry", "Sales Invoice"):
		frappe.clear_cache(doctype=doctype)
