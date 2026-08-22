"""
PET APP - Mobile Authentication API (PRODUCTION READY)
Static Token (api_key:api_secret) | No Sessions | Mobile Friendly

Flow:
1) register_guardian(phone, password) -> Guardian ONLY + OTP (NO User/Customer)
2) verify_otp(phone, otp, password) -> creates User + Customer + activates + returns token
3) complete_profile(full_name, city, address_line1, email_id?) -> updates Customer + Address
4) login(phone, password) -> returns token
5) forgot_password / reset_password -> OTP reset (requires verified user)
"""

import frappe
import random
import re
from frappe.rate_limiter import rate_limit
from frappe.utils import add_to_date, now_datetime
from frappe.utils.password import get_decrypted_password, check_password
from pet_app.api.link_aliases import with_link_aliases
from pet_app.utils.guardian_customer import (
    change_guardian_phone_number,
    get_guardian_by_user,
    get_or_create_customer_from_guardian,
    guardian_customer_locks,
    log_customer_event,
    sync_customer_from_guardian,
    upsert_primary_address_from_guardian,
)
from werkzeug.security import generate_password_hash, check_password_hash
from pet_app.api.response import standardize_response


# ====== CONFIG ======
def is_debug_mode() -> bool:
    conf = getattr(frappe, "conf", None)
    if not conf:
        return False
    try:
        return bool(conf.get("development_mode"))
    except Exception:
        return bool(getattr(conf, "development_mode", False))


def _with_guardian_alias(payload: dict) -> dict:
    return with_link_aliases(
        payload,
        guardian_field="guardian_id",
        include_pet=False,
        include_doctor=False,
        include_provider=False,
    )


OTP_EXPIRY_MINUTES = 10
MAX_OTP_ATTEMPTS = 5
DEFAULT_COUNTRY = "Iraq"
DEFAULT_ADDRESS_TYPE = "Billing"
AUTH_RATE_LIMIT = 20
AUTH_RATE_WINDOW = 60


# ====== VALIDATORS ======

def validate_iraqi_phone(phone: str) -> str:
    phone = (phone or "").strip().replace(" ", "").replace("-", "")
    if re.match(r"^07\d{9}$", phone):
        return phone
    if re.match(r"^9647\d{9}$", phone):
        return "0" + phone[3:]
    frappe.throw("Invalid phone. Use: 07XXXXXXXXX or 9647XXXXXXXXX")


def validate_password(password: str) -> str:
    password = password or ""
    if not 6 <= len(password) <= 50:
        frappe.throw("Password: 6-50 characters")

    if not re.match(r'^[a-zA-Z0-9!@#$%^&*()_+\-=\[\]{};:"\\|,.<>/?]+$', password):
        frappe.throw("Invalid password characters")

    return password


def validate_full_name(name: str) -> str:
    name = (name or "").strip()
    if not name or len(name) > 100:
        frappe.throw("Name: 1-100 characters")
    return name


def validate_city(city: str) -> str:
    city = (city or "").strip()
    if not city or len(city) > 100:
        frappe.throw("City is required (1-100 characters)")
    return city


def validate_address_line1(address_line1: str) -> str:
    address_line1 = (address_line1 or "").strip()
    if not address_line1 or len(address_line1) > 300:
        frappe.throw("Address line1 is required (1-300 characters)")
    return address_line1


def validate_country(country: str) -> str:
    country = (country or DEFAULT_COUNTRY).strip()
    if not country or len(country) > 100:
        frappe.throw("Country is required (1-100 characters)")
    return country


def validate_otp(otp: str) -> str:
    otp = str(otp or "").strip()
    if not re.match(r"^\d{6}$", otp):
        frappe.throw("Invalid OTP format (must be 6 digits)")
    return otp


# ====== HELPERS ======

def guardian_has_column(fieldname: str) -> bool:
    return frappe.db.has_column("Guardian", fieldname)


def require_guardian_columns(fieldnames):
    missing = [fieldname for fieldname in fieldnames if not guardian_has_column(fieldname)]
    if missing:
        frappe.throw(
            "Guardian schema is missing columns: {0}. Run bench migrate before using this API.".format(
                ", ".join(missing)
            )
        )


