import secrets
import string

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import cint, cstr, today, validate_email_address
from frappe.utils.password import update_password

from pet_app.api.auth_mobile import create_api_keys, validate_iraqi_phone
from pet_app.api.permissions import require_doctype_permission
from pet_app.api.response import standardize_response

# COMPANY = "HM" and DRIVER_CASH_PARENT = "Cash In Hand - H" are gone. Neither existed
# on this site - there is no Company "HM" and no account by that name - so every code
# path that touched them was unreachable. Company is now derived from the document being
# worked on, the way _create_stock_issue does it, and the driver's cash account is a
# required field on the Driver record chosen by staff.
DRIVER_ROLE = "Driver"
DRIVER_MANAGEMENT_ROLES = ("System Manager", "Administrator")
DRIVER_SYSTEM_EMAIL_DOMAIN = "petapp.com"
ACTIVE_DRIVER_STATUS = "Active"
INACTIVE_DRIVER_STATUS = "Left"
DEFAULT_ADDRESS_COUNTRY = "Iraq"
DRIVER_LOGIN_RATE_LIMIT = 20
DRIVER_LOGIN_RATE_WINDOW = 60
GENERIC_DRIVER_LOGIN_ERROR = _("Invalid phone or password")


def _logger():
    return frappe.logger("pet_app.driver")


def _is_cod(doc) -> bool:
    return getattr(doc, "custom_payment_method", None) == "Cash on Delivery"


def _has_driver_management_role(user=None):
    user = user or frappe.session.user
    if user == "Administrator":
        return True
    user_roles = set(frappe.get_roles(user) or [])
    return bool(user_roles & set(DRIVER_MANAGEMENT_ROLES))


def _has_driver_permission(ptype, user=None):
    try:
        return bool(frappe.has_permission("Driver", ptype=ptype, user=user or frappe.session.user))
    except Exception:
        return False


def _check_permission(ptype="write"):
    if _has_driver_management_role() or _has_driver_permission(ptype):
        return
    frappe.throw(_("Not authorized"), frappe.PermissionError)


def _log_driver_event(driver_id, action, user_id=None, level="info", details=None):
    message = f"driver={driver_id} action={action}"
    if user_id:
        message += f" user={user_id}"
    if details:
        message += f" details={details}"
    getattr(_logger(), level)(message)


def generate_driver_system_email(driver_name):
    driver_name = cstr(driver_name).strip().lower()
    if not driver_name:
        frappe.throw(_("Driver name is required to generate the system email"))
    return f"{driver_name}@{DRIVER_SYSTEM_EMAIL_DOMAIN}"


def split_full_name(full_name):
    parts = cstr(full_name).strip().split()
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:]) if len(parts) > 1 else ""


def _normalize_optional_email(value):
    value = cstr(value).strip().lower()
    if not value:
        return None
    validate_email_address(value, throw=True)
    return value


def _normalize_username(value):
    value = cstr(value).strip().lower()
    normalized = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)
    normalized = normalized.strip("._-")
    return normalized


def _random_driver_password(length=16):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _driver_enabled(status):
    return 1 if cstr(status) == ACTIVE_DRIVER_STATUS else 0


def _expected_user_name(driver_name):
    return generate_driver_system_email(driver_name)


def _require_driver_role():
    """The role is created by pet_app.patches.driver_role_and_fields.

    Appending a role row that does not exist fails Frappe's link validation with a
    message that says nothing about drivers - which is precisely how this subsystem
    failed silently for so long. Fail with something actionable instead.
    """
    if not frappe.db.exists("Role", DRIVER_ROLE):
        frappe.throw(
            _(
                "The {0} role does not exist, so drivers cannot be provisioned. "
                "Run `bench --site <site> migrate` to apply "
                "pet_app.patches.driver_role_and_fields."
            ).format(frappe.bold(DRIVER_ROLE))
        )


def _ensure_driver_role(user_doc):
    _require_driver_role()
    if DRIVER_ROLE not in {row.role for row in user_doc.roles}:
        user_doc.append("roles", {"role": DRIVER_ROLE})


def _build_available_username(base, exclude_user=None):
    seed = _normalize_username(base) or "driver"
    candidate = seed
    suffix = 1

    while True:
        taken_by = frappe.db.get_value("User", {"username": candidate}, "name")
        if not taken_by or taken_by == exclude_user:
            return candidate
        suffix += 1
        candidate = f"{seed}{suffix}"


