import frappe
from frappe.auth import LoginManager
from frappe.utils import random_string
from frappe.utils.password import get_decrypted_password
from frappe import _

@frappe.whitelist(allow_guest=True)
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