def existing_guardian_fields(fieldnames):
    return [fieldname for fieldname in fieldnames if guardian_has_column(fieldname)]

def log_security_event(event_type: str, phone: str, message: str = ""):
    if is_debug_mode() and message:
        frappe.logger().info(f"[AUTH] [{event_type}] {phone}: {message}")
    else:
        frappe.logger().warning(f"[AUTH] [{event_type}] {phone}")


def generate_otp() -> str:
    conf = getattr(frappe, "conf", None)
    if conf:
        try:
            if conf.get("developer_mode") or conf.get("development_mode") or conf.get("test_otp"):
                return "123456"
        except Exception:
            if (
                getattr(conf, "developer_mode", False)
                or getattr(conf, "development_mode", False)
                or getattr(conf, "test_otp", False)
            ):
                return "123456"
    return "".join(str(random.randint(0, 9)) for _ in range(6))


def get_guardian(phone: str):
    return frappe.db.get_value("Guardian", {"phone": phone}, "*", as_dict=True)


def set_new_otp(guardian_name: str, pending_phone_change: str | None = None) -> str:
    otp = generate_otp()
    updates = {
        "otp_code": otp,
        "otp_expires_at": add_to_date(now_datetime(), minutes=OTP_EXPIRY_MINUTES),
        "wrong_attempts": 0,
    }
    if guardian_has_column("pending_phone_change"):
        updates["pending_phone_change"] = pending_phone_change

    frappe.db.set_value(
        "Guardian",
        guardian_name,
        updates,
        update_modified=False
    )
    _queue_otp_notification(guardian_name, otp, pending_phone_change=pending_phone_change)
    return otp


