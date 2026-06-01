from __future__ import annotations

import frappe

from pet_app.api import guardian_portal, sales
from pet_app.api.accounting import cashier
from pet_app.api.v1._helpers import call_api
from pet_app.pet_app.doctype.vet_visit import vet_visit


@frappe.whitelist()
def get_guardian_invoices(outstanding_only=0, limit=50):
	return call_api(guardian_portal.get_guardian_invoices, outstanding_only=outstanding_only, limit=limit)


@frappe.whitelist(methods=["POST"])
def create_sales_invoice_for_guardian(guardian, items, **kwargs):
	return call_api(sales.create_sales_invoice_for_guardian, guardian, items, **kwargs)


@frappe.whitelist(methods=["POST"])
def create_visit_invoice(visit_name):
	return call_api(vet_visit.create_sales_invoice, visit_name)


@frappe.whitelist(methods=["POST"])
def create_payment_entry_with_cashier_context(**kwargs):
	return call_api(cashier.create_payment_entry_with_cashier_context, **kwargs)
