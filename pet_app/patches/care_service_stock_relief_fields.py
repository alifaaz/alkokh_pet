"""A care service can name the drug it gives, and record the stock it actually issued.

Deworming is dosed by weight: the band the operator picks decides both the price and how
much of the tablet is consumed. The price half already worked - `Care Service Billing
Option.default_rate`. This adds the two halves that were missing.

`CareService template.medication` names WHAT is given, once per service, rather than
repeating the drug on all 22 weight bands where a change would have to be made 22 times.
`Care Service Billing Option.stock_deduction_qty` - which already exists and had no
consumer - says HOW MUCH, per band.

The three `PetCareService` stamps mirror `Pet Billable Item`
(stock_issued_qty / stock_entry / warehouse) for the reason that module's docstring gives:
what was issued is RECORDED, not recomputed, so a cancellation reverses what actually left
the shelf even if somebody edits the band's quantity afterwards.

UNITS ARE THE ITEM'S OWN STOCK UOM, WHATEVER IT IS TODAY. Nothing here converts anything.
The real deworming products are stocked in `Strip`, not `Tablet` - Drontal, Drontal Plus,
Milbemax, NexGard, Bravecto - so two tablets out of a ten-tablet strip is entered as 0.2,
not 2. `Strip` permits fractions (`must_be_whole_number = 0`); `Box`, `Tablet` and `Nos` do
not, and `_validate_whole_number_qty` refuses a fraction against those by name. If an
item's Stock UOM is ever changed, every band quantity already entered against it becomes
wrong by the size of the pack, silently. That decision is deferred, and this patch does not
take it: no Item's `stock_uom` is touched.

Schema only, nothing backfilled. Every existing template has no medication and every
existing band has qty 0, so grooming and bathing cannot enter the stock path at all - which
is the guard, expressed as data rather than as a condition.
"""

from __future__ import annotations

import frappe


TARGETS = (
	("careservice_template", "CareService template", ("medication",)),
	("petcareservice", "PetCareService", ("stock_issued_qty", "stock_entry", "stock_warehouse")),
)


def execute():
	for module_path, doctype, fieldnames in TARGETS:
		if not frappe.db.exists("DocType", doctype):
			continue

		frappe.reload_doc("pet_app", "doctype", module_path, force=True)

		missing = [f for f in fieldnames if not frappe.db.has_column(doctype, f)]
		if missing:
			frappe.log_error(
				title="CARE_SERVICE_STOCK_RELIEF_FIELDS_MISSING",
				message=f"{doctype} is missing {', '.join(missing)} after the doctype reload.",
			)
