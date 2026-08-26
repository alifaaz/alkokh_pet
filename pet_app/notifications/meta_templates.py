"""WABA-scoped Graph calls and the local mirror of Meta's message templates.

This module is deliberately separate from ``channels/whatsapp_meta.py``. That one
is the *messaging* surface - it posts to ``/{phone-number-id}/messages`` and is on
the send path. This one is the *management* surface: it talks to
``/{waba-id}/message_templates``, needs a different token permission, and never
sends a message. Nothing here is imported by the send path.

Two rules shape the whole file:

1. **Nothing about Meta's vocabulary is hardcoded.** Statuses and categories are
   written verbatim as data, never validated against a list and never
   case-normalised. The local -> Meta category translation lives in the
   ``Pet App WhatsApp Meta Category Map`` doctype, so a new category is a row
   somebody adds rather than a patch somebody writes. The API version comes from
   ``graph_api_version`` on the account; no version literal appears in this file.
2. **Meta's error detail survives to the caller.** ``WhatsAppMetaAPIError`` carries
   ``code``/``error_subcode``/``type``/``fbtrace_id``/``http_status`` in
   ``.details``, and the API layer passes that through. Callers get Meta's own
   diagnosis, not a flattened string.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import NamedTuple

import frappe
import requests
from frappe import _
from frappe.utils import cint, cstr, now_datetime
from frappe.utils.password import get_decrypted_password

from pet_app.notifications.channels.whatsapp_meta import WhatsAppMetaAPIError


MIRROR_DOCTYPE = "Pet App WhatsApp Meta Template"
CATEGORY_MAP_DOCTYPE = "Pet App WhatsApp Meta Category Map"
LOCAL_TEMPLATE_DOCTYPE = "Pet App WhatsApp Template"

GRAPH_HOST = "https://graph.facebook.com"
TEMPLATES_EDGE = "message_templates"
REQUEST_TIMEOUT = 30

# Page size for the Graph listing and for the mirror listing. There is no suitable
# field on Pet App Notification Settings - reminder_batch_size is about reminders and
# the max_messages_per_* pair belongs to the (currently inert) rate limiter - so this
# is a single named constant rather than a setting invented for it.
META_TEMPLATE_PAGE_SIZE = 100

# A safety stop for the sync loop. Meta pages 100 at a time, so this is 25k templates
# against a WABA limit in the low thousands: high enough never to truncate a real
# account, low enough that a paging bug cannot spin forever.
MAX_SYNC_PAGES = 250

# Meta's template name rule: lowercase letters, digits and underscores. This is a
# format, not a value list, so a regex is the honest way to express it.
TEMPLATE_NAME_PATTERN = re.compile(r"^[a-z0-9_]+$")
TEMPLATE_NAME_MAX_LENGTH = 512

BINDING_BOUND = "bound"
BINDING_UNBOUND = "unbound"
BINDING_AMBIGUOUS = "ambiguous"

# The Graph error shapes that mean "this token cannot manage templates". Meta reports
# a missing permission as an OAuthException, most often code 200 ("Permissions error")
# or code 10 ("does not have permission for this action"), and usually names the
# permission in the message.
MANAGEMENT_PERMISSION = "whatsapp_business_management"
_PERMISSION_ERROR_CODES = {10, 200, 299}


class MetaTemplatePermissionError(WhatsAppMetaAPIError):
	"""Raised when the access token lacks whatsapp_business_management.

	Kept distinct so the API layer can hand back an instruction instead of Meta's
	terse "(#200) Permissions error", which sends people looking for the wrong fix:
	the token works perfectly for sending, and only fails for template management.
	"""


# ---------------------------------------------------------------------------
# account / credentials
# ---------------------------------------------------------------------------


def resolve_template_account(account=None):
	"""The WhatsApp account whose WABA owns the templates.

	Mirrors engine.resolve_whatsapp_account's precedence (explicit -> configured
	default -> the is_default row) so template management and sending cannot end up
	pointed at different accounts.
	"""
	if account and not isinstance(account, str):
		return account
	name = account
	if not name:
		name = frappe.db.get_single_value("Pet App Notification Settings", "default_whatsapp_account")
	if not name:
		name = frappe.db.get_value("Pet App WhatsApp Account", {"is_default": 1, "enabled": 1}, "name")
	if not name:
		frappe.throw(_("WhatsApp account is not configured."))
	return frappe.get_doc("Pet App WhatsApp Account", name)


class MetaTemplateClient:
	"""Graph calls scoped to a WhatsApp Business Account."""

	def __init__(self, account=None):
		self.account = resolve_template_account(account)

	# -- configuration, all read from the account row ----------------------

	@property
	def waba_id(self) -> str:
		value = cstr(self.account.get("whatsapp_business_account_id")).strip()
		if not value:
			frappe.throw(
				_(
					"WhatsApp Business Account ID is not set on account {0}. "
					"Fill the whatsapp_business_account_id field before managing Meta templates."
				).format(self.account.name)
			)
		return value

	@property
	def api_version(self) -> str:
		value = cstr(self.account.get("graph_api_version")).strip()
		if not value:
			frappe.throw(
				_(
					"Graph API version is not set on account {0}. "
					"Fill the graph_api_version field before managing Meta templates."
				).format(self.account.name)
			)
		return value

	def _access_token(self) -> str:
		token = get_decrypted_password(
			"Pet App WhatsApp Account", self.account.name, "access_token", raise_exception=False
		)
		if not token:
			frappe.throw(_("WhatsApp access token is not configured."))
		return token

	def _url(self, path: str) -> str:
		return f"{GRAPH_HOST}/{self.api_version}/{path}"

	def _headers(self) -> dict:
		return {"Authorization": f"Bearer {self._access_token()}"}

	# -- the four Graph endpoints ------------------------------------------

	def list_page(self, after: str | None = None, limit: int | None = None) -> dict:
		"""One page of GET /{waba-id}/message_templates."""
		params = {"limit": cint(limit) or META_TEMPLATE_PAGE_SIZE}
		if after:
			params["after"] = after
		response = requests.get(
			self._url(f"{self.waba_id}/{TEMPLATES_EDGE}"),
			headers=self._headers(),
			params=params,
			timeout=REQUEST_TIMEOUT,
		)
		return self._response_json(response)

	def create(self, *, name: str, language: str, category: str, components: list) -> dict:
		"""POST /{waba-id}/message_templates."""
		payload = {
			"name": name,
			"language": language,
			"category": category,
			"components": components,
		}
		response = requests.post(
			self._url(f"{self.waba_id}/{TEMPLATES_EDGE}"),
			headers=self._headers(),
			json=payload,
			timeout=REQUEST_TIMEOUT,
		)
		return self._response_json(response)

	def edit(self, *, meta_template_id: str, components: list) -> dict:
		"""POST /{template-id} - components are the only editable part here."""
		response = requests.post(
			self._url(cstr(meta_template_id)),
			headers=self._headers(),
			json={"components": components},
			timeout=REQUEST_TIMEOUT,
		)
		return self._response_json(response)

	def delete(self, *, name: str) -> dict:
		"""DELETE /{waba-id}/message_templates?name={name}.

		Meta deletes every language variant sharing the name; there is no
		per-language delete on this edge.
		"""
		response = requests.delete(
			self._url(f"{self.waba_id}/{TEMPLATES_EDGE}"),
			headers=self._headers(),
			params={"name": name},
			timeout=REQUEST_TIMEOUT,
		)
		return self._response_json(response)

	# -- error parsing -----------------------------------------------------

	def _response_json(self, response):
		"""Parse a Graph response, preserving Meta's diagnosis on failure.

		Deliberately a copy of WhatsAppMetaChannel._response_json rather than a
		refactor of it: that method sits on the send path, and this task does not
		touch the send path. The captured keys are identical.
		"""
		try:
			data = response.json()
		except ValueError:
			data = {}
		if response.ok:
			# Meta answers the edit and delete edges with 200 and {"success": true|false}.
			# A 200 is therefore not proof of anything: without this check a refused edit
			# and a refused delete both read as success, which is exactly how three write
			# buttons came to report results they had never confirmed. The flag is only
			# consulted when Meta actually sends it - list and create return no "success"
			# key and are unaffected.
			if "success" in data and not data.get("success"):
				self._raise_meta_error(
					response,
					data,
					_("Meta rejected the request and returned success: false without a reason."),
				)
			return data

		self._raise_meta_error(response, data, f"Meta WhatsApp API returned HTTP {response.status_code}.")

	def _raise_meta_error(self, response, data, default_message):
		"""Raise from Meta's error object, whatever the HTTP status was.

		Shared by the non-2xx branch and the 200-with-success-false branch so both
		produce the same class, the same code and the same details keys - the frontend
		reads errors[0].message and meta.code and must not have to care which shape of
		refusal it was.
		"""
		error = data.get("error") or {}
		code = error.get("code") or response.status_code
		subcode = error.get("error_subcode")
		error_type = error.get("type")
		message = error.get("message") or default_message
		if subcode:
			message = f"{message} (subcode {subcode})"
		details = {
			"http_status": response.status_code,
			"error_code": code,
			"error_subcode": subcode,
			"error_type": error_type,
			"trace_id": error.get("fbtrace_id"),
		}

		if _is_missing_management_permission(code, error_type, error.get("message")):
			raise MetaTemplatePermissionError(
				_(
					"This WhatsApp access token cannot manage templates: it is missing the "
					"{0} permission. Sending messages needs only whatsapp_business_messaging, "
					"so the token can work for sending and still fail here. Regenerate it in "
					"Meta Business Settings > Users > System Users > (your system user) > "
					"Generate New Token, tick {0} alongside whatsapp_business_messaging, then "
					"paste the new token into the access_token field on WhatsApp account {1}. "
					"Meta said: {2}"
				).format(MANAGEMENT_PERMISSION, self.account.name, message),
				error_code=code,
				details={**details, "missing_permission": MANAGEMENT_PERMISSION},
			)

		raise WhatsAppMetaAPIError(message, error_code=code, details=details)


def _is_missing_management_permission(code, error_type, message) -> bool:
	"""True when Graph is refusing for lack of the management permission.

	Matches on the permission name when Meta includes it (it usually does), and
	otherwise on the OAuthException codes Meta uses for a scope refusal. Kept
	narrow: a wrong-token or expired-token error (code 190) is a different problem
	and must keep its own message.
	"""
	if MANAGEMENT_PERMISSION in cstr(message):
		return True
	try:
		numeric_code = int(code)
	except (TypeError, ValueError):
		return False
	return cstr(error_type) == "OAuthException" and numeric_code in _PERMISSION_ERROR_CODES


# ---------------------------------------------------------------------------
# category translation - data, never an if chain
# ---------------------------------------------------------------------------


def meta_category_for(local_category: str) -> str:
	"""Translate a local category to Meta's, through the map doctype.

	Accepts either side of the mapping: a local category resolves through its row,
	and a value that is already some row's meta_category passes through. Both
	answers come out of the table - there is no literal category anywhere in this
	function - so adding a category is a row, not a release.

	Throws, naming the row to fix, when the value is unmapped or its mapping is
	blank. It never guesses and never falls back.
	"""
	value = cstr(local_category).strip()
	if not value:
		frappe.throw(_("Template category is required."))

	row = frappe.db.get_value(CATEGORY_MAP_DOCTYPE, value, ["name", "meta_category"], as_dict=True)
	if row:
		mapped = cstr(row.meta_category).strip()
		if not mapped:
			frappe.throw(
				_(
					"Local category {0} has no Meta category. Open {1} {0} and set "
					"meta_category to the Meta category it should be submitted as."
				).format(value, CATEGORY_MAP_DOCTYPE)
			)
		return mapped

	# Already a Meta category? Only if the map itself says so.
	if frappe.db.exists(CATEGORY_MAP_DOCTYPE, {"meta_category": value}):
		return value

	frappe.throw(
		_(
			"No {0} row maps category {1}. Add a row with local_category {1} and set "
			"meta_category to the Meta category it should be submitted as."
		).format(CATEGORY_MAP_DOCTYPE, value)
	)


# ---------------------------------------------------------------------------
# send-time resolution: name and language come from the mirror, or not at all
# ---------------------------------------------------------------------------


# The one Meta value this module compares against rather than merely storing.
# Deciding what may be sent needs a notion of "approved", and there is no way to
# express that without naming it. The comparison is deliberately `!= APPROVED`
# rather than a list of bad statuses, so any status Meta invents fails closed: an
# unrecognised state refuses the send instead of quietly permitting it.
SENDABLE_STATUS = "APPROVED"


class MetaSendTarget(NamedTuple):
	"""Everything the payload builder needs, all of it from the mirror row.

	Carries the declared parameter shape as well as the identity, so both send paths
	derive shape from the same place. Two derivation rules for one Meta API is drift.
	"""

	meta_template_id: str
	template_name: str
	language: str
	parameter_format: str
	components: list

	@property
	def label(self) -> str:
		return f"{self.template_name} ({self.language})"


# Columns every send-time resolution needs off the mirror row.
_TARGET_FIELDS = ("name", "template_name", "language", "status", "missing_on_meta", "components_json", "raw_json")


def _load_json(value, default):
	if not value:
		return default
	try:
		return json.loads(value) or default
	except (TypeError, ValueError):
		return default


def declared_components(row) -> list:
	"""The components a mirror row declares, resolved one way for every caller.

	components_json first, raw_json's components as the fallback. Serialisation and
	validation must resolve this identically or a template could be counted one way in
	the picker and another at validation - which is the whole reason this is a shared
	function rather than two readings of the same row.
	"""
	get = row.get if hasattr(row, "get") else (lambda key, default=None: getattr(row, key, default))
	components = _load_json(get("components_json"), [])
	if not components:
		components = _load_json(get("raw_json"), {}).get("components") or []
	return components


def _target_from_row(row) -> MetaSendTarget:
	"""Build a send target, reading the declared shape out of the stored Meta object.

	parameter_format has no column of its own - it lives in raw_json, which is stored
	verbatim precisely so unmodelled keys like this one survive a sync.
	"""
	raw = _load_json(row.get("raw_json"), {})
	components = declared_components(row)

	return MetaSendTarget(
		meta_template_id=cstr(row.name),
		template_name=cstr(row.template_name),
		language=cstr(row.language),
		parameter_format=cstr(raw.get("parameter_format")),
		components=components,
	)


class MetaTemplateNotSendable(frappe.ValidationError):
	"""A local template has no approved Meta template behind it.

	Raised before any Graph call. Carries ``exc_type`` so the queue records a
	stable error code, and ``details`` so the reason survives to the caller on the
	rich error path.
	"""

	def __init__(self, message, code=None, details=None):
		super().__init__(message)
		self.exc_type = code or "META_TEMPLATE_NOT_SENDABLE"
		self.details = details or {}


def resolve_send_identity(template) -> MetaSendTarget:
	"""The send target - identity and declared parameter shape - from the bound mirror row.

	The local Pet App WhatsApp Template row is not trusted for either value. Both
	its ``template_name`` and ``language`` are unvalidated free text and have
	drifted in practice - ``Arabic``, ``en`` and ``en_US`` coexist across the local
	rows, and Meta rejects ``Arabic`` with #132001 because the locale code is ``ar``.

	There is deliberately **no fallback to the local values**. A fallback would
	reintroduce exactly the failure this removes, and would do it silently: the send
	would look fine here and be rejected by Meta afterwards, costing a queue row and
	a retry cycle each time.

	Raises MetaTemplateNotSendable when there is no binding, when the bound row is
	not APPROVED, or when Meta no longer has the template.
	"""
	local_name = cstr(getattr(template, "name", None) or (template.get("name") if hasattr(template, "get") else None))
	label = cstr(getattr(template, "template_name", None) or local_name) or _("(unnamed)")

	rows = (
		frappe.get_all(
			MIRROR_DOCTYPE,
			filters={"local_template": local_name},
			fields=list(_TARGET_FIELDS),
			ignore_permissions=True,
		)
		if local_name
		else []
	)

	if not rows:
		raise MetaTemplateNotSendable(
			_(
				"WhatsApp template {0} is not linked to an approved Meta template, so it "
				"cannot be sent. Open the Meta Templates tab, run a sync, and make sure a "
				"Meta template exists whose name and language exactly match this one."
			).format(label),
			code="META_TEMPLATE_NOT_LINKED",
			details={"local_template": local_name, "reason": "unbound"},
		)

	if len(rows) > 1:
		raise MetaTemplateNotSendable(
			_(
				"WhatsApp template {0} is linked to more than one Meta template ({1}), so "
				"there is no single template to send. Fix the duplicates in the Meta "
				"Templates tab first."
			).format(label, ", ".join(sorted(row.name for row in rows))),
			code="META_TEMPLATE_AMBIGUOUS",
			details={"local_template": local_name, "matches": sorted(row.name for row in rows)},
		)

	row = rows[0]
	_assert_sendable(row, linked_to=label, local_template=local_name)
	return _target_from_row(row)


def _assert_sendable(row, *, linked_to=None, local_template=None):
	"""The state checks a mirror row must pass before it can be sent.

	Shared by both send paths so there is one definition of "sendable". ``linked_to``
	is the local template's label when the send came through a binding, and None for
	a mirror-direct send, which changes only the wording.
	"""
	details = {"meta_template_id": row.name, "status": cstr(row.status)}
	if local_template:
		details["local_template"] = local_template

	if cint(row.missing_on_meta):
		message = (
			_(
				"WhatsApp template {0} is linked to Meta template {1} ({2}), which no longer "
				"exists on Meta. Recreate it in the Meta Templates tab, then sync."
			).format(linked_to, row.template_name, row.language)
			if linked_to
			else _(
				"Meta template {0} ({1}) no longer exists on Meta, so it cannot be sent. "
				"Recreate it in the Meta Templates tab, then sync."
			).format(row.template_name, row.language)
		)
		raise MetaTemplateNotSendable(
			message,
			code="META_TEMPLATE_MISSING_ON_META",
			details={**details, "reason": "missing_on_meta"},
		)

	# Status is compared, never rewritten, and is quoted back exactly as Meta sent it.
	if cstr(row.status) != SENDABLE_STATUS:
		message = (
			_(
				"WhatsApp template {0} is linked to Meta template {1} ({2}), which is "
				"currently in status {3}. It can only be sent once Meta reports it as {4}."
			).format(linked_to, row.template_name, row.language, cstr(row.status) or _("(none)"), SENDABLE_STATUS)
			if linked_to
			else _(
				"Meta template {0} ({1}) is currently in status {2}. It can only be sent "
				"once Meta reports it as {3}."
			).format(row.template_name, row.language, cstr(row.status) or _("(none)"), SENDABLE_STATUS)
		)
		raise MetaTemplateNotSendable(message, code="META_TEMPLATE_NOT_APPROVED", details=details)


def resolve_meta_send_identity(meta_template) -> MetaSendTarget:
	"""The send target for a mirror-direct send.

	No local template and no binding: the caller named a mirror row, and that row's
	own name and language are what goes to Meta, verbatim. This is the whole point
	of the path - the fifteen approved Meta templates carry correct locale codes and
	do not need a local row to have been created, let alone bound.

	The Phase 2 state checks still apply, through the same _assert_sendable.
	"""
	name = cstr(meta_template).strip()
	if not name:
		raise MetaTemplateNotSendable(
			_("A Meta template must be selected."),
			code="META_TEMPLATE_NOT_SELECTED",
			details={"reason": "missing"},
		)

	row = frappe.db.get_value(MIRROR_DOCTYPE, name, list(_TARGET_FIELDS), as_dict=True)
	if not row:
		raise MetaTemplateNotSendable(
			_(
				"Meta template {0} is not in the local mirror, so it cannot be sent. "
				"Run a sync from the Meta Templates tab."
			).format(name),
			code="META_TEMPLATE_NOT_IN_MIRROR",
			details={"meta_template_id": name, "reason": "not_mirrored"},
		)

	_assert_sendable(row)
	return _target_from_row(row)


def local_category_for(meta_category) -> str:
	"""Reverse of meta_category_for, through the same table.

	A mirror-direct send has only Meta's category (``UTILITY``), while the consent
	and marketing gates are written against the local vocabulary (``Utility``).
	Without this, a MARKETING Meta template would sail past the
	allow_marketing_messages gate, because "MARKETING" != "Marketing".

    Falls back to the Meta value unchanged when nothing maps it - there is no
    sensible local category to invent, and inventing one would be the guessing this
    module refuses elsewhere. See the note in the send path about what that means.
	"""
	value = cstr(meta_category).strip()
	if not value:
		return value
	mapped = frappe.db.get_value(CATEGORY_MAP_DOCTYPE, {"meta_category": value}, "local_category")
	return cstr(mapped) if mapped else value


# ---------------------------------------------------------------------------
# slot meanings: which allowlisted variable fills each {{n}}
# ---------------------------------------------------------------------------


SLOT_DOCTYPE = "Pet App WhatsApp Meta Template Slot"
SLOT_FIELD = "slot_map"


def allowlisted_variable_keys() -> set:
	"""The keys a slot may name, from the one allowlist the app already has.

	Deliberately the same 84 keys the local template panels offer, so a slot meaning
	and a Jinja token in a body_preview mean the same thing and resolve the same way.
	"""
	from pet_app.notifications.context import list_template_variables

	return {row["key"] for row in list_template_variables()}


def _assert_slot_schema():
	"""Refuse clearly if the child table has not been migrated in yet.

	Frappe would otherwise raise a raw SQL error naming a missing table, which reads
	like a bug rather than a pending migration.
	"""
	if not frappe.db.exists("DocType", SLOT_DOCTYPE):
		frappe.throw(_("Meta template slot meanings need a schema update. Run bench migrate first."))


# Sentinel for "the caller did not mention source_doctype at all", so that passing an
# empty string can mean "clear it" without the two being indistinguishable.
_UNSET = object()


def validate_source_doctype(source_doctype) -> str:
	"""A declared source must be one the variable catalogue actually knows.

	Checked against SOURCE_NAMESPACES - the mapping list_template_variables scopes by -
	rather than the rule designer's SOURCE_REGISTRY. The two hold the same seven keys
	today, but this field exists to narrow variables, and it is SOURCE_NAMESPACES that
	decides whether narrowing means anything: an unrecognised doctype does not raise
	there, it silently collapses the catalogue to {clinic, guardian, pet}. Validating
	against the symbol that does the work is what stops that being stored.
	"""
	from pet_app.notifications.context import SOURCE_NAMESPACES

	value = cstr(source_doctype).strip()
	if not value:
		return ""
	if value not in SOURCE_NAMESPACES:
		frappe.throw(
			_("{0} cannot be used as a template source. Choose one of: {1}.").format(
				value, ", ".join(sorted(SOURCE_NAMESPACES))
			)
		)
	return value


def source_doctype_supported() -> bool:
	"""Whether p1_17 has been migrated in yet."""
	return frappe.get_meta(MIRROR_DOCTYPE).has_field("source_doctype")


def stored_source_doctype(meta_template) -> str:
	"""The declared source on a mirror row, or "" when there is none.

	Returns "" on a site where the column does not exist yet, so every read path keeps
	its pre-field behaviour exactly - unscoped catalogue, no namespace restriction.
	"""
	if not source_doctype_supported():
		return ""
	return cstr(
		frappe.db.get_value(MIRROR_DOCTYPE, cstr(meta_template).strip(), "source_doctype")
	).strip()


def source_doctype_options() -> list:
	"""The doctypes a template may declare as its source, sorted.

	Read from SOURCE_NAMESPACES - the same symbol validate_source_doctype checks
	against - so a value offered in a selector cannot be one the save then rejects.
	Deliberately not the rule designer's SOURCE_REGISTRY: the two agree today, and a
	value that passes one and fails the other is exactly what must not reach a dropdown.
	"""
	from pet_app.notifications.context import SOURCE_NAMESPACES

	return sorted(SOURCE_NAMESPACES)


def scoped_variables(source_doctype) -> list:
	"""The catalogue for a source, through the one scoping function there is."""
	from pet_app.notifications.context import list_template_variables

	value = cstr(source_doctype).strip()
	return list_template_variables(value) if value else list_template_variables()


def scoped_namespaces(source_doctype) -> set:
	"""Namespaces a declared source can populate. Empty source means no restriction."""
	if not cstr(source_doctype).strip():
		return set()
	return {row["namespace"] for row in scoped_variables(source_doctype)}


def get_slot_map(meta_template) -> list:
	"""Stored slot meanings for a mirror row, ordered by slot."""
	_assert_slot_schema()
	name = cstr(meta_template).strip()
	if not name or not frappe.db.exists(MIRROR_DOCTYPE, name):
		frappe.throw(_("Meta template {0} is not in the local mirror.").format(name or "?"))
	rows = frappe.get_all(
		SLOT_DOCTYPE,
		filters={"parent": name, "parenttype": MIRROR_DOCTYPE},
		fields=["slot", "variable_key", "fallback"],
		order_by="slot asc",
		ignore_permissions=True,
	)
	return [
		{"slot": cint(row.slot), "variable_key": cstr(row.variable_key), "fallback": cstr(row.fallback)}
		for row in rows
	]


def slot_map_payload(meta_template, source_doctype=_UNSET) -> dict:
	"""The slot-map editor's payload, built in one place.

	Both the read and the save endpoint project through this, so a client re-rendering
	from a save response gets exactly what a read would have given it - same keys, same
	slot-row shape - and needs no branch on which call it came from. Returning Frappe
	child-doc dicts from the save was what made the two diverge: they carried a
	``doctype`` key the read never sent, and omitted everything the editor needs to
	redraw itself.

	``source_doctype`` passed scopes the catalogue to that source as a **preview**,
	writing nothing. It exists because an operator choosing a source before saving has
	to see that source's variables, and the alternative was the frontend relabelling
	the previous source's list with the new name - a display that was simply untrue.

	Only the variable scoping moves. ``slots``, ``declared_count``, ``map_is_stale``,
	``source_options`` and ``suggested_source_doctype`` describe the stored row and are
	unaffected by a preview.
	"""
	target = cstr(meta_template).strip()
	slots = get_slot_map(target)
	declared_count = declared_slot_count(target)
	# Passing "" is a legal preview - "show me what no declared source looks like" -
	# so the sentinel, not emptiness, is what distinguishes preview from stored.
	is_preview = source_doctype is not _UNSET
	source_doctype = (
		validate_source_doctype(source_doctype) if is_preview else stored_source_doctype(target)
	)
	return {
		"meta_template": target,
		"slots": slots,
		"declared_count": declared_count,
		# Derived at read time, no schema: a map exists and no longer matches the number
		# of variables the template declares.
		"map_is_stale": bool(slots) and len(slots) != declared_count,
		# The declared source, and whether the catalogue below is narrowed by it. With
		# no source declared this returns every allowlisted variable.
		"source_doctype": source_doctype,
		"scoped": bool(source_doctype),
		# Every doctype a source may be set to, so a selector never hardcodes names.
		"source_options": source_doctype_options(),
		# A source an action rule already implies. Suggestion only.
		"suggested_source_doctype": suggested_source_doctype(target),
		# True when source_doctype above is a caller's preview rather than the value on
		# the row, so the editor can label it without inferring.
		"source_is_preview": is_preview,
		"variables": scoped_variables(source_doctype),
	}


def suggested_source_doctype(meta_template) -> str | None:
	"""The source an action rule already implies for this template.

	Enabled state is deliberately ignored. Someone mapping a template's variables
	almost always has the rule drafted but not yet switched on - that is the order the
	work happens in - so filtering to enabled rules made the suggestion go null exactly
	when it was most useful.

	One distinct source across the bound rules suggests it; zero or several suggest
	nothing. A suggestion only, never written without an explicit save.
	"""
	sources = frappe.get_all(
		"Pet App WhatsApp Action Rule",
		filters={"meta_template": cstr(meta_template).strip()},
		pluck="source_doctype",
		ignore_permissions=True,
	)
	unique = {cstr(value).strip() for value in sources if cstr(value).strip()}
	return unique.pop() if len(unique) == 1 else None


def declared_slot_count(meta_template) -> int:
	"""How many variables the template's own message declares."""
	from pet_app.notifications.renderer import declared_parameter_count

	row = frappe.db.get_value(MIRROR_DOCTYPE, cstr(meta_template).strip(), list(_TARGET_FIELDS), as_dict=True)
	if not row:
		frappe.throw(_("Meta template {0} is not in the local mirror.").format(cstr(meta_template) or "?"))
	return declared_parameter_count(_target_from_row(row).components)