def _release_legacy_user_username(doc, system_user_name):
    legacy_user_name = doc.user if doc.user and doc.user != system_user_name else None
    if not legacy_user_name or not frappe.db.exists("User", legacy_user_name):
        return

    legacy_user = frappe.get_doc("User", legacy_user_name)
    if cstr(legacy_user.username) == cstr(doc.custom_username):
        legacy_user.username = _build_available_username(
            f"legacy-{doc.name.lower()}",
            exclude_user=legacy_user.name,
        )
        legacy_user.flags.ignore_permissions = True
        legacy_user.save(ignore_permissions=True)
        _log_driver_event(
            doc.name,
            "legacy_username_released",
            user_id=legacy_user.name,
            details=f"new_username={legacy_user.username}",
        )


def _set_driver_user_link(doc, user_name):
    if doc.user == user_name:
        return
    frappe.db.set_value("Driver", doc.name, "user", user_name, update_modified=False)
    doc.user = user_name


def _set_driver_address_link(doc, address_name):
    if doc.address == address_name:
        return
    frappe.db.set_value("Driver", doc.name, "address", address_name, update_modified=False)
    doc.address = address_name


def _ensure_driver_only_links(address_doc, driver_name):
    existing_links = list(address_doc.links or [])
    kept_links = []
    changed = False
    has_driver_link = False

    for link in existing_links:
        if link.link_doctype == "Driver" and link.link_name == driver_name:
            has_driver_link = True
            kept_links.append(link)
        else:
            changed = True

    if len(kept_links) != len(existing_links):
        address_doc.set("links", kept_links)

    if not has_driver_link:
        address_doc.append("links", {"link_doctype": "Driver", "link_name": driver_name})
        changed = True

    return changed


def _existing_driver_with_username(username, current_driver=None):
    for name in frappe.get_all("Driver", filters={"custom_username": username}, pluck="name"):
        if name != current_driver:
            return name
    return None


def _existing_user_with_username(username, allowed_user_names=None):
    allowed_user_names = set(allowed_user_names or [])
    for name in frappe.get_all("User", filters={"username": username}, pluck="name"):
        if name not in allowed_user_names:
            return name
    return None


def validate_driver(doc):
    doc.full_name = cstr(doc.full_name).strip()
    if not doc.full_name:
        frappe.throw(_("Driver full name is required"))

    doc.cell_number = validate_iraqi_phone(doc.cell_number)
    doc.custom_email = _normalize_optional_email(getattr(doc, "custom_email", None))
    doc.custom_username = _normalize_username(doc.custom_username)
    if not doc.custom_username:
        frappe.throw(_("Driver username is required"))

    duplicate_driver = _existing_driver_with_username(doc.custom_username, current_driver=doc.name or None)
    if duplicate_driver:
        frappe.throw(
            _("Driver username {0} is already used by {1}").format(doc.custom_username, duplicate_driver)
        )

    allowed_user_names = []
    if doc.name:
        allowed_user_names.append(_expected_user_name(doc.name))
    if doc.user and frappe.db.exists("User", doc.user):
        allowed_user_names.append(doc.user)

    duplicate_user = _existing_user_with_username(doc.custom_username, allowed_user_names=allowed_user_names)
    if duplicate_user:
        frappe.throw(
            _("Username {0} is already used by User {1}").format(doc.custom_username, duplicate_user)
        )

    _validate_driver_cash_account(doc)

    previous_doc = doc.get_doc_before_save()
    if previous_doc and previous_doc.user and doc.user and doc.user != previous_doc.user:
        frappe.throw(_("Driver user is managed automatically and cannot be edited manually"))

    if doc.is_new():
        doc.user = None
    elif previous_doc and previous_doc.user:
        doc.user = previous_doc.user

    if doc.user and not frappe.db.exists("User", doc.user):
        doc.user = None


