import frappe
from frappe import _

DRIVER_ROLES = ("System Manager", "Administrator")

def _check_permission():
    if frappe.session.user == "Administrator":
        return
    user_roles = frappe.get_roles(frappe.session.user)
    if not any(r in user_roles for r in DRIVER_ROLES):
        frappe.throw(_("Not authorized"), frappe.PermissionError)


@frappe.whitelist()
def create_driver(full_name, email, phone, license_number=None,
                  license_expiry=None, address_line1=None,
                  city=None, country="Iraq"):
    """
    POST /api/method/pet_app.api.driver.create_driver
    {
      "full_name": "Ahmed Hassan",
      "email": "ahmed@alkokh.com",
      "phone": "07701234567",
      "license_number": "123456",
      "license_expiry": "2027-01-01",
      "address_line1": "Tikrit Street",
      "city": "Baghdad",
      "country": "Iraq"
    }
    """
    _check_permission()

    if frappe.db.exists("User", email):
        frappe.throw(_(f"User '{email}' already exists"))

    # 1. Create User
    user = frappe.get_doc({
        "doctype": "User",
        "email": email,
        "username": email.split("@")[0].replace(".", "_"),
        "first_name": full_name.split()[0],
        "last_name": " ".join(full_name.split()[1:]) if len(full_name.split()) > 1 else "",
        "mobile_no": phone,
        "send_welcome_email": 0,
        "roles": [{"role": "Driver"}]
    })
    user.flags.ignore_permissions = True
    user.insert()

    # 2. Create Address
    address = None
    if address_line1:
        address = frappe.get_doc({
            "doctype": "Address",
            "address_title": full_name,
            "address_type": "Personal",
            "address_line1": address_line1,
            "city": city or "",
            "country": country,
            "phone": phone,
            "email_id": email,
            "links": [{
                "link_doctype": "User",
                "link_name": email
            }]
        })
        address.flags.ignore_permissions = True
        address.insert()

    # 3. Create Driver
    driver = frappe.get_doc({
        "doctype": "Driver",
        "full_name": full_name,
        "cell_number": phone,
        "status": "Active",
        "user": email,
        "license_number": license_number,
        "expiry_date": license_expiry,
        "address": address.name if address else None
    })
    driver.flags.ignore_permissions = True
    driver.insert()

    frappe.db.commit()

    frappe.response["data"] = {
        "driver": driver.name,
        "user": user.name,
        "full_name": full_name,
        "email": email,
        "phone": phone,
        "status": driver.status,
        "address": {
            "address_line1": address_line1,
            "city": city,
            "country": country
        } if address else None
    }


@frappe.whitelist()
def delete_driver(driver_id):
    """
    POST /api/method/pet_app.api.driver.delete_driver
    {"driver_id": "HR-DRI-2026-00004"}
    """
    _check_permission()

    doc = frappe.get_doc("Driver", driver_id)

    # Check no active orders
    active = frappe.db.exists("Sales Order", {
        "custom_driver": driver_id,
        "custom_order_status": ["in", ["Preparing", "Out for Delivery"]]
    })
    if active:
        frappe.throw(_("Cannot delete driver with active orders"))

    user_email = doc.user

    # Delete Address linked to User
    if user_email:
        addresses = frappe.get_all("Address",
            filters={"email_id": user_email},
            fields=["name"]
        )
        for addr in addresses:
            frappe.delete_doc("Address", addr.name, ignore_permissions=True, force=True)

    # Delete Driver
    frappe.delete_doc("Driver", driver_id, ignore_permissions=True, force=True)

    # Delete User
    if user_email:
        frappe.delete_doc("User", user_email, ignore_permissions=True, force=True)

    frappe.db.commit()

    frappe.response["data"] = {
        "deleted_driver": driver_id,
        "deleted_user": user_email
    }
