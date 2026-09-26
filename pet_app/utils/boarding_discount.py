"""Invoice-level boarding discounts with booking-scoped distribution.

The discount covers every invoiced charge of the booking (room stays, services,
medication, extras), not only accommodation. The `accommodation_*` fields keep their
names for the API contract but hold boarding-wide figures.
"""
from decimal import Decimal, InvalidOperation
import json

import frappe
from frappe import _
from frappe.utils import flt

from pet_app.utils.invoice_source import has_marker
from erpnext.controllers.taxes_and_totals import calculate_taxes_and_totals


def parse_discount(value):
	if value is None:
		return None
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except (ValueError, TypeError):
			frappe.throw(_("Discount must contain a type and numeric value."))
	if not isinstance(value, dict) or set(value) != {"type", "value"}:
		frappe.throw(_("Discount must contain a type and numeric value."))
	kind, amount = value["type"], value["value"]
	if kind not in ("amount", "percentage"):
		frappe.throw(_("Discount type must be amount or percentage."))
	if isinstance(amount, bool) or not isinstance(amount, (int, float)):
		frappe.throw(_("Discount value must be a finite nonnegative number."))
	try:
		number = Decimal(str(amount))
		if not number.is_finite() or number < 0:
			raise InvalidOperation
	except InvalidOperation:
		frappe.throw(_("Discount value must be a finite nonnegative number."))
	if kind == "percentage" and number > 100:
		frappe.throw(_("Percentage discount cannot exceed 100%."))
	return {"type": kind, "value": amount}


def discount_payload(boarding):
	known = bool(boarding and boarding.get("discount_recorded_at"))
	return {
		"discount": json.loads(boarding.discount_request) if known and boarding.get("discount_request") else None,
		**{key: flt(boarding.get(key)) if known else None for key in (
			"accommodation_subtotal", "accommodation_discount_amount", "accommodation_total")},
	}


def distribute_discount(amounts, discount, precision):
	"""Currency-rounded proportional shares, with a bounded final adjustment."""
	amounts = [Decimal(str(flt(amount, precision))) for amount in amounts]
	total = sum(amounts, Decimal(0))
	if not discount:
		return [Decimal(0) for amount in amounts], Decimal(0)
	if any(amount < 0 for amount in amounts):
		frappe.throw(_("Accommodation charges must be nonnegative before applying a discount."))
	requested = Decimal(str(discount["value"])) if discount else Decimal(0)
	if discount and discount["type"] == "amount" and requested > total:
		frappe.throw(_("Amount discount cannot exceed the boarding bill total."))
	target = requested * total / 100 if discount and discount["type"] == "percentage" else requested
	target = Decimal(str(flt(target, precision)))
	if not total:
		return amounts, Decimal(0)
	shares = [min(amount, Decimal(str(flt(target * amount / total, precision)))) for amount in amounts]
	remainder = target - sum(shares)
	for index in reversed(range(len(shares))):
		change = min(remainder, amounts[index] - shares[index]) if remainder > 0 else max(remainder, -shares[index])
		shares[index] += change
		remainder -= change
		if not remainder:
			break
	return shares, target


def eligible_invoice_rows(invoice, boarding_name, sources=None):
	if sources is None:
		sources = set(frappe.get_all("Pet Billable Item", filters={
			"parent": boarding_name, "parenttype": "Pet Boarding",
			"status": ["not in", ["Cancelled", "Included"]],
		}, pluck="name"))
	return [row for row in invoice.items
		if has_marker(row.description, "Pet Boarding", boarding_name)
		and any(has_marker(row.description, "Pet Billable Item", source) for source in sources)]


