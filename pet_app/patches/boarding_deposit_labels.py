from __future__ import annotations

import frappe

# Relabels Pet Boarding.balance to "Estimated Balance Due" and documents both money fields.
#
# `balance` is total_cost - deposit, computed on the record. It was never a ledger figure,
# and with deposits real it is now actively misleading: the invoice is raised in FULL and
# the deposit is allocated against it as an advance, so the invoice's own outstanding is
# the truth and `balance` is a different number arrived at a different way.
#
# It is labelled rather than made truthful because it CANNOT be made truthful. Invoice
# reuse means one draft carries several records' charges - a stay, a visit, a lab - so the
# invoice's outstanding is the customer's, not this stay's, and no honest per-stay figure
# can be derived from it. The same argument retired the visit workbench's borrowed total.
#
# Label and description only. No data changes, no column changes.

DOCTYPE = "pet_boarding"


def execute():
    frappe.reload_doc("pet_app", "doctype", DOCTYPE)
    frappe.clear_cache(doctype="Pet Boarding")
