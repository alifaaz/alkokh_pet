from __future__ import annotations

import hashlib
import hmac
import json

import frappe
from frappe.utils import cint, cstr, now_datetime
from werkzeug.wrappers import Response

from pet_app.notifications.webhook import handle_whatsapp_webhook
from pet_app.utils.api_response import api_error


@frappe.whitelist(allow_guest=True)
def webhook(**kwargs):
	method = getattr(frappe.request, "method", "GET") if getattr(frappe, "request", None) else "GET"
	if method == "GET":
		mode = frappe.form_dict.get("hub.mode") or frappe.form_dict.get("hub_mode")
		token = frappe.form_dict.get("hub.verify_token") or frappe.form_dict.get("hub_verify_token")
		challenge = frappe.form_dict.get("hub.challenge") or frappe.form_dict.get("hub_challenge")
		if mode == "subscribe" and _verify_token(token):
			return Response(cstr(challenge), status=200, content_type="text/plain")
		return Response("Forbidden", status=403, content_type="text/plain")
	try:
		raw_body = getattr(frappe.request, "data", None) or b""
		if not _verify_signature(raw_body, frappe.get_request_header("X-Hub-Signature-256")):
			return Response("Forbidden", status=403, content_type="text/plain")
		payload = frappe.local.form_dict or {}
		if raw_body:
			payload = json.loads(raw_body)
		return handle_whatsapp_webhook(payload)
	except Exception as exc:
		return api_error(cstr(exc), code=getattr(exc, "exc_type", None) or exc.__class__.__name__)


def _verify_token(token) -> bool:
	if not token:
		return False

	for account_name in frappe.get_all(
		"Pet App WhatsApp Account",
		filters={"enabled": 1},
		pluck="name",
		ignore_permissions=True,
	):
		account = frappe.get_doc("Pet App WhatsApp Account", account_name)
		if _account_verify_token(account) == token:
			return True

	single = frappe.db.get_single_value("Pet App Notification Settings", "default_whatsapp_account")
	if single:
		account = frappe.get_doc("Pet App WhatsApp Account", single)
		return _account_verify_token(account) == token
	return False


def _account_verify_token(account) -> str | None:
	token = account.get_password("verify_token", raise_exception=False)
	if token:
		return token
	raw_token = account.get("verify_token")
	if raw_token and not account.is_dummy_password(raw_token):
		return raw_token
	return None


# Why a rejection is recorded at all: the 403 below returns before anything else runs,
# so until now a refused webhook left no trace whatsoever - no row, no log, no counter.
# A signature check that nobody can observe failing is undiagnosable the one time it
# matters, whether the cause is an attacker or our own missing secret.
REJECTION_LOG_TITLE = "WhatsApp webhook rejected"

# Reason codes. Deliberately distinct: the first two mean "someone sent us something we
# cannot authenticate", the third (used from Phase 2) means "we cannot authenticate
# anything because we have no secret" - misconfiguration, not attack.
REJECT_SIGNATURE_MISSING = "SIGNATURE_MISSING"
REJECT_SIGNATURE_MISMATCH = "SIGNATURE_MISMATCH"

# One record per reason per window. An unauthenticated caller must not be able to make
# us write a row per request - that turns a rejected request into a storage primitive,
# which is most of what the check exists to prevent. frappe.cache() is used rather than
# a table because the throttle state is worthless after a restart and must not itself
# cost a write; Redis is already a hard dependency here.
REJECTION_WINDOW_SECONDS = 300


def _record_webhook_rejection(reason, *, signature_present, body_length):
	"""Record a rejected webhook, throttled, never raising.

	Never stores the request body. The body is unverified, attacker-controlled and
	unbounded, and the whole point of the rejection is that we do not trust it; keeping
	it would hand an anonymous caller a way to write arbitrary content into our logs.
	Only a reason code, whether a signature header was present at all, the body length,
	the remote address and the time.

	Wrapped end to end: a logging failure must never become a 500, and must never turn
	a rejection into an acceptance. The caller ignores the return value.
	"""
	try:
		cache = frappe.cache()
		# make_key applied once, then only raw redis commands. RedisWrapper prefixes the
		# site name inside set_value/get_value/exists but NOT inside the inherited
		# set/get/incr, so the two families cannot be mixed: a prefixed key handed to
		# exists() is prefixed a second time and never matches. get() is used for the
		# window probe for exactly that reason.
		window_key = cache.make_key(f"pet_app:wh_reject:window:{reason}")
		pending_key = cache.make_key(f"pet_app:wh_reject:pending:{reason}")

		suppressed = cint(cache.incr(pending_key)) - 1
		if cache.get(window_key) is not None:
			# Inside an open window: counted above, not written.
			return

		cache.set(window_key, 1, ex=REJECTION_WINDOW_SECONDS)
		cache.set(pending_key, 0)

		frappe.log_error(
			title=REJECTION_LOG_TITLE,
			message=json.dumps(
				{
					"reason": reason,
					"signature_present": bool(signature_present),
					"body_length": cint(body_length),
					"remote_addr": cstr(getattr(frappe.local, "request_ip", None)),
					"timestamp": cstr(now_datetime()),
					# Occurrences of this reason swallowed since the previous record. A
					# window's total can only be known once it closes, so it is reported
					# at the start of the next one.
					"suppressed_since_last": max(cint(suppressed), 0),
					"window_seconds": REJECTION_WINDOW_SECONDS,
				},
				default=str,
			),
		)
	except Exception:
		# Deliberately silent. Nothing about recording may change the outcome.
		pass


def _verify_signature(raw_body, signature) -> bool:
	secrets = []
	for account_name in frappe.get_all(
		"Pet App WhatsApp Account",
		filters={"enabled": 1},
		pluck="name",
		ignore_permissions=True,
	):
		secret = frappe.get_doc("Pet App WhatsApp Account", account_name).get_password(
			"app_secret", raise_exception=False
		)
		if secret:
			secrets.append(secret)
	if not secrets:
		return True
	if not signature or not cstr(signature).startswith("sha256="):
		_record_webhook_rejection(
			REJECT_SIGNATURE_MISSING,
			signature_present=bool(signature),
			body_length=len(raw_body or b""),
		)
		return False
	body = raw_body if isinstance(raw_body, bytes) else cstr(raw_body).encode()
	if any(
		hmac.compare_digest(cstr(signature), f"sha256={hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()}")
		for secret in secrets
	):
		return True
	_record_webhook_rejection(
		REJECT_SIGNATURE_MISMATCH, signature_present=True, body_length=len(body)
	)
	return False
