from __future__ import annotations

import frappe

# Per-order billing state on the four clinical order doctypes.
#
# `billed` and `sales_invoice` record that an order was invoiced on its own, at its own
# performing branch, rather than waiting for the visit to close. They are deliberately
# NOT the same fields as Vet Visit.sales_invoice: that one keeps only the visit's own
# branch's invoice and is untouched by this path, so an order billed to another branch has
# nowhere else to record where its money went.
#
# Until this patch runs, utils.order_billing refuses to bill per-order at all (it checks
# meta.has_field first) and every charge keeps flowing through the visit exactly as it
# does today. That makes deploying the code before running the patch safe, and in the
# wrong order harmless - which matters, because billing without being able to record that
# it billed would duplicate the charge on the next release.

DOCTYPES = ("lab", "imaging", "pet_procedure", "petcareservice")


def execute():
    for slug in DOCTYPES:
        frappe.reload_doc("pet_app", "doctype", slug)
    frappe.clear_cache()
