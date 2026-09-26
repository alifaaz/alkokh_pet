"""Administrator-only, previewed customer accounting teardown.

Cancels vouchers through ERPNext before deleting their complete ledger groups.
Clinical records and stock/dispense evidence are retained; historical charges are
cancelled. No party-only GL deletion, forced link deletion, or implicit commit.
"""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import cint, cstr, escape_html, now_datetime
from passlib.hash import pbkdf2_sha256

from pet_app.api.response import ok
from pet_app.utils.invoice_source import parse_markers

CLINICAL = ("Vet Visit", "Pet Boarding", "Lab", "Imaging", "Pet Procedure",
            "PetCareService", "Preventive Care Record")
LEDGERS = ("GL Entry", "Payment Ledger Entry", "Advance Payment Ledger Entry", "Stock Ledger Entry")
VOUCHERS = ("Payment Entry", "Journal Entry", "Sales Invoice")


def require_admin():
    if frappe.session.user != "Administrator" and "Administrator" not in frappe.get_roles():
        frappe.throw(_("Only the Administrator role can reset customer accounts."), frappe.PermissionError)


def _authenticate(password):
    require_admin()
    encoded = frappe.conf.get("customer_account_reset_password_hash")
    if not encoded or not pbkdf2_sha256.verify(cstr(password), encoded):
        # This is a second authorization gate, not a failed site login.
        # AuthenticationError clears the session cookies in frappe.app and sends
        # HTTP 401, which Desk interprets as session expiration.
        frappe.throw(_("Invalid account reset password."), frappe.ValidationError)


def _rows(dt, filters):
    if not frappe.db.table_exists(dt):
        return []
    return frappe.qb.get_query(dt, filters=filters, fields=["*"], order_by="name asc",
                               ignore_permissions=True,
                               for_update=bool(frappe.flags.account_reset_lock)).run(as_dict=True)


def _document(dt, row):
    doc = frappe._dict(row.copy())
    doc.doctype = dt
    for field in frappe.get_meta(dt).get_table_fields():
        doc[field.fieldname] = _rows(field.options, {"parenttype": dt, "parent": row.name, "parentfield": field.fieldname})
    return doc


def _resolve(guardian=None, customer=None):
    if guardian:
        linked = frappe.db.get_value("Guardian", guardian, "customer_id")
        if not linked:
            frappe.throw(_("This guardian has no linked Customer."))
        if customer and customer != linked:
            frappe.throw(_("Guardian and Customer do not match."))
        customer = linked
    if not customer or not frappe.db.exists("Customer", customer):
        frappe.throw(_("Select an existing Guardian or Customer."))
    return customer


def _add(target, dt, filters):
    for row in _rows(dt, filters):
        target[dt][row.name] = row


def _clinical(customer, guardians, invoices):
    found = {dt: {} for dt in CLINICAL}
    for dt in CLINICAL:
        if not frappe.db.table_exists(dt):
            continue
        m = frappe.get_meta(dt)
        for field, values in (("customer", [customer]), ("guardian", guardians),
                              ("guardian_id", guardians), ("sales_invoice", invoices)):
            if values and m.has_field(field):
                _add(found, dt, {field: ["in", values]})
    # Follow clinical parent links, never a pet's ownership alone: pets can have
    # several guardians with independent accounts.
    for dt in CLINICAL:
        if not frappe.db.table_exists(dt):
            continue
        m = frappe.get_meta(dt)
        visits = list(found["Vet Visit"])
        if visits and m.has_field("visit"):
            _add(found, dt, {"visit": ["in", visits]})
        if m.has_field("source_doctype") and m.has_field("source_name"):
            for source in ("Vet Visit", "Pet Boarding"):
                names = list(found[source])
                if names:
                    _add(found, dt, {"source_doctype": source, "source_name": ["in", names]})
    return found