def validate_slot_map(meta_template, slots, source_doctype=_UNSET) -> list:
	"""Check a slot map against the template it belongs to, before storing it.

	A map that disagrees with the template is a refusal waiting to happen at send
	time, on a message somebody is trying to send to a customer. Catching it at save
	turns that into a form error nobody sees twice.
	"""
	from pet_app.notifications.renderer import declared_parameter_count

	target = frappe.db.get_value(
		MIRROR_DOCTYPE, cstr(meta_template).strip(), list(_TARGET_FIELDS), as_dict=True
	)
	if not target:
		frappe.throw(_("Meta template {0} is not in the local mirror.").format(cstr(meta_template) or "?"))
	shape = _target_from_row(target)
	declared = declared_parameter_count(shape.components)

	cleaned = []
	for row in slots or []:
		cleaned.append(
			{
				"slot": cint((row or {}).get("slot")),
				"variable_key": cstr((row or {}).get("variable_key")).strip(),
				"fallback": cstr((row or {}).get("fallback")).strip(),
			}
		)

	positions = [row["slot"] for row in cleaned]
	if len(set(positions)) != len(positions):
		duplicates = sorted({position for position in positions if positions.count(position) > 1})
		frappe.throw(
			_("Slots {0} are mapped more than once. Each slot may be mapped only once.").format(
				", ".join(str(position) for position in duplicates)
			)
		)
	if sorted(positions) != list(range(1, len(cleaned) + 1)):
		frappe.throw(
			_(
				"Slots must run from 1 with no gaps. Template {0} was given slots {1}."
			).format(shape.label, ", ".join(str(position) for position in sorted(positions)) or "none")
		)
	if len(cleaned) != declared:
		frappe.throw(
			_(
				"Template {0} has {1} variable(s) in its message and {2} slot(s) were mapped. "
				"Map exactly {1}."
			).format(shape.label, declared, len(cleaned))
		)

	allowed = allowlisted_variable_keys()
	# Validate against the source being saved, not the one on disk, so declaring a
	# source and mapping to it can happen in a single call.
	effective_source = (
		stored_source_doctype(meta_template)
		if source_doctype is _UNSET
		else validate_source_doctype(source_doctype)
	)
	# Empty source means no restriction: rows saved before any source existed keep
	# validating exactly as they did.
	permitted_namespaces = scoped_namespaces(effective_source)

	for row in sorted(cleaned, key=lambda item: item["slot"]):
		if not row["variable_key"]:
			frappe.throw(_("Slot {0} has no variable selected.").format(row["slot"]))
		if row["variable_key"] not in allowed:
			frappe.throw(
				_("Slot {0} names {1}, which is not an available variable.").format(
					row["slot"], row["variable_key"]
				)
			)
		if permitted_namespaces:
			namespace = row["variable_key"].partition(".")[0]
			if namespace not in permitted_namespaces:
				frappe.throw(
					_(
						"Slot {0} names {1}, which is not available for a {2} template. "
						"Nothing populates the {3} namespace from a {2} record, so it would "
						"resolve to nothing at send."
					).format(row["slot"], row["variable_key"], effective_source, namespace)
				)
	return sorted(cleaned, key=lambda item: item["slot"])


