"""Real ledger tests. Run only against an isolated test database, never production.

Uses unittest directly to avoid ERPNext's unrelated app-wide test-record preloader.
Each test rolls back; schema installation happens separately before this module runs.
"""
import json
import unittest
import uuid
from unittest.mock import patch

import frappe
from frappe.utils import flt

from pet_app.api import driver_orders as api
from pet_app.utils.driver_orders import cash_balance, loads


class TestDriverOrders(unittest.TestCase):
    def setUp(self):
        if not str(frappe.conf.db_name).startswith("test_driver_orders_"):
            self.skipTest("Requires an isolated test_driver_orders_* database.")
        frappe.set_user("Administrator")
        frappe.db.set_single_value("Pet App Accounting Settings", "custom_enable_driver_orders", 1)
        self.profile = frappe.get_doc("POS Profile", "Alkokh Vet Store - Main Cashier")
        self.company = self.profile.company
        self.profile.allow_partial_payment = 1
        self.profile.append("applicable_for_users", {"user": "Administrator", "default": 1})
        self.profile.save(ignore_permissions=True)
        frappe.clear_cache(doctype="POS Profile")
        self.driver = frappe.get_doc("Driver", "HR-DRI-2026-00021")
        self.account = self.driver.custom_cash_account
        self.start_cash = cash_balance(self.account)
        suffix = uuid.uuid4().hex[:10]
        self.customer = frappe.get_doc({"doctype": "Customer", "customer_name": f"_Test Driver {suffix}",
            "customer_type": "Individual", "customer_group": frappe.db.get_value("Customer Group", {}, "name"),
            "territory": frappe.db.get_value("Territory", {}, "name")}).insert(ignore_permissions=True).name
        self.item = frappe.get_doc({"doctype": "Item", "item_code": f"_Test Driver {suffix}",
            "item_name": f"_Test Driver {suffix}", "stock_uom": "Nos", "is_stock_item": 1,
            "item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
            "valuation_rate": 1000, "uoms": [{"uom": "Nos", "conversion_factor": 1},
            {"uom": "Box", "conversion_factor": 5}]}).insert(ignore_permissions=True).name
        receipt = frappe.get_doc({"doctype": "Stock Entry", "stock_entry_type": "Material Receipt", "company": self.company,
            "items": [{"item_code": self.item, "qty": 100, "t_warehouse": self.profile.warehouse, "basic_rate": 1000}]})
        receipt.insert(ignore_permissions=True)
        receipt.submit()

    def tearDown(self):
        frappe.db.rollback()
        frappe.set_user("Administrator")
        frappe.clear_cache()

    def call(self, name, **kwargs):
        kwargs.setdefault("pos_profile", self.profile.name)
        kwargs.setdefault("idempotency_key", uuid.uuid4().hex)
        result = getattr(api, name)(**kwargs)
        self.assertTrue(result.get("ok"), json.dumps(result, default=str))
        return result["data"]

    def order(self, qty=5, rate=10000, **kwargs):
        return self.call("create_pos_order", customer=self.customer, driver=self.driver.name,
            items=[{"item_code": self.item, "qty": qty, "rate": rate}], delivery_fee=5000, **kwargs)

    def dispatch(self, qty=5, **kwargs):
        result = self.order(qty=qty, **kwargs)
        name = result["order"]
        self.call("prepare_order", order=name)
        self.call("dispatch_order", order=name)
        return name, result["items"][0]["order_item"]

    def bin(self, warehouse):
        return flt(frappe.db.get_value("Bin", {"item_code": self.item, "warehouse": warehouse}, "actual_qty"))

    def test_full_delivery_fee_payment_and_partial_handover(self):
        order, row = self.dispatch()
        self.assertEqual(self.bin(self.profile.warehouse), 95)
        self.assertEqual(self.bin(self.driver.custom_warehouse), 5)
        self.assertEqual(cash_balance(self.account), self.start_cash)
        result = self.call("record_delivery_result", order=order, accepted_items=[{"order_item": row, "qty": 5}],
            payment={"amount": 50000, "received_by": "driver", "mode_of_payment": "نقداً"})
        self.assertEqual(result["goods_total"], 50000)
        self.assertEqual(result["direct_delivery_fee"], 5000)
        self.assertEqual(result["customer_total"], 55000)
        self.assertEqual(result["outstanding_amount"], 0)
        self.assertEqual(cash_balance(self.account)-self.start_cash, 50000)
        self.assertEqual(self.bin(self.driver.custom_warehouse), 0)
        self.call("receive_driver_cash", driver=self.driver.name, amount=20000)
        self.assertEqual(cash_balance(self.account)-self.start_cash, 30000)

    def test_partial_delivery_retry_fee_once_and_return_leftovers(self):
        order, row = self.dispatch()
        first = self.call("record_delivery_result", order=order, accepted_items=[{"order_item": row, "qty": 2}])
        self.assertEqual(first["direct_delivery_fee"], 5000)
        self.assertEqual(first["delivery_state"], "Partially Delivered")
        second = self.call("record_delivery_result", order=order, accepted_items=[{"order_item": row, "qty": 1}], remainder="close")
        self.assertEqual(second["direct_delivery_fee"], 0)
        self.assertEqual(second["delivery_state"], "Completed")
        held = next(r for r in loads(self.driver.name) if r.custom_driver_order_item == row)
        self.call("return_unsold_stock", driver=self.driver.name, items=[{"load_row": held.name, "stock_qty": 2}])
        self.assertEqual(self.bin(self.driver.custom_warehouse), 0)
        self.assertEqual(self.bin(self.profile.warehouse), 97)

    def test_refusal_then_redelivery(self):
        order, row = self.dispatch()
        result = self.call("record_delivery_result", order=order, accepted_items=[])
        self.assertEqual(result["delivery_state"], "Returned")
        self.assertEqual(self.bin(self.driver.custom_warehouse), 5)
        result = self.call("record_delivery_result", order=order, accepted_items=[{"order_item": row, "qty": 5}])
        self.assertEqual(result["direct_delivery_fee"], 5000)

    def test_general_van_sale_card_and_credit(self):
        self.call("load_van", driver=self.driver.name, items=[{"item_code": self.item, "qty": 5}])
        result = self.call("create_van_sale", driver=self.driver.name, customer=self.customer,
            items=[{"item_code": self.item, "qty": 2, "rate": 10000}],
            payment={"amount": 20000, "received_by": "bank", "mode_of_payment": "بطاقة بنكية", "external_reference": "test-terminal-1"})
        self.assertEqual(result["outstanding_amount"], 0)
        self.assertEqual(cash_balance(self.account), self.start_cash)
        result = self.call("create_van_sale", driver=self.driver.name, customer=self.customer,
            items=[{"item_code": self.item, "qty": 3, "rate": 10000}])
        self.assertEqual(result["outstanding_amount"], 30000)

    def test_advance_then_delivery_no_second_collection(self):
        result = self.order(payment_arrangement="Prepaid")
        order = result["order"]
        self.call("record_customer_payment", reference_doctype="Sales Order", reference_name=order,
            payment={"amount": 50000, "received_by": "shop", "mode_of_payment": "نقداً"})
        self.call("prepare_order", order=order)
        self.call("dispatch_order", order=order)
        result = self.call("record_delivery_result", order=order, accepted_items=[{"order_item": result["items"][0]["order_item"], "qty": 5}])
        self.assertEqual(result["outstanding_amount"], 0)
        self.assertEqual(cash_balance(self.account), self.start_cash)

    def test_sold_return_and_customer_refund(self):
        order, row = self.dispatch()
        sale = self.call("record_delivery_result", order=order, accepted_items=[{"order_item": row, "qty": 5}],
            payment={"amount": 50000, "received_by": "driver", "mode_of_payment": "نقداً"})
        item = frappe.get_doc("Sales Invoice", sale["invoice"]).items[0].name
        credit = self.call("return_sold_goods", invoice=sale["invoice"], items=[{"invoice_item": item, "qty": 2}])
        self.assertEqual(credit["goods_total"], -20000)
        self.assertEqual(credit["direct_delivery_fee"], 0)
        self.assertEqual(self.bin(self.driver.custom_warehouse), 2)
        self.call("refund_customer", return_invoice=credit["invoice"], amount=20000,
            payment={"received_by": "driver", "mode_of_payment": "نقداً"})
        self.assertEqual(cash_balance(self.account)-self.start_cash, 30000)

    def test_unused_advance_refund(self):
        result = self.order(payment_arrangement="Prepaid")
        order = result["order"]
        paid = self.call("record_customer_payment", reference_doctype="Sales Order", reference_name=order,
            payment={"amount": 50000, "received_by": "shop", "mode_of_payment": "نقداً"})
        payment = next(r["name"] for r in paid["documents"] if r["doctype"] == "Payment Entry")
        self.call("close_order_remainder", order=order)
        refund = self.call("refund_customer", advance_payment=payment, amount=50000,
            payment={"received_by": "shop", "mode_of_payment": "نقداً"})
        self.assertEqual(len(refund["documents"]), 1)

    def test_idempotent_dispatch(self):
        order = self.order()["order"]
        self.call("prepare_order", order=order)
        key = uuid.uuid4().hex
        one = self.call("dispatch_order", order=order, idempotency_key=key)
        two = self.call("dispatch_order", order=order, idempotency_key=key)
        self.assertEqual(one["documents"], two["documents"])
        self.assertTrue(two["replayed"])
        self.assertEqual(self.bin(self.driver.custom_warehouse), 5)

    def test_reserved_order_stock_cannot_be_sold_as_van_stock(self):
        self.dispatch()
        response = api.create_van_sale(driver=self.driver.name, customer=self.customer, pos_profile=self.profile.name,
            items=[{"item_code": self.item, "qty": 1, "rate": 1000}], idempotency_key=uuid.uuid4().hex)
        self.assertFalse(response["ok"])
        self.assertEqual(response["meta"]["code"], "DRIVER_STOCK_ALLOCATED")

    def test_uom_load_and_sale(self):
        self.call("load_van", driver=self.driver.name, items=[{"item_code": self.item, "qty": 2, "uom": "Box"}])
        self.assertEqual(self.bin(self.driver.custom_warehouse), 10)
        self.call("create_van_sale", driver=self.driver.name, customer=self.customer,
            items=[{"item_code": self.item, "qty": 1, "uom": "Box", "rate": 50000}])
        self.assertEqual(self.bin(self.driver.custom_warehouse), 5)

    def test_failure_after_invoice_submit_rolls_back_sale(self):
        order, row = self.dispatch()
        with patch.object(api, "_receive", side_effect=RuntimeError("simulated payment failure")):
            response = api.record_delivery_result(order=order, pos_profile=self.profile.name,
                accepted_items=[{"order_item": row, "qty": 5}], payment={"amount": 50000}, idempotency_key=uuid.uuid4().hex)
        self.assertFalse(response["ok"])
        self.assertFalse(frappe.db.exists("Sales Invoice", {"customer": self.customer}))

    def test_fee_cannot_be_included_in_goods_payment(self):
        order, row = self.dispatch()
        response = api.record_delivery_result(order=order, pos_profile=self.profile.name,
            accepted_items=[{"order_item": row, "qty": 5}],
            payment={"amount": 55000, "received_by": "driver", "mode_of_payment": "نقداً"}, idempotency_key=uuid.uuid4().hex)
        self.assertFalse(response["ok"])
        self.assertEqual(response["meta"]["code"], "GOODS_PAYMENT_EXCEEDS_BALANCE")

    def test_disabled_and_guest_refused(self):
        frappe.db.set_single_value("Pet App Accounting Settings", "custom_enable_driver_orders", 0)
        response = api.load_van(driver=self.driver.name, pos_profile=self.profile.name,
            items=[{"item_code": self.item, "qty": 1}], idempotency_key=uuid.uuid4().hex)
        self.assertFalse(response["ok"])
        self.assertEqual(response["meta"]["code"], "DRIVER_ORDERS_DISABLED")
        frappe.set_user("Guest")
        response = api.load_van(driver=self.driver.name, pos_profile=self.profile.name,
            items=[], idempotency_key=uuid.uuid4().hex)
        self.assertFalse(response["ok"])

    def test_managed_invoice_cannot_be_cancelled(self):
        order, row = self.dispatch()
        result = self.call("record_delivery_result", order=order, accepted_items=[{"order_item": row, "qty": 5}])
        invoice = frappe.get_doc("Sales Invoice", result["invoice"])
        with self.assertRaises(frappe.ValidationError):
            invoice.cancel()

    def test_fixed_discount_preserved_on_partial_delivery(self):
        order, row = self.dispatch(discount_type="Amount", discount_value=5000)
        result = self.call("record_delivery_result", order=order, accepted_items=[{"order_item": row, "qty": 2}])
        self.assertEqual(result["goods_total"], 18000)
        result = self.call("record_delivery_result", order=order, accepted_items=[{"order_item": row, "qty": 3}])
        self.assertEqual(result["goods_total"], 27000)

    def test_assign_driver_after_order_creation(self):
        result = self.call("create_pos_order", customer=self.customer,
            items=[{"item_code": self.item, "qty": 1, "rate": 10000}])
        self.call("assign_driver_to_order", order=result["order"], driver=self.driver.name, delivery_fee=5000)
        self.call("prepare_order", order=result["order"])
        self.call("dispatch_order", order=result["order"])
        doc = frappe.get_doc("Sales Order", result["order"])
        self.assertEqual(doc.custom_delivery_fee, 5000)

    def test_partial_advance_used_then_multiple_refunds(self):
        result = self.order(payment_arrangement="Prepaid")
        order = result["order"]
        paid = self.call("record_customer_payment", reference_doctype="Sales Order", reference_name=order,
            payment={"amount": 50000, "received_by": "shop", "mode_of_payment": "نقداً"})
        payment = next(r["name"] for r in paid["documents"] if r["doctype"] == "Payment Entry")
        self.call("prepare_order", order=order)
        self.call("dispatch_order", order=order)
        invoice = self.call("record_delivery_result", order=order,
            accepted_items=[{"order_item": result["items"][0]["order_item"], "qty": 2}], remainder="close")
        self.assertEqual(invoice["outstanding_amount"], 0)
        for amount in (10000, 20000):
            self.call("refund_customer", advance_payment=payment, amount=amount,
                payment={"received_by": "shop", "mode_of_payment": "نقداً"})
        balance = frappe.db.sql("select sum(debit-credit) from `tabGL Entry` where party_type='Customer' and party=%s and is_cancelled=0", self.customer)[0][0]
        self.assertAlmostEqual(flt(balance), 0)

    def test_partial_customer_payment_and_wallet(self):
        order, row = self.dispatch()
        result = self.call("record_delivery_result", order=order,
            accepted_items=[{"order_item": row, "qty": 5}],
            payment={"amount": 20000, "received_by": "driver", "mode_of_payment": "نقداً"})
        self.assertEqual(result["outstanding_amount"], 30000)
        self.call("record_customer_payment", reference_doctype="Sales Invoice", reference_name=result["invoice"],
            payment={"amount": 30000, "received_by": "bank", "mode_of_payment": "محفظة إلكترونية / Mobile Money", "external_reference": "wallet-test-1"})
        self.assertEqual(cash_balance(self.account)-self.start_cash, 20000)
        self.assertEqual(frappe.db.get_value("Sales Invoice", result["invoice"], "outstanding_amount"), 0)

    def test_request_key_conflict(self):
        key = uuid.uuid4().hex
        self.call("load_van", driver=self.driver.name, items=[{"item_code": self.item, "qty": 1}], idempotency_key=key)
        response = api.load_van(driver=self.driver.name, pos_profile=self.profile.name,
            items=[{"item_code": self.item, "qty": 2}], idempotency_key=key)
        self.assertEqual(response["meta"]["code"], "IDEMPOTENCY_CONFLICT")

    def test_cross_branch_write_rejected(self):
        order, row = self.dispatch()
        response = api.record_delivery_result(order=order, pos_profile="Alkokh Vet Hotel - Cashier",
            accepted_items=[{"order_item": row, "qty": 1}], idempotency_key=uuid.uuid4().hex)
        self.assertEqual(response["meta"]["code"], "DRIVER_BRANCH_MISMATCH")

    def test_raw_driver_stock_entry_rejected(self):
        doc = frappe.get_doc({"doctype": "Stock Entry", "company": self.company, "stock_entry_type": "Material Transfer",
            "items": [{"item_code": self.item, "qty": 1, "s_warehouse": self.profile.warehouse, "t_warehouse": self.driver.custom_warehouse}]})
        with self.assertRaises(frappe.ValidationError):
            doc.insert(ignore_permissions=True)

    def test_invoice_snapshot_survives_driver_master_change(self):
        order, row = self.dispatch()
        sale = self.call("record_delivery_result", order=order, accepted_items=[{"order_item": row, "qty": 5}])
        # Simulate a later master reassignment after custody has been settled. Returns
        # must still use the original submitted load, not today's Driver mapping.
        frappe.db.set_value("Driver", self.driver.name, "custom_warehouse", self.profile.warehouse)
        original = frappe.get_doc("Sales Invoice", sale["invoice"])
        credit = self.call("return_sold_goods", invoice=original.name, items=[{"invoice_item": original.items[0].name, "qty": 1}])
        self.assertEqual(frappe.db.get_value("Sales Invoice", credit["invoice"], "set_warehouse"), self.driver.custom_warehouse)

    def test_tax_template_changes_do_not_reprice_order(self):
        parent = frappe.db.get_value("Account", {"company": self.company, "root_type": "Liability", "is_group": 1}, "name")
        tax_account = frappe.get_doc({"doctype": "Account", "company": self.company, "account_name": self.item+" Tax",
            "parent_account": parent, "root_type": "Liability", "account_type": "Tax"}).insert(ignore_permissions=True).name
        template = frappe.get_doc({"doctype": "Item Tax Template", "title": self.item+" Tax", "company": self.company,
            "taxes": [{"tax_type": tax_account, "tax_rate": 10}]}).insert(ignore_permissions=True)
        item = frappe.get_doc("Item", self.item)
        item.append("taxes", {"item_tax_template": template.name})
        item.save(ignore_permissions=True)
        sales_tax = frappe.get_doc({"doctype": "Sales Taxes and Charges Template", "title": self.item+" Sales Tax", "company": self.company,
            "taxes": [{"charge_type": "On Net Total", "account_head": tax_account, "description": "Test tax", "rate": 10}]}).insert(ignore_permissions=True)
        frappe.db.set_value("POS Profile", self.profile.name, "taxes_and_charges", sales_tax.name)
        order, row = self.dispatch()
        original_total = frappe.db.get_value("Sales Order", order, "grand_total")
        self.assertEqual(original_total, 55000)
        frappe.db.set_value("Item Tax Template Detail", template.taxes[0].name, "tax_rate", 20)
        frappe.clear_cache(doctype="Item Tax Template")
        result = self.call("record_delivery_result", order=order, accepted_items=[{"order_item": row, "qty": 5}])
        self.assertEqual(result["goods_total"], original_total)

    def test_assigned_cashier_can_operate_but_not_other_branch(self):
        email = f"driver-cashier-{uuid.uuid4().hex[:8]}@example.test"
        user = frappe.get_doc({"doctype": "User", "email": email, "first_name": "Driver Test Cashier",
            "enabled": 1, "send_welcome_email": 0, "user_type": "System User",
            "roles": [{"role": role} for role in ("POS Page", "Sales Invoices", "Sales Order", "Stock User",
                "Payment Entries Update", "Delivery User", "Customers Read", "Items Read", "Accounts User")]})
        user.insert(ignore_permissions=True)
        profile = frappe.get_doc("POS Profile", self.profile.name)
        profile.append("applicable_for_users", {"user": email, "default": 1})
        profile.save(ignore_permissions=True)
        for doctype, name in (("POS Profile", self.profile.name), ("Branch", "main"),
                              ("Warehouse", self.profile.warehouse), ("Warehouse", self.driver.custom_warehouse)):
            frappe.get_doc({"doctype": "User Permission", "user": email, "allow": doctype, "for_value": name}).insert(ignore_permissions=True)
        frappe.clear_cache(user=email)
        frappe.set_user(email)
        order, row = self.dispatch()
        result = self.call("record_delivery_result", order=order, accepted_items=[{"order_item": row, "qty": 5}],
            payment={"amount": 50000, "received_by": "driver", "mode_of_payment": "نقداً"})
        self.assertEqual(result["outstanding_amount"], 0)
        self.call("receive_driver_cash", driver=self.driver.name, amount=50000)
        denied = api.load_van(driver=self.driver.name, pos_profile="Alkokh Vet Hotel - Cashier",
            items=[{"item_code": self.item, "qty": 1}], idempotency_key=uuid.uuid4().hex)
        self.assertFalse(denied["ok"])

    def test_new_driver_receives_distinct_warehouse(self):
        parent = frappe.db.get_single_value("Pet App Accounting Settings", "custom_driver_cash_parent")
        account = frappe.get_doc({"doctype": "Account", "account_name": self.item+" Driver Cash",
            "parent_account": parent, "company": self.company, "root_type": "Asset", "account_type": "Cash",
            "account_currency": "IQD"}).insert(ignore_permissions=True)
        # User/address provisioning is an existing, separate subsystem. Exercise the
        # real Driver insert hooks and warehouse/account setup without sending mail.
        with patch("pet_app.api.driver.sync_driver_user"), patch("pet_app.api.driver.sync_driver_address"):
            driver = frappe.get_doc({"doctype": "Driver", "full_name": self.item, "status": "Active",
                "cell_number": "07703456789", "custom_username": uuid.uuid4().hex[:12],
                "custom_cash_account": account.name}).insert(ignore_permissions=True)
        driver.reload()
        self.assertTrue(driver.custom_warehouse)
        self.assertNotEqual(driver.custom_warehouse, self.driver.custom_warehouse)
        self.assertEqual(frappe.db.get_value("Warehouse", driver.custom_warehouse, "is_group"), 0)

    # --- Driver screen: split collection at the door, and the settled/payment indicators ---

    def scan(self, order, collection=None):
        from pet_app.api import driver_self
        user = frappe.db.get_value("Driver", self.driver.name, "user")
        if not user:
            self.skipTest("The test driver has no linked user.")
        frappe.set_user(user)
        try:
            return driver_self.scan_deliver(order=order, idempotency_key=uuid.uuid4().hex,
                collection=json.dumps(collection) if collection is not None else None)
        finally:
            frappe.set_user("Administrator")

    def card_mode(self):
        from pet_app.api.driver_self import _card_modes
        modes = _card_modes()
        if not modes:
            self.skipTest("No non-cash Mode of Payment with a company account.")
        return modes[0]

    def invoice_of(self, order):
        return frappe.get_doc("Sales Invoice", frappe.db.get_value("Sales Invoice Item",
            {"sales_order": order, "docstatus": 1}, "parent"))

    def test_driver_split_cash_card_and_due(self):
        order, _ = self.dispatch()
        mode = self.card_mode()
        result = self.scan(order, {"cash_amount": 10000, "card_amount": 5000, "card_mode": mode})
        self.assertTrue(result.get("ok"), json.dumps(result, default=str))
        invoice = self.invoice_of(order)
        due = flt(invoice.grand_total) - 15000
        self.assertEqual(result["data"]["collection"], {"cash": 10000, "card": 5000, "due": due})
        self.assertEqual(cash_balance(self.account) - self.start_cash, 10000)
        self.assertEqual(flt(invoice.outstanding_amount), due)
        card_account = frappe.db.get_value("Mode of Payment Account", {"parent": mode, "company": self.company}, "default_account")
        self.assertTrue(frappe.db.exists("Payment Entry", {"docstatus": 1, "paid_to": card_account, "paid_amount": 5000,
            "reference_no": order}))
        status = api.order_money_status([order])[order]
        self.assertEqual(status["payment_status"], "Partly Paid")
        self.assertEqual((status["cash_collected"], status["other_collected"]), (10000, 5000))

    def test_driver_all_due_takes_no_payment(self):
        order, _ = self.dispatch()
        result = self.scan(order, {"cash_amount": 0, "card_amount": 0})
        self.assertTrue(result.get("ok"), json.dumps(result, default=str))
        self.assertEqual(result["data"]["delivery_state"], "Completed")
        self.assertFalse([d for d in result["data"]["documents"] if d["doctype"] == "Payment Entry"])
        self.assertEqual(cash_balance(self.account), self.start_cash)
        self.assertEqual(api.order_money_status([order])[order]["payment_status"], "Unpaid")

    def test_driver_overpay_rolls_back_everything(self):
        order, _ = self.dispatch()
        result = self.scan(order, {"cash_amount": 50000, "card_amount": 50000, "card_mode": self.card_mode()})
        self.assertFalse(result.get("ok"))
        self.assertFalse(frappe.db.exists("Sales Invoice Item", {"sales_order": order, "docstatus": 1}))
        self.assertEqual(frappe.db.get_value("Sales Order", order, "custom_delivery_state"), "Out for Delivery")

    def test_driver_without_collection_takes_full_cash(self):
        order, _ = self.dispatch()
        result = self.scan(order)
        self.assertTrue(result.get("ok"), json.dumps(result, default=str))
        self.assertEqual(flt(result["data"]["outstanding_amount"]), 0)
        self.assertEqual(cash_balance(self.account) - self.start_cash, flt(self.invoice_of(order).grand_total))

    def test_driver_cash_refund_capped_at_cash_taken(self):
        from pet_app.api import driver_self
        order, _ = self.dispatch()
        total = flt(self.order_total(order))
        self.assertTrue(self.scan(order, {"cash_amount": 10000, "card_amount": total - 10000, "card_mode": self.card_mode()}).get("ok"))
        before = cash_balance(self.account)
        frappe.set_user(frappe.db.get_value("Driver", self.driver.name, "user"))
        try:
            result = driver_self.report_return(order=order, reason="changed_mind", refund_cash=1, idempotency_key=uuid.uuid4().hex)
        finally:
            frappe.set_user("Administrator")
        self.assertTrue(result.get("ok"), json.dumps(result, default=str))
        self.assertLessEqual(before - cash_balance(self.account), 10000 + 0.01)

    def order_total(self, order):
        # The invoice does not exist before delivery: goods + a booked fee (when fees are configured).
        from pet_app.utils.driver_orders import fee_config
        so = frappe.get_doc("Sales Order", order)
        return flt(so.grand_total) + (flt(so.custom_delivery_fee) if fee_config() else 0)

    def test_settlement_flips_the_settled_indicator(self):
        if not frappe.get_meta("Sales Order").has_field("custom_driver_settlement"):
            self.skipTest("Driver settlement is not migrated on this site.")
        frappe.db.set_single_value("Pet App Accounting Settings", "custom_driver_settlement_start", "2000-01-01")
        order, _ = self.dispatch()
        self.assertTrue(self.scan(order, {"cash_amount": 10000, "card_amount": 0}).get("ok"))
        self.assertEqual(api.order_money_status([order])[order]["settlement_status"], "pending")
        self.call("create_driver_settlement", driver=self.driver.name, orders=[order], mode_of_payment="نقداً",
            collected_amount=10000, handed_over_amount=10000)
        status = api.order_money_status([order])[order]
        self.assertEqual(status["settlement_status"], "settled")
        self.assertTrue(status["is_settled"])
        listed = self.call("list_pos_orders", settled=1, page_length=100)["orders"]
        self.assertIn(order, [r["name"] for r in listed])
        self.assertNotIn(order, [r["name"] for r in self.call("list_pos_orders", settled=0, page_length=100)["orders"]])

    def test_driver_is_notified_on_assign_dispatch_and_cancel(self):
        user = frappe.db.get_value("Driver", self.driver.name, "user")
        if not user or not frappe.db.get_value("User", user, "enabled"):
            self.skipTest("The test driver has no enabled linked user.")
        count = lambda: frappe.db.count("Notification Log", {"for_user": user, "document_type": "Sales Order"})
        start = count()
        key = uuid.uuid4().hex
        order, _ = self.dispatch(idempotency_key=key)
        self.assertEqual(count() - start, 2)  # assigned + out for delivery
        self.order(idempotency_key=key)  # a replay notifies nobody again
        self.assertEqual(count() - start, 2)
        self.call("cancel_order", order=order)
        self.assertEqual(count() - start, 3)
        link = frappe.get_all("Notification Log", filters={"for_user": user}, pluck="link", order_by="creation desc", limit=1)[0]
        self.assertTrue(link.endswith("/drivers/me"))

    # --- Driver screen: order lines and customer search ---

    def as_driver(self, method, **kwargs):
        from pet_app.api import driver_self
        user = frappe.db.get_value("Driver", self.driver.name, "user")
        if not user:
            self.skipTest("The test driver has no linked user.")
        frappe.set_user(user)
        try:
            return getattr(driver_self, method)(**kwargs)
        finally:
            frappe.set_user("Administrator")

    def my_lines(self, order):
        result = self.as_driver("get_my_order", order=order)
        self.assertTrue(result.get("ok"), json.dumps(result, default=str))
        return [(r["item_code"], r["item_name"], r["qty"], r["uom"], r["rate"], r["amount"], r["delivered_qty"])
            for r in result["data"]["order"]["items"]]

    def test_driver_sees_order_lines_in_every_state(self):
        order, _ = self.dispatch(qty=5)
        line = (self.item, self.item, 5, "Nos", 10000, 50000)
        self.assertEqual(self.my_lines(order), [line + (0,)])
        self.assertTrue(self.scan(order).get("ok"))
        self.assertEqual(self.my_lines(order), [line + (5,)])
        result = self.as_driver("report_return", order=order, reason="changed_mind", idempotency_key=uuid.uuid4().hex)
        self.assertTrue(result.get("ok"), json.dumps(result, default=str))
        self.assertEqual(frappe.db.get_value("Sales Order", order, "custom_delivery_state"), "Returned")
        self.assertEqual(self.my_lines(order), [line + (0,)])
        frappe.db.set_value("Sales Order", order, "custom_driver", "HR-DRI-NOT-MINE")
        self.assertEqual(self.as_driver("get_my_order", order=order)["meta"]["code"], "ORDER_NOT_YOURS")

    def test_driver_search_folds_spelling_and_counts_matches(self):
        from pet_app.api.driver_self import _fold
        suffix = uuid.uuid4().hex[:8]
        self.customer = frappe.get_doc({"doctype": "Customer", "customer_name": f"أحمد تجربة {suffix}",
            "customer_type": "Individual", "customer_group": frappe.db.get_value("Customer Group", {}, "name"),
            "territory": frappe.db.get_value("Territory", {}, "name")}).insert(ignore_permissions=True).name
        order, _ = self.dispatch()
        phone = "0770" + str(uuid.uuid4().int)[:7]
        frappe.db.set_value("Sales Order", order, "contact_mobile", phone, update_modified=False)

        def search(term, group="all"):
            result = self.as_driver("list_my_orders", group=group, limit_start=0, page_length=100, search=term)
            self.assertTrue(result.get("ok"), json.dumps(result, default=str))
            data = result["data"]
            for row in data["orders"]:  # every row contains the term
                self.assertTrue(any(_fold(term) in _fold(v) for v in (row["name"], row["customer"],
                    row["customer_name"], row["phone"]) if v), row["name"])
            return data

        found = search(f"احمد تجربه {suffix}")
        self.assertEqual(([r["name"] for r in found["orders"]], found["total"]), ([order], 1))
        self.assertEqual(found["counts"], {"on_road": 1, "returned": 0, "delivered": 0, "all": 1})
        self.assertEqual(search(f"احمد تجربه {suffix}", group="returned")["total"], 0)
        by_phone = phone[-6:].translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩"))
        self.assertIn(order, [r["name"] for r in search(by_phone)["orders"]])
        nothing = search(f"zzzz{suffix}")
        self.assertEqual((nothing["orders"], nothing["total"], set(nothing["counts"].values())), ([], 0, {0}))
        plain = self.as_driver("list_my_orders", group="all", limit_start=0, page_length=10)["data"]
        self.assertEqual(self.as_driver("list_my_orders", group="all", limit_start=0, page_length=10, search=" ")["data"], plain)
        # A hidden phone is not searchable either.
        frappe.db.set_single_value("Pet App Accounting Settings", "custom_driver_sees_customer_phone", 0)
        self.assertNotIn(order, [r["name"] for r in search(by_phone)["orders"]])


class TestDriverSearchFold(unittest.TestCase):
    def test_spellings_fold_together(self):
        from pet_app.api.driver_self import _fold
        for typed, stored in [("احمد", "أحمد"), ("اسراء", "إسراء"), ("امنه", "آمنة"), ("علي", "على"),
                ("محمد", "مُحَمَّد"), ("علي", "عـــلي"), ("٠٧٧٠١٢٣", "0770123"), ("0770", "‎0770‏"),
                ("sal-ord", "SAL-ORD"), ("نور احمد", "  نور   احمد ")]:
            self.assertEqual(_fold(typed), _fold(stored), (typed, stored))
        self.assertEqual([_fold(v) for v in (None, "", "ـ", "  ")], ["", "", "", ""])