def _plan(customer):
    guardians = [frappe._dict({k: r.get(k) for k in ("name", "customer_id", "full_name", "modified")})
                 for r in _rows("Guardian", {"customer_id": customer})]
    vouchers = {
        "Sales Invoice": _rows("Sales Invoice", {"customer": customer}),
        "Payment Entry": _rows("Payment Entry", {"party_type": "Customer", "party": customer}),
        "Journal Entry": [],
    }
    je_names = {r.parent for r in _rows("Journal Entry Account", {"party_type": "Customer", "party": customer})}
    if je_names:
        vouchers["Journal Entry"] = _rows("Journal Entry", {"name": ["in", sorted(je_names)]})
    documents = {dt: [_document(dt, r) for r in rows] for dt, rows in vouchers.items()}
    keys = {(dt, r.name) for dt, rows in vouchers.items() for r in rows}
    invoices = [r.name for r in vouchers["Sales Invoice"]]
    clinical = _clinical(customer, [r.name for r in guardians], invoices)
    # Provenance on invoice lines and billable rows covers records whose parent
    # invoice backlink was never set (per-order and merged invoice workflows).
    for invoice in documents["Sales Invoice"]:
        for line in invoice.get("items", []):
            for source_dt, source_name in parse_markers(line.get("description")):
                if source_dt in clinical:
                    _add(clinical, source_dt, {"name": source_name})
    for row in _rows("Pet Billable Item", {"sales_invoice": ["in", invoices]}) if invoices else []:
        if row.parenttype in clinical:
            _add(clinical, row.parenttype, {"name": row.parent})
    blockers = []
    for dt, docs in documents.items():
        for doc in docs:
            if dt == "Journal Entry":
                others = [r for r in doc.accounts if r.party and (r.party_type != "Customer" or r.party != customer)]
                if others:
                    blockers.append(f"Journal Entry {doc.name} includes another party.")
            if dt == "Payment Entry":
                for ref in doc.references:
                    if ref.reference_name and (ref.reference_doctype, ref.reference_name) not in keys:
                        blockers.append(f"Payment Entry {doc.name} references {ref.reference_doctype} {ref.reference_name} outside this reset.")
            if dt == "Sales Invoice":
                if doc.get("is_consolidated") or doc.get("custom_driver_order"):
                    blockers.append(f"Sales Invoice {doc.name} belongs to a consolidated POS or driver workflow.")
                for row in doc.get("items", []):
                    if row.get("sales_order") or row.get("delivery_note"):
                        blockers.append(f"Sales Invoice {doc.name} is linked to a Sales Order or Delivery Note.")
    ledger = {}
    for dt in LEDGERS:
        grouped = {}
        for vt in VOUCHERS:
            names = [r.name for r in vouchers[vt]]
            if names:
                for r in _rows(dt, {"voucher_type": vt, "voucher_no": ["in", names]}):
                    grouped[r.name] = r
        # A foreign voucher pointing at a target is not permission to delete it.
        meta = frappe.get_meta(dt) if frappe.db.table_exists(dt) else None
        against_field = "against_voucher" if dt == "GL Entry" else "against_voucher_no"
        if meta and meta.has_field(against_field):
            for vt in VOUCHERS:
                names = [r.name for r in vouchers[vt]]
                if not names:
                    continue
                for r in _rows(dt, {"against_voucher_type": vt, against_field: ["in", names]}):
                    if (r.voucher_type, r.voucher_no) not in keys:
                        blockers.append(f"{dt} {r.name} references this account from outside the reset ({r.voucher_type} {r.voucher_no}).")
        if dt in ("GL Entry", "Payment Ledger Entry"):
            for r in _rows(dt, {"party_type": "Customer", "party": customer}):
                if (r.voucher_type, r.voucher_no) not in keys:
                    blockers.append(f"{dt} {r.name} belongs to unsupported or missing {r.voucher_type} {r.voucher_no}.")
            for r in grouped.values():
                if r.get("party") and (r.get("party_type") != "Customer" or r.party != customer):
                    blockers.append(f"{dt} {r.name} includes another party.")
        ledger[dt] = sorted(grouped.values(), key=lambda r: r.name)
    for dt in ("POS Invoice", "Sales Order", "Delivery Note", "Dunning"):
        rows = _rows(dt, {"customer": customer}) if frappe.db.table_exists(dt) and frappe.get_meta(dt).has_field("customer") else []
        if rows:
            blockers.append(f"Customer has {len(rows)} {dt} records; handle that workflow before resetting.")
    medical_docs = {}
    for dt, rows in clinical.items():
        medical_docs[dt] = []
        for name in sorted(rows):
            doc = _document(dt, rows[name])
            if doc.get("customer") and doc.customer != customer:
                blockers.append(f"{dt} {name} belongs to another Customer.")
            for gf in ("guardian", "guardian_id"):
                if doc.get(gf) and frappe.db.get_value("Guardian", doc.get(gf), "customer_id") not in (None, "", customer):
                    blockers.append(f"{dt} {name} belongs to another guardian's account.")
            if doc.get("sales_invoice") and doc.sales_invoice not in invoices:
                blockers.append(f"{dt} {name} is billed to an invoice outside this reset.")
            for row in doc.get("billable_items") or []:
                if row.get("sales_invoice") and row.sales_invoice not in invoices:
                    blockers.append(f"Charge {row.name} is billed outside this reset.")
            medical_docs[dt].append(doc)
    # Include billable backlinks even when the clinical parent's invoice link is blank.
    for row in _rows("Pet Billable Item", {"sales_invoice": ["in", invoices]}) if invoices else []:
        if row.parent not in clinical.get(row.parenttype, {}):
            blockers.append(f"Charge {row.name} belongs to an unselected {row.parenttype} {row.parent}.")
    balance = sum(float(r.get("amount") or 0) for r in ledger["Payment Ledger Entry"] if not r.get("delinked"))
    reminders = []
    attachments = []
    notification_logs = []
    for dt, rows in vouchers.items():
        names = [r.name for r in rows]
        if names:
            reminders.extend(_rows("Pet Reminder", {"reference_doctype": dt, "reference_name": ["in", names]}))
            notification_logs.extend(_rows("Pet Notification Log", {"reference_doctype": dt, "reference_name": ["in", names]}))
            attachments.extend(_rows("File", {"attached_to_doctype": dt, "attached_to_name": ["in", names]}))
    # Move attachments to Customer before deleting vouchers; File.on_trash removes
    # disk bytes immediately, which a database rollback cannot restore.
    snapshot = {"notification_logs": notification_logs, "reminders": reminders, "attachments": attachments, "customer": customer, "guardians": guardians, "documents": documents,
                "medical": medical_docs, "ledgers": ledger}
    fingerprint = hashlib.sha256(frappe.as_json(snapshot).encode()).hexdigest()
    return {"customer": customer, "customer_name": frappe.db.get_value("Customer", customer, "customer_name"),
            "guardians": [r.name for r in guardians], "fingerprint": fingerprint,
            "counts": {dt: len(rows) for dt, rows in documents.items()},
            "medical_counts": {dt: len(rows) for dt, rows in medical_docs.items() if rows},
            "reminder_count": len(reminders), "attachment_count": len(attachments),
            "notification_log_count": len(notification_logs),
            "ledger_counts": {dt: len(rows) for dt, rows in ledger.items()},
            "net_balance": balance, "blockers": sorted(set(blockers)), "snapshot": snapshot}


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=10, seconds=300)
def preview(password, guardian=None, customer=None):
    _authenticate(password)
    plan = _plan(_resolve(guardian, customer))
    plan.pop("snapshot")
    return ok(plan)


