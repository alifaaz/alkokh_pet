"""Concurrent charges against a genuinely isolated database; commits test fixtures.

Both connections establish a repeatable-read snapshot before either charges. This
reproduces the stale-snapshot race that a plain SELECT after the customer lock misses.
"""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import frappe

from pet_app.tests.test_invoice_reuse import TestInvoiceReuse
from pet_app.utils.invoice_reuse import get_or_create_open_invoice


def run():
    assert str(frappe.conf.db_name).startswith('test_driver_orders_'), 'Never run on production'
    site = frappe.local.site
    results = []
    for round_no in range(3):
        fixture = TestInvoiceReuse()
        fixture.setUp()
        frappe.db.commit()
        gate = Barrier(2)

        def charge(item):
            frappe.init(site=site)
            frappe.connect()
            assert str(frappe.conf.db_name).startswith('test_driver_orders_')
            frappe.set_user('Administrator')
            frappe.flags.in_test = True
            try:
                frappe.db.sql('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                frappe.db.sql('SELECT name FROM `tabSales Invoice` WHERE customer=%s', fixture.customer)
                gate.wait(timeout=20)
                # MariaDB innodb_snapshot_isolation=ON rejects locking a row
                # committed after our snapshot (error 1020). Retry the WHOLE charge
                # transaction, never roll back unrelated caller work inside the helper.
                for attempt in range(3):
                    try:
                        result = get_or_create_open_invoice(customer=fixture.customer, company=fixture.company,
                            branch='main', items=[dict(item_code=item, qty=1, rate=100)],
                            source_doctype='User', source_name='Administrator')
                        frappe.db.commit()
                        return result.invoice.name, attempt
                    except frappe.QueryDeadlockError:
                        frappe.db.rollback()
                        if attempt == 2:
                            raise

            except Exception:
                frappe.db.rollback()
                raise
            finally:
                frappe.destroy()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(charge, item) for item in (fixture.service, fixture.stock)]
            outcomes = [future.result(timeout=45) for future in futures]
            names = [outcome[0] for outcome in outcomes]
        frappe.db.rollback()
        assert len(set(names)) == 1, names
        assert frappe.db.count('Sales Invoice', {'customer': fixture.customer, 'docstatus': 0}) == 1
        invoice = frappe.get_doc('Sales Invoice', names[0])
        assert len(invoice.items) == 2
        assert {r.item_code for r in invoice.items} == {fixture.service, fixture.stock}
        assert invoice.update_stock == 1
        results.append({'round': round_no + 1, 'invoices': 1, 'lines': 2, 'update_stock': 1,
            'whole_transaction_retries': sum(outcome[1] for outcome in outcomes)})
    return results