def sync_driver_user(doc):
    if not doc.name:
        frappe.throw(_("Driver must exist before syncing the linked User"))

    # Checked before the User is built, because the role row is embedded in the insert
    # payload below and a missing role would fail there with an opaque link error.
    _require_driver_role()

    system_user_name = _expected_user_name(doc.name)
    _release_legacy_user_username(doc, system_user_name)

    user_doc = frappe.get_doc("User", system_user_name) if frappe.db.exists("User", system_user_name) else None
    password_created = None

    if not user_doc:
        first_name, last_name = split_full_name(doc.full_name)
        user_doc = frappe.get_doc({
            "doctype": "User",
            "email": system_user_name,
            "username": doc.custom_username,
            "first_name": first_name or doc.name,
            "last_name": last_name,
            "mobile_no": doc.cell_number,
            "enabled": _driver_enabled(doc.status),
            "send_welcome_email": 0,
            "roles": [{"role": DRIVER_ROLE}],
        })
        user_doc.flags.ignore_permissions = True
        user_doc.insert(ignore_permissions=True)
        password_created = _random_driver_password()
        update_password(user_doc.name, password_created)
        doc.flags.generated_driver_password = password_created
        _log_driver_event(doc.name, "user_created", user_id=user_doc.name)

    first_name, last_name = split_full_name(doc.full_name)
    changed = False
    desired_values = {
        "first_name": first_name or doc.name,
        "last_name": last_name,
        "mobile_no": doc.cell_number or "",
        "username": doc.custom_username,
        "enabled": _driver_enabled(doc.status),
    }

    for fieldname, value in desired_values.items():
        if cstr(user_doc.get(fieldname)) != cstr(value):
            user_doc.set(fieldname, value)
            changed = True

    before_roles = len(user_doc.roles)
    _ensure_driver_role(user_doc)
    if len(user_doc.roles) != before_roles:
        changed = True

    if changed:
        user_doc.flags.ignore_permissions = True
        user_doc.save(ignore_permissions=True)
        _log_driver_event(doc.name, "user_synced", user_id=user_doc.name)

    _set_driver_user_link(doc, user_doc.name)
    return user_doc, password_created


def sync_driver_address(doc, address_payload=None):
    address_payload = address_payload or {}
    address_doc = None

    if doc.address and frappe.db.exists("Address", doc.address):
        address_doc = frappe.get_doc("Address", doc.address)
    elif address_payload:
        address_doc = frappe.new_doc("Address")
        address_doc.address_type = "Personal"
        address_doc.flags.ignore_permissions = True

    if not address_doc:
        return None

    changed = _ensure_driver_only_links(address_doc, doc.name)
    desired_values = {
        "address_title": doc.full_name,
        "address_type": getattr(address_doc, "address_type", None) or "Personal",
        "phone": doc.cell_number or "",
        "email_id": doc.custom_email or "",
    }

    for fieldname in ("address_line1", "address_line2", "city", "country"):
        if fieldname in address_payload:
            desired_values[fieldname] = cstr(address_payload.get(fieldname))

    for fieldname, value in desired_values.items():
        if cstr(getattr(address_doc, fieldname, "")) != cstr(value):
            setattr(address_doc, fieldname, value)
            changed = True

    if address_doc.is_new():
        address_doc.insert(ignore_permissions=True)
        _set_driver_address_link(doc, address_doc.name)
        _log_driver_event(doc.name, "address_created", details=f"address={address_doc.name}")
    elif changed:
        address_doc.flags.ignore_permissions = True
        address_doc.save(ignore_permissions=True)
        _log_driver_event(doc.name, "address_synced", details=f"address={address_doc.name}")

    return address_doc


def _app_company():
    """The company this app books against.

    Replaces the COMPANY = "HM" literal. Read from the app's own settings first so the
    answer is configurable, then Frappe's global default.
    """
    return (
        frappe.db.get_single_value("Pet App Accounting Settings", "default_company")
        or frappe.defaults.get_user_default("Company")
        or frappe.defaults.get_global_default("company")
    )


def _validate_driver_cash_account(doc):
    """Same shape as Medication._validate_default_warehouse: exists, not a group, not
    disabled, right company - plus required, because a driver without a cash account
    cannot handle money.

    Deliberately NOT derived. The previous _ensure_driver_cash_account invented an
    account under a hardcoded parent that does not exist on this site; which ledger a
    driver's float sits in is a bookkeeping decision for staff to make explicitly.
    """
    account = cstr(doc.custom_cash_account).strip()
    if not account:
        frappe.throw(_("Cash Account is required for a Driver."))

    row = frappe.db.get_value(
        "Account",
        account,
        ["name", "company", "is_group", "disabled", "account_type"],
        as_dict=True,
    )
    if not row:
        frappe.throw(_("Cash Account {0} does not exist.").format(frappe.bold(account)))
    if cint(row.disabled):
        frappe.throw(_("Cash Account {0} is disabled.").format(frappe.bold(account)))
    if cint(row.is_group):
        frappe.throw(_("Cash Account {0} must be a ledger account, not a group.").format(frappe.bold(account)))

    company = _app_company()
    if company and row.company != company:
        frappe.throw(
            _("Cash Account {0} belongs to Company {1}, not {2}.").format(
                frappe.bold(account), frappe.bold(row.company), frappe.bold(company)
            )
        )

    doc.custom_cash_account = row.name
    return row.name