@contextmanager
def _no_internal_commit():
    commit = frappe.db.commit
    def refuse():
        raise RuntimeError("Account reset refused an unexpected intermediate database commit.")
    frappe.db.commit = refuse
    try:
        yield
    finally:
        frappe.db.commit = commit


def _backup(plan):
    root = Path(frappe.get_site_path("private", "account-reset-backups"))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    reset_id = f"{now_datetime():%Y%m%d-%H%M%S}-{frappe.generate_hash(length=12)}"
    path = root / f"{reset_id}.json"
    payload = {"reset_id": reset_id, "actor": frappe.session.user, "created_at": str(now_datetime()),
               "state": "backup_created", "plan": plan}
    with path.open("x", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        handle.write(frappe.as_json(payload))
        handle.flush()
        os.fsync(handle.fileno())
    return reset_id


def _clear_medical(plan):
    for dt, docs in plan["snapshot"]["medical"].items():
        m = frappe.get_meta(dt)
        for doc in docs:
            updates = {field: value for field, value in {
                "sales_invoice": None, "billed": 0, "billing_status": "Unbilled",
                "total_billable_amount": 0, "paid_amount": 0, "balance_amount": 0,
                "deposit_payment_entry": None, "deposit": 0, "total_cost": 0, "balance": 0,
                "accommodation_subtotal": 0, "accommodation_discount_amount": 0,
                "accommodation_total": 0, "discount_request": None,
                "discount_recorded_at": None, "discount_recorded_by": None, "rate": 0, "price": 0,
            }.items() if m.has_field(field)}
            frappe.db.set_value(dt, doc.name, updates)
            # Keep clinical dose, dispense and stock evidence on these rows.
            # Cancel only the charge, never a medication's clinical status.
            for row in doc.get("billable_items") or []:
                frappe.db.set_value("Pet Billable Item", row.name,
                                    {"sales_invoice": None, "status": "Cancelled", "rate": 0, "amount": 0})
            frappe.clear_document_cache(dt, doc.name)


def _detach_notification_logs(plan):
    # Logs are historical evidence, not accounting transactions. Preserve their
    # message, recipient, delivery status and reminder link. The snapshot and an
    # audit comment retain the exact source after removing its live Dynamic Link.
    for row in plan["snapshot"].get("notification_logs", []):
        frappe.get_doc("Pet Notification Log", row.name).add_comment(
            "Info", text=_("Customer account reset for {0}: original reference {1} {2} archived before voucher deletion.").format(
                escape_html(plan["customer"]), escape_html(row.reference_doctype), escape_html(row.reference_name)))
        frappe.db.set_value("Pet Notification Log", row.name,
                            {"reference_doctype": None, "reference_name": None})
        frappe.clear_document_cache("Pet Notification Log", row.name)


def _cancel_and_delete(plan):
    documents = plan["snapshot"]["documents"]
    # Disconnect clinical accounting fields before invoice cancellation hooks try
    # re-saving an old clinical record and accidentally generating new charges.
    _clear_medical(plan)
    _detach_notification_logs(plan)
    for row in plan["snapshot"]["attachments"]:
        frappe.db.set_value("File", row.name, {"attached_to_doctype": "Customer",
                            "attached_to_name": plan["customer"], "attached_to_field": None})
    for row in plan["snapshot"]["reminders"]:
        frappe.db.set_value("Pet Reminder", row.name, {"reference_name": None, "reference_doctype": None,
                            "status": "Cancelled"})
    for dt in VOUCHERS:
        # Returns and amendments depend on their originals.
        docs = sorted(documents[dt], key=lambda d: (bool(d.get("return_against")), bool(d.get("amended_from")), cstr(d.creation)), reverse=True)
        for original in docs:
            doc = frappe.get_doc(dt, original.name)
            doc.flags.ignore_permissions = True
            if doc.docstatus == 1:
                doc.cancel()
        for original in docs:
            if frappe.db.get_value(dt, original.name, "docstatus") == 1:
                frappe.throw(_("A submitted voucher cannot be deleted."))
            # Native cancellation has reversed stock. Only remove complete voucher
            # groups, including both sides of GL; never filter deletion by party.
            if frappe.db.exists("Stock Ledger Entry", {"voucher_type": dt, "voucher_no": original.name, "is_cancelled": 0}):
                frappe.throw(_("Stock cancellation is incomplete for {0}.").format(original.name))
            for ledger_dt in LEDGERS:
                if frappe.db.table_exists(ledger_dt):
                    frappe.db.delete(ledger_dt, {"voucher_type": dt, "voucher_no": original.name})
            frappe.delete_doc(dt, original.name, ignore_permissions=True, ignore_missing=False)


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=5, seconds=300)
def execute(password, fingerprint, confirm_customer, guardian=None, customer=None):
    _authenticate(password)
    customer = _resolve(guardian, customer)
    if confirm_customer != customer:
        frappe.throw(_("Type the exact Customer ID to confirm the reset."))
    previous_lock = frappe.flags.account_reset_lock
    frappe.flags.account_reset_lock = True
    frappe.db.savepoint("account_reset")
    try:
        with _no_internal_commit():
            frappe.db.sql("select name from `tabCustomer` where name=%s for update", customer)
            plan = _plan(customer)
            if plan["fingerprint"] != fingerprint:
                frappe.throw(_("Account records changed. Preview the reset again."))
            if plan["blockers"]:
                frappe.throw("\n".join(plan["blockers"]))
            if not any(plan["counts"].values()) and not any(plan["medical_counts"].values()):
                frappe.throw(_("This customer has no records to reset."))
            reset_id = _backup(plan)
            _cancel_and_delete(plan)
            for dt, docs in plan["snapshot"]["documents"].items():
                for doc in docs:
                    if frappe.db.exists(dt, doc.name):
                        frappe.throw(_("Voucher remains after reset: {0}").format(doc.name))
                    for ledger in LEDGERS:
                        if _rows(ledger, {"voucher_type": dt, "voucher_no": doc.name}):
                            frappe.throw(_("Ledger entries remain for {0}.").format(doc.name))
            for dt in ("Sales Invoice", "Payment Entry", "GL Entry", "Payment Ledger Entry"):
                filters = {"customer": customer} if dt == "Sales Invoice" else {"party_type": "Customer", "party": customer}
                if _rows(dt, filters):
                    frappe.throw(_("Reset verification failed: records remain in {0}.").format(dt))
            frappe.get_doc("Customer", customer).add_comment(
                "Info", text=_("Account reset {0} by {1}. Invoices/payments and accounting ledgers removed; clinical history retained. Counts: {2}").format(
                    reset_id, frappe.session.user, json.dumps(plan["counts"])))
            return ok({"reset_id": reset_id, "customer": customer, "counts": plan["counts"],
                       "medical_counts": plan["medical_counts"], "net_balance": 0})
    except Exception:
        frappe.db.rollback(save_point="account_reset")
        raise
    finally:
        frappe.flags.account_reset_lock = previous_lock
