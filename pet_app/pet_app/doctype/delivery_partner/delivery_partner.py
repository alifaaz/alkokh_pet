"""An aggregator that lists our products, charges the customer, and settles monthly.

The whole point of this record is that a partner order is NOT cash. Talabat takes the
customer's money at their end and transfers the month's takings minus commission; the
counter never touches it. So a partner order is booked as a `Due` Sales Invoice against
THIS partner's own Customer, and the money is collected later from the partner. See
`pet_app.api.pos.create_pos_sale` for the refusals that keep it that way, and
`docs/backend/delivery-partners.md` for the contract.

`customer` is backend-owned
---------------------------
A client never sends it and the form never shows it as writable. It is created here on
insert, because a partner pointing at the wrong Customer bills one app's orders to
another app's account and nothing surfaces it until a statement fails to reconcile a
month later - by which time the invoices are submitted and the ledger is wrong.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr, flt


class DeliveryPartner(Document):
	def validate(self):
		self._validate_commission()
		self._validate_customer_ownership()
		if not self.is_new() and not self.customer:
			# A record whose after_insert customer creation failed would otherwise never
			# acquire one, and every partner sale against it would fail at the till. Saving
			# it again repairs it. Assigned, not db_set: this document is about to be written.
			self.customer = self._resolve_or_create_customer()

	def after_insert(self):
		# db_set rather than save(): after_insert runs inside the insert's own transaction,
		# and a nested save would re-run validate on a half-written document.
		self.db_set("customer", self._resolve_or_create_customer(), update_modified=False)

	# ------------------------------------------------------------------
	# Validation
	# ------------------------------------------------------------------

	def _validate_commission(self):
		"""The client refuses a commission that cannot be settled. The client is not the authority.

		A percentage partner keeps a share of each order; an amount partner keeps a flat fee
		per order. Either way the figure has to leave something behind: no commission at all
		is not a delivery partner, it is a shopfront, and one that swallows the whole order
		settles to nothing or to a negative receipt, which the tie-out arithmetic in
		`create_settlement` would have to be told what to do with.

		Whichever type is NOT in use is zeroed rather than left as typed. Both figures are
		read downstream on the strength of the type alone, and a stale rate sitting behind
		an amount partner is the kind of thing that surfaces as a wrong commission months
		later, when somebody switches the type back.
		"""
		if self.commission_type == "Amount":
			amount = flt(self.commission_amount)
			if amount <= 0:
				frappe.throw(
					_("Commission amount must be greater than 0. Received {0}.").format(amount),
					title=_("Invalid Commission Amount"),
				)
			self.commission_rate = 0
			return

		# Percentage, and anything predating commission_type - which is what those were.
		self.commission_type = "Percentage"
		rate = flt(self.commission_rate)
		if rate <= 0 or rate >= 100:
			frappe.throw(
				_("Commission rate must be greater than 0 and less than 100. Received {0}.").format(rate),
				title=_("Invalid Commission Rate"),
			)
		self.commission_amount = 0

	def _validate_customer_ownership(self):
		"""A partner may not be repointed at a Customer that carries unrelated history.

		Not "any history": the partner's OWN submitted orders are exactly what is expected
		to be there, and re-saving a working partner must not start failing. What is
		refused is adopting a Customer that has been billed for something else - a walk-in,
		a clinic visit, another partner - because from then on two different populations
		share one receivable and the partner's statement can never be reconciled.
		"""
		if self.is_new() or not self.customer:
			return

		before = self.get_doc_before_save()
		previous = cstr(before.customer).strip() if before else ""
		if not previous or previous == cstr(self.customer).strip():
			return

		foreign = frappe.get_all(
			"Sales Invoice",
			filters={
				"customer": self.customer,
				"docstatus": ["<", 2],
				"custom_delivery_partner": ["!=", self.name],
			},
			fields=["name"],
			order_by="creation asc",
			limit_page_length=1,
			ignore_permissions=True,
		)
		if foreign:
			frappe.throw(
				_(
					"Customer {0} is already billed for other business - {1} is one such invoice. "
					"Pointing {2} at it would mix two populations in one receivable and its "
					"statement could never be reconciled. Leave {3} in place, or create a "
					"Customer that is used only by this partner."
				).format(self.customer, foreign[0].name, self.name, previous),
				title=_("Customer Already Has Other History"),
			)

	# ------------------------------------------------------------------
	# Customer
	# ------------------------------------------------------------------

	def _resolve_or_create_customer(self) -> str:
		"""The Customer every order for this partner is billed to.

		`customer_type` is Company, deliberately: `validate_customer_identity_projection`
		refuses a new Individual Customer that has no Guardian behind it, and a delivery
		aggregator is a company rather than a person anyway.
		"""
		if self.customer and frappe.db.exists("Customer", self.customer):
			return self.customer

		existing = frappe.db.get_value("Customer", {"customer_name": self.partner_name}, "name")
		if existing:
			customer = existing
		else:
			doc = frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": self.partner_name,
					"customer_type": "Company",
				}
			)
			doc.flags.ignore_guardian_identity_validation = True
			doc.insert(ignore_permissions=True, ignore_mandatory=True)
			customer = doc.name

		return customer
