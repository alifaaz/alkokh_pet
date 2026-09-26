"""Explicit staging probe: commits its fixtures in an isolated test database.

Run after test_driver_orders. Separate thread-local Frappe connections contend on
the same Company row, so this tests actual database serialization, not mocks.
"""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import uuid

import frappe

from pet_app.tests.test_driver_orders import TestDriverOrders


def run():
    assert str(frappe.conf.db_name).startswith("test_driver_orders_"), "Never run on production"
    site = frappe.local.site
    fixture = TestDriverOrders()
    fixture.setUp()
    order = fixture.order()["order"]
    fixture.call("prepare_order", order=order)
    frappe.db.commit()
    key = uuid.uuid4().hex
    gate = Barrier(2)

    def worker():
        frappe.init(site=site)
        frappe.connect()
        frappe.set_user("Administrator")
        frappe.flags.in_test = True
        try:
            from pet_app.api.driver_orders import dispatch_order
            gate.wait(timeout=20)
            result = dispatch_order(order=order, pos_profile=fixture.profile.name, idempotency_key=key)
            if result["ok"]:
                frappe.db.commit()
            else:
                frappe.db.rollback()
            return result
        finally:
            frappe.destroy()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker) for _ in range(2)]
        results = [f.result(timeout=45) for f in futures]
    assert all(r["ok"] for r in results), results
    assert sorted(r["data"]["replayed"] for r in results) == [False, True], results
    frappe.db.rollback()  # release parent snapshot before observing worker commits
    count = frappe.db.count("Stock Entry", {"custom_sales_order": order, "docstatus": 1})
    assert count == 1, count
    # A second pair uses distinct request keys and tries to sell all of the same stock.
    # Exactly one should succeed; the other must see the committed closed order.
    row = frappe.get_doc("Sales Order", order).items[0].name
    gate = Barrier(2)

    def sell():
        frappe.init(site=site)
        frappe.connect()
        frappe.set_user("Administrator")
        frappe.flags.in_test = True
        try:
            from pet_app.api.driver_orders import record_delivery_result
            gate.wait(timeout=20)
            result = record_delivery_result(order=order, pos_profile=fixture.profile.name,
                accepted_items=[{"order_item": row, "qty": 5}], idempotency_key=uuid.uuid4().hex)
            if result["ok"]:
                frappe.db.commit()
            else:
                frappe.db.rollback()
            return result
        finally:
            frappe.destroy()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(sell) for _ in range(2)]
        results = [f.result(timeout=45) for f in results]
    assert sum(bool(r["ok"]) for r in results) == 1, results
    frappe.db.rollback()
    assert frappe.db.count("Sales Invoice", {"customer": fixture.customer, "docstatus": 1}) == 1
    return {"duplicate_dispatch": "one transfer, one replay", "competing_deliveries": "one invoice, one refusal"}