def save_slot_map(meta_template, slots, source_doctype=_UNSET) -> list:
	"""Replace a mirror row's slot map, and optionally its declared source.

	``source_doctype`` omitted leaves whatever is stored; passing an empty string
	clears it. Either way the map is validated against the value that will be stored,
	so declaring a source and mapping to it is one call and cannot half-apply.
	"""
	_assert_slot_schema()
	cleaned = validate_slot_map(meta_template, slots, source_doctype)
	doc = frappe.get_doc(MIRROR_DOCTYPE, cstr(meta_template).strip())
	if source_doctype is not _UNSET:
		if not source_doctype_supported():
			frappe.throw(
				_("Declaring a template source needs a schema update. Run bench migrate first.")
			)
		doc.source_doctype = validate_source_doctype(source_doctype)
	doc.set(SLOT_FIELD, [])
	for row in cleaned:
		doc.append(SLOT_FIELD, row)
	# validate_slot_map has already proved the map matches the declared count, so a
	# row that was flagged stale is not stale any more.
	doc.slot_map_stale = 0
	doc.save(ignore_permissions=True)
	return cleaned


def stored_slot_map(meta_template) -> list:
	"""The stored map, or an empty list when there is none or the table is absent.

	The send path must not die because p1_14 has not been migrated on some site; a
	missing table simply means no template has a map yet.
	"""
	if not frappe.db.exists("DocType", SLOT_DOCTYPE):
		return []
	return frappe.get_all(
		SLOT_DOCTYPE,
		filters={"parent": cstr(meta_template).strip(), "parenttype": MIRROR_DOCTYPE},
		fields=["slot", "variable_key", "fallback"],
		order_by="slot asc",
		ignore_permissions=True,
	)


