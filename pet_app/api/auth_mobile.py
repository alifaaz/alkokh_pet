"""
Pet App Authentication System - PRODUCTION READY
Static Token (api_key:api_secret) | No Sessions | Mobile Friendly
"""

import frappe
import random
import re
from frappe.utils import add_to_date, now_datetime
from frappe.utils.password import get_decrypted_password, check_password


# ====== CONFIG ======
DEBUG_MODE = frappe.conf.get("development_mode", False)
OTP_EXPIRY_MINUTES = 10
MAX_OTP_ATTEMPTS = 5


# ====== VALIDATORS ======

def validate_iraqi_phone(phone):
    """Validate Iraqi phone: 07XXXXXXXXX or 9647XXXXXXXXX"""
    phone = phone.strip().replace(" ", "").replace("-", "")

    # 07 + 9 digits
    if re.match(r"^07\d{9}$", phone):
        return phone

    # 9647 + 9 digits → 07XXXXXXXXX
    if re.match(r"^9647\d{9}$", phone):
        return "0" + phone[3:]

    frappe.throw("Invalid phone. Use: 07XXXXXXXXX or 9647XXXXXXXXX")


def validate_password(password):
    """Validate password: 6-50 chars, alphanumeric + special chars"""
    if not 6 <= len(password) <= 50:
        frappe.throw("Password: 6-50 characters")

    if not re.match(r'^[a-zA-Z0-9!@#$%^&*()_+\-=\[\]{};:"\\|,.<>/?]+$', password):
        frappe.throw("Invalid password characters")

    return password


def validate_full_name(name):
    """Validate full name: 1-100 chars"""
    name = name.strip()
    if not name or len(name) > 100:
        frappe.throw("Name: 1-100 characters")
    return name


# ====== HELPERS ======

def generate_otp():
    """Generate 6-digit OTP"""
    return "".join(str(random.randint(0, 9)) for _ in range(6))


def set_new_otp(guardian_name):
    """Generate and set new OTP with expiry"""
    otp = generate_otp()
    frappe.db.set_value(
        "Guardian",
        guardian_name,
        {
            "otp_code": otp,
            "otp_expires_at": add_to_date(now_datetime(), minutes=OTP_EXPIRY_MINUTES),
            "wrong_attempts": 0,
        },
    )
    return otp


def get_guardian(phone):
    """Get guardian by phone"""
    return frappe.db.get_value(
        "Guardian", {"phone": phone}, "*", as_dict=True
    )


def activate_account(guardian_name, user_id):
    """Activate guardian and user"""
    frappe.db.set_value(
        "Guardian",
        guardian_name,
        {
            "otp_verified": 1,
            "is_active": 1,
            "wrong_attempts": 0,
        },
    )
    frappe.db.set_value("User", user_id, "enabled", 1)


def create_api_keys(user_id):
    """Create static API keys if missing and return REAL secret (not masked)"""
    user_doc = frappe.get_doc("User", user_id)

    # create if missing
    if not user_doc.api_key:
        user_doc.api_key = frappe.generate_hash(length=15)
        user_doc.api_secret = frappe.generate_hash(length=30)
        user_doc.save(ignore_permissions=True)

    # get decrypted secret (Password field – normally masked)
    api_secret = get_decrypted_password(
        "User", user_doc.name, "api_secret", raise_exception=False
    )

    # if for some reason empty → regenerate once
    if not api_secret:
        user_doc.api_secret = frappe.generate_hash(length=30)
        user_doc.save(ignore_permissions=True)
        api_secret = get_decrypted_password(
            "User", user_doc.name, "api_secret", raise_exception=False
        )

    return user_doc.api_key, api_secret


def log_security_event(event_type, phone, message=""):
    """Log security events (no sensitive data in production logs)"""
    if DEBUG_MODE and message:
        frappe.logger().info(f"[{event_type}] {phone}: {message}")
    else:
        frappe.logger().warning(f"[{event_type}] {phone}")


