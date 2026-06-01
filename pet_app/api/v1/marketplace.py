from __future__ import annotations

import frappe

from pet_app.api import order, product, product_category
from pet_app.api.v1._helpers import call_api


@frappe.whitelist()
def get_products(**kwargs):
	return call_api(product.get_products, **kwargs)


@frappe.whitelist()
def get_stock_info(product_id, warehouse=None):
	return call_api(product.get_stock_info, product_id, warehouse=warehouse)


@frappe.whitelist()
def list_product_categories(**kwargs):
	return call_api(product_category.list_product_categories, **kwargs)


@frappe.whitelist(methods=["POST"])
def place_order(**kwargs):
	return call_api(order.place_order, **kwargs)