def apply_slot_map_to_context(target, context) -> dict:
	"""Fill in context["parameters"] from the stored slot map, when nothing supplied it.

	Precedence, and it is the whole design:

	1. **Explicit values win.** If the caller put a ``parameters`` key in the context,
	   it is used verbatim and no resolution happens. The composer sends that key on
	   every mirror-direct send, and an operator who typed four values meant those
	   four values - a resolver running first would silently overwrite them.
	2. Otherwise, if the template has a stored slot map, resolve through it.
	3. Otherwise, leave the context alone and let the count check refuse exactly as it
	   does today.

	Resolution needs whatever namespaces the caller's context carries. A rule fires
	from a source document and has them; a manual send has a Guardian at best, so a
	slot needing ``boarding.*`` refuses by name rather than being filled with a guess.
	No source document is ever invented, and a booking is never guessed from a
	guardian - a guardian with five pets and three past stays has no correct answer.
	"""
	from pet_app.notifications.renderer import CONTEXT_PARAMETERS_KEY, declared_parameter_count

	context = context if isinstance(context, dict) else {}
	if CONTEXT_PARAMETERS_KEY in context:
		return context

	slots = stored_slot_map(target.meta_template_id)
	if not slots:
		return context

	# A map that no longer matches the template refuses here, locally, with something
	# an operator can act on - rather than resolving the wrong number of values and
	# letting the count check (or worse, Meta) be the one to notice.
	declared = declared_parameter_count(target.components)
	if len(slots) != declared:
		raise MetaTemplateNotSendable(
			_(
				"Template {0} has {1} variable(s) but its saved variable map has {2}. "
				"The template changed on Meta; open its variable map and map all {1}."
			).format(target.label, declared, len(slots)),
			code="META_TEMPLATE_SLOT_MAP_STALE",
			details={
				"meta_template_id": target.meta_template_id,
				"declared": declared,
				"mapped": len(slots),
			},
		)

	values = resolve_slots(
		[
			{"slot": cint(row.slot), "variable_key": cstr(row.variable_key), "fallback": cstr(row.fallback)}
			for row in slots
		],
		context,
		label=target.label,
		meta_template_id=target.meta_template_id,
	)
	return {**context, CONTEXT_PARAMETERS_KEY: values}


