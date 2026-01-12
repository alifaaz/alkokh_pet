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
from frappe.utils import add_to_date, now_datetime
from frappe.utils.password import get_decrypted_password, check_password
from werkzeug.security import generate_password_hash, check_password_hash


# ====== CONFIG ======
DEBUG_MODE = frappe.conf.get("development_mode", False)
OTP_EXPIRY_MINUTES = 10
MAX_OTP_ATTEMPTS = 5
DEFAULT_COUNTRY = "Iraq"
DEFAULT_ADDRESS_TYPE = "Billing"


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


def validate_otp(otp: str) -> str:
    otp = str(otp or "").strip()
    if not re.match(r"^\d{6}$", otp):
        frappe.throw("Invalid OTP format (must be 6 digits)")
    return otp


# ====== HELPERS ======

def log_security_event(event_type: str, phone: str, message: str = ""):
    if DEBUG_MODE and message:
        frappe.logger().info(f"[AUTH] [{event_type}] {phone}: {message}")
    else:
        frappe.logger().warning(f"[AUTH] [{event_type}] {phone}")


def generate_otp() -> str:
    return "".join(str(random.randint(0, 9)) for _ in range(6))


def get_guardian(phone: str):
    return frappe.db.get_value("Guardian", {"phone": phone}, "*", as_dict=True)


def set_new_otp(guardian_name: str) -> str:
    otp = generate_otp()
    frappe.db.set_value(
        "Guardian",
        guardian_name,
        {
            "otp_code": otp,
            "otp_expires_at": add_to_date(now_datetime(), minutes=OTP_EXPIRY_MINUTES),
            "wrong_attempts": 0,
        },
        update_modified=False
    )
    return otp


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


def ensure_customer(phone: str, customer_name: str = None) -> str:
    """
    Create Customer ONLY after OTP verification.
    Bypass validations for placeholder creation.
    """
    customer = frappe.get_doc({
        "doctype": "Customer",
        "customer_name": customer_name or phone,
        "mobile_no": phone,
        "customer_type": "Individual",
    })
    customer.flags.ignore_validate = True
    customer.insert(ignore_permissions=True, ignore_mandatory=True)
    return customer.name


def upsert_primary_address(customer_id: str, full_name: str, city: str, address_line1: str) -> str:
    """
    Create/Update primary Address linked to Customer.
    """
    existing = frappe.db.sql(
        """
        SELECT a.name
        FROM `tabAddress` a
        INNER JOIN `tabDynamic Link` dl
          ON dl.parent = a.name
         AND dl.parenttype = 'Address'
         AND dl.link_doctype = 'Customer'
         AND dl.link_name = %s
        WHERE a.is_primary_address = 1
        LIMIT 1
        """,
        (customer_id,),
        as_dict=False
    )

    if existing:
        addr_name = existing[0][0]
        frappe.db.set_value(
            "Address",
            addr_name,
            {
                "address_title": full_name,
                "address_type": DEFAULT_ADDRESS_TYPE,
                "address_line1": address_line1,
                "city": city,
                "country": DEFAULT_COUNTRY,
                "is_primary_address": 1,
            },
            update_modified=False
        )
        return addr_name

    address = frappe.get_doc({
        "doctype": "Address",
        "address_title": full_name,
        "address_type": DEFAULT_ADDRESS_TYPE,
        "address_line1": address_line1,
        "city": city,
        "country": DEFAULT_COUNTRY,
        "is_primary_address": 1,
        "links": [{"link_doctype": "Customer", "link_name": customer_id}],
    })
    address.insert(ignore_permissions=True)
    return address.name


def get_guardian_by_user(user_id: str):
    return frappe.db.get_value("Guardian", {"user_id": user_id}, "*", as_dict=True)


# ====== ENDPOINTS ======

