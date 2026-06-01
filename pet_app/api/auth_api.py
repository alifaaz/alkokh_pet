import frappe
import json
from frappe.auth import LoginManager
from frappe.utils import random_string
from frappe.utils.password import get_decrypted_password
from frappe import _
from frappe.integrations.oauth2 import get_oauth_server
from frappe.oauth import generate_json_error_response
from oauthlib.oauth2 import FatalClientError, OAuth2Error
from urllib.parse import urlencode
from pet_app.api.response import standardize_response

OAUTH_TOKEN_ENDPOINT = "/api/method/pet_app.api.auth_api.login_and_get_oauth_token"


def _resolve_oauth_username(username):
    username = (username or "").strip()
    if frappe.db.exists("User", username):
        return username

    guardian_user = frappe.db.get_value(
        "Guardian",
        {
            "phone": username,
            "otp_verified": 1,
            "is_active": 1,
        },
        "user_id",
    )
    if guardian_user:
        return guardian_user

    return username

@frappe.whitelist(allow_guest=True)
@standardize_response
def login_and_get_session(usr, pwd):
    login_manager = LoginManager()

    try:
        login_manager.authenticate(user=usr, pwd=pwd)
        login_manager.post_login()
    except Exception:
        frappe.throw(_("Invalid login"), frappe.AuthenticationError)

    return {
        "sid": frappe.session.sid,
        "user": frappe.session.user,
        "full_name": frappe.db.get_value("User", frappe.session.user, "full_name"),
    }


@frappe.whitelist(allow_guest=True)
@standardize_response
def login_and_get_api_keys(usr, pwd):
    login_manager = LoginManager()
    
    try:
        login_manager.authenticate(user=usr, pwd=pwd)
        login_manager.post_login()
    except Exception:
        frappe.throw(_("Invalid login"), frappe.AuthenticationError)

    user = frappe.session.user
    user_doc = frappe.get_doc("User", user)

    # إذا ما عنده api_key
    if not user_doc.api_key:
        user_doc.api_key = random_string(15)
        user_doc.save(ignore_permissions=True)

    # api_secret قراءة أو توليد
    api_secret = get_decrypted_password("User", user_doc.name, "api_secret", raise_exception=False)

    if not api_secret:
        api_secret = random_string(30)
        frappe.utils.password.update_password(api_secret, "User", user_doc.name, "api_secret")

    return {
        "api_key": user_doc.api_key,
        "api_secret": api_secret,
        "user": user_doc.name,
        "full_name": user_doc.full_name,
    }


@frappe.whitelist(allow_guest=True, methods=["POST"])
def login_and_get_oauth_token(
    usr=None,
    pwd=None,
    username=None,
    password=None,
    client_id=None,
    scope=None,
    grant_type=None,
):
    """
    Login a registered user and return a Frappe OAuth2 bearer token.

    This wraps frappe.integrations.oauth2.get_token's password grant flow so
    frontend apps can request an OAuth token from a pet_app endpoint.
    """
    username = _resolve_oauth_username(username or usr or frappe.form_dict.get("username"))
    password = password or pwd or frappe.form_dict.get("password")
    client_id = (client_id or frappe.form_dict.get("client_id") or "").strip()
    scope = (scope or frappe.form_dict.get("scope") or "").strip()
    grant_type = (grant_type or frappe.form_dict.get("grant_type") or "password").strip()

    if grant_type != "password":
        frappe.throw(_("Only password grant_type is supported"))

    if not username or not password:
        frappe.throw(_("Username and password are required"), frappe.AuthenticationError)

    if not client_id:
        frappe.throw(_("OAuth client_id is required"))

    login_manager = LoginManager()
    try:
        login_manager.authenticate(user=username, pwd=password)
    except Exception:
        frappe.throw(_("Invalid login"), frappe.AuthenticationError)

    if login_manager.user == "Guest":
        frappe.throw(_("Invalid login"), frappe.AuthenticationError)

    token_request = {
        "grant_type": grant_type,
        "username": login_manager.user,
        "password": password,
        "client_id": client_id,
    }
    if scope:
        token_request["scope"] = scope

    current_user = frappe.session.user
    try:
        frappe.set_user(login_manager.user)
        _headers, body, status = get_oauth_server().create_token_response(
            uri=frappe.request.url,
            http_method=frappe.request.method,
            body=urlencode(token_request),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            credentials=getattr(frappe.flags, "oauth_credentials", None),
        )
    except (FatalClientError, OAuth2Error) as e:
        return generate_json_error_response(e)
    finally:
        frappe.set_user(current_user)

    token_response = frappe._dict(json.loads(body or "{}"))

    if token_response.error:
        frappe.local.response = token_response
        frappe.local.response["http_status_code"] = status or 400
        return

    user = frappe.db.get_value("User", login_manager.user, ["name", "full_name"], as_dict=True)
    token_response.update(
        {
            "user": user.name if user else username,
            "full_name": user.full_name if user else None,
        }
    )

    frappe.local.response = token_response
    frappe.local.response["http_status_code"] = status or 200
    return


def set_cors_for_oauth_token_endpoint():
    if (
        frappe.conf.allow_cors == "*"
        or not frappe.local.request
        or not frappe.local.request.headers.get("Origin")
        or not frappe.request.path.startswith(OAUTH_TOKEN_ENDPOINT)
        or frappe.request.method not in ("POST", "OPTIONS")
    ):
        return

    allowed = frappe.get_cached_value(
        "OAuth Settings",
        "OAuth Settings",
        "allowed_public_client_origins",
    )
    if not allowed:
        return

    allowed = allowed.strip().splitlines()
    frappe.local.allow_cors = "*" if "*" in allowed else allowed