def resolve_slot_parameters(meta_template, context) -> list:
	"""The ordered positional values for a template, from its stored slot map.

	Returns exactly what a human typing into the composer produces - a flat, ordered
	list of scalars - so both paths hand the same shape to build_declared_parameters
	and are validated by it identically. There is one validator, not two.

	``context`` is a namespace dict as build_document_context produces it, so
	``guardian.display_name`` reads context["guardian"]["display_name"].

	A slot that resolves to nothing uses its fallback when one is set, and refuses the
	send when one is not. Empty is never silently sent: an operator opts into a hole
	per slot, and the safe behaviour is the default.
	"""
	name = cstr(meta_template).strip()
	label = cstr(frappe.db.get_value(MIRROR_DOCTYPE, name, "template_name")) or name
	return resolve_slots(get_slot_map(name), context, label=label, meta_template_id=name)


def resolve_slots(slots, context, *, label="", meta_template_id="") -> list:
	"""The resolution core, over an already-loaded slot map.

	Split out from resolve_slot_parameters so it can be exercised against a declared
	shape without a stored map - and so storage and resolution stay separable.
	"""
	values = []
	for row in slots:
		resolved = _lookup_variable(context, row["variable_key"])
		text = cstr(resolved) if resolved is not None else ""
		if not text.strip():
			if row["fallback"]:
				text = row["fallback"]
			else:
				raise MetaTemplateNotSendable(
					_(
						"Slot {0} of template {1} needs {2}, which has no value for this "
						"recipient. Set a fallback for that slot, or send a template that "
						"does not need it."
					).format(row["slot"], label, row["variable_key"]),
					code="META_TEMPLATE_PARAM_UNRESOLVED",
					details={
						"meta_template_id": meta_template_id,
						"slot": row["slot"],
						"variable_key": row["variable_key"],
					},
				)
		values.append(text)
	return values


def _lookup_variable(context, variable_key):
	"""namespace.fieldname against a namespace dict. No arbitrary attribute access."""
	namespace, _sep, fieldname = cstr(variable_key).partition(".")
	if not namespace or not fieldname or not isinstance(context, dict):
		return None
	bucket = context.get(namespace)
	if not isinstance(bucket, dict):
		return None
	return bucket.get(fieldname)


# ---------------------------------------------------------------------------
# binding: exact (template_name, language) match, nothing else
# ---------------------------------------------------------------------------