@frappe.whitelist(allow_guest=True)
def register_guardian(phone, password):
    """
    Step 1: Guardian ONLY + OTP (NO User/Customer)
    """
    phone = validate_iraqi_phone(phone)
    password = validate_password(password)

    guardian = get_guardian(phone)

    if guardian and guardian.get("otp_verified"):
        frappe.throw("Account already verified. Login instead")

    pwd_hash = generate_password_hash(password)

    if guardian:
        # Resend OTP only
        frappe.db.set_value(
            "Guardian",
            guardian.get("name"),
            {"pending_password_hash": pwd_hash},
            update_modified=False
        )
        otp = set_new_otp(guardian.get("name"))
        log_security_event("RESEND_OTP", phone)

        resp = {"status": "otp_resent", "guardian_id": guardian.get("name"), "message": "OTP sent"}
        if DEBUG_MODE:
            resp["otp"] = otp
        return resp

    # Create Guardian ONLY (no User, no Customer)
    doc = frappe.get_doc({
        "doctype": "Guardian",
        "phone": phone,
        "is_active": 0,
        "otp_verified": 0,
        "wrong_attempts": 0,
        "otp_code": None,
        "otp_expires_at": None,
        "pending_password_hash": pwd_hash,
        "user_id": None,
        "customer_id": None,
    })
    doc.insert(ignore_permissions=True)

    otp = set_new_otp(doc.name)
    log_security_event("NEW_GUARDIAN", phone)

    resp = {"status": "otp_sent", "guardian_id": doc.name, "message": "OTP sent"}
    if DEBUG_MODE:
        resp["otp"] = otp
    return resp


@frappe.whitelist(allow_guest=True)
def resend_otp(phone):
    phone = validate_iraqi_phone(phone)

    guardian = get_guardian(phone)
    if not guardian:
        frappe.throw("Phone not found")

    if guardian.get("otp_verified"):
        frappe.throw("Already verified. Login instead")

    otp = set_new_otp(guardian.get("name"))
    log_security_event("RESEND_OTP", phone)

    resp = {"status": "success", "message": "OTP sent"}
    if DEBUG_MODE:
        resp["otp"] = otp
    return resp


@frappe.whitelist(allow_guest=True)
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

    # Create User + Customer NOW (only after OTP verified)
    user_id = ensure_user(phone, password, full_name=None)
    customer_id = ensure_customer(phone, customer_name=phone)

    # Activate + Link
    frappe.db.set_value(
        "Guardian",
        guardian.get("name"),
        {
            "user_id": user_id,
            "customer_id": customer_id,
            "otp_verified": 1,
            "is_active": 1,
            "wrong_attempts": 0,
            "otp_code": None,
            "otp_expires_at": None,
            "pending_password_hash": None,
        },
        update_modified=False
    )

    api_key, api_secret = create_api_keys(user_id)
    log_security_event("OTP_VERIFIED", phone)

    return {
        "status": "verified",
        "token": f"token {api_key}:{api_secret}",
        "guardian_id": guardian.get("name"),
        "customer_id": customer_id
    }


@frappe.whitelist(allow_guest=True)
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
def complete_profile(full_name, city, address_line1, email_id=None):
    """
    Step 3: Complete profile - Update Customer + Address
    Requires: Token auth
    """
    user_id = frappe.session.user
    guardian = get_guardian_by_user(user_id)
    if not guardian:
        frappe.throw("Guardian not found")

    customer_id = guardian.get("customer_id")
    if not customer_id:
        frappe.throw("Customer not linked. Verify OTP first")

    full_name = validate_full_name(full_name)
    city = validate_city(city)
    address_line1 = validate_address_line1(address_line1)

    # Update Customer
    cust_updates = {"customer_name": full_name}
    if email_id:
        cust_updates["email_id"] = (email_id or "").strip()

    frappe.db.set_value("Customer", customer_id, cust_updates, update_modified=False)

    # Create/Update Address
    address_id = upsert_primary_address(customer_id, full_name, city, address_line1)

    # Sync Guardian (if fields exist)
    gmeta = frappe.get_meta("Guardian")
    g_updates = {}
    if gmeta.has_field("full_name"):
        g_updates["full_name"] = full_name
    if gmeta.has_field("city"):
        g_updates["city"] = city
    if gmeta.has_field("address_line1"):
        g_updates["address_line1"] = address_line1
    if g_updates:
        frappe.db.set_value("Guardian", guardian.get("name"), g_updates, update_modified=False)

    # Update User first_name
    frappe.db.set_value("User", user_id, "first_name", full_name, update_modified=False)

    log_security_event("PROFILE_COMPLETED", guardian.get("phone"))

    return {
        "status": "success",
        "guardian_id": guardian.get("name"),
        "customer_id": customer_id,
        "primary_address_id": address_id
    }


