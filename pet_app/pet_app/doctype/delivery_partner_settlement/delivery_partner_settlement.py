"""A month's takings from one delivery partner, and the Payment Entry that banked them.

Written by `pet_app.api.delivery_partners.create_settlement` in ONE transaction together
with its Payment Entry and the stamps on every invoice it covers. The controller here
only enforces the arithmetic, so a record edited in the desk cannot drift away from the
Payment Entry that was posted against it.

The tie-out
-----------
    received_amount + commission_amount + adjustment_amount == gross_amount

100,000 of sales at 25% arrives as 75,000: the partner keeps 25,000 and banks 75,000, and
the receivable is cleared for the full 100,000 by debiting the commission as an expense.
A settlement that does not tie out is refused rather than posted, because the alternative
is a Payment Entry that leaves the invoices part-open with no document explaining why.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

# Currency rounding slack, matching pet_app.api.pos. IQD has no minor unit; anything
# smaller than this is float noise rather than a real gap.
AMOUNT_EPSILON = 0.005


class DeliveryPartnerSettlement(Document):
	def validate(self):
		self._sync_gross_from_rows()
		self._validate_tie_out()
		self._sync_derived()

	def _sync_gross_from_rows(self):
		"""`gross_amount` is derived, never typed - and it is derived from OUTSTANDING.

		Each row carries what its invoice still owed when the settlement was built, so a
		partly paid invoice contributes only the remainder. Recomputing here means a row
		removed by hand in the desk cannot leave the header claiming to cover it.
		"""
		if not self.invoices:
			return
		self.gross_amount = flt(sum(flt(row.gross_amount) for row in self.invoices))

	def _validate_tie_out(self):
		gross = flt(self.gross_amount)
		accounted = flt(self.received_amount) + flt(self.commission_amount) + flt(self.adjustment_amount)
		gap = gross - accounted
		if abs(gap) > AMOUNT_EPSILON:
			frappe.throw(
				_(
					"This settlement does not tie out. Gross {0} but received {1} plus "
					"commission {2} plus adjustment {3} comes to {4} - a gap of {5}."
				).format(
					frappe.format_value(gross, {"fieldtype": "Currency"}),
					frappe.format_value(flt(self.received_amount), {"fieldtype": "Currency"}),
					frappe.format_value(flt(self.commission_amount), {"fieldtype": "Currency"}),
					frappe.format_value(flt(self.adjustment_amount), {"fieldtype": "Currency"}),
					frappe.format_value(accounted, {"fieldtype": "Currency"}),
					frappe.format_value(gap, {"fieldtype": "Currency"}),
				),
				title=_("Settlement Does Not Tie Out"),
			)

	def _sync_derived(self):
		"""`commission_rate` and `invoice_count` are stored, so keep them true.

		Both exist because the client reads them off a list row and must not fetch a child
		table per settlement to render one. Stored figures drift the moment somebody edits
		the document in the desk, so they are recomputed here rather than written once at
		creation. The rate is THIS settlement's commission over THIS settlement's gross -
		never the partner's live rate, which would restate a banked month on the first
		renegotiation.
		"""
		self.invoice_count = len(self.invoices or [])
		gross = flt(self.gross_amount)
		self.commission_rate = flt(flt(self.commission_amount) * 100.0 / gross) if gross else 0.0
