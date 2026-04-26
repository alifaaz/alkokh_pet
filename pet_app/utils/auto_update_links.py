"""
auto_update_links.py
--------------------
Generic sync engine for Frappe/ERPNext v16.

Flow:
    PUT /api/resource/Supplier/X
        ↓
    before_save  → read request → store payload in frappe.local._pending_sync
        ↓
    before_save  → sync Contact/Address immediately
        ↓
    validate     → Frappe fetches from linked docs
        ↓
    on_update    → fix renamed links first
        ↓
    on_update    → sync again using stored payload and refresh primary_address
        ↓
    after_rename → rename linked Contact/Address
"""

import frappe
from frappe.utils import cstr


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

SYNC_CONFIG = {
    "Supplier": {
        "contact_field": "supplier_primary_contact",
        "address_field": "supplier_primary_address",
        "fetched_fields": {
            "email_id": "email_id",
            "mobile_no": "mobile_no",
        },
        "contact_name": lambda n: f"{n}-{n}",
        "address_name": lambda n: f"{n}-Billing",
        "contact_rows": [
            {
                "source_field": "email_id",
                "table_field": "email_ids",
                "value_field": "email_id",
                "primary_flag": "is_primary",
            },
            {
                "source_field": "mobile_no",
                "table_field": "phone_nos",
                "value_field": "phone",
                "primary_flag": "is_primary_mobile_no",
            },
        ],
        "address_fields": {
            "address_line1": "address_line1",
            "city": "city",
            "country": "country",
        },
    },
    "Customer": {
        "contact_field": "customer_primary_contact",
        "address_field": "customer_primary_address",
        "fetched_fields": {
            "email_id": "email_id",
            "mobile_no": "mobile_no",
        },
        "contact_name": lambda n: f"{n}-{n}",
        "address_name": lambda n: f"{n}-Billing",
        "contact_rows": [
            {
                "source_field": "email_id",
                "table_field": "email_ids",
                "value_field": "email_id",
                "primary_flag": "is_primary",
            },
            {
                "source_field": "mobile_no",
                "table_field": "phone_nos",
                "value_field": "phone",
                "primary_flag": "is_primary_mobile_no",
            },
        ],
        "address_fields": {
            "address_line1": "address_line1",
            "city": "city",
            "country": "country",
        },
    },
    "Driver": {
        "contact_field": None,
        "address_field": "address",

        "fetched_fields": {
            "cell_number": "cell_number",
            },

    "contact_rows": [],

    "address_fields": {
        "address_line1": "address_line1",
        "city": "city",
        "country": "country",
    },
}
}


# ---------------------------------------------------------------------------
# REQUEST READER
# ---------------------------------------------------------------------------

def _unwrap_payload(raw):
    if not raw:
        return {}

    if isinstance(raw, str):
        try:
            raw = frappe.parse_json(raw)
        except Exception:
            return {}

    if isinstance(raw, list):
        for item in raw:
            payload = _unwrap_payload(item)
            if payload:
                return payload
        return {}

    if not isinstance(raw, dict):
        return {}

    for key in ("data", "doc"):
        if key in raw:
            payload = _unwrap_payload(raw[key])
            if payload:
                return payload

    return raw


def _get_request_payload() -> dict:
    """
    Read the HTTP payload and return a clean dict.
    Supports JSON body, form_dict, and envelopes like {data:{}} / {doc:{}}.
    """
    candidates = []

    try:
        if getattr(frappe.local, "request", None):
            candidates.append(frappe.local.request.get_json(silent=True, force=True))
    except Exception:
        pass

    try:
        form_dict = dict(frappe.local.form_dict or {})
        candidates.append(form_dict)
        for key in ("data", "doc"):
            if key in form_dict:
                candidates.append(form_dict.get(key))
    except Exception:
        pass

    for raw in candidates:
        payload = _unwrap_payload(raw)
        if payload:
            return payload

    return {}


# ---------------------------------------------------------------------------
# SYNCER — writes Contact/Address
# ---------------------------------------------------------------------------

