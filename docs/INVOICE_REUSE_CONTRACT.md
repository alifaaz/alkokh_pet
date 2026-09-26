# Regular invoice reuse and stock ownership

Updated 2026-09-09. Applies to `pet_app`'s shared invoice creation paths. This is the current policy for invoice reuse and stock timing; it supersedes older warehouse-based or dispense-owned stock descriptions.

## Behavior

- One open regular draft is reused for the same Customer, Company and Branch across dates. `update_stock` is not part of the match key.
- Services arriving first keep their draft when stock charges arrive; stock arriving first accepts subsequent services. Stock inference reads Item and enabled Product Bundle contents, including stock components of non-stock bundle parents.
- Stock Items enable `update_stock=1`. ERPNext performs all stock posting on submission. Non-stock services produce no stock entries. A warehouse on a service does not classify it as stock.
- POS/counter documents, returns, driver operations, and Delivery Note-backed invoices do not enter regular reuse. POS/driver standalone creators retain `force_new`. Manual regular invoices now reuse too: `allow_duplicate` remains accepted for older clients but does not create another draft; the response includes `created`.
- Source description markers and source-to-invoice links are preserved. Existing draft-line cancellation and ERPNext credit-note/cancellation mechanisms remain in charge of reversals.
- No historical invoices are consolidated. If several old drafts already exist, the newest eligible one is selected; other drafts are not rewritten, deleted or submitted by this change.

The implementation is in `pet_app/utils/invoice_reuse.py` and `pet_app/utils/invoice_stock.py`. The stock guard runs before validation and submission, including cashier submission of a previously non-stock draft with clinical source markers/back-links. A caller's old `requires_stock=False` no longer suppresses stock Items.

## Creation and dispensing paths

| Path | Current behavior |
| --- | --- |
| Per-order lab/procedure/service, including visit-linked vaccination | Shared helper infers stock from invoiced Items/bundles. |
| Standalone vaccination/deworming | Same shared helper; completion does not independently issue stock. |
| Visit medication on its configured billing trigger | Invoice line snapshots dose count, price, UOM and stock conversion. |
| Visit close | Same stock policy; the former whole-invoice stock suppression has been removed. |
| Boarding checkout and death settlement | Shared invoice helper; billable medication gets its configured dose conversion. |
| Guardian/manual sale creators | Shared helper infers stock regardless of warehouse presence or an old false stock hint. |
| Visit/boarding dispense | Records clinical quantities/status only, without a Material Issue. Existing historical issue fields are preserved. |

When a prescription carries a UOM/conversion snapshot, it is used. Otherwise configured dose options and existing medication defaults resolve conversion. A dose count is not silently treated as that many vials. Ambiguous dose options or a missing conversion between different UOMs are refused. Invoice conversions remain stored even if a dose master changes before submission. No schema fields or doctypes were added.

Clinical return recording does not change an invoice quantity or replace a credit note. The existing Material Receipt return path remains available for historical rows that actually recorded a Material Issue; current stock invoices use ERPNext's standard returns.

## Exceptions requiring an explicit operational decision

1. **Historical stock issues.** An invoiced stock item traced to recorded `stock_issued_qty` or a submitted Material Issue is refused for review before append/submission. The code neither deducts it twice nor switches off stock for the whole invoice. It also checks legacy source-to-invoice back-links and bundle stock contents. Conservative refusal is intentional when old source evidence is incomplete. Existing submitted invoices are not modified.
2. **Included boarding medication.** `Included` rows remain off invoices. Their dispensing can be recorded, but no stock deduction occurs under invoice-only ownership. Boarding dispensing returns `data.stock_notice` and an orange message describing the missing invoice stock line. Visit medication absorbed into Treatment boarding has the same conflict. No quantity, charge, or zero-price stock line is invented.
3. **Separate care-service consumables.** A configured medication consumed behind a non-stock service, without a corresponding invoiced stock Item/bundle component, is reported at completion. It no longer creates a Material Issue. A direct stock service whose configured consumption conflicts with its billed quantity is refused. Catalogue/quantity decisions require the owner's input; this change does not create those mappings.
4. **Concurrent stale snapshots.** Customer and invoice locks prevent duplicate drafts. MariaDB on the isolated site uses REPEATABLE READ with `innodb_snapshot_isolation=1`; a transaction that already read before another charge committed can receive `QueryDeadlockError` (1020). The whole originating transaction must be retried after rollback. The helper deliberately does not roll back caller writes or silently commit/retry part of a clinical operation. No automatic HTTP retry has been added, and no database isolation setting was changed.

## Verification

Run only on `driver-orders.test.localhost`, whose database is `test_driver_orders_20260909`. The new suites assert an isolated database prefix. Tests use actual invoice/stock documents and roll back each case. The concurrency probe commits unique test fixtures and uses two independent Frappe connections, establishing stale snapshots before simultaneous service/stock charges.

```python
# From bench/sites, using bench/env/bin/python; never initialize frappe.localhost.
import frappe, unittest
frappe.init(site="driver-orders.test.localhost")
frappe.connect()
assert str(frappe.conf.db_name).startswith("test_driver_orders_")
frappe.flags.in_test = True
try:
    suite = unittest.defaultTestLoader.loadTestsFromName("pet_app.tests.test_invoice_reuse")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    assert result.wasSuccessful()
finally:
    frappe.db.rollback()
    frappe.destroy()
```

The separate committed concurrency probe is `pet_app.tests.invoice_reuse_concurrency.run()`, called inside the same guarded initialization. Three rounds each produced one draft with both lines and `update_stock=1`, after one whole-transaction retry per round.

Existing regression suites: `test_billing_trigger`, `test_billing_cancellation`, `test_branch_warehouse`, `test_pos_sale_creation`, `test_driver_orders` (113 tests passed).

## Changed files

| Area | Files |
| --- | --- |
| Shared reuse and stock guard | `pet_app/utils/invoice_reuse.py`, `pet_app/utils/invoice_stock.py` (new), `pet_app/hooks.py` |
| Billing and dose conversion | `pet_app/utils/order_billing.py`, `pet_app/utils/medication_stock.py`, `pet_app/utils/care_service_billing.py`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py` |
| API paths | `pet_app/api/pharmacy.py`, `pet_app/api/healthcare/boarding.py`, `pet_app/api/sales.py` |
| Tests | `pet_app/tests/test_invoice_reuse.py` (new), `pet_app/tests/invoice_reuse_concurrency.py` (new), `pet_app/tests/test_billing_trigger.py`, `pet_app/tests/test_pos_sale_creation.py` |
| Contracts and guidance | `docs/INVOICE_REUSE_CONTRACT.md` (this file), `docs/MEDICATION_BILLING_CONTRACT.md`, `docs/VISIT_BOARDING_CONTRACT.md`, `AGENTS.md` |

## Deployment requirements

No migration, core patch, new DocType, or schema change. Deploy the changed application files together and reload web/background processes and hook caches through the normal deployment process. This installation uses preloaded gunicorn; HUP alone does not load new Python code. No production migration, restart, invoice rewrite, or deployment was performed during this work.

Before relying on the new policy operationally, resolve the included-medication/consumable catalogue exceptions and review any historical stock-issue refusals. Surface transaction conflicts to the cashier and retry the full failed operation, not just its invoice write. Existing split drafts remain as they are.
