"""Real concurrent checkout, isolated database only. Commits test fixtures."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import frappe

from pet_app.api.healthcare.boarding import check_out_boarding
from pet_app.tests.test_boarding_discount import TestBoardingDiscount
from pet_app.utils.invoice_source import build_marker


def run():
    assert str(frappe.conf.db_name).startswith('test_driver_orders_'), 'Never run on production'
    site = frappe.local.site
    results = []
    for number in range(3):
        fixture = TestBoardingDiscount()
        fixture.setUp()
        fixture.pay(100000)
        frappe.db.commit()
        gate = Barrier(2)
        discount = {'type':'amount','value':10000.01}

        def checkout():
            frappe.init(site=site)
            frappe.connect()
            frappe.set_user('Administrator')
            frappe.flags.in_test = True
            try:
                # Both readers start with an open booking, before either closes it.
                frappe.db.sql('SELECT name FROM `tabPet Boarding` WHERE name=%s', fixture.stay)
                gate.wait(timeout=20)
                for attempt in range(3):
                    result = check_out_boarding(fixture.stay, discount=discount)
                    if result.get('ok'):
                        frappe.db.commit()
                        return result['data'], attempt
                    frappe.db.rollback()
                    if result.get('meta',{}).get('code') not in ('QueryDeadlockError','QueryTimeoutError') or attempt == 2:
                        raise AssertionError(result)
            finally:
                frappe.destroy()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(checkout) for _ in range(2)]
            outcomes = [f.result(timeout=45) for f in futures]
        frappe.db.rollback()
        names = {data['sales_invoice'] for data, _ in outcomes}
        assert len(names) == 1, outcomes
        invoices = frappe.db.sql('''SELECT DISTINCT parent FROM `tabSales Invoice Item`
            WHERE description LIKE %s''', '%' + build_marker('Pet Boarding',fixture.stay) + '%')
        assert len(invoices) == 1, invoices
        invoice = frappe.get_doc('Sales Invoice', names.pop())
        assert invoice.docstatus == 1
        assert invoice.total_advance == 100000
        assert invoice.grand_total == 189999.99
        assert all(data['accommodation_discount_amount'] == 10000.01 for data,_ in outcomes)
        assert frappe.db.count('Payment Entry Reference',{'reference_name':invoice.name}) == 1
        results.append({'round':number+1,'invoices':1,'allocated':invoice.total_advance,
            'transaction_retries':sum(attempt for _,attempt in outcomes)})
    return results
