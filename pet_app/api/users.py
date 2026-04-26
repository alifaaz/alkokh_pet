import json

import frappe
from frappe import _
from frappe.utils.password import update_password


ALLOWED_USER_ADMIN_ROLES = {"System Manager", "Healthcare Administrator", "Users"}


def _require_user_admin_access():
    if frappe.session.user == "Administrator":
        return

    if set(frappe.get_roles(frappe.session.user) or []).isdisjoint(ALLOWED_USER_ADMIN_ROLES):
        frappe.throw("Not permitted", frappe.PermissionError)


def _log_user_admin_error(message, **context):
    details = ", ".join(f"{key}={value}" for key, value in context.items() if value is not None)
    frappe.log_error(
        title="pet_app.api.users.change_user_password",
        message=f"{message}{': ' + details if details else ''}",
    )

@frappe.whitelist()
def get_users_with_role_profile(limit_start=0, limit_page_length=20, enabled_only=0, filters=None):
    _require_user_admin_access()

    limit_start = int(limit_start)
    limit_page_length = int(limit_page_length)
    enabled_only = int(enabled_only)

    # ✅ search filters
    filters = json.loads(filters) if filters else []

    if enabled_only:
        filters.append(["enabled", "=", 1])

    # hide system users
    filters.append(["name", "not like", "%@petapp.local"])

    users = frappe.get_all(
        "User",
        filters=filters,
        fields=[
            "name",
            "full_name",
            "email",
            "username",
            "user_image",
            "enabled",
            "last_login"
        ],
        limit_start=limit_start,
        limit_page_length=limit_page_length,
        order_by="name asc",
    )

    if not users:
        frappe.response["data"] = []
        return

    user_ids = [u["name"] for u in users]

    rows = frappe.get_all(
        "User Role Profile",
        filters={
            "parenttype": "User",
            "parentfield": "role_profiles",
            "parent": ["in", user_ids]
        },
        fields=["parent", "role_profile"]
    )

    user_to_profiles = {}
    for r in rows:
        user_to_profiles.setdefault(r["parent"], []).append(r["role_profile"])

    out = []
    for u in users:
        profiles = sorted(set(user_to_profiles.get(u["name"], [])))

        # status
        if u.get("enabled") == 0:
            status = "Inactive"
        elif not u.get("last_login"):
            status = "Pending"
        else:
            status = "Active"

        out.append({
            "name": u["name"],
            "full_name": u.get("full_name"),
            "email": u.get("email"),
            "username": u.get("username"),
            "user_image": u.get("user_image"),
            "role_profile": profiles[0] if profiles else None,
            "status": status,
            "enabled": u.get("enabled"),
        })

    frappe.response["data"] = out


@frappe.whitelist()
def get_all_role_profiles_with_roles(limit_page_length=999):
    _require_user_admin_access()

    limit_page_length = int(limit_page_length)
    aws_id = frappe.session.user

    profiles = frappe.get_all(
        "Role Profile",
        fields=["name", "creation"],
        limit_page_length=limit_page_length
    )

    if not profiles:
        frappe.response["data"] = []
        return {"data": []}

    profile_names = [p["name"] for p in profiles]

    rows = frappe.get_all(
        "Has Role",
        filters={
            "parenttype": "Role Profile",
            "parentfield": "roles",
            "parent": ["in", profile_names],
        },
        fields=["parent", "role"],
        limit_page_length=999999
    )

    roles_map = {}
    for r in rows:
        roles_map.setdefault(r["parent"], []).append(r["role"])

    STANDARD_PROFILES = {"Accounts", "Inventory", "Manufacturing", "Purchase", "Sales"}
    HIDDEN_PROFILES = set()
    HIDE_EMPTY_ROLES = True
    HIDE_STANDARD = True

    out = []
    for p in profiles:
        profile_name = p["name"]
        roles = sorted(set(roles_map.get(profile_name, [])))

        if HIDE_STANDARD and profile_name in STANDARD_PROFILES:
            continue
        if profile_name in HIDDEN_PROFILES:
            continue
        if HIDE_EMPTY_ROLES and not roles:
            continue

        out.append({
            "name": aws_id,
            "role_profile": profile_name,
            "creation": p["creation"],
            "roles": roles
        })

    out.sort(key=lambda x: x["role_profile"])
    frappe.response["data"] = out
    return


@frappe.whitelist(methods=["POST"])
def change_user_password(user: str, new_password: str):
    """Change another user's password.

    Example:
        curl -X POST https://SITE/api/method/pet_app.api.users.change_user_password \
          -H "Authorization: token KEY:SECRET" \
          -H "Content-Type: application/json" \
          -d '{"user":"doctor@example.com","new_password":"NewStrongPass123!"}'
    """
    user = (user or "").strip()
    new_password = new_password or ""
    current_user = frappe.session.user

    if not user:
        _log_user_admin_error("Missing user parameter", current_user=current_user)
        frappe.throw(_("User is required"))

    if not new_password:
        _log_user_admin_error("Missing new_password parameter", current_user=current_user, target_user=user)
        frappe.throw(_("Password is required"))

    try:
        _require_user_admin_access()
    except frappe.PermissionError:
        _log_user_admin_error("Permission denied", current_user=current_user, target_user=user)
        frappe.throw(
            _("You are not permitted to change another user's password."),
            frappe.PermissionError,
        )

    if not frappe.db.exists("User", user):
        _log_user_admin_error("Target user does not exist", current_user=current_user, target_user=user)
        frappe.throw(_("User {0} does not exist").format(user))

    update_password(user, new_password)

    return {"message": _("Password updated successfully")}