class LinkedDocSyncer:
    """
    Runs in before_save and on_update.
    Uses request payload only for fields actually sent by the client.
    """

    def __init__(self, doc, payload=None):
        self.doc = doc
        self.cfg = SYNC_CONFIG.get(doc.doctype)
        self.payload = payload if payload is not None else _get_request_payload()

    def run(self):
        if not self.cfg or not self.payload:
            return

        self._sync_contact()
        self._sync_address()

    def _sync_contact(self):
        contact_name = frappe.db.get_value(
            self.doc.doctype,
            self.doc.name,
            self.cfg["contact_field"],
        )
        if not contact_name or not frappe.db.exists("Contact", contact_name):
            return

        contact = frappe.get_doc("Contact", contact_name)
        changed = False

        for row in self.cfg.get("contact_rows", []):
            if row["source_field"] not in self.payload:
                continue

            if self._update_primary_row(
                linked_doc=contact,
                table_field=row["table_field"],
                value_field=row["value_field"],
                primary_flag=row["primary_flag"],
                new_value=cstr(self.payload.get(row["source_field"])),
            ):
                changed = True

        if changed:
            contact.save(ignore_permissions=True)
            frappe.logger().info(
                f"[Sync] Contact {contact_name} <- {self.doc.doctype} {self.doc.name}"
            )

    def _sync_address(self):
        address_name = frappe.db.get_value(
            self.doc.doctype,
            self.doc.name,
            self.cfg["address_field"],
        )
        if not address_name or not frappe.db.exists("Address", address_name):
            return

        address = frappe.get_doc("Address", address_name)
        changed = False

        for src, tgt in self.cfg.get("address_fields", {}).items():
            if src not in self.payload:
                continue

            new_val = cstr(self.payload.get(src))
            current_val = cstr(getattr(address, tgt, ""))

            if new_val != current_val:
                setattr(address, tgt, new_val)
                changed = True

        if changed:
            address.save(ignore_permissions=True)
            frappe.logger().info(
                f"[Sync] Address {address_name} <- {self.doc.doctype} {self.doc.name}"
            )

    def _update_primary_row(
        self,
        linked_doc,
        table_field,
        value_field,
        primary_flag,
        new_value,
    ) -> bool:
        rows = getattr(linked_doc, table_field, [])
        primary = next(
            (row for row in rows if cstr(getattr(row, primary_flag, "")) == "1"),
            None,
        )

        if primary:
            if cstr(getattr(primary, value_field, "")) == new_value:
                return False
            setattr(primary, value_field, new_value)
            return True

        linked_doc.append(
            table_field,
            {
                value_field: new_value,
                primary_flag: 1,
            },
        )
        return True


# ---------------------------------------------------------------------------
# RENAMER — renames Contact/Address and fixes links
# ---------------------------------------------------------------------------