def resolve_binding(template_name: str, language: str) -> tuple[str | None, str]:
	"""Find the local template this Meta template corresponds to.

	Exact match on both columns and nothing more - no case folding, no slugifying,
	no "close enough". The DB collation is case-insensitive, so candidates are
	re-checked byte-for-byte in Python before one is accepted; otherwise MariaDB
	would quietly bind ``Feedback`` to ``feedback``.

	Returns ``(local_template or None, binding_state)``.
	"""
	name = cstr(template_name)
	lang = cstr(language)
	if not name or not lang:
		return None, BINDING_UNBOUND

	candidates = frappe.get_all(
		LOCAL_TEMPLATE_DOCTYPE,
		filters={"template_name": name, "language": lang},
		fields=["name", "template_name", "language"],
		ignore_permissions=True,
	)
	exact = [row.name for row in candidates if row.template_name == name and row.language == lang]

	if len(exact) == 1:
		return exact[0], BINDING_BOUND
	if not exact:
		return None, BINDING_UNBOUND
	return None, BINDING_AMBIGUOUS


# ---------------------------------------------------------------------------
# mirror upsert + sync
# ---------------------------------------------------------------------------


def upsert_mirror_row(template: dict, account=None, *, raw_override=None):
	"""Create or update one mirror row from a Meta template object.

	``raw_json`` is always written verbatim, so keys this app does not model are
	preserved for whatever reads them next.
	"""
	meta_template_id = cstr(template.get("id")).strip()
	if not meta_template_id:
		frappe.throw(_("Meta returned a template with no id."))

	exists = frappe.db.exists(MIRROR_DOCTYPE, meta_template_id)
	doc = frappe.get_doc(MIRROR_DOCTYPE, meta_template_id) if exists else frappe.new_doc(MIRROR_DOCTYPE)
	if not exists:
		doc.meta_template_id = meta_template_id

	template_name = cstr(template.get("name"))
	language = cstr(template.get("language"))

	doc.template_name = template_name
	doc.language = language
	# Verbatim. No mapping, no validation, no case normalisation.
	doc.category = cstr(template.get("category"))
	doc.status = cstr(template.get("status"))
	doc.rejected_reason = cstr(template.get("rejected_reason"))
	# Compare what the row declared before this sync with what Meta is sending now.
	# Must happen before components_json is overwritten, and only matters when there is
	# a map to go stale. Never clears the map itself.
	doc.slot_map_stale = _slot_map_went_stale(doc, template.get("components") or [])
	doc.components_json = json.dumps(template.get("components") or [], default=str)
	doc.raw_json = json.dumps(raw_override if raw_override is not None else template, default=str)
	if account is not None:
		doc.provider_account = account.name if hasattr(account, "name") else cstr(account)
	doc.local_template, doc.binding_state = resolve_binding(template_name, language)
	doc.last_synced_at = now_datetime()
	doc.missing_on_meta = 0

	if exists:
		doc.save(ignore_permissions=True)
	else:
		doc.insert(ignore_permissions=True)
	return doc


def declared_count_or_none(components):
	"""Declared parameter count, or None when the shape cannot be read.

	The single counting implementation for every caller that cannot afford to raise -
	serialisation, the sync's stale check - wrapping the same
	renderer.declared_parameter_count that validation and the send path call directly.
	One function, so a template cannot be counted as 4 in the picker and 5 at
	validation. An unreadable shape (a variable in a button, a gap in the numbering,
	named parameters) is reported as None rather than a number, and the caller that
	does raise will explain why.
	"""
	from pet_app.notifications.renderer import declared_parameter_count

	try:
		return declared_parameter_count(components or [])
	except Exception:
		return None


def _slot_map_went_stale(doc, incoming_components) -> int:
	"""Whether this row's stored slot map still matches the template Meta is sending.

	Returns the value slot_map_stale should take. A row with no map is never stale; a
	map whose size matches the incoming declared count clears the flag, so fixing the
	map (or Meta reverting the edit) heals it on the next sync.
	"""
	mapped = len(doc.get(SLOT_FIELD) or [])
	if not mapped:
		return 0

	incoming = declared_count_or_none(incoming_components)
	if incoming is None:
		# Unreadable shape: say nothing rather than guess. Keep what the row had.
		return cint(doc.get("slot_map_stale"))
	return 0 if mapped == incoming else 1


def sync_meta_templates(account=None) -> dict:
	"""Page the whole template list from Meta and upsert the mirror.

	Rows we hold that Meta no longer returns are flagged ``missing_on_meta`` rather
	than deleted: deleting one would destroy its ``local_template`` binding, which
	is ours and not recoverable from Meta.
	"""
	client = MetaTemplateClient(account)
	counts = {
		"synced": 0,
		"created": 0,
		"updated": 0,
		"bound": 0,
		"unbound": 0,
		"ambiguous": 0,
		"missing_on_meta": 0,
		"pages": 0,
		"truncated": 0,
	}

	seen: set[str] = set()
	after = None

	for _page in range(MAX_SYNC_PAGES):
		page = client.list_page(after=after)
		rows = page.get("data") or []
		counts["pages"] += 1

		for template in rows:
			meta_template_id = cstr(template.get("id")).strip()
			existed = bool(meta_template_id and frappe.db.exists(MIRROR_DOCTYPE, meta_template_id))
			doc = upsert_mirror_row(template, client.account)
			seen.add(doc.name)
			counts["synced"] += 1
			counts["updated" if existed else "created"] += 1
			if doc.binding_state == BINDING_BOUND:
				counts["bound"] += 1
			elif doc.binding_state == BINDING_AMBIGUOUS:
				counts["ambiguous"] += 1
			else:
				counts["unbound"] += 1

		paging = page.get("paging") or {}
		after = ((paging.get("cursors") or {}).get("after")) if paging.get("next") else None
		if not after or not rows:
			break
	else:
		# Loop ran to MAX_SYNC_PAGES without Meta running out of pages. Never
		# silently truncate: say so in the result.
		counts["truncated"] = 1

	counts["missing_on_meta"] = _flag_missing(client.account, seen)
	# Guarded: p1_15 may not be migrated in yet, and counting a column that does not
	# exist would fail the whole sync over a reporting number.
	counts["slot_map_stale"] = (
		frappe.db.count(MIRROR_DOCTYPE, {"provider_account": client.account.name, "slot_map_stale": 1})
		if frappe.get_meta(MIRROR_DOCTYPE).has_field("slot_map_stale")
		else 0
	)
	return counts


def _flag_missing(account, seen: set[str]) -> int:
	"""Mark rows Meta no longer returns. Never deletes."""
	rows = frappe.get_all(
		MIRROR_DOCTYPE,
		filters={"provider_account": account.name},
		fields=["name", "missing_on_meta"],
		ignore_permissions=True,
	)
	missing = 0
	for row in rows:
		if row.name in seen:
			continue
		missing += 1
		if not cint(row.missing_on_meta):
			frappe.db.set_value(MIRROR_DOCTYPE, row.name, "missing_on_meta", 1)
	return missing


def apply_status_update(
	*, meta_template_id=None, template_name=None, language=None, status=None, rejected_reason=None
):
	"""Apply a message_template_status_update webhook to the mirror.

	Matches on Meta's template id first - that is the mirror's docname, so it is
	exact by construction - and falls back to an exact (template_name, language)
	pair, re-checked byte-for-byte for the same reason resolve_binding does it: the
	DB collation is case-insensitive and would otherwise match the wrong row. An
	ambiguous fallback matches nothing rather than guessing.

	The status is written exactly as Meta sent it. No mapping, no validation, no
	case change: a status Meta invents next year lands here unchanged.

	Returns the mirror docname it updated, or None when nothing matched.
	"""
	name = None
	candidate_id = cstr(meta_template_id).strip()
	if candidate_id and frappe.db.exists(MIRROR_DOCTYPE, candidate_id):
		name = candidate_id

	if not name:
		wanted_name = cstr(template_name)
		wanted_language = cstr(language)
		if wanted_name and wanted_language:
			rows = frappe.get_all(
				MIRROR_DOCTYPE,
				filters={"template_name": wanted_name, "language": wanted_language},
				fields=["name", "template_name", "language"],
				ignore_permissions=True,
			)
			exact = [
				row.name
				for row in rows
				if row.template_name == wanted_name and row.language == wanted_language
			]
			if len(exact) == 1:
				name = exact[0]

	if not name:
		return None

	values = {"last_synced_at": now_datetime()}
	if status is not None:
		values["status"] = cstr(status)
	if rejected_reason is not None:
		values["rejected_reason"] = cstr(rejected_reason)
	frappe.db.set_value(MIRROR_DOCTYPE, name, values)
	return name


