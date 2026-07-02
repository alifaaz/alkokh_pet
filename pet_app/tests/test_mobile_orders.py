from __future__ import annotations

from unittest import TestCase

from pet_app.api import order as order_api
from pet_app.api.mobile import orders as mobile_orders


class _FakeMeta:
	def __init__(self, fields):
		self.fields = set(fields)

	def has_field(self, fieldname):
		return fieldname in self.fields


class _FakeSalesOrder:
	doctype = "Sales Order"

	def __init__(self, fields):
		self.meta = _FakeMeta(fields)
		self.values = {}

	def set(self, fieldname, value):
		self.values[fieldname] = value


class TestMobileOrderHelpers(TestCase):
	def test_delivery_coordinates_prefer_fixture_field_names(self):
		doc = _FakeSalesOrder(["custom_delivery_latitude", "custom_delivery_longitude"])

		result = order_api._set_sales_order_delivery_coordinates(
			doc,
			delivery_lat=33.3152,
			delivery_lng=44.3661,
		)

		self.assertEqual(result["latitude_field"], "custom_delivery_latitude")
		self.assertEqual(result["longitude_field"], "custom_delivery_longitude")
		self.assertEqual(doc.values["custom_delivery_latitude"], 33.3152)
		self.assertEqual(doc.values["custom_delivery_longitude"], 44.3661)

	def test_delivery_coordinates_support_legacy_field_names(self):
		doc = _FakeSalesOrder(["custom_delivery_lat", "custom_delivery_lng"])

		result = order_api._set_sales_order_delivery_coordinates(
			doc,
			delivery_lat=33.3152,
			delivery_lng=44.3661,
		)

		self.assertEqual(result["latitude_field"], "custom_delivery_lat")
		self.assertEqual(result["longitude_field"], "custom_delivery_lng")
		self.assertEqual(doc.values["custom_delivery_lat"], 33.3152)
		self.assertEqual(doc.values["custom_delivery_lng"], 44.3661)

	def test_order_summary_reads_sales_order_coordinate_aliases(self):
		row = {
			"name": "SAL-ORD-0001",
			"custom_order_status": "Preparing",
			"custom_delivery_latitude": 33.3152,
			"custom_delivery_longitude": 44.3661,
			"grand_total": 10000,
		}

		summary = mobile_orders._order_summary(row)

		self.assertEqual(summary["doctype"], "Sales Order")
		self.assertEqual(summary["status"], "Preparing")
		self.assertEqual(summary["delivery"]["latitude"], 33.3152)
		self.assertEqual(summary["delivery"]["longitude"], 44.3661)

	def test_promo_failure_is_not_wrapped_as_success(self):
		with self.assertRaises(mobile_orders.MobileOrderError) as raised:
			mobile_orders._unwrap_api_result(
				{"ok": True, "data": {"success": False, "message": "Invalid coupon"}}
			)

		self.assertEqual(raised.exception.code, "promo.invalid")

	def test_mobile_payment_method_is_cash_only(self):
		self.assertEqual(mobile_orders._normalize_payment_method("cod"), "Cash on Delivery")
		self.assertEqual(mobile_orders._normalize_payment_method("cash"), "Cash on Delivery")

		with self.assertRaises(mobile_orders.MobileOrderError) as raised:
			mobile_orders._normalize_payment_method("card")

		self.assertEqual(raised.exception.code, "payment.unsupported")