# ====== AUTH ENDPOINTS ======

@frappe.whitelist(allow_guest=True)
def register_guardian(full_name, phone, password):
    """Register new account or resend OTP if unverified"""
    full_name = validate_full_name(full_name)
    phone = validate_iraqi_phone(phone)
    password = validate_password(password)

    existing = get_guardian(phone)

    # Account exists and verified
    if existing and existing.get("otp_verified"):
        frappe.throw("Account exists. Login instead")

    # Account exists but unverified - resend OTP
    if existing:
        otp = set_new_otp(existing.get("name"))
        log_security_event("RESEND_OTP", phone)
        return {
            "status": "otp_resent",
            "message": "OTP sent to your phone",
            "guardian_id": existing.get("name"),
        }

    # New account
    email = f"{phone}@petapp.local"
    user = frappe.get_doc(
        {
            "doctype": "User",
            "email": email,
            "first_name": full_name,
            "enabled": 0,
            "send_welcome_email": 0,
            "roles": [{"role": "Guest"}],
        }
    )
    user.insert(ignore_permissions=True)
    user.new_password = password
    user.save(ignore_permissions=True)

    guardian = frappe.get_doc(
        {
            "doctype": "Guardian",
            "full_name": full_name,
            "phone": phone,
            "is_active": 0,
            "otp_verified": 0,
            "user_id": user.name,
            "wrong_attempts": 0,
        }
    )
    guardian.insert(ignore_permissions=True)

    otp = set_new_otp(guardian.name)
    log_security_event("NEW_ACCOUNT", phone)

    return {
        "status": "otp_sent",
        "message": "Account created. OTP sent to your phone",
        "guardian_id": guardian.name,
    }


@frappe.whitelist(allow_guest=True)
def verify_otp(phone, otp):
    """Verify OTP and activate account (NO session login, token only)"""
    phone = validate_iraqi_phone(phone)

    if not re.match(r"^\d{6}$", str(otp)):
        frappe.throw("Invalid OTP format")

    guardian = get_guardian(phone)
    if not guardian:
        frappe.throw("Phone not found")

    if guardian.get("otp_verified"):
        frappe.throw("Already verified. Login instead")

    if guardian.get("otp_expires_at") and now_datetime() > guardian.get(
        "otp_expires_at"
    ):
        frappe.throw("OTP expired. Request new one")

    attempts = guardian.get("wrong_attempts", 0) or 0
    if attempts >= MAX_OTP_ATTEMPTS:
        frappe.throw("Too many attempts. Request new OTP")

    if guardian.get("otp_code") != str(otp):
        new_attempts = attempts + 1
        frappe.db.set_value(
            "Guardian", guardian.get("name"), {"wrong_attempts": new_attempts}
        )
        remaining = MAX_OTP_ATTEMPTS - new_attempts
        log_security_event("WRONG_OTP", phone)
        frappe.throw(
            f"Invalid OTP. {remaining} attempts left"
            if remaining > 0
            else "Too many attempts. Request new OTP"
        )

    # Activate account (no login_as)
    activate_account(guardian.get("name"), guardian.get("user_id"))

    # Static token for mobile
    api_key, api_secret = create_api_keys(guardian.get("user_id"))
    log_security_event("OTP_VERIFIED", phone)

    return {
        "message": "verified",
        "token": f"token {api_key}:{api_secret}",
    }


@frappe.whitelist(allow_guest=True)
def resend_otp(phone):
    """Resend OTP"""
    phone = validate_iraqi_phone(phone)

    guardian = get_guardian(phone)
    if not guardian:
        frappe.throw("Phone not found")

    if guardian.get("otp_verified"):
        frappe.throw("Already verified. Login instead")

    otp = set_new_otp(guardian.get("name"))
    log_security_event("RESEND_OTP", phone)

    return {
        "message": "otp_resent",
        "status": "success",
    }