def _receiving_cashier_till(company=None):
    """The till of the cashier who is receiving the driver's cash, right now.

    Supersedes the Phase 1 global handover account, which has been removed: the driver
    hands cash to a person, and that person's own profile decides which ledger it lands
    in. There is no site-wide default and no admin exception - an admin or the doctor
    receiving cash does so through their own cashier profile like anyone else.

    Resolution and validation both live in pet_app.api.accounting.cashier, which already
    owns cashier profiles; this is a thin call-through so there is only one resolver.
    Throws with an actionable message when the acting user has no profile or the profile
    has no cash account, and never falls back.
    """
    from pet_app.api.accounting.cashier import resolve_session_cashier_till

    return resolve_session_cashier_till(company=company)


def _disable_driver_user(user_name):
    if not user_name or not frappe.db.exists("User", user_name):
        return

    user_doc = frappe.get_doc("User", user_name)
    if not user_doc.enabled:
        return

    user_doc.enabled = 0
    user_doc.flags.ignore_permissions = True
    user_doc.save(ignore_permissions=True)


def _driver_has_order_history(driver_id):
    return bool(frappe.db.exists("Sales Order", {"custom_driver": driver_id}))


def _driver_has_active_orders(driver_id):
    return bool(frappe.db.exists(
        "Sales Order",
        {
            "custom_driver": driver_id,
            "custom_order_status": ["in", ["Preparing", "Out for Delivery", "Returned", "Cash Collected"]],
        },
    ))


def _driver_has_accounting_history(cash_account):
    if not cash_account:
        return False

    if frappe.db.exists("GL Entry", {"account": cash_account}):
        return True

    if frappe.db.exists("Journal Entry Account", {"account": cash_account}):
        return True

    return False


def before_driver_save(doc, method=None):
    validate_driver(doc)


def after_driver_insert(doc, method=None):
    # No cash-account provisioning step: the account is a required field validated in
    # validate_driver, so by the time we are here it is already set and checked.
    sync_driver_user(doc)
    sync_driver_address(doc, getattr(doc.flags, "driver_address_payload", None))
    _log_driver_event(doc.name, "provisioned", user_id=doc.user)


def on_driver_update(doc, method=None):
    sync_driver_user(doc)
    sync_driver_address(doc)
    _log_driver_event(doc.name, "updated", user_id=doc.user)


@frappe.whitelist()
@standardize_response
def create_driver(
    full_name,
    phone,
    custom_cash_account=None,
    custom_username=None,
    custom_email=None,
    email=None,
    status=ACTIVE_DRIVER_STATUS,
    license_number=None,
    license_expiry=None,
    address_line1=None,
    address_line2=None,
    city=None,
    country=DEFAULT_ADDRESS_COUNTRY,
):
    _check_permission("create")
    name_parts = cstr(full_name).split()
    address_payload = None

    driver = frappe.get_doc({
        "doctype": "Driver",
        "full_name": full_name,
        "cell_number": phone,
        "custom_email": custom_email or email,
        "custom_username": custom_username or (name_parts[0] if name_parts else ""),
        # Required and never derived - staff choose which ledger this driver's cash
        # sits in. validate_driver refuses the record if it is missing or unsuitable.
        "custom_cash_account": custom_cash_account,
        "status": status or ACTIVE_DRIVER_STATUS,
        "license_number": license_number,
        "expiry_date": license_expiry,
    })

    if address_line1 or address_line2 or city or (country and country != DEFAULT_ADDRESS_COUNTRY):
        address_payload = {
            "address_line1": address_line1 or "",
            "address_line2": address_line2 or "",
            "city": city or "",
            "country": country or DEFAULT_ADDRESS_COUNTRY,
        }
        driver.flags.driver_address_payload = address_payload

    driver.flags.ignore_permissions = True
    driver.insert(ignore_permissions=True)
    temporary_password = getattr(driver.flags, "generated_driver_password", None)
    driver.reload()

    # Fallback hardening: if a provisioning hook misses the address payload, complete it here.
    if address_payload and not driver.address:
        sync_driver_address(driver, address_payload=address_payload)
        driver.reload()

    return {
        "name": driver.name,
        "full_name": driver.full_name,
        "status": driver.status,
        "custom_cash_account": driver.custom_cash_account,
        "cell_number": driver.cell_number,
        "address": driver.address,
        "user": driver.user,
        "custom_username": driver.custom_username,
        "custom_email": driver.custom_email,
        "license_number": driver.license_number,
        "expiry_date": driver.expiry_date,
        # Returned only once at creation time; never persisted or logged.
        "temporary_password": temporary_password,
    }