@frappe.whitelist()
def get_profile():
    """
    Get current user profile
    Requires: Token auth
    """
    user_id = frappe.session.user
    guardian = get_guardian_by_user(user_id)
    if not guardian:
        frappe.throw("Not found")

    customer = None
    address = None

    if guardian.get("customer_id"):
        customer = frappe.db.get_value("Customer", guardian.get("customer_id"), "*", as_dict=True)

        # Fetch primary address
        addr_rows = frappe.db.sql(
            """
            SELECT a.name
            FROM `tabAddress` a
            INNER JOIN `tabDynamic Link` dl
              ON dl.parent = a.name
             AND dl.parenttype = 'Address'
             AND dl.link_doctype = 'Customer'
             AND dl.link_name = %s
            WHERE a.is_primary_address = 1
            LIMIT 1
            """,
            (guardian.get("customer_id"),),
            as_dict=False
        )
        if addr_rows:
            address = frappe.db.get_value("Address", addr_rows[0][0], "*", as_dict=True)

    return {
        "guardian": {
            "id": guardian.get("name"),
            "phone": guardian.get("phone"),
            "full_name": guardian.get("full_name"),
            "is_active": guardian.get("is_active"),
            "otp_verified": guardian.get("otp_verified"),
            "user_id": guardian.get("user_id"),
            "customer_id": guardian.get("customer_id"),
        },
        "customer": {
            "id": customer.get("name") if customer else None,
            "customer_name": customer.get("customer_name") if customer else None,
            "mobile_no": customer.get("mobile_no") if customer else None,
            "email_id": customer.get("email_id") if customer else None,
        } if customer else None,
        "primary_address": {
            "id": address.get("name") if address else None,
            "address_line1": address.get("address_line1") if address else None,
            "city": address.get("city") if address else None,
            "country": address.get("country") if address else None,
        } if address else None
    }


@frappe.whitelist(allow_guest=True)
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
    if DEBUG_MODE:
        resp["otp"] = otp
    return resp


@frappe.whitelist(allow_guest=True)
def reset_password(phone, otp, new_password):
    phone = validate_iraqi_phone(phone)
    otp = validate_otp(otp)
    new_password = validate_password(new_password)

    guardian = get_guardian(phone)
    if not guardian:
        frappe.throw("Phone not found")

    if not guardian.get("otp_verified") or not guardian.get("user_id"):
        frappe.throw("Account not verified. Verify first")

    if guardian.get("otp_expires_at") and now_datetime() > guardian.get("otp_expires_at"):
        frappe.throw("OTP expired. Request new one")

    if guardian.get("otp_code") != str(otp):
        frappe.throw("Invalid OTP")

    user_doc = frappe.get_doc("User", guardian.get("user_id"))
    user_doc.new_password = new_password
    user_doc.save(ignore_permissions=True)

    # IMPORTANT: Update pending_password_hash too (for login verification)
    pwd_hash = generate_password_hash(new_password)
    frappe.db.set_value(
        "Guardian",
        guardian.get("name"),
        {
            "pending_password_hash": pwd_hash,
            "wrong_attempts": 0,
            "otp_code": None,
            "otp_expires_at": None
        },
        update_modified=False
    )

    log_security_event("PASSWORD_RESET", phone)
    return {"status": "success", "message": "Password reset successfully"}


@frappe.whitelist()
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