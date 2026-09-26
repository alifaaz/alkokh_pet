"""One driver's cash and fees on a set of delivered orders, and the Journal Entry that
cleared them. Written by `pet_app.api.driver_orders.create_driver_settlement` in one
transaction with that entry and the stamp on every covered Sales Order.

The tie-out
-----------
    collected_amount == handed_over_amount + fee_amount + adjustment_amount

The driver took 100,000 at the door and earned 8,000 in fees: he hands over 92,000.
`handed_over_amount` is negative when his fees exceed what he collected (the till pays
him). The controller only re-checks the arithmetic, so a desk edit cannot drift away from
the posted entry.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

AMOUNT_EPSILON = 0.005


class DriverSettlement(Document):
	def validate(self):
		if self.orders:
			self.collected_amount = flt(sum(flt(r.collected_amount) for r in self.orders))
		self.order_count = len(self.orders or [])
		gap = flt(self.collected_amount) - (
			flt(self.handed_over_amount) + flt(self.fee_amount) + flt(self.adjustment_amount)
		)
		if abs(gap) > AMOUNT_EPSILON:
			frappe.throw(
				_("This settlement does not tie out: collected {0} is not handed over {1} + fees {2} + adjustment {3} (gap {4}).").format(
					flt(self.collected_amount), flt(self.handed_over_amount), flt(self.fee_amount),
					flt(self.adjustment_amount), gap,
				),
				title=_("Settlement Does Not Tie Out"),
			)
		if abs(flt(self.adjustment_amount)) > AMOUNT_EPSILON and not (self.adjustment_reason or "").strip():
			frappe.throw(_("Give a reason for the adjustment."))