def _driver_debit_remark(doc):
    return f"Driver Debit - {doc.name} - {doc.custom_driver}"


def _driver_return_remark(doc):
    return f"Driver Return - {doc.name} - {doc.custom_driver}"


def _driver_collection_remark(doc):
    return f"Driver Collection - {doc.name} - {doc.custom_driver}"


def _driver_collection_legacy_remark(doc):
    return f"Driver Collection - {doc.name}"


def _document_company(doc):
    """Company of the order being booked, the way _create_stock_issue reads doc.company.

    _collect_driver_cash and _reverse_driver_entry receive the frappe._dict built by
    order._driver_context rather than the Sales Order itself, so that dict now carries
    company too. Falls back to the app default only if the document has none.
    """
    company = getattr(doc, "company", None) or (doc.get("company") if hasattr(doc, "get") else None)
    company = cstr(company).strip() or _app_company()
    if not company:
        frappe.throw(
            _("Cannot determine the Company for order {0}; driver accounting needs one.").format(
                frappe.bold(getattr(doc, "name", "?"))
            )
        )
    return company


def _get_driver_cash_account(driver_id):
    return frappe.db.get_value("Driver", driver_id, "custom_cash_account")


def _get_company_receivable_account(company):
    """Company now comes from the document being booked, not a module constant."""
    if not company:
        frappe.throw(_("Company is required to resolve the receivable account."))
    account = frappe.db.get_value("Company", company, "default_receivable_account")
    if not account:
        frappe.throw(
            _("Company {0} has no Default Receivable Account set.").format(frappe.bold(company))
        )
    return account


def _journal_entry_exists(*remarks):
    cleaned = [remark for remark in remarks if remark]
    if not cleaned:
        return False
    filter_value = cleaned[0] if len(cleaned) == 1 else ["in", cleaned]
    return bool(frappe.db.exists("Journal Entry", {"user_remark": filter_value, "docstatus": 1}))


def _require_journal_entry_create_submit():
    """Retained for any caller that books outside an authorised order transition.

    NOT used by the three order-driven entries below. Those run underneath a transition
    the caller was already authorised for - by ORDER_ROLES for staff, or by ownership for
    a driver whose Driver record matches the order's custom_driver - and re-checking here
    would demand Journal Entry permissions that no driver will ever hold, which is
    exactly how the guardian stock reversal used to fail.

    Safe to elevate because the elevation cannot widen scope: every entry is keyed to one
    Sales Order, books only that order's grand_total, and touches only the cash account of
    the driver named on that order plus one counterpart resolved from the acting user's
    own cashier profile or the order's company. None of it can reach another order's books.
    """
    require_doctype_permission("Journal Entry", "create")
    require_doctype_permission("Journal Entry", "submit")


def _clear_cash_account_party(je, cash_account):
    for row in je.accounts:
        if row.account == cash_account:
            row.party_type = None
            row.party = None