class LinkedDocRenamer:
    """
    after_rename  -> rename linked Contact and Address
    on_update     -> repair link fields on the main doc
    """

    def __init__(self, doc, old=None):
        self.doc = doc
        self.cfg = SYNC_CONFIG.get(doc.doctype)
        self.old = old
        self.new = doc.name

    def rename_linked(self):
        if not self.cfg or not self.old or self.old == self.new:
            return

        pairs = [
            ("Contact", self.cfg["contact_name"](self.old), self.cfg["contact_name"](self.new)),
            ("Address", self.cfg["address_name"](self.old), self.cfg["address_name"](self.new)),
        ]

        for doctype, old_name, new_name in pairs:
            if frappe.db.exists(doctype, old_name):
                frappe.rename_doc(doctype, old_name, new_name, force=True)
                frappe.logger().info(f"[Rename] {doctype} {old_name} -> {new_name}")

    def fix_links(self):
        """
        Runs in on_update before syncing.
        Repairs link fields after rename so sync always uses current names.
        """
        if not self.cfg:
            return

        expected = {
            self.cfg["contact_field"]: self.cfg["contact_name"](self.new),
            self.cfg["address_field"]: self.cfg["address_name"](self.new),
        }

        update = {}

        for field, expected_name in expected.items():
            doctype = "Contact" if "contact" in field else "Address"
            current = frappe.db.get_value(self.doc.doctype, self.new, field)

            if current != expected_name and frappe.db.exists(doctype, expected_name):
                update[field] = expected_name

        if update:
            frappe.db.set_value(
                self.doc.doctype,
                self.new,
                update,
                update_modified=False,
            )
            for field, value in update.items():
                setattr(self.doc, field, value)

            frappe.logger().info(f"[FixLinks] {self.doc.doctype} {self.new} -> {update}")


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def _refresh_primary_address(doc, payload):
    cfg = SYNC_CONFIG.get(doc.doctype)
    if not cfg or not payload:
        return

    address_fields = cfg.get("address_fields", {})
    if not any(field in payload for field in address_fields):
        return

    address_name = frappe.db.get_value(doc.doctype, doc.name, cfg["address_field"])
    if not address_name or not frappe.db.exists("Address", address_name):
        return

    address = frappe.get_doc("Address", address_name)
    primary_address = (
        f"{cstr(address.address_line1)}<br>\n"
        f"{cstr(address.city)}<br>\n"
        f"{cstr(address.country)}<br>\n"
        f"<br>\n"
    )

    frappe.db.set_value(
        doc.doctype,
        doc.name,
        "primary_address",
        primary_address,
        update_modified=False,
    )
    doc.primary_address = primary_address


def _refresh_fetched_fields(doc, payload):
    cfg = SYNC_CONFIG.get(doc.doctype)
    if not cfg or not payload:
        return

    updates = {}

    for payload_field, doc_field in cfg.get("fetched_fields", {}).items():
        if payload_field not in payload:
            continue

        value = cstr(payload.get(payload_field))
        updates[doc_field] = value
        setattr(doc, doc_field, value)

    if updates:
        frappe.db.set_value(
            doc.doctype,
            doc.name,
            updates,
            update_modified=False,
        )


# ---------------------------------------------------------------------------
# HOOK FUNCTIONS
# ---------------------------------------------------------------------------

def before_save(doc, method=None):
    try:
        payload = _get_request_payload()

        if payload:
            frappe.local._pending_sync = payload

        LinkedDocSyncer(doc, payload=payload).run()

    except Exception:
        frappe.log_error(
            title=f"[Sync] before_save - {doc.doctype} {doc.name}",
            message=frappe.get_traceback(),
        )


def on_update(doc, method=None):
    try:
        LinkedDocRenamer(doc).fix_links()
        payload = getattr(frappe.local, "_pending_sync", None) or _get_request_payload()

        if payload:
            LinkedDocSyncer(doc, payload=payload).run()
            _refresh_fetched_fields(doc, payload)
            _refresh_primary_address(doc, payload)
            if doc.doctype == "Driver":
                if "cell_number" in payload and doc.user:
                    frappe.db.set_value(
                        "User",
                        doc.user,
                        "mobile_no",
                        doc.cell_number,
                        update_modified=False
                    )
        if hasattr(frappe.local, "_pending_sync"):
            delattr(frappe.local, "_pending_sync")

    except Exception:
        frappe.log_error(
            title=f"[Sync] on_update - {doc.doctype} {doc.name}",
            message=frappe.get_traceback(),
        )


def after_rename(doc, method=None, old=None, new=None, merge=False):
    """
    Hook: after_rename

    Rename linked Contact / Address only.
    Sync is handled earlier in the same before_save flow, immediately
    after frappe.rename_doc(), while frappe.local._pending_sync still exists.
    """
    try:
        LinkedDocRenamer(doc, old=old).rename_linked()

    except Exception:
        frappe.log_error(
            title=f"[Sync] after_rename - {doc.doctype} {old} -> {new}",
            message=frappe.get_traceback(),
        )



# Backward compatibility
handle_linked_update = on_update
sync_linked_docs = on_update
