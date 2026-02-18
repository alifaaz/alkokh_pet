import frappe

@frappe.whitelist()
def get_users_with_role_profile(limit_start=0, limit_page_length=20, enabled_only=0):
    allowed = {"System Manager", "Healthcare Administrator"}
    if set(frappe.get_roles(frappe.session.user) or []).isdisjoint(allowed):
        frappe.throw("Not permitted", frappe.PermissionError)

    limit_start = int(limit_start)
    limit_page_length = int(limit_page_length)
    enabled_only = int(enabled_only)

    user_filters = []
    if enabled_only:
        user_filters.append(["enabled", "=", 1])

    # ✅ hide @petapp.local users
    user_filters.append(["name", "not like", "%@petapp.local"])

    users = frappe.get_all(
        "User",
        filters=user_filters,
        fields=["name", "full_name", "email", "user_image", "enabled", "last_login"],
        limit_start=limit_start,
        limit_page_length=limit_page_length,
        order_by="name asc",
    )

    if not users:
        return {"data": []}

    user_ids = [u["name"] for u in users]

    rows = frappe.get_all(
        "User Role Profile",
        filters={"parenttype": "User", "parentfield": "role_profiles", "parent": ["in", user_ids]},
        fields=["parent", "role_profile"],
        limit_page_length=999999
    )

    user_to_profiles = {}
    for r in rows:
        user_to_profiles.setdefault(r["parent"], []).append(r["role_profile"])

    out = []
    for u in users:
        profiles = sorted(set(user_to_profiles.get(u["name"], [])))

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
            "user_image": u.get("user_image"),
            "role_profile": profiles[0] if profiles else None,
            "status": status,
            "enabled": u.get("enabled"),
        })

    frappe.response["data"] = out


@frappe.whitelist()
def get_all_role_profiles_with_roles(limit_page_length=999):
    # 🔐 permission gate
    allowed = {"System Manager", "Healthcare Administrator"}
    user_roles = set(frappe.get_roles(frappe.session.user) or [])
    if user_roles.isdisjoint(allowed):
        frappe.throw("Not permitted", frappe.PermissionError)

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
    return {"data": out}


@frappe.whitelist()
def get_all_role_profiles_with_roles(limit_page_length=999):
    # 🔐 permission gate
    allowed = {"System Manager", "Healthcare Administrator"}
    user_roles = set(frappe.get_roles(frappe.session.user) or [])
    if user_roles.isdisjoint(allowed):
        frappe.throw("Not permitted", frappe.PermissionError)

    limit_page_length = int(limit_page_length)
    aws_id = frappe.session.user

    # ✅ 1) fetch all role profiles
    profiles = frappe.get_all(
        "Role Profile",
        fields=["name", "creation"],
        limit_page_length=limit_page_length
    )

    if not profiles:
        frappe.response["data"] = []
        return

    profile_names = [p["name"] for p in profiles]

    # ✅ 2) fetch roles from child table
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

    # ✅ 3) group roles by profile
    roles_map = {}
    for r in rows:
        roles_map.setdefault(r["parent"], []).append(r["role"])

    # ✅ 4) hide rules (عدلها مثل ما تريد)
    STANDARD_PROFILES = {"Accounts", "Inventory", "Manufacturing", "Purchase", "Sales"}
    HIDDEN_PROFILES = {
        # مثال: إذا تريد تخفي هاي أيضاً
        # "ManagementTT",
    }
    HIDE_EMPTY_ROLES = True          # يخفي اللي roles=[]
    HIDE_STANDARD = True             # يخفي الستاندرد

    # ✅ 5) build response with filtering
    out = []
    for p in profiles:
        profile_name = p["name"]
        roles = sorted(set(roles_map.get(profile_name, [])))

        # --- filtering (الإخفاء) ---
        if HIDE_STANDARD and profile_name in STANDARD_PROFILES:
            continue
        if profile_name in HIDDEN_PROFILES:
            continue
        if HIDE_EMPTY_ROLES and len(roles) == 0:
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
