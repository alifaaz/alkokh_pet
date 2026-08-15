import frappe
from frappe import _
from frappe.utils import cstr, flt, getdate, nowdate


PET_CARE_SERVICE_SOURCE_PREFIX = "[alkokh-source:PetCareService:"
PET_CARE_SERVICE_SOURCE_SUFFIX = "]"

def _effective_posting_date(doc):
    """The posting_date this save will actually end up with.

    ERPNext refreshes posting_date to today on every save unless set_posting_time is
    truthy (TransactionBase.validate_posting_time). That happens INSIDE
    SalesInvoice.validate(), between validate_auto_set_posting_time() and
    validate_due_date() - so no hook can observe the new value and still act on it.
    Anticipating it here is the only way to have due_date consistent by the time
    validate_due_date() runs.
    """
    if doc.get("set_posting_time"):
        return getdate(doc.posting_date) if doc.posting_date else getdate(nowdate())
    return getdate(nowdate())


def _due_date_for(doc, posting_date):
    """due_date derived from the applicable payment terms, or the posting date.

    Deliberately not a bare clamp to posting_date. No customer has payment terms today,
    so the two are identical right now - but the first customer given credit would have
    their credit period silently erased on every append, and nothing would notice.
    party.get_due_date resolves the template from the invoice, then the customer, then
    the customer group.
    """
    from erpnext.accounts.party import get_due_date

    if not doc.get("customer"):
        return posting_date
    try:
        derived = get_due_date(
            posting_date,
            "Customer",
            doc.customer,
            doc.get("company"),
            template_name=doc.get("payment_terms_template"),
        )
    except Exception:
        return posting_date
    return getdate(derived) if derived else posting_date


def _refresh_payment_schedule(doc, posting_date, expected):
    """Move any Payment Schedule row that would sit before the posting date.

    Fixing only the parent field is not enough and this is the step that makes the whole
    thing work. AccountsController.set_due_date() runs inside validate() and does
    `self.due_date = max(row.due_date for row in payment_schedule)` - so a stale child
    row silently overwrites whatever the parent was set to here, and the throw follows.
    Every draft on this site carries exactly one such row, written on the day it opened.

    Rows already on or after the posting date are left alone: a later date is a credit
    period somebody chose, and pulling it back would erase it.
    """
    for row in doc.get("payment_schedule") or []:
        if not row.due_date or getdate(row.due_date) < posting_date:
            row.due_date = expected


def fix_due_date(doc, method=None):
    """before_validate on Sales Invoice: keep due_date consistent with the posting date.

    Invoice reuse appends charges to a draft opened on an earlier day. ERPNext moves
    posting_date to today on every save (TransactionBase.validate_posting_time) and left
    due_date behind, so it threw "Due Date cannot be before Posting Date" and the visit
    could not complete. 562 of 713 open drafts were in that state.

    Wired to before_validate, not validate: both the posting_date move and the throw
    happen inside SalesInvoice.validate(), where no doc_event hook can intervene.
    """
    if not doc.get("posting_date") and not doc.get("due_date"):
        return

    posting_date = _effective_posting_date(doc)
    expected = _due_date_for(doc, posting_date)

    if not doc.due_date:
        doc.due_date = expected
        _refresh_payment_schedule(doc, posting_date, expected)
        return

    current = getdate(doc.due_date)
    posting_is_moving = doc.posting_date and getdate(doc.posting_date) != posting_date

    # The broken case: due_date is about to be earlier than posting_date.
    if current < posting_date:
        doc.due_date = expected
        _refresh_payment_schedule(doc, posting_date, expected)
        return

    # Credit terms roll with the invoice date, so a moving posting_date moves the due
    # date with it. Without terms, expected == posting_date and a manually-set later
    # due_date is left alone rather than being pulled back.
    if posting_is_moving and doc.get("payment_terms_template") and expected != current:
        doc.due_date = expected

    # Always reconcile the child rows, even when the parent needed no change: a stale row
    # is enough on its own to resurrect the old date through set_due_date().
    _refresh_payment_schedule(doc, posting_date, expected)

def before_insert(doc, method=None):
    if frappe.session.user == "Administrator":
        return

    if getattr(doc, "from_custom_flow", False) or getattr(doc.flags, "from_custom_flow", False):
        return

    if _is_pet_care_service_draft_invoice(doc):
        doc.flags.from_custom_flow = True
        return

    frappe.throw(_("Direct Sales Invoice creation is not allowed."), frappe.PermissionError)


def _is_pet_care_service_draft_invoice(doc) -> bool:
    if doc.docstatus != 0 or not doc.get("customer"):
        return False
    if not doc.get("items"):
        return False

    for row in doc.get("items") or []:
        service_name = _source_service_name(row.get("description"))
        if not service_name:
            return False
        if not _row_matches_pet_care_service(doc, row, service_name):
            return False
    return True


def _source_service_name(description: str | None) -> str | None:
    description = cstr(description)
    start = description.find(PET_CARE_SERVICE_SOURCE_PREFIX)
    if start < 0:
        return None
    start += len(PET_CARE_SERVICE_SOURCE_PREFIX)
    end = description.find(PET_CARE_SERVICE_SOURCE_SUFFIX, start)
    if end < 0:
        return None
    return description[start:end].strip() or None


def _row_matches_pet_care_service(invoice, row, service_name: str) -> bool:
    service = frappe.db.get_value(
        "PetCareService",
        service_name,
        ["name", "guardian_id", "item_code", "price"],
        as_dict=True,
    )
    if not service or not service.get("guardian_id"):
        return False

    customer = frappe.db.get_value("Guardian", service.guardian_id, "customer_id")
    if not customer or customer != invoice.customer:
        return False

    if service.get("item_code") and row.get("item_code") != service.item_code:
        return False
    if service.get("price") not in (None, "") and flt(row.get("rate")) != flt(service.price):
        return False
    return True