def _create_driver_debit_entry(doc):
    if not _is_cod(doc) or not doc.custom_driver:
        return

    remark = _driver_debit_remark(doc)
    if _journal_entry_exists(remark):
        return

    company = _document_company(doc)
    cash_account = _get_driver_cash_account(doc.custom_driver)
    receivable_account = _get_company_receivable_account(company)
    if not cash_account or not receivable_account:
        frappe.log_error(
            f"Missing account for driver debit on order {doc.name}",
            "Driver Accounting Error",
        )
        return

    je = frappe.get_doc({
        "doctype": "Journal Entry",
        "voucher_type": "Journal Entry",
        "company": company,
        "posting_date": today(),
        "user_remark": remark,
        "accounts": [
            {
                "account": cash_account,
                "debit_in_account_currency": doc.grand_total,
                "credit_in_account_currency": 0,
            },
            {
                "account": receivable_account,
                "debit_in_account_currency": 0,
                "credit_in_account_currency": doc.grand_total,
                "party_type": "Customer",
                "party": doc.customer,
                "is_advance": "Yes",
                "reference_type": "Sales Order",
                "reference_name": doc.name,
            },
        ],
    })
    _clear_cash_account_party(je, cash_account)
    je.flags.ignore_permissions = True
    je.insert(ignore_permissions=True)
    je.submit()
    _log_driver_event(doc.custom_driver, "je_driver_debit_created", details=f"order={doc.name}")


def _collect_driver_cash(doc):
    if not _is_cod(doc) or not doc.custom_driver:
        return

    if _journal_entry_exists(_driver_collection_remark(doc), _driver_collection_legacy_remark(doc)):
        return

    company = _document_company(doc)
    cash_account = _get_driver_cash_account(doc.custom_driver)
    if not cash_account:
        frappe.log_error(
            f"Missing account for driver collection on order {doc.name}",
            "Driver Accounting Error",
        )
        return

    # The receiving side is the acting cashier's own till, resolved at the moment of
    # handover. Throws with an actionable message when that user has no profile or the
    # profile has no cash account - cash that moved physically but was never booked is
    # worse than a loud refusal, so this never logs-and-continues.
    till = _receiving_cashier_till(company=company)
    main_cash_account = till.cash_account

    je = frappe.get_doc({
        "doctype": "Journal Entry",
        "voucher_type": "Cash Entry",
        "company": company,
        "posting_date": today(),
        "user_remark": _driver_collection_remark(doc),
        "accounts": [
            {
                "account": main_cash_account,
                "debit_in_account_currency": doc.grand_total,
                "credit_in_account_currency": 0,
            },
            {
                "account": cash_account,
                "debit_in_account_currency": 0,
                "credit_in_account_currency": doc.grand_total,
            },
        ],
    })
    je.flags.ignore_permissions = True
    je.insert(ignore_permissions=True)
    je.submit()
    _log_driver_event(doc.custom_driver, "je_driver_collection_created", details=f"order={doc.name}")


def _reverse_driver_entry(doc):
    if not _is_cod(doc) or not doc.custom_driver:
        return

    remark = _driver_return_remark(doc)
    if _journal_entry_exists(remark):
        return

    company = _document_company(doc)
    cash_account = _get_driver_cash_account(doc.custom_driver)
    receivable_account = _get_company_receivable_account(company)
    if not cash_account or not receivable_account:
        frappe.log_error(
            f"Missing account for driver return on order {doc.name}",
            "Driver Accounting Error",
        )
        return

    je = frappe.get_doc({
        "doctype": "Journal Entry",
        "voucher_type": "Journal Entry",
        "company": company,
        "posting_date": today(),
        "user_remark": remark,
        "accounts": [
            {
                "account": receivable_account,
                "debit_in_account_currency": doc.grand_total,
                "credit_in_account_currency": 0,
                "party_type": "Customer",
                "party": doc.customer,
            },
            {
                "account": cash_account,
                "debit_in_account_currency": 0,
                "credit_in_account_currency": doc.grand_total,
            },
        ],
    })
    _clear_cash_account_party(je, cash_account)
    je.flags.ignore_permissions = True
    je.insert(ignore_permissions=True)
    je.submit()
    _log_driver_event(doc.custom_driver, "je_driver_return_created", details=f"order={doc.name}")


