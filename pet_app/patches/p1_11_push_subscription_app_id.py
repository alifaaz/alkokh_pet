from __future__ import annotations

import frappe

from pet_app.patches.p1_5_notification_engine_schema import ensure_doctype


def execute():
	ensure_doctype(
		"Pet App Push Subscription",
		{
			"fields": [
				{
					"fieldname": "onesignal_app_id",
					"fieldtype": "Data",
					"in_list_view": 1,
					"in_standard_filter": 1,
				},
			],
		},
	)
	_remove_subscription_id_unique_flag()
	_backfill_subscription_app_id()
	_normalize_worker_default()
	frappe.clear_cache()


def _remove_subscription_id_unique_flag():
	if not frappe.db.exists("DocType", "Pet App Push Subscription"):
		return
	doc = frappe.get_doc("DocType", "Pet App Push Subscription")
	for field in doc.fields:
		if field.fieldname == "subscription_id" and field.unique:
			field.unique = 0
			doc.save(ignore_permissions=True)
			frappe.db.updatedb("Pet App Push Subscription")
			return


def _backfill_subscription_app_id():
	if not frappe.db.has_column("Pet App Push Subscription", "onesignal_app_id"):
		return
	app_id = frappe.db.get_single_value("Pet App Notification Settings", "onesignal_app_id")
	if not app_id:
		return
	frappe.db.sql(
		"""
		update `tabPet App Push Subscription`
		set onesignal_app_id = %s
		where ifnull(onesignal_app_id, '') = ''
		""",
		app_id,
	)


def _normalize_worker_default():
	if not frappe.db.exists("DocType", "Pet App Notification Settings"):
		return
	doc = frappe.get_doc("DocType", "Pet App Notification Settings")
	for field in doc.fields:
		if field.fieldname == "onesignal_service_worker_path" and field.default == "onesignal/OneSignalSDKWorker.js":
			field.default = "OneSignalSDKWorker.js"
			doc.save(ignore_permissions=True)
			break
	current = frappe.db.get_single_value("Pet App Notification Settings", "onesignal_service_worker_path")
	if not current or current == "onesignal/OneSignalSDKWorker.js":
		frappe.db.set_single_value("Pet App Notification Settings", "onesignal_service_worker_path", "OneSignalSDKWorker.js")
