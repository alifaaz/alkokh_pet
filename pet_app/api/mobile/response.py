import frappe


def ok(data: dict = None):
    return {"ok": True, "data": data or {}}


def error(code: str, message: str, http_status: int = 400):
    frappe.local.response["http_status_code"] = http_status
    return {"error": {"code": code, "message": message}}