@frappe.whitelist()
@standardize_response
def delete_driver(driver_id):
    _check_permission("delete")

    doc = frappe.get_doc("Driver", driver_id)
    cash_account = doc.custom_cash_account

    if _driver_has_active_orders(driver_id):
        frappe.throw(_("Cannot delete driver with active orders"))

    has_history = _driver_has_order_history(driver_id) or _driver_has_accounting_history(cash_account)
    if has_history:
        if doc.status != INACTIVE_DRIVER_STATUS:
            doc.status = INACTIVE_DRIVER_STATUS
            doc.flags.ignore_permissions = True
            doc.save(ignore_permissions=True)
        _disable_driver_user(doc.user)
        _log_driver_event(driver_id, "soft_disabled", user_id=doc.user)
        return {
            "driver": driver_id,
            "action": "disabled",
            "status": doc.status,
            "user": doc.user,
        }

    if doc.address and frappe.db.exists("Address", doc.address):
        frappe.delete_doc("Address", doc.address, ignore_permissions=True, force=True)

    # The cash account is deliberately NOT deleted. It used to be auto-created per
    # driver under a hardcoded parent, so deleting it with the driver was tidy-up. It is
    # now a real ledger chosen by staff and very likely shared between drivers, so
    # deleting it here would destroy live books to remove one person.

    if doc.user and frappe.db.exists("User", doc.user):
        frappe.delete_doc("User", doc.user, ignore_permissions=True, force=True)

    frappe.delete_doc("Driver", driver_id, ignore_permissions=True, force=True)
    _log_driver_event(driver_id, "hard_deleted", user_id=doc.user)
    return {
        "driver": driver_id,
        "action": "deleted",
    }


@frappe.whitelist(allow_guest=True, methods=["POST"])
@standardize_response
@rate_limit(limit=DRIVER_LOGIN_RATE_LIMIT, seconds=DRIVER_LOGIN_RATE_WINDOW, methods=["POST"])
def driver_login(phone, password):
    # Endpoint-level throttling is enforced via Frappe's built-in rate limiter above.
    try:
        phone = validate_iraqi_phone(phone)
    except Exception:
        _log_driver_event("unknown", "login_failed", level="warning", details="invalid_phone_format")
        frappe.throw(GENERIC_DRIVER_LOGIN_ERROR, frappe.AuthenticationError)

    driver = frappe.db.get_value(
        "Driver",
        {"cell_number": phone},
        ["name", "user", "status", "custom_cash_account", "full_name", "custom_username"],
        as_dict=True,
    )

    if not driver:
        _log_driver_event("unknown", "login_failed", level="warning", details=f"phone={phone}")
        frappe.throw(GENERIC_DRIVER_LOGIN_ERROR, frappe.AuthenticationError)
    if driver.status != ACTIVE_DRIVER_STATUS:
        _log_driver_event(driver.name, "login_blocked", user_id=driver.user, level="warning", details=f"status={driver.status}")
        frappe.throw(GENERIC_DRIVER_LOGIN_ERROR, frappe.AuthenticationError)
    if not driver.user or not frappe.db.exists("User", driver.user):
        _log_driver_event(driver.name, "login_failed", user_id=driver.user, level="warning", details="user_not_provisioned")
        frappe.throw(GENERIC_DRIVER_LOGIN_ERROR, frappe.AuthenticationError)

    try:
        from frappe.core.doctype.user.user import check_password

        check_password(driver.user, password)
    except Exception:
        _log_driver_event(driver.name, "login_failed", user_id=driver.user, level="warning", details="invalid_password")
        frappe.throw(GENERIC_DRIVER_LOGIN_ERROR, frappe.AuthenticationError)

    api_key, api_secret = create_api_keys(driver.user)
    _log_driver_event(driver.name, "login_success", user_id=driver.user)
    return {
        "status": "success",
        "token": f"token {api_key}:{api_secret}",
        "driver_id": driver.name,
        "full_name": driver.full_name,
        "username": driver.custom_username,
        "cash_account": driver.custom_cash_account,
    }


@frappe.whitelist()
@standardize_response
def get_driver_balance(driver_id):
    if not driver_id:
        frappe.throw(_("Driver is required."))

    if not frappe.db.exists("Driver", driver_id):
        frappe.throw(_("Driver not found"))

    driver = frappe.db.get_value("Driver", driver_id, ["user", "custom_cash_account"], as_dict=True)
    if not driver:
        frappe.throw(_("Driver not found"))

    if frappe.session.user != "Administrator":
        can_manage = _has_driver_management_role() or _has_driver_permission("read")
        if not can_manage and driver.user != frappe.session.user:
            frappe.throw(_("Not authorized"), frappe.PermissionError)

    cash_account = driver.custom_cash_account
    if not cash_account:
        return {"balance": 0}

    balance = frappe.db.sql(
        """
        SELECT COALESCE(SUM(debit) - SUM(credit), 0)
        FROM `tabGL Entry`
        WHERE account = %s
          AND is_cancelled = 0
        """,
        cash_account,
    )[0][0] or 0

    return {
        "driver_id": driver_id,
        "cash_account": cash_account,
        "balance": balance,
    }