def apply_accommodation_discount(boarding, invoice, discount, precision):
	"""Keep qty/rate/amount intact; put the discount on the Sales Invoice itself."""
	if invoice and invoice.docstatus != 0:
		frappe.throw(_("Accommodation discounts can only be applied before invoice submission."))
	sources = {row.name for row in boarding.billable_items
		if row.status not in ("Cancelled", "Included")}
	eligible = eligible_invoice_rows(invoice, boarding.name, sources) if invoice else []
	amounts = [flt(row.net_amount, precision) for row in eligible]
	_shares, applied = distribute_discount(amounts, discount, precision)
	if invoice and applied:
		if invoice.discount_amount or invoice.additional_discount_percentage:
			frappe.throw(_("Review the existing invoice discount before applying an accommodation discount."))
		invoice.custom_boarding_discount_booking = boarding.name
		invoice.apply_discount_on = "Net Total"
		invoice.additional_discount_percentage = 0
		invoice.discount_amount = float(applied)
		invoice.save(ignore_permissions=True)
		actual = sum(Decimal(str(flt(row.distributed_discount_amount, precision))) for row in eligible)
		if actual != applied:
			frappe.throw(_("Invoice pricing changed the accommodation discount. Please review the charges."))
	boarding.discount_request = json.dumps(discount) if discount else None
	boarding.accommodation_subtotal = flt(sum(amounts), precision)
	boarding.accommodation_discount_amount = float(applied)
	boarding.accommodation_total = flt(sum(amounts) - float(applied), precision)
	boarding.discount_recorded_at = boarding.check_out
	boarding.discount_recorded_by = frappe.session.user


class BoardingSalesInvoiceMixin:
	def calculate_taxes_and_totals(self):
		booking = self.get("custom_boarding_discount_booking")
		if (not booking and self.meta.has_field("custom_boarding_discount_booking")
			and self.get("is_return") and self.get("return_against")):
			booking = frappe.db.get_value("Sales Invoice", self.return_against, "custom_boarding_discount_booking")
			if booking:
				self.custom_boarding_discount_booking = booking
		if not booking:
			return super().calculate_taxes_and_totals()
		BoardingInvoiceTaxes(self)
		self.calculate_commission()
		self.calculate_contribution()


class BoardingInvoiceTaxes(calculate_taxes_and_totals):
	"""Use ERPNext's net discount fields, restricting distribution to accommodation.

	All tax calculation, rounding, totals, GL and advance handling remain ERPNext's.
	Only its document-discount allocation is replaced. Gross rows never change.
	"""
	def set_discount_amount(self):
		if self.doc.apply_discount_on != "Net Total" or self.doc.additional_discount_percentage:
			frappe.throw(_("Boarding discounts must use an amount against Net Total."))
		if self.doc.get("is_return"):
			# A partial service return must not inherit the accommodation discount.
			# Return only the discount attributable to the actual returned source rows.
			original = frappe.get_doc("Sales Invoice", self.doc.return_against)
			if original.get("custom_boarding_discount_booking") != self.doc.custom_boarding_discount_booking:
				frappe.throw(_("The return does not match the discounted boarding invoice."))
			sources = {row.name: row for row in original.items}
			self.return_shares = {}
			for row in self._items:
				source = sources.get(row.get("sales_invoice_item"))
				if not source or row.qty > 0 or abs(row.qty) > abs(source.qty):
					frappe.throw(_("Return quantities must refer to the original invoice rows."))
				self.return_shares[row.idx] = -flt(
					flt(source.distributed_discount_amount) * abs(row.qty / source.qty),
					self.doc.precision("discount_amount"),
				)
			self.doc.discount_amount = flt(sum(self.return_shares.values()), self.doc.precision("discount_amount"))
		return super().set_discount_amount()

	def apply_discount_amount(self):
		for row in self._items:
			row.distributed_discount_amount = 0
		if not self.doc.discount_amount:
			self.doc.base_discount_amount = 0
			return
		if self.doc.get("is_return"):
			eligible = self._items
			shares = [self.return_shares[row.idx] for row in eligible]
		else:
			eligible = eligible_invoice_rows(self.doc, self.doc.custom_boarding_discount_booking)
			shares, _target = distribute_discount(
				[row.net_amount for row in eligible],
				{"type": "amount", "value": self.doc.discount_amount},
				self.doc.precision("discount_amount"),
			)
		self.doc.base_discount_amount = flt(self.doc.discount_amount * self.doc.conversion_rate,
			self.doc.precision("base_discount_amount"))
		for row, share in zip(eligible, shares):
			row.distributed_discount_amount = float(share)
			row.net_amount = flt(Decimal(str(row.net_amount)) - Decimal(str(share)), row.precision("net_amount"))
			row.net_rate = flt(row.net_amount / row.qty, row.precision("net_rate")) if row.qty else 0
			self._set_in_company_currency(row, ["net_rate", "net_amount"])
		self.discount_amount_applied = True
		self._calculate()