@frappe.whitelist(allow_guest=True)
def login(phone, password):
    """Login with phone + password (Token only, no session)"""
    phone = validate_iraqi_phone(phone)
    password = validate_password(password)

    guardian = get_guardian(phone)
    if not guardian:
        frappe.throw("Phone not found")

    if not guardian.get("otp_verified"):
        frappe.throw("Verify phone first")

    user_id = guardian.get("user_id")

    # Password check without creating session
    try:
        check_password(user_id, password)
    except frappe.AuthenticationError:
        log_security_event("FAILED_LOGIN", phone)
        frappe.throw("Invalid password")

    api_key, api_secret = create_api_keys(user_id)
    log_security_event("LOGIN", phone)

    return {
        "message": "login_success",
        "token": f"token {api_key}:{api_secret}",
    }


@frappe.whitelist()
def logout():
    """Mobile logout: client should just delete stored token.
    Here we only respond OK (no server session)."""
    return {"message": "logged_out"}


@frappe.whitelist()
def get_profile():
    """Get current user profile (requires token auth)"""
    # frappe.session.user will be set by Token Auth (api_key:api_secret)
    guardian = frappe.db.get_value(
        "Guardian", {"user_id": frappe.session.user}, "*", as_dict=True
    )

    if not guardian:
        frappe.throw("Not found")

    return {
        "full_name": guardian.get("full_name"),
        "phone": guardian.get("phone"),
        "is_active": guardian.get("is_active"),
        "otp_verified": guardian.get("otp_verified"),
        "created": guardian.get("creation"),
    }


@frappe.whitelist()
def update_profile(full_name=None, password=None):
    """Update profile (requires token auth)"""
    guardian = frappe.get_doc("Guardian", {"user_id": frappe.session.user})
    user_doc = frappe.get_doc("User", guardian.user_id)

    if full_name:
        guardian.full_name = validate_full_name(full_name)

    if password:
        password = validate_password(password)
        user_doc.new_password = password
        user_doc.save(ignore_permissions=True)

    guardian.save(ignore_permissions=True)
    log_security_event("PROFILE_UPDATE", guardian.phone)

    return {
        "message": "updated",
        "full_name": guardian.full_name,
    }
@frappe.whitelist(allow_guest=True)
def forgot_password(phone):
    """Send OTP for password reset (does NOT activate account)"""
    phone = validate_iraqi_phone(phone)

    guardian = get_guardian(phone)
    if not guardian:
        frappe.throw("Phone not found")

    # Generate new OTP for reset
    otp = set_new_otp(guardian.get("name"))
    log_security_event("FORGOT_PASSWORD_OTP", phone)

    return {
        "status": "reset_otp_sent",
        "message": "OTP sent for password reset"
    }
@frappe.whitelist(allow_guest=True)
def reset_password(phone, otp, new_password):
    """Reset password using OTP (no login, no session)"""
    phone = validate_iraqi_phone(phone)
    new_password = validate_password(new_password)

    guardian = get_guardian(phone)
    if not guardian:
        frappe.throw("Phone not found")

    # Validate OTP
    if not re.match(r'^\d{6}$', str(otp)):
        frappe.throw("Invalid OTP format")

    if guardian.get("otp_expires_at") and now_datetime() > guardian.get("otp_expires_at"):
        frappe.throw("OTP expired. Request new one")

    if guardian.get("otp_code") != str(otp):
        frappe.throw("Invalid OTP")

    # Update password
    user_doc = frappe.get_doc("User", guardian.get("user_id"))
    user_doc.new_password = new_password
    user_doc.save(ignore_permissions=True)

    # Reset attempts & clear OTP
    frappe.db.set_value("Guardian", guardian.get("name"), {
        "wrong_attempts": 0,
        "otp_code": None,
        "otp_expires_at": None
    })

    log_security_event("PASSWORD_RESET", phone)

    return {
        "message": "password_reset_success"
    }