def _queue_otp_notification(guardian_name: str, otp: str, pending_phone_change: str | None = None):
    if not frappe.db.exists("DocType", "Pet App Notification Queue"):
        return
    try:
        from pet_app.notifications.engine import send_whatsapp_otp

        phone = pending_phone_change or frappe.db.get_value("Guardian", guardian_name, "phone")
        send_whatsapp_otp(
            guardian=guardian_name if not pending_phone_change else None,
            phone=phone if pending_phone_change else None,
            otp=otp,
            context={"guardian": guardian_name},
            idempotency_key=f"otp:{guardian_name}:{frappe.generate_hash(length=10)}",
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Pet App OTP notification failed")


def _current_user_is_admin() -> bool:
    return frappe.session.user == "Administrator" or "System Manager" in frappe.get_roles(frappe.session.user)


def _resolve_guardian_for_phone_change(user_id=None, guardian_id=None):
    if frappe.session.user == "Guest":
        frappe.throw("Authentication required", frappe.PermissionError)

    is_admin = _current_user_is_admin()
    guardian = None

    if guardian_id:
        guardian = frappe.db.get_value("Guardian", guardian_id, "*", as_dict=True)
    elif user_id:
        if not is_admin and user_id != frappe.session.user:
            frappe.throw("Not permitted", frappe.PermissionError)
        guardian = get_guardian_by_user(user_id)
    else:
        guardian = get_guardian_by_user(frappe.session.user)

    if not guardian:
        frappe.throw("Guardian not found")

    if not is_admin and guardian.get("user_id") != frappe.session.user:
        frappe.throw("Not permitted", frappe.PermissionError)

    return guardian


def _set_phone_change_otp(guardian_name: str, new_phone: str) -> str:
    require_guardian_columns(["pending_phone_change"])
    return set_new_otp(guardian_name, pending_phone_change=new_phone)


def _verify_phone_change_otp(guardian_name: str, old_phone: str, new_phone: str, otp_code: str):
    guardian = frappe.db.get_value(
        "Guardian",
        guardian_name,
        existing_guardian_fields(["name", "otp_code", "otp_expires_at", "wrong_attempts", "pending_phone_change"]),
        as_dict=True,
    )

    if not guardian:
        log_customer_event(
            "PHONE_CHANGE_FAILED",
            guardian_name=guardian_name,
            phone=old_phone,
            details=f"new_phone={new_phone}; reason=guardian_not_found",
            level="error",
        )
        frappe.throw("Guardian not found")

    if not guardian.get("otp_code"):
        log_customer_event(
            "PHONE_CHANGE_FAILED",
            guardian_name=guardian_name,
            phone=old_phone,
            details=f"new_phone={new_phone}; reason=missing_otp",
            level="error",
        )
        frappe.throw("OTP expired. Request new one")

    if guardian.get("otp_expires_at") and now_datetime() > guardian.get("otp_expires_at"):
        _clear_phone_change_otp(guardian_name)
        log_customer_event(
            "PHONE_CHANGE_FAILED",
            guardian_name=guardian_name,
            phone=old_phone,
            details=f"new_phone={new_phone}; reason=expired_otp",
            level="error",
        )
        frappe.throw("OTP expired. Request new one")

    if guardian.get("pending_phone_change") != new_phone:
        _clear_phone_change_otp(guardian_name)
        log_customer_event(
            "PHONE_CHANGE_FAILED",
            guardian_name=guardian_name,
            phone=old_phone,
            details=f"new_phone={new_phone}; reason=otp_phone_mismatch",
            level="error",
        )
        frappe.throw("OTP expired. Request new one")

    attempts = guardian.get("wrong_attempts", 0) or 0
    if attempts >= MAX_OTP_ATTEMPTS:
        _clear_phone_change_otp(guardian_name)
        log_customer_event(
            "PHONE_CHANGE_FAILED",
            guardian_name=guardian_name,
            phone=old_phone,
            details=f"new_phone={new_phone}; reason=too_many_attempts",
            level="error",
        )
        frappe.throw("Too many attempts. Request new OTP")

    if guardian.get("otp_code") != str(otp_code):
        new_attempts = attempts + 1
        frappe.db.set_value("Guardian", guardian_name, "wrong_attempts", new_attempts, update_modified=False)
        remaining_attempts = MAX_OTP_ATTEMPTS - new_attempts
        log_customer_event(
            "PHONE_CHANGE_FAILED",
            guardian_name=guardian_name,
            phone=old_phone,
            details=f"new_phone={new_phone}; reason=invalid_otp",
            level="error",
        )
        frappe.throw(
            f"Invalid OTP. {remaining_attempts} attempts left"
            if remaining_attempts > 0
            else "Too many attempts. Request new OTP"
        )


def _clear_phone_change_otp(guardian_name: str):
    updates = {
        "otp_code": None,
        "otp_expires_at": None,
        "wrong_attempts": 0,
    }
    if guardian_has_column("pending_phone_change"):
        updates["pending_phone_change"] = None
    frappe.db.set_value("Guardian", guardian_name, updates, update_modified=False)


def create_api_keys(user_id: str):
    user_doc = frappe.get_doc("User", user_id)

    if not user_doc.api_key:
        user_doc.api_key = frappe.generate_hash(length=15)
        user_doc.api_secret = frappe.generate_hash(length=30)
        user_doc.save(ignore_permissions=True)

    api_secret = get_decrypted_password("User", user_doc.name, "api_secret", raise_exception=False)
    if not api_secret:
        user_doc.api_secret = frappe.generate_hash(length=30)
        user_doc.save(ignore_permissions=True)
        api_secret = get_decrypted_password("User", user_doc.name, "api_secret", raise_exception=False)

    return user_doc.api_key, api_secret


def ensure_user(phone: str, password: str, full_name: str = None) -> str:
    """
    Create User ONLY after OTP verification.
    """
    email = f"{phone}@petapp.local"

    if frappe.db.exists("User", email):
        user = frappe.get_doc("User", email)
        if full_name:
            user.first_name = full_name
    else:
        user = frappe.get_doc({
            "doctype": "User",
            "email": email,
            "first_name": full_name or phone,
            "enabled": 1,
            "send_welcome_email": 0,
            "roles": [{"role": "Guardian"}],
        })
        user.insert(ignore_permissions=True)

    user.new_password = password
    user.enabled = 1
    user.save(ignore_permissions=True)
    return user.name


# ====== ENDPOINTS ======

@frappe.whitelist(allow_guest=True)
@standardize_response
def register_guardian(phone, password, full_name):
    """
    Step 1: Guardian ONLY + OTP (NO User/Customer)
    """
    phone = validate_iraqi_phone(phone)
    password = validate_password(password)
    full_name = validate_full_name(full_name)
    require_guardian_columns(["full_name"])

    with guardian_customer_locks([phone]):
        guardian = get_guardian(phone)

        if guardian and guardian.get("otp_verified"):
            frappe.throw("Account already verified. Login instead")

        pwd_hash = generate_password_hash(password)

        if guardian:
            # Resend OTP only
            frappe.db.set_value(
                "Guardian",
                guardian.get("name"),
                {
                    "full_name": full_name,
                    "pending_password_hash": pwd_hash,
                },
                update_modified=False
            )
            otp = set_new_otp(guardian.get("name"))
            log_security_event("RESEND_OTP", phone)

            resp = {"status": "otp_resent", "guardian_id": guardian.get("name"), "message": "OTP sent"}
            if is_debug_mode():
                resp["otp"] = otp
            return _with_guardian_alias(resp)

        # Create Guardian ONLY (no User, no Customer)
        doc = frappe.get_doc({
            "doctype": "Guardian",
            "phone": phone,
            "full_name": full_name,
            "is_active": 0,
            "otp_verified": 0,
            "wrong_attempts": 0,
            "otp_code": None,
            "otp_expires_at": None,
            "pending_password_hash": pwd_hash,
            "user_id": None,
            "customer_id": None,
        })
        doc.flags.skip_guardian_customer_auto_create = True
        doc.insert(ignore_permissions=True)

        otp = set_new_otp(doc.name)
        log_security_event("NEW_GUARDIAN", phone)

        resp = {"status": "otp_sent", "guardian_id": doc.name, "message": "OTP sent"}
        if is_debug_mode():
            resp["otp"] = otp
        return _with_guardian_alias(resp)

@frappe.whitelist(allow_guest=True)
@standardize_response
@rate_limit(limit=AUTH_RATE_LIMIT, seconds=AUTH_RATE_WINDOW)
def send_otp(phone):
    phone = validate_iraqi_phone(phone)

    with guardian_customer_locks([phone]):
        guardian = get_guardian(phone)
        if not guardian:
            frappe.throw("Phone not found")

        if guardian.get("otp_verified"):
            frappe.throw("Already verified. Login instead")

        otp = set_new_otp(guardian.get("name"))
        log_security_event("SEND_OTP", phone)

    resp = {"status": "success", "guardian_id": guardian.get("name"), "message": "OTP sent"}
    if is_debug_mode():
        resp["otp"] = otp
    return _with_guardian_alias(resp)


@frappe.whitelist(allow_guest=True)
@standardize_response
def resend_otp(phone):
    return send_otp(phone)


@frappe.whitelist(allow_guest=True)
@standardize_response
@rate_limit(limit=AUTH_RATE_LIMIT, seconds=AUTH_RATE_WINDOW)
def verify_otp(phone, otp, password):
    """
    Step 2: Verify OTP + password => create User + Customer + activate + return token
    """
    phone = validate_iraqi_phone(phone)
    otp = validate_otp(otp)
    password = validate_password(password)

    guardian = get_guardian(phone)
    if not guardian:
        frappe.throw("Phone not found")

    if guardian.get("otp_verified"):
        frappe.throw("Already verified. Login instead")

    if guardian.get("otp_expires_at") and now_datetime() > guardian.get("otp_expires_at"):
        frappe.throw("OTP expired. Request new one")

    attempts = guardian.get("wrong_attempts", 0) or 0
    if attempts >= MAX_OTP_ATTEMPTS:
        frappe.throw("Too many attempts. Request new OTP")

    if guardian.get("otp_code") != str(otp):
        new_attempts = attempts + 1
        frappe.db.set_value("Guardian", guardian.get("name"), "wrong_attempts", new_attempts, update_modified=False)
        remaining = MAX_OTP_ATTEMPTS - new_attempts
        log_security_event("WRONG_OTP", phone)
        frappe.throw(f"Invalid OTP. {remaining} attempts left" if remaining > 0 else "Too many attempts. Request new OTP")

    stored_hash = guardian.get("pending_password_hash")
    if not stored_hash:
        frappe.throw("Missing password. Please register again")

    if not check_password_hash(stored_hash, password):
        frappe.throw("Password mismatch")

    full_name = validate_full_name(guardian.get("full_name"))

    # Resolve Customer first so verification never creates duplicate accounting parties.
    customer_id = get_or_create_customer_from_guardian(guardian)

    # Create User only after the accounting party is resolved safely.
    user_id = ensure_user(phone, password, full_name=full_name)

    # Activate + Link
    guardian_updates = {
        "user_id": user_id,
        "customer_id": customer_id,
        "otp_verified": 1,
        "is_active": 1,
        "wrong_attempts": 0,
        "otp_code": None,
        "otp_expires_at": None,
        "pending_password_hash": None,
    }
    if guardian_has_column("pending_phone_change"):
        guardian_updates["pending_phone_change"] = None

    frappe.db.set_value(
        "Guardian",
        guardian.get("name"),
        guardian_updates,
        update_modified=False
    )


    guardian_image = frappe.db.get_value(
        "Guardian",
        guardian.get("name"),
        "guardian_image"
    )
    
    if guardian_image:
        frappe.db.set_value(
            "User",
            user_id,
            "user_image",
            guardian_image,
            update_modified=False
        )
    

    api_key, api_secret = create_api_keys(user_id)
    log_security_event("OTP_VERIFIED", phone)

    response = {
        "status": "verified",
        "token": f"token {api_key}:{api_secret}",
        "guardian_id": guardian.get("name"),
        "customer_id": customer_id
    }
    return _with_guardian_alias(response)


@frappe.whitelist(allow_guest=True)
@standardize_response
@rate_limit(limit=AUTH_RATE_LIMIT, seconds=AUTH_RATE_WINDOW)
def login(phone, password):
    phone = validate_iraqi_phone(phone)
    password = str(password or "").strip()
    
    if not password:
        frappe.throw("Password is required")

    guardian = get_guardian(phone)
    if not guardian:
        frappe.throw("Phone not found")

    if not guardian.get("otp_verified") or not guardian.get("user_id"):
        frappe.throw("Verify phone first")

    user_id = guardian.get("user_id")
    
    # ✅ Use Frappe's built-in check_password function
    try:
        # This works with the __Auth table directly
        from frappe.core.doctype.user.user import check_password as frappe_check_password
        frappe_check_password(user_id, password)
        
        # Password is correct
        api_key, api_secret = create_api_keys(user_id)
        log_security_event("LOGIN", phone)
        
        return {"status": "success", "token": f"token {api_key}:{api_secret}"}
    
    except Exception as e:
        # Password is incorrect or other error
        log_security_event("FAILED_LOGIN", phone)
        frappe.throw("Invalid phone or password")


@frappe.whitelist()
@standardize_response
def complete_profile(full_name, city, address_line1, email_id=None, country=DEFAULT_COUNTRY):
    """
    Step 3: Complete profile - Update Customer + Address
    Requires: Token auth
    """
    user_id = frappe.session.user
    guardian = get_guardian_by_user(user_id)
    if not guardian:
        frappe.throw("Guardian not found")

    full_name = validate_full_name(full_name)
    city = validate_city(city)
    address_line1 = validate_address_line1(address_line1)
    country = validate_country(country)
    email_id = (email_id or "").strip()
    required_columns = ["full_name", "address_line1", "city", "country"]
    if email_id:
        required_columns.append("email_id")
    require_guardian_columns(required_columns)

    guardian_doc = frappe.get_doc("Guardian", guardian.get("name"))
    guardian_doc.full_name = full_name
    guardian_doc.city = city
    guardian_doc.address_line1 = address_line1
    if guardian_has_column("country"):
        guardian_doc.country = country
    if guardian_has_column("email_id"):
        guardian_doc.email_id = email_id
    guardian_doc.save(ignore_permissions=True)

    customer_id = guardian_doc.customer_id or get_or_create_customer_from_guardian(guardian_doc)
    sync_customer_from_guardian(guardian_doc, customer_id)
    address_id = upsert_primary_address_from_guardian(guardian_doc.as_dict(), customer_id)
    if not address_id:
        frappe.throw("Address was not created. Address line1 and city are required")

    # Update User first_name
    frappe.db.set_value("User", user_id, "first_name", full_name, update_modified=False)

    log_security_event("PROFILE_COMPLETED", guardian_doc.phone)

    response = {
        "status": "success",
        "guardian_id": guardian_doc.name,
        "customer_id": customer_id,
        "primary_address_id": address_id
    }
    return _with_guardian_alias(response)


@frappe.whitelist()
@standardize_response
def request_guardian_phone_change_otp(new_phone, user_id=None, guardian_id=None):
    """
    Request an OTP bound to the current Guardian and the requested new phone.
    The actual identity update is performed by change_guardian_phone().
    """
    new_phone = validate_iraqi_phone(new_phone)
    guardian = _resolve_guardian_for_phone_change(user_id=user_id, guardian_id=guardian_id)
    guardian_name = guardian.get("name")
    old_phone = guardian.get("phone")

    if old_phone == new_phone:
        frappe.throw("New phone is the same as the current phone")

    with guardian_customer_locks([old_phone, new_phone]):
        guardian = frappe.db.get_value("Guardian", guardian_name, "*", as_dict=True)
        if not guardian:
            frappe.throw("Guardian not found")

        old_phone = guardian.get("phone")
        if old_phone == new_phone:
            frappe.throw("New phone is the same as the current phone")

        existing_guardian = frappe.db.get_value(
            "Guardian",
            {"phone": new_phone, "name": ["!=", guardian_name]},
            "name",
        )
        if existing_guardian:
            log_customer_event(
                "PHONE_CHANGE_CONFLICT",
                guardian_name=guardian_name,
                phone=old_phone,
                details=f"new_phone={new_phone}; existing_guardian={existing_guardian}",
                level="error",
            )
            frappe.throw("Phone already in use by another account")

        customer_id = guardian.get("customer_id")
        customer_filters = {"mobile_no": new_phone}
        if customer_id:
            customer_filters["name"] = ["!=", customer_id]
        existing_customer = frappe.get_all("Customer", filters=customer_filters, fields=["name"], limit=1)
        if existing_customer:
            log_customer_event(
                "PHONE_CHANGE_CONFLICT",
                guardian_name=guardian_name,
                customer_id=customer_id,
                phone=old_phone,
                details=f"new_phone={new_phone}; existing_customer={existing_customer[0]['name']}",
                level="error",
            )
            frappe.throw("Phone already in use by another Customer")

        otp = _set_phone_change_otp(guardian_name, new_phone)
        log_security_event("PHONE_CHANGE_OTP_REQUESTED", old_phone)

    resp = {"status": "otp_sent", "guardian_id": guardian_name, "new_phone": new_phone, "message": "OTP sent"}
    if is_debug_mode():
        resp["otp"] = otp
    return _with_guardian_alias(resp)


@frappe.whitelist()
@standardize_response
def change_guardian_phone(new_phone, otp_code, user_id=None, guardian_id=None):
    """
    Atomically update Guardian.phone and linked Customer.mobile_no after
    verifying an OTP bound to the target phone number.
    """
    new_phone = validate_iraqi_phone(new_phone)
    otp_code = validate_otp(otp_code)
    guardian = _resolve_guardian_for_phone_change(user_id=user_id, guardian_id=guardian_id)
    guardian_name = guardian.get("name")
    old_phone = guardian.get("phone")

    if old_phone == new_phone:
        frappe.throw("New phone is the same as the current phone")

    _verify_phone_change_otp(guardian_name, old_phone, new_phone, otp_code)
    result = change_guardian_phone_number(guardian, new_phone)
    _clear_phone_change_otp(guardian_name)

    response = {
        "status": "success",
        "guardian_id": result.get("guardian_id"),
        "customer_id": result.get("customer_id"),
        "old_phone": result.get("old_phone"),
        "new_phone": result.get("new_phone"),
    }
    return _with_guardian_alias(response)


@frappe.whitelist()
@standardize_response
def get_guardian_profile(guardian_id):
    if frappe.session.user == "Guest":
        frappe.throw("Not permitted", frappe.PermissionError)
    if not guardian_id:
        frappe.throw("Guardian not found")

    if not frappe.db.exists("Guardian", guardian_id):
        frappe.throw("Guardian not found")

    is_admin = _current_user_is_admin()
    guardian_user = frappe.db.get_value("Guardian", guardian_id, "user_id")
    if not is_admin and guardian_user != frappe.session.user:
        frappe.throw("Not permitted", frappe.PermissionError)

    guardian_fields = existing_guardian_fields([
        "name", "phone", "full_name", "email_id", "address_line1", "city", "country", "guardian_image",
        "is_active", "otp_verified", "user_id", "customer_id",
        "creation"
    ])
    guardian = frappe.db.get_value("Guardian", guardian_id, guardian_fields, as_dict=True)

    customer = None
    orders_count = 0
    total_spent = 0.0
    addresses = []
    # Coupon usage history
    coupon_usage = frappe.db.sql("""
        SELECT 
            so.coupon_code,
            cc.coupon_code as coupon_code_str,
            COUNT(so.name) as times_used,
            COALESCE(SUM(so.discount_amount), 0) as total_saved,
            MAX(so.transaction_date) as last_used
        FROM `tabSales Order` so
        LEFT JOIN `tabCoupon Code` cc ON cc.name = so.coupon_code
        WHERE so.customer = %s
        AND so.coupon_code IS NOT NULL
        AND so.docstatus != 2
        GROUP BY so.coupon_code
        """, (guardian["customer_id"],), as_dict=True)
    

    if guardian.get("customer_id"):
        customer = frappe.db.get_value("Customer", guardian["customer_id"], [
            "name", "customer_name", "mobile_no", "email_id"
        ], as_dict=True)

        # Orders count + total spent
        orders_data = frappe.db.sql("""
            SELECT COUNT(*) as cnt, COALESCE(SUM(grand_total), 0) as total
            FROM `tabSales Order`
            WHERE customer = %s AND docstatus = 1
        """, (guardian["customer_id"],), as_dict=True)

        if orders_data:
            orders_count = orders_data[0].get("cnt") or 0
            total_spent  = orders_data[0].get("total") or 0.0

        # All addresses
        # Guarded on meta, not assumed: between a code deploy and bench migrate the
        # Custom Field does not exist yet, and naming a missing column here would 500
        # the whole guardian summary. NULL keeps the response shape stable either way.
        notes_col = (
            "a.custom_notes AS notes"
            if frappe.get_meta("Address").has_field("custom_notes")
            else "NULL AS notes"
        )
        addr_rows = frappe.db.sql(f"""
            SELECT a.name, a.address_title, a.address_type,
                   a.address_line1, a.address_line2,
                   {notes_col},
                   a.city, a.country, a.is_primary_address
            FROM `tabAddress` a
            INNER JOIN `tabDynamic Link` dl
              ON dl.parent = a.name
             AND dl.parenttype = 'Address'
             AND dl.link_doctype = 'Customer'
             AND dl.link_name = %s
            ORDER BY a.is_primary_address DESC, a.creation ASC
        """, (guardian["customer_id"],), as_dict=True)

        addresses = addr_rows or []

    frappe.response["data"] = {
        "guardian": {
            "id":             guardian.get("name"),
            "phone":          guardian.get("phone"),
            "full_name":      guardian.get("full_name"),
            "email_id":       guardian.get("email_id"),
            "address_line1":  guardian.get("address_line1"),
            "city":           guardian.get("city"),
            "country":        guardian.get("country"),
            "image":          guardian.get("guardian_image"),
            "is_active":      guardian.get("is_active"),
            "otp_verified":   guardian.get("otp_verified"),
            "user_id":        guardian.get("user_id"),
            "customer_id":    guardian.get("customer_id"),
            "joined":         str(guardian.get("creation") or ""),
        },
        "customer": {
            "id":            customer.get("name") if customer else None,
            "customer_name": customer.get("customer_name") if customer else None,
            "mobile_no":     customer.get("mobile_no") if customer else None,
            "email_id":      customer.get("email_id") if customer else None,
            "orders_count":  orders_count,
            "total_spent":   total_spent,
            "coupon_usage": [        
                {
                    "coupon_name":  c.get("coupon_code"),
                    "coupon_code":  c.get("coupon_code_str"),
                    "times_used":   c.get("times_used"),
                    "total_saved":  c.get("total_saved"),
                    "last_used":    str(c.get("last_used") or ""),
                }
                for c in coupon_usage
            ],
        } if customer else None,
        "addresses": [
            {
                "id":              a.get("name"),
                "title":           a.get("address_title"),
                "type":            a.get("address_type"),
                "address_line1":   a.get("address_line1"),
                "address_line2":   a.get("address_line2"),
                "city":            a.get("city"),
                "country":         a.get("country"),
                "is_primary":      bool(a.get("is_primary_address")),
            }
            for a in addresses
        ]
    }


@frappe.whitelist(allow_guest=True)
@standardize_response
@rate_limit(limit=AUTH_RATE_LIMIT, seconds=AUTH_RATE_WINDOW)
def forgot_password(phone):
    phone = validate_iraqi_phone(phone)

    guardian = get_guardian(phone)
    if not guardian:
        frappe.throw("Phone not found")

    if not guardian.get("otp_verified") or not guardian.get("user_id"):
        frappe.throw("Account not verified. Verify first")

    otp = set_new_otp(guardian.get("name"))
    log_security_event("FORGOT_PASSWORD_OTP", phone)

    resp = {"status": "success", "message": "OTP sent for password reset"}
    if is_debug_mode():
        resp["otp"] = otp
    return resp

@frappe.whitelist(allow_guest=True)
@standardize_response
@rate_limit(limit=AUTH_RATE_LIMIT, seconds=AUTH_RATE_WINDOW)
def reset_password(phone, otp, new_password):
    phone = validate_iraqi_phone(phone)
    otp = validate_otp(otp)
    new_password = validate_password(new_password)

    guardian = get_guardian(phone)
    if not guardian:
        frappe.throw("Phone not found")

    if not guardian.get("otp_verified") or not guardian.get("user_id"):
        frappe.throw("Account not verified. Verify first")

    if guardian.get("pending_phone_change"):
        frappe.throw("This OTP is for phone change. Request a password reset OTP")

    if guardian.get("otp_expires_at") and now_datetime() > guardian.get("otp_expires_at"):
        frappe.throw("OTP expired. Request new one")

    if guardian.get("otp_code") != str(otp):
        frappe.throw("Invalid OTP")

    user_doc = frappe.get_doc("User", guardian.get("user_id"))
    user_doc.new_password = new_password
    user_doc.save(ignore_permissions=True)

    # IMPORTANT: Update pending_password_hash too (for login verification)
    pwd_hash = generate_password_hash(new_password)
    guardian_updates = {
        "pending_password_hash": pwd_hash,
        "wrong_attempts": 0,
        "otp_code": None,
        "otp_expires_at": None,
    }
    if guardian_has_column("pending_phone_change"):
        guardian_updates["pending_phone_change"] = None

    frappe.db.set_value(
        "Guardian",
        guardian.get("name"),
        guardian_updates,
        update_modified=False
    )

    log_security_event("PASSWORD_RESET", phone)
    return {"status": "success", "message": "Password reset successfully"}

@frappe.whitelist()
@standardize_response
def logout():
    """
    Logout endpoint (token-based)
    
    Client should:
    1. Call this endpoint with token
    2. Delete token from mobile storage
    3. Redirect to login screen
    
    Note: No server-side session to destroy (stateless)
    """
    user_id = frappe.session.user
    guardian = get_guardian_by_user(user_id)
    
    if not guardian:
        frappe.throw("Guardian not found")
    
    log_security_event("LOGOUT", guardian.get("phone"))
    
    return {
        "status": "success",
        "message": "Logged out successfully"
    }