# ---------------------------------------------------------------------------
# mirror reads - the listing the frontend uses
# ---------------------------------------------------------------------------


MIRROR_FIELDS = (
	"name",
	"meta_template_id",
	# raw_json is fetched but never emitted: declared_components falls back to it, and
	# the list must resolve components exactly as validation does.
	"raw_json",
	"template_name",
	"language",
	"category",
	"status",
	"rejected_reason",
	"components_json",
	"last_synced_at",
	"local_template",
	"binding_state",
	"missing_on_meta",
	"provider_account",
)


def slot_map_sizes(meta_template_ids) -> dict:
	"""How many slots each of these mirror rows has mapped, in one query.

	Batched so serialising a page of templates does not issue a query per row.
	"""
	ids = [cstr(value).strip() for value in (meta_template_ids or []) if cstr(value).strip()]
	if not ids or not frappe.db.exists("DocType", SLOT_DOCTYPE):
		return {}
	placeholders = ", ".join(["%s"] * len(ids))
	rows = frappe.db.sql(
		f"""
		select parent, count(*) as mapped
		from `tab{SLOT_DOCTYPE}`
		where parenttype = %s and parent in ({placeholders})
		group by parent
		""",
		[MIRROR_DOCTYPE, *ids],
		as_dict=True,
	)
	return {cstr(row.parent): cint(row.mapped) for row in rows}


def mirror_payload(row, *, mapped_slot_count=None) -> dict:
	"""The frozen MetaTemplate shape the frontend is built against."""
	get = row.get if hasattr(row, "get") else (lambda key, default=None: getattr(row, key, default))
	meta_template_id = cstr(get("meta_template_id"))
	components = declared_components(row)
	# Same counting function validation and the send path use. None means the shape is
	# one this app cannot fill; the picker should treat that as "not ready" and let
	# validation give the reason.
	declared_count = declared_count_or_none(components)
	mapped = (
		mapped_slot_count
		if mapped_slot_count is not None
		else slot_map_sizes([meta_template_id]).get(meta_template_id, 0)
	)
	return {
		"meta_template_id": get("meta_template_id"),
		"name": get("template_name"),
		"language": get("language"),
		"category": get("category"),
		"status": get("status"),
		"rejected_reason": get("rejected_reason"),
		"components": components,
		"last_synced_at": get("last_synced_at"),
		"local_template_key": get("local_template"),
		# Meta no longer returns this template. The row is kept, not deleted, so its
		# local_template binding and operator-authored slot_map survive - but without
		# this projected the list showed a deleted template with a green APPROVED chip,
		# indistinguishable from a live one. status is Meta's last known word, not a
		# statement that the template still exists.
		"missing_on_meta": bool(cint(get("missing_on_meta"))),
		# How many {{n}} the backend counted across the components it can fill
		# (header and body). None when the declared shape is unsupported.
		"declared_count": declared_count,
		# A map exists whose size equals that count. Note this is False for a template
		# with no variables at all - none is needed, and none exists. A picker asking
		# "can a rule send this" wants `declared_count == 0 || slot_map_present`.
		"slot_map_present": bool(declared_count is not None and mapped > 0 and mapped == declared_count),
	}


def _load_components(value):
	if not value:
		return []
	try:
		return json.loads(value)
	except (TypeError, ValueError):
		return []


def encode_cursor(template_name, meta_template_id) -> str:
	raw = json.dumps([cstr(template_name), cstr(meta_template_id)], default=str)
	return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor):
	"""Decode an opaque list cursor, or None when it is absent or unreadable."""
	if not cursor:
		return None
	try:
		values = json.loads(base64.urlsafe_b64decode(cstr(cursor).encode()).decode())
	except (ValueError, binascii.Error, UnicodeDecodeError):
		frappe.throw(_("The paging cursor is not valid. Reload the template list."))
	if not isinstance(values, list) or len(values) != 2:
		frappe.throw(_("The paging cursor is not valid. Reload the template list."))
	return cstr(values[0]), cstr(values[1])


def list_mirror_templates(status=None, language=None, limit=None, after=None) -> dict:
	"""Keyset page over the mirror, ordered by (template_name, meta_template_id).

	Cursor paging, not offset: the cursor carries the last row's sort key, so a
	template inserted by a concurrent sync cannot shift a page boundary and hide a
	row. Reads the mirror only - Meta is not called, so the tab keeps working while
	Graph is unreachable.
	"""
	page_size = cint(limit) or META_TEMPLATE_PAGE_SIZE
	conditions = []
	values: list = []

	if status:
		conditions.append("`status` = %s")
		values.append(cstr(status))
	if language:
		conditions.append("`language` = %s")
		values.append(cstr(language))

	cursor = decode_cursor(after)
	if cursor:
		conditions.append("(`template_name` > %s or (`template_name` = %s and `meta_template_id` > %s))")
		values.extend([cursor[0], cursor[0], cursor[1]])

	where = f"where {' and '.join(conditions)}" if conditions else ""
	# One row beyond the page so we know whether a next cursor exists.
	values.append(page_size + 1)

	rows = frappe.db.sql(
		f"""
		select {", ".join(f"`{field}`" for field in MIRROR_FIELDS)}
		from `tab{MIRROR_DOCTYPE}`
		{where}
		order by `template_name` asc, `meta_template_id` asc
		limit %s
		""",
		values,
		as_dict=True,
	)

	has_more = len(rows) > page_size
	rows = rows[:page_size]
	paging = {}
	if has_more and rows:
		paging["after"] = encode_cursor(rows[-1].get("template_name"), rows[-1].get("meta_template_id"))

	mapped = slot_map_sizes([row.get("meta_template_id") for row in rows])
	return {
		"templates": [
			mirror_payload(row, mapped_slot_count=mapped.get(cstr(row.get("meta_template_id")), 0))
			for row in rows
		],
		"paging": paging,
	}


def get_mirror_template(meta_template_id) -> dict:
	name = cstr(meta_template_id).strip()
	if not name:
		frappe.throw(_("Meta template id is required."))
	if not frappe.db.exists(MIRROR_DOCTYPE, name):
		frappe.throw(_("Meta template {0} is not in the local mirror. Run a sync first.").format(name))
	return mirror_payload(frappe.get_doc(MIRROR_DOCTYPE, name))


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------


def validate_template_name(name: str) -> str:
	"""Meta's naming rule, enforced before the round trip.

	A format check, not a value list: lowercase letters, digits and underscores.
	Nothing validated this before, so a bad name only surfaced as a Graph rejection.
	"""
	value = cstr(name).strip()
	if not value:
		frappe.throw(_("Template name is required."))
	if not TEMPLATE_NAME_PATTERN.match(value):
		frappe.throw(
			_(
				"Template name {0} is not valid for Meta. Use lowercase letters, digits and "
				"underscores only - no spaces, capitals or other characters."
			).format(value)
		)
	if len(value) > TEMPLATE_NAME_MAX_LENGTH:
		frappe.throw(
			_("Template name is too long: {0} characters, Meta allows {1}.").format(
				len(value), TEMPLATE_NAME_MAX_LENGTH
			)
		)
	return value


