"""Regular invoices own stock movement; historical Material Issues require review.

Uses Item and Product Bundle metadata, never warehouse presence, to decide whether
ERPNext must update stock. No ledger entries are created here.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt

from pet_app.utils.invoice_source import parse_markers


def stock_items_for(item_code: str) -> set[str]:
    if not item_code:
        return set()
    if frappe.db.get_value("Item", item_code, "is_stock_item"):
        return {item_code}
    # Match ERPNext's enabled Product Bundle definition, including its stock children.
    return set(frappe.db.sql("""
        SELECT child.item_code FROM `tabProduct Bundle` bundle
        JOIN `tabProduct Bundle Item` child ON child.parent = bundle.name
        JOIN `tabItem` item ON item.name = child.item_code
        WHERE bundle.new_item_code = %s AND bundle.disabled = 0
          AND item.is_stock_item = 1 AND child.qty != 0
    """, item_code, pluck=True))


def items_require_stock(items) -> bool:
    return any(flt(row.get("qty")) and stock_items_for(row.get("item_code")) for row in items)


def assert_not_preissued(row, label):
    """Never turn an old dispense issue into a second deduction on an invoice."""
    entry = row.get("stock_entry")
    issued_entry = entry and frappe.db.exists(
        "Stock Entry", {"name": entry, "docstatus": 1, "purpose": "Material Issue"}
    )
    if flt(row.get("stock_issued_qty")) > 0 or issued_entry:
        frappe.throw(_(
            "{0} has a historical stock issue ({1}). Invoice stock deduction would issue "
            "it again. The cashier must review the existing issue and charge before billing."
        ).format(label, entry or _("recorded issued quantity")))


def _sources(doc):
    sources = set()
    for row in doc.get("items") or []:
        sources.update(parse_markers(row.get("description")))
    if doc.name and not doc.is_new():
        # Older invoices may have only back-links, without description markers.
        for doctype in ("Vet Visit", "Pet Boarding", "PetCareService", "Preventive Care Record"):
            sources.update((doctype, name) for name in frappe.get_all(
                doctype, filters={"sales_invoice": doc.name}, pluck="name"
            ))
        sources.update((row.parenttype, row.parent) for row in frappe.get_all(
            "Pet Billable Item", filters={"sales_invoice": doc.name}, fields=["parenttype", "parent"]
        ))
    return sources


def assert_no_historical_issue(doc):
    stock_items = set()
    for row in doc.get("items") or []:
        if flt(row.get("qty")):
            stock_items.update(stock_items_for(row.get("item_code")))
    for row in doc.get("packed_items") or []:
        if flt(row.get("qty")):
            stock_items.update(stock_items_for(row.get("item_code")))
    if not stock_items:
        return
    for doctype, name in _sources(doc):
        if doctype == "Vet Visit":
            rows = frappe.get_all("Vet Visit Medication Item",
                filters={"parent": name, "medication_item": ["in", sorted(stock_items)]},
                fields=["name", "stock_issued_qty", "stock_entry"])
            # Care services on the visit can have their own historical issue.
            services = frappe.get_all("PetCareService", filters={"visit": name}, pluck="name")
        elif doctype == "Pet Boarding":
            rows = frappe.get_all("Pet Billable Item",
                filters={"parent": name, "parenttype": doctype, "item_code": ["in", sorted(stock_items)]},
                fields=["name", "stock_issued_qty", "stock_entry"])
            services = frappe.get_all("PetCareService",
                filters={"source_doctype": doctype, "source_name": name}, pluck="name")
        elif doctype == "Preventive Care Record":
            # Same interlock PetCareService gets below, for the same reason: the doctype
            # carries stock_issued_qty/stock_entry, so if anything ever fills them - a future
            # leg, or an operator linking a Material Issue by hand - the invoice must refuse
            # to deduct the same vial a second time rather than silently doubling it.
            # Nothing fills them today, so this is an interlock rather than a live check, and
            # it is registered now precisely so it cannot be forgotten when something does.
            rows = frappe.get_all("Preventive Care Record",
                filters={"name": name, "item_code": ["in", sorted(stock_items)]},
                fields=["name", "stock_issued_qty", "stock_entry"])
            services = []
        else:
            rows = []
            services = [name] if doctype == "PetCareService" else []
        for row in rows:
            assert_not_preissued(row, f"{doctype} {name}, row {row.name}")
        # PetCareService CAN NO LONGER RECORD A PRE-ISSUE, so there is nothing here to read.
        #
        # This check existed because the preventive completion path used to post its own
        # Material Issue and stamp `stock_issued_qty` / `stock_entry` on the service. That path
        # was removed with the rest of preventive care, and `preventive_care_purge` dropped
        # both columns - so querying them raised
        # `(1054, "Unknown column 'stock_issued_qty' in 'SELECT'")` on EVERY Sales Invoice save
        # that had a PetCareService in its sources, which includes the invoice
        # `administer_preventive` raises. Administration was impossible.
        #
        # Guarded rather than deleted outright, for the reason the rest of this app guards
        # schema: the code may run on a site that has not applied the purge yet, where the
        # columns still exist and a historical stamp still means something. Once they are gone
        # the loop is correctly a no-op - the equivalent interlock for the replacement doctype
        # is the `Preventive Care Record` branch above.
        #
        # THE GUARD IS `meta.has_field`, NOT `frappe.db.has_column`, and the two are not
        # interchangeable on a server that hosts more than one site.
        # `has_column` -> `get_db_table_columns` queries
        #     information_schema.columns WHERE table_name = 'tabPetCareService'
        # with NO `table_schema` filter, so it returns the UNION of that table's columns across
        # every database on the host. This bench also holds `test_driver_orders_20260909`, whose
        # copy of the table still has all three columns - so `has_column` answered True for all
        # three here while the very same query raised
        # `(1054, "Unknown column 'stock_issued_qty' in 'SELECT'")`. Clearing a cache does not
        # help; the query is simply not scoped to this site. The doctype meta is.
        service_meta = frappe.get_meta("PetCareService")
        if services and all(
            service_meta.has_field(column) for column in ("stock_issued_qty", "stock_entry")
        ):
            for service_name in services:
                service = frappe.db.get_value("PetCareService", service_name,
                    ["item_code", "stock_issued_qty", "stock_entry"], as_dict=True)
                if not service:
                    continue
                issued_items = set(frappe.get_all("Stock Entry Detail",
                    filters={"parent": service.stock_entry}, pluck="item_code")) if service.stock_entry else set()
                if (service.item_code in stock_items or issued_items.intersection(stock_items)
                        or (not issued_items and flt(service.stock_issued_qty) > 0)):
                    assert_not_preissued(service, f"PetCareService {service_name}")


def prepare_invoice_stock(doc, method=None):
    """Before validation/submission, including cashiers submitting an older draft.

    Returns, driver operations and Delivery Note billing retain their own policy.
    The helper explicitly prepares new regular invoices before insert as well.
    """
    if doc.docstatus == 2 or doc.get("is_return") or doc.get("custom_driver_flow"):
        return
    if any(row.get("delivery_note") for row in doc.get("items") or []):
        return
    if not doc.flags.from_custom_flow and not _sources(doc):
        return
    assert_no_historical_issue(doc)
    # Never switch an existing stock invoice off just because its last stock row
    # was removed. Its standard cancellation/returns behavior remains ERPNext's.
    doc.update_stock = int(bool(cint(doc.update_stock) or items_require_stock(doc.get("items") or [])))
