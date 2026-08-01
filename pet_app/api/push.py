from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, cstr, now_datetime
from frappe.utils.password import get_decrypted_password

from pet_app.notifications import engine
from pet_app.notifications.channels.onesignal import OneSignalAPIError, OneSignalPushChannel
from pet_app.utils.api_response import api_error, api_success


WEB_PUSH_TYPES = {"ChromePush", "FirefoxPush", "SafariPush", "EdgePush"}
TITLE_MAX_LENGTH = 192
BODY_MAX_LENGTH = 1000


class PushAPIError(frappe.ValidationError):
	def __init__(self, message: str, code: str, details=None):
		super().__init__(message)
		self.code = code
		self.details = details or {}


@frappe.whitelist()
def get_config():
	try:
		user = _require_user()
		settings = engine.get_settings()
		enabled = _is_push_enabled(settings, "web")
		return api_success(
			{
				"enabled": enabled,
				"web_enabled": bool(cint(settings.get("onesignal_web_enabled"))),
				"mobile_enabled": bool(cint(settings.get("onesignal_mobile_enabled"))),
				"app_id": settings.get("onesignal_app_id") if enabled else None,
				"external_id": user,
				"service_worker_path": _service_worker_path(settings),
				"service_worker_scope": _service_worker_scope(settings),
				"frontend_base_url": cstr(settings.get("push_frontend_base_url")).strip() or None,
			}
		)
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def register_subscription(subscription_id=None, platform=None, onesignal_id=None, device_id=None, permission=None, token=None, opted_in=None):
	try:
		user = _require_user()
		subscription_id = cstr(subscription_id).strip()
		if not subscription_id:
			return api_error(_("Subscription ID is required."), code="VALIDATION_ERROR")
		if not frappe.db.exists("DocType", "Pet App Push Subscription"):
			frappe.throw(_("Push subscription schema is missing. Run bench migrate."))

		doc = _upsert_subscription(
			user=user,
			subscription_id=subscription_id,
			platform=platform,
			onesignal_id=onesignal_id,
			device_id=device_id,
			permission=permission,
			token=token,
			opted_in=opted_in,
		)
		return api_success({"subscription": _subscription_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def manual_register_subscription(user=None, subscription_id=None, onesignal_id=None, platform="web", permission=None, token=None, opted_in=1):
	try:
		target_user = _target_user(user)
		subscription_id = cstr(subscription_id).strip()
		if not subscription_id:
			return api_error(_("Subscription ID is required."), code="VALIDATION_ERROR")

		opted = _optional_bool(opted_in)
		if opted is not True:
			raise PushAPIError(_("Push subscription is not opted in."), "PUSH_SUBSCRIPTION_NOT_SENDABLE")

		status = _onesignal_status(target_user)
		subscription = _verified_subscription(status, subscription_id=subscription_id, require_sendable=True)
		identity_onesignal_id = cstr(status.get("onesignal_id"))
		if cstr(onesignal_id).strip() and cstr(onesignal_id).strip() != identity_onesignal_id:
			raise PushAPIError(_("OneSignal user ID is not linked to the requested user."), "PUSH_SUBSCRIPTION_NOT_FOUND")

		doc = _upsert_subscription(
			user=target_user,
			subscription_id=subscription_id,
			platform=platform,
			onesignal_id=identity_onesignal_id or onesignal_id,
			permission=permission or "manual",
			token=token if token is not None else subscription.get("token"),
			opted_in=1,
		)
		return api_success({"subscription": _subscription_payload(doc), "onesignal_status": _status_response(status, subscription)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def unregister_subscription(subscription_id=None):
	try:
		user = _require_user()
		subscription_id = cstr(subscription_id).strip()
		if not subscription_id:
			return api_error(_("Subscription ID is required."), code="VALIDATION_ERROR")
		name = frappe.db.get_value(
			"Pet App Push Subscription",
			_subscription_filters(user=user, subscription_id=subscription_id),
			"name",
		)
		if name:
			frappe.db.set_value("Pet App Push Subscription", name, "disabled", 1, update_modified=True)
		return api_success({"subscription_id": subscription_id, "disabled": True})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["GET", "POST"])
def list_subscriptions(user=None, include_disabled=0):
	try:
		target_user = _target_user(user)
		subscriptions = _local_subscriptions(target_user, include_disabled=bool(cint(include_disabled)))
		return api_success(
			{
				"user": target_user,
				"subscriptions": subscriptions,
				"active_count": len([row for row in subscriptions if not row.get("disabled")]),
				"total_count": len(subscriptions),
			}
		)
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["GET", "POST"])
def get_onesignal_status(user=None, subscription_id=None):
	try:
		target_user = _target_user(user)
		status = _onesignal_status(target_user, missing_ok=True)
		requested_subscription_id = cstr(subscription_id).strip()
		selected = _find_subscription(status.get("subscriptions") or [], requested_subscription_id) if requested_subscription_id else None
		return api_success(_status_response(status, selected=selected, requested_subscription_id=requested_subscription_id or None))
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def send_test_push(user=None, subscription_id=None, title=None, body=None):
	try:
		target_user = _target_user(user)
		title, body = _clean_test_message(title, body)
		subscription_id = cstr(subscription_id).strip()
		settings = _push_settings()
		channel = OneSignalPushChannel(settings=settings)
		click_url = _test_push_url(settings)

		if subscription_id:
			status = _onesignal_status(target_user)
			subscription = _verified_subscription(status, subscription_id=subscription_id, require_sendable=True)
			response = channel.send_test_push(
				subscription_id=subscription_id,
				title=title,
				body=body,
				url=click_url,
				data={"kind": "manual_test", "target_user": target_user},
			)
			return api_success(_test_response(response, target_user, subscription_id=subscription.get("id"), dry_run=bool(cint(settings.get("dry_run")))))

		response = channel.send_test_push(
			user=target_user,
			title=title,
			body=body,
			url=click_url,
			data={"kind": "manual_test", "target_user": target_user},
		)
		return api_success(_test_response(response, target_user, subscription_id=None, dry_run=bool(cint(settings.get("dry_run")))))
	except Exception as exc:
		return _error_response(exc)


def push_enabled_for(platform="web") -> bool:
	if not frappe.db.exists("DocType", "Pet App Notification Settings"):
		return False
	return _is_push_enabled(engine.get_settings(), platform)


def _require_user():
	user = frappe.session.user
	if not user or user == "Guest":
		raise frappe.PermissionError
	return user


def _target_user(user=None):
	current_user = _require_user()
	target_user = cstr(user).strip() or current_user
	if target_user != current_user and not _can_manage_push():
		raise PushAPIError(_("Not permitted to access another user's push subscriptions."), "PUSH_USER_FORBIDDEN")
	if not frappe.db.exists("User", target_user):
		raise PushAPIError(_("User {0} was not found.").format(frappe.bold(target_user)), "PUSH_USER_NOT_FOUND")
	return target_user


def _can_manage_push():
	user = frappe.session.user
	return user == "Administrator" or "System Manager" in frappe.get_roles(user)


def _push_settings():
	settings = engine.get_settings()
	if not (
		cint(settings.get("enabled"))
		and cint(settings.get("onesignal_enabled"))
		and cstr(settings.get("onesignal_app_id")).strip()
	):
		raise PushAPIError(_("OneSignal push notifications are not configured."), "PUSH_NOT_CONFIGURED")
	if not _onesignal_api_key_present():
		raise PushAPIError(_("OneSignal REST API key is not configured."), "PUSH_NOT_CONFIGURED")
	return settings


def _onesignal_status(user, missing_ok=False):
	settings = _push_settings()
	channel = OneSignalPushChannel(settings=settings)
	try:
		record = channel.get_user_by_alias("external_id", user, missing_ok=missing_ok)
	except OneSignalAPIError:
		raise
	if not record:
		return {
			"user": user,
			"external_id": user,
			"onesignal_id": None,
			"subscription": None,
			"subscriptions": [],
			"raw": None,
		}

	identity = record.get("identity") or {}
	subscriptions = record.get("subscriptions") or []
	selected = _select_web_subscription(subscriptions, settings.get("onesignal_app_id"))
	return {
		"user": user,
		"external_id": user,
		"onesignal_id": identity.get("onesignal_id"),
		"subscription": selected,
		"subscriptions": subscriptions,
		"raw": record,
	}


def _select_web_subscription(subscriptions, app_id):
	web_rows = [row for row in subscriptions if _is_web_push_subscription(row)]
	for row in web_rows:
		if _subscription_is_sendable(row, app_id):
			return row
	return web_rows[0] if web_rows else None


def _verified_subscription(status, subscription_id, require_sendable=False):
	subscription_id = cstr(subscription_id).strip()
	if not subscription_id:
		raise PushAPIError(_("Subscription ID is required."), "VALIDATION_ERROR")
	for row in status.get("subscriptions") or []:
		if cstr(row.get("id")) != subscription_id:
			continue
		if not _is_web_push_subscription(row):
			raise PushAPIError(_("Subscription is not a web push subscription."), "PUSH_SUBSCRIPTION_NOT_FOUND")
		if cstr(row.get("app_id")) != cstr(engine.get_settings().get("onesignal_app_id")):
			raise PushAPIError(_("Subscription does not belong to the configured OneSignal app."), "PUSH_SUBSCRIPTION_NOT_FOUND")
		if require_sendable and not _subscription_is_sendable(row, engine.get_settings().get("onesignal_app_id")):
			raise PushAPIError(
				_("Subscription is not sendable."),
				"PUSH_SUBSCRIPTION_NOT_SENDABLE",
				details=_safe_subscription(row),
			)
		return row
	raise PushAPIError(_("Subscription was not found for this user."), "PUSH_SUBSCRIPTION_NOT_FOUND")


def _find_subscription(subscriptions, subscription_id):
	subscription_id = cstr(subscription_id).strip()
	if not subscription_id:
		return None
	for row in subscriptions or []:
		if cstr(row.get("id")) == subscription_id:
			return row
	return None


def _status_response(status, selected=None, requested_subscription_id=None):
	selected = selected if requested_subscription_id else selected or status.get("subscription")
	app_id = engine.get_settings().get("onesignal_app_id")
	if requested_subscription_id:
		status_subscriptions = [selected] if selected else []
	else:
		status_subscriptions = status.get("subscriptions") or []
	sendable_subscriptions = [row for row in status_subscriptions if _is_web_push_subscription(row) and _subscription_is_sendable(row, app_id)]
	local_subscriptions = _local_subscriptions(status.get("user"), include_disabled=True, subscription_id=requested_subscription_id)
	return {
		"user": status.get("user"),
		"external_id": status.get("external_id"),
		"onesignal_id": status.get("onesignal_id"),
		"subscription_id": selected.get("id") if selected else requested_subscription_id,
		"subscription_found": bool(selected),
		"enabled": bool(sendable_subscriptions),
		"token_present": bool(selected and selected.get("token")),
		"sendable_subscription_count": len(sendable_subscriptions),
		"sendable_subscription_ids": [row.get("id") for row in sendable_subscriptions if row.get("id")],
		"subscriptions": [_safe_subscription(row) for row in status_subscriptions],
		"local_subscriptions": local_subscriptions,
		"local_active_count": len([row for row in local_subscriptions if not row.get("disabled")]),
	}


def _test_response(response, user, subscription_id, dry_run):
	payload = response.get("payload") or {}
	return {
		"accepted": True,
		"dry_run": dry_run,
		"user": user,
		"subscription_id": subscription_id,
		"click_url": payload.get("url"),
		"provider_message_id": response.get("provider_message_id"),
		"provider_response": _safe_provider_response(response.get("response")),
	}


def _upsert_subscription(*, user, subscription_id, platform=None, onesignal_id=None, device_id=None, permission=None, token=None, opted_in=None):
	if not frappe.db.exists("DocType", "Pet App Push Subscription"):
		frappe.throw(_("Push subscription schema is missing. Run bench migrate."))

	opted = _optional_bool(opted_in)
	token_text = cstr(token).strip()
	app_id = cstr(engine.get_settings().get("onesignal_app_id")).strip() or None
	name = frappe.db.get_value("Pet App Push Subscription", _subscription_filters(subscription_id=subscription_id), "name")
	values = {
		"user": user,
		"guardian": frappe.db.get_value("Guardian", {"user_id": user}, "name"),
		"subscription_id": subscription_id,
		"onesignal_app_id": app_id,
		"onesignal_id": cstr(onesignal_id).strip() or None,
		"platform": _platform(platform),
		"device_id": cstr(device_id).strip() or None,
		"permission": cstr(permission).strip() or None,
		"last_seen_at": now_datetime(),
	}
	if token is not None:
		values["push_token"] = token_text or None
	if opted is not None:
		values["opted_in"] = 1 if opted else 0
		values["disabled"] = 0 if opted and token_text else 1
	elif not name:
		values["opted_in"] = 0
		values["disabled"] = 0
	if name:
		frappe.db.set_value("Pet App Push Subscription", name, values, update_modified=True)
		return frappe.get_doc("Pet App Push Subscription", name)
	return frappe.get_doc({"doctype": "Pet App Push Subscription", **values}).insert(ignore_permissions=True)


def _is_push_enabled(settings, platform) -> bool:
	if not (
		cint(settings.get("enabled"))
		and cint(settings.get("onesignal_enabled"))
		and settings.get("onesignal_app_id")
	):
		return False
	if platform == "mobile":
		return bool(cint(settings.get("onesignal_mobile_enabled")))
	return bool(cint(settings.get("onesignal_web_enabled")))


def _service_worker_path(settings):
	path = cstr(settings.get("onesignal_service_worker_path") or "onesignal/OneSignalSDKWorker.js").strip()
	return path.lstrip("/")


def _service_worker_scope(settings):
	scope = cstr(settings.get("onesignal_service_worker_scope") or "/onesignal/").strip()
	if not scope.startswith("/"):
		scope = f"/{scope}"
	return scope if scope.endswith("/") else f"{scope}/"


def _absolute_url(url):
	text = cstr(url).strip()
	if not text:
		return None
	if text.startswith(("http://", "https://")):
		return text
	if not text.startswith("/"):
		text = f"/{text}"
	return frappe.utils.get_url(text)


def _test_push_url(settings):
	base_url = cstr(settings.get("push_frontend_base_url")).strip().rstrip("/")
	if base_url:
		if not base_url.startswith(("http://", "https://")):
			base_url = f"https://{base_url.lstrip('/')}"
		return base_url
	return _absolute_url("/app/notification-log")


def _clean_test_message(title, body):
	title = cstr(title).strip() or _("Pet App test push")
	body = cstr(body).strip() or _("Your browser push subscription is working.")
	if len(title) > TITLE_MAX_LENGTH:
		raise PushAPIError(_("Push title is too long."), "VALIDATION_ERROR")
	if len(body) > BODY_MAX_LENGTH:
		raise PushAPIError(_("Push body is too long."), "VALIDATION_ERROR")
	return title, body


def _platform(platform):
	value = cstr(platform or "unknown").strip().lower()
	return value if value in {"web", "android", "ios", "unknown"} else "unknown"


def _optional_bool(value):
	if value is None or value == "":
		return None
	if isinstance(value, bool):
		return value
	text = cstr(value).strip().lower()
	if text in {"1", "true", "yes", "y", "on", "granted"}:
		return True
	if text in {"0", "false", "no", "n", "off", "denied", "default"}:
		return False
	return None


def _is_web_push_subscription(row):
	return cstr(row.get("type")) in WEB_PUSH_TYPES


def _subscription_is_sendable(row, app_id):
	return bool(cstr(row.get("app_id")) == cstr(app_id) and row.get("enabled") and row.get("token"))


def _subscription_payload(doc) -> dict:
	return {
		"id": doc.name,
		"user": doc.user,
		"guardian": doc.guardian,
		"onesignal_app_id": doc.get("onesignal_app_id"),
		"subscription_id": doc.subscription_id,
		"onesignal_id": doc.onesignal_id,
		"platform": doc.platform,
		"device_id": doc.device_id,
		"token_present": bool(doc.get("push_token")),
		"opted_in": bool(doc.get("opted_in")),
		"permission": doc.permission,
		"last_seen_at": cstr(doc.last_seen_at or ""),
		"disabled": bool(doc.disabled),
	}


def _local_subscriptions(user, include_disabled=False, subscription_id=None):
	if not user or not frappe.db.exists("DocType", "Pet App Push Subscription"):
		return []
	filters = _subscription_filters(user=user, subscription_id=subscription_id)
	if not include_disabled:
		filters["disabled"] = 0
	rows = frappe.get_all(
		"Pet App Push Subscription",
		filters=filters,
		fields=["name"],
		order_by="last_seen_at desc, modified desc",
		ignore_permissions=True,
	)
	return [_subscription_payload(frappe.get_doc("Pet App Push Subscription", row.name)) for row in rows]


def _subscription_filters(user=None, subscription_id=None):
	filters = {}
	if user:
		filters["user"] = user
	if subscription_id:
		filters["subscription_id"] = subscription_id
	if _has_subscription_app_id_field():
		filters["onesignal_app_id"] = cstr(engine.get_settings().get("onesignal_app_id")).strip() or None
	return filters


def _has_subscription_app_id_field():
	return frappe.db.exists("DocType", "Pet App Push Subscription") and frappe.get_meta("Pet App Push Subscription").has_field("onesignal_app_id")


def _onesignal_api_key_present():
	return bool(
		get_decrypted_password(
			"Pet App Notification Settings",
			"Pet App Notification Settings",
			"onesignal_rest_api_key",
			raise_exception=False,
		)
	)


def _safe_subscription(row):
	app_id = engine.get_settings().get("onesignal_app_id")
	return {
		"id": row.get("id"),
		"type": row.get("type"),
		"enabled": bool(row.get("enabled")),
		"notification_types": row.get("notification_types"),
		"token_present": bool(row.get("token")),
		"app_id_matches": cstr(row.get("app_id")) == cstr(app_id),
		"sendable": bool(_is_web_push_subscription(row) and _subscription_is_sendable(row, app_id)),
		"device_model": row.get("device_model"),
		"device_os": row.get("device_os"),
		"sdk": row.get("sdk"),
	}


def _safe_provider_response(response):
	if not isinstance(response, dict):
		return response
	data = dict(response)
	data.pop("recipients", None)
	return data


def _provider_error_details(details):
	details = details or {}
	response = details.get("response") if isinstance(details, dict) else None
	return {"response": _safe_provider_response(response)} if response is not None else {}


def _error_response(exc):
	if isinstance(exc, PushAPIError):
		return api_error(cstr(exc), code=exc.code, details=exc.details)
	if isinstance(exc, OneSignalAPIError):
		return api_error(cstr(exc), code="PUSH_PROVIDER_REJECTED", details=_provider_error_details(getattr(exc, "details", None)))
	if isinstance(exc, frappe.PermissionError):
		return api_error(_("Not permitted"), code="PUSH_USER_FORBIDDEN")
	return api_error(cstr(exc), code=getattr(exc, "code", None) or getattr(exc, "exc_type", None) or exc.__class__.__name__)