def coerce_components(components) -> list:
	"""Accept components as a list or as a JSON string; never reshape them.

	Meta's component structure is passed through exactly as given. Note for callers
	building a BODY component: ``example.body_text`` is an array of arrays - one
	inner array per example set - and a flat array is rejected by Meta with an
	unhelpful message.
	"""
	if components is None or components == "":
		frappe.throw(_("Template components are required."))
	if isinstance(components, str):
		try:
			components = json.loads(components)
		except ValueError:
			frappe.throw(_("Template components must be valid JSON."))
	if isinstance(components, dict):
		components = [components]
	if not isinstance(components, list) or not components:
		frappe.throw(_("Template components must be a non-empty list."))
	return components


def create_meta_template(*, name, language, category, components, account=None) -> dict:
	"""Submit a new template to Meta and mirror what comes back."""
	template_name = validate_template_name(name)
	language_code = cstr(language).strip()
	if not language_code:
		frappe.throw(_("Template language is required."))
	resolved_category = meta_category_for(category)
	payload_components = coerce_components(components)

	client = MetaTemplateClient(account)
	response = client.create(
		name=template_name,
		language=language_code,
		category=resolved_category,
		components=payload_components,
	)

	meta_template_id = cstr(response.get("id")).strip()
	if not meta_template_id:
		frappe.throw(_("Meta accepted the template but returned no id."))

	# Mirror it now so the list reflects the new template without waiting for a
	# sync. Meta's create response is not a full template object, so raw_json holds
	# that response verbatim until the next sync replaces it with the real thing;
	# status and category are whatever Meta returned, never assumed.
	upsert_mirror_row(
		{
			"id": meta_template_id,
			"name": template_name,
			"language": language_code,
			"category": response.get("category") or resolved_category,
			"status": response.get("status"),
			"components": payload_components,
		},
		client.account,
		raw_override=response,
	)

	return {"meta_template_id": meta_template_id, "status": cstr(response.get("status"))}


def edit_meta_template(*, meta_template_id, components, account=None) -> dict:
	"""Replace a template's components on Meta, then mirror what Meta accepted.

	The mirror is written only after Meta confirms, so a refused edit leaves the local
	row describing what Meta actually holds. Writing it first meant a rejected edit
	still looked applied locally until the next sync quietly reverted it.

	Meta's edit response is ``{"success": true}`` - no status, no components. The
	returned ``status`` is therefore the mirror's own stored status, said plainly here
	rather than dressed up as something Meta reported. An edited template is normally
	re-reviewed, so expect the real status to change shortly, via a sync or the
	message_template_status_update webhook.
	"""
	template_id = cstr(meta_template_id).strip()
	if not template_id:
		frappe.throw(_("Meta template id is required."))
	payload_components = coerce_components(components)

	client = MetaTemplateClient(account)
	# Raises on a non-2xx and now also on a 200 carrying success: false, so reaching
	# the next line means Meta accepted the edit.
	client.edit(meta_template_id=template_id, components=payload_components)

	stored_status = ""
	if frappe.db.exists(MIRROR_DOCTYPE, template_id):
		doc = frappe.get_doc(MIRROR_DOCTYPE, template_id)
		# Same staleness comparison the sync makes, for the same reason: an edit that
		# changes the placeholder count leaves an operator's slot map describing a
		# template that no longer exists. Flag it here rather than waiting for a sync.
		# The map itself is never cleared or rewritten - that decision stands.
		doc.slot_map_stale = _slot_map_went_stale(doc, payload_components)
		doc.components_json = json.dumps(payload_components, default=str)
		doc.last_synced_at = now_datetime()
		doc.save(ignore_permissions=True)
		stored_status = cstr(doc.status)

	return {"meta_template_id": template_id, "status": stored_status}


def template_exists_on_meta(client, template_name) -> bool:
	"""Ask Meta whether a template of this name still exists on the WABA.

	Used instead of interpreting an error subcode. Meta's own error reference does not
	document subcode 2593002 - the one a delete-by-name returns for a template already
	gone - and states that error_subcode is "Deprecated. Will not be returned in v16.0+
	responses", yet v25.0 returns it. Destroying local data on the strength of an
	undocumented, officially deprecated field would be guessing.

	A GET of the template list answers the actual question directly, through a
	documented read endpoint. It costs one extra request, only on the failure path.
	"""
	wanted = cstr(template_name)
	after = None
	for _page in range(MAX_SYNC_PAGES):
		page = client.list_page(after=after)
		for template in page.get("data") or []:
			if cstr(template.get("name")) == wanted:
				return True
		paging = page.get("paging") or {}
		after = ((paging.get("cursors") or {}).get("after")) if paging.get("next") else None
		if not after:
			break
	return False


def _mirror_rows_named(account_name, template_name) -> list:
	return frappe.get_all(
		MIRROR_DOCTYPE,
		filters={"template_name": template_name, "provider_account": account_name},
		pluck="name",
		ignore_permissions=True,
	)


def _rules_referencing(mirror_names) -> list:
	"""Action rules configured to send one of these mirror rows.

	Checked before Meta is called, not after. Frappe's own link guard would otherwise
	refuse the local delete once the template was already gone from Meta, leaving the
	two sides disagreeing with no way back.
	"""
	if not mirror_names:
		return []
	return frappe.get_all(
		"Pet App WhatsApp Action Rule",
		filters={"meta_template": ["in", list(mirror_names)]},
		pluck="name",
		ignore_permissions=True,
	)


def delete_meta_template(*, name, account=None) -> dict:
	"""Delete a template at Meta and remove its mirror rows locally.

	Delete means delete, on both sides. The mirror rows go, and the operator-authored
	slot_map on them goes with them - an accepted consequence, not an oversight.

	Three outcomes, distinguished without ever reading an error subcode:

	1. Meta confirms -> rows removed.
	2. Meta refuses, and a GET confirms the template is not on the WABA -> it is
	   already gone, which is the outcome asked for, so the rows are removed and this
	   reports success.
	3. Meta refuses for any other reason, or the confirming GET cannot be made -> the
	   original error is raised and nothing local changes. An ambiguous failure never
	   destroys local data.
	"""
	template_name = cstr(name).strip()
	if not template_name:
		frappe.throw(_("Template name is required."))

	client = MetaTemplateClient(account)
	mirror_names = _mirror_rows_named(client.account.name, template_name)

	blocking_rules = _rules_referencing(mirror_names)
	if blocking_rules:
		frappe.throw(
			_(
				"Template {0} is used by WhatsApp action rule(s): {1}. Point those rules at "
				"another template, or delete them, before deleting this template."
			).format(template_name, ", ".join(sorted(blocking_rules))),
			title=_("Template is in use"),
		)

	already_absent = False
	try:
		client.delete(name=template_name)
	except WhatsAppMetaAPIError as exc:
		try:
			still_on_meta = template_exists_on_meta(client, template_name)
		except Exception:
			# Could not confirm either way - the delete failure stands unchanged.
			raise exc
		if still_on_meta:
			raise
		already_absent = True

	removed = []
	for row in mirror_names:
		# force=1 bypasses Frappe's link guard deliberately. Notification Queue rows
		# carry meta_template as a record of what was sent; that history must not make a
		# template undeletable, and a dangling link on a past queue row harms nothing.
		# Live configuration - action rules - was already refused above.
		frappe.delete_doc(MIRROR_DOCTYPE, row, force=1, ignore_permissions=True, ignore_missing=True)
		removed.append(row)

	return {
		"ok": True,
		"name": template_name,
		"removed_mirror_rows": removed,
		# True when Meta had already lost the template and only local work remained.
		"already_absent_on_meta": already_absent,
	}
