"""Pet App Template Category: the screens a WhatsApp template can be filed under.

The doctype is DB-only (created by patch p1_28, extended by p1_29 and p1_30) and has no
controller module, so its validation lives here and is wired through ``doc_events`` in
hooks.py.

A category is bound to where it applies by two optional keys:

* ``page_key`` - a page, the ``page.*`` key from Pet App Access Settings -> Page Access.
* ``surface_key`` - one send dialog or button on a page.

Since p1_31 the binding lives on the SCREEN, not the category: one ``Pet App Template
Screen`` record per page or send surface, listing the categories that screen shows. A screen
may show several categories and a category may appear on several screens. The category's
own ``page_key`` / ``surface_key`` are kept read-only for reference and are only read on a
site where p1_31 has not run yet.

RESOLUTION ORDER, ONE RULE: the surface's categories, if its screen record lists any
enabled one; otherwise the page's; otherwise none, and the screen gets the full catalogue.
The surface REPLACES the page - it does not add to it. See ``resolve_category``.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr

CATEGORY_DOCTYPE = "Pet App Template Category"
SCREEN_DOCTYPE = "Pet App Template Screen"
SCREEN_CATEGORY_DOCTYPE = "Pet App Template Screen Category"
SCREEN_CATEGORIES_FIELD = "categories"

# The public fields of a category, in the order every endpoint returns them.
CATEGORY_FIELDS = ("name", "category_name", "label_en", "label_ar")


class KeyTakenError(frappe.ValidationError):
	"""Another category already claims this page or surface."""

	code = "KEY_TAKEN"

	def __init__(self, message, details=None):
		super().__init__(message)
		self.exc_type = self.code
		self.details = details or {}


class PageKeyTakenError(KeyTakenError):
	code = "PAGE_KEY_TAKEN"


class SurfaceKeyTakenError(KeyTakenError):
	code = "SURFACE_KEY_TAKEN"


class ScreenKeyError(KeyTakenError):
	"""A screen record names no screen, or names both a page and a surface."""

	code = "SCREEN_KEY_INVALID"


# fieldname -> (refusal, the label used in its message)
BINDING_KEYS = {
	"page_key": (PageKeyTakenError, "Screen"),
	"surface_key": (SurfaceKeyTakenError, "Send surface"),
}


def surface_key_options() -> list[dict]:
	"""What the desk picker for ``surface_key`` offers: every registered send surface.

	Read from ``send_targets.SEND_SURFACES`` at call time, so a surface added to the
	registry appears in the picker with the deploy that adds it - there is no second copy
	to re-seed. Any value already stored on a category that the registry does NOT hold is
	appended, labelled as such, so the picker still shows and can keep it: a value set
	before a registry key was renamed must stay visible, not be silently blanked.

	Returns ``[{"value", "label", "description", "registered"}]``, registry first by label.
	"""
	from pet_app.notifications.send_targets import SEND_SURFACES

	options = [
		{"value": key, "label": label, "description": key, "registered": True}
		for key, label in sorted(SEND_SURFACES.items(), key=lambda item: (item[1].lower(), item[0]))
	]
	if has_surface_key():
		options += _stored_outside("surface_key", set(SEND_SURFACES))
	return options


PAGE_REGISTRY = ("Pet App Access Settings", "Pet App Page Access", "pages")


def registered_pages() -> list[dict]:
	"""Every row of Pet App Access Settings -> Page Access, read now: ``[{page_key, label, enabled}]``.

	Data, not code - pages are added and relabelled in desk - so this is never cached.
	"""
	settings, child, parentfield = PAGE_REGISTRY
	if not frappe.db.exists("DocType", child):
		return []
	return frappe.get_all(
		child,
		filters={"parent": settings, "parenttype": settings, "parentfield": parentfield},
		fields=["page_key", "label", "enabled"],
		order_by="idx asc",
		ignore_permissions=True,
	)


def page_key_options() -> list[dict]:
	"""What the desk picker for ``page_key`` offers: every page in the Page Access registry.

	Shown by the page's label with its key underneath, sorted by label. A disabled page is
	still offered, marked as such: a category may be bound ahead of the page being switched
	back on, and hiding it would hide a stored binding's meaning too. Any value already
	stored on a category that matches no Page Access row is appended as "(not in registry)",
	exactly as for surface_key, so the picker never blanks it.

	Returns ``[{"value", "label", "description", "registered"}]``, registry first by label.
	"""
	pages = [row for row in registered_pages() if cstr(row.page_key).strip()]
	options = [
		{
			"value": row.page_key,
			"label": (cstr(row.label) or row.page_key)
			+ ("" if row.enabled else " " + _("(page disabled)")),
			"description": row.page_key,
			"registered": True,
		}
		for row in sorted(pages, key=lambda row: ((cstr(row.label) or row.page_key).lower(), row.page_key))
	]
	if has_page_key():
		options += _stored_outside("page_key", {row.page_key for row in pages})
	return options


def _stored_outside(fieldname: str, known: set) -> list[dict]:
	"""Values stored on any screen's or category's ``fieldname`` that ``known`` does not hold.

	Both holders are read: the screen records are where bindings live now, and the
	category's legacy value is still stored and must not vanish from a picker either.
	"""
	stored = set()
	for doctype in (SCREEN_DOCTYPE, CATEGORY_DOCTYPE):
		if frappe.db.exists("DocType", doctype) and frappe.db.has_column(doctype, fieldname):
			stored.update(
				frappe.get_all(doctype, filters={fieldname: ["is", "set"]}, pluck=fieldname, ignore_permissions=True)
			)
	return [
		{"value": key, "label": _("{0} (not in registry)").format(key), "description": key, "registered": False}
		for key in sorted({cstr(value).strip() for value in stored} - known)
	]


def has_page_key() -> bool:
	"""False until patch p1_29 has run on this site. Readers must not assume the column."""
	return bool(frappe.db.has_column(CATEGORY_DOCTYPE, "page_key"))


def has_surface_key() -> bool:
	"""False until patch p1_30 has run on this site. Readers must not assume the column."""
	return bool(frappe.db.has_column(CATEGORY_DOCTYPE, "surface_key"))


def has_screens() -> bool:
	"""False until patch p1_31 has run. Until then the category-held keys answer, as before."""
	return bool(frappe.db.exists("DocType", SCREEN_DOCTYPE)) and bool(
		frappe.db.exists("DocType", SCREEN_CATEGORY_DOCTYPE)
	)


def binding_fields() -> list[str]:
	"""The binding keys this site's table actually has."""
	return [field for field, present in (("page_key", has_page_key()), ("surface_key", has_surface_key())) if present]


def validate_template_category(doc, method=None):
	"""Normalise ``page_key`` and ``surface_key``; refuse one another category already holds.

	Each is trimmed, and blank stored as None, so "unbound" is one value rather than
	several. The duplicate check is case-insensitive to agree with the unique index, which
	uses the table's case-insensitive collation: without that, "Page.X" would pass here and
	fail at the index with Frappe's generic "must be unique" message.

	The two keys are checked independently: a page key and a surface key live in different
	namespaces, so one category's page_key never collides with another's surface_key.

	Since p1_31 these two fields are read-only legacy values on the category; the same rules
	apply to the screen record, see ``validate_template_screen``.
	"""
	_validate_binding_keys(doc, CATEGORY_DOCTYPE, _("category"))


def validate_template_screen(doc, method=None):
	"""A screen record: exactly one key, unique per screen, and a clean category list.

	* One of ``page_key`` / ``surface_key``, never both: a record is ONE screen. A dialog
	  and the page it sits on are two screens, and two records.
	* Unique: a page (or surface) has one record, and that record holds its whole list - so
	  a second record for the same screen is refused with PAGE_KEY_TAKEN /
	  SURFACE_KEY_TAKEN, naming the record that already holds it.
	* A category listed twice is kept once, in its first position.
	* ``label`` defaults to the page's label from Page Access, or to the key.
	"""
	_validate_binding_keys(doc, SCREEN_DOCTYPE, _("screen"))
	page, surface = doc.get("page_key"), doc.get("surface_key")
	if bool(page) == bool(surface):
		message = (
			_("A screen is either a page or a send surface. Set Page Key or Surface Key, not both.")
			if page
			else _("Choose the page or the send surface this screen record is for.")
		)
		frappe.msgprint(message, title=_("Screen key"), indicator="red")
		raise ScreenKeyError(message, details={"page_key": page, "surface_key": surface})
	seen, rows = set(), []
	for row in doc.get(SCREEN_CATEGORIES_FIELD) or []:
		if row.category and row.category not in seen:
			seen.add(row.category)
			rows.append(row)
	if len(rows) != len(doc.get(SCREEN_CATEGORIES_FIELD) or []):
		doc.set(SCREEN_CATEGORIES_FIELD, rows)
	if not cstr(doc.get("label")).strip():
		labels = {row.page_key: row.label for row in registered_pages()}
		doc.label = (labels.get(page) if page else None) or page or surface


def _validate_binding_keys(doc, doctype: str, holder_noun: str):
	for fieldname, (error, label) in BINDING_KEYS.items():
		if not doc.meta.has_field(fieldname):
			continue
		value = cstr(doc.get(fieldname)).strip()
		doc.set(fieldname, value or None)
		if not value:
			continue
		holder = frappe.db.sql(
			f"select name from `tab{doctype}` where lower(`{fieldname}`) = lower(%s) and name != %s limit 1",
			(value, doc.name or ""),
		)
		if holder:
			message = _(
				"{0} {1} is already assigned to the {2} {3}. It can belong to one {2} only - edit that one instead."
			).format(_(label), frappe.bold(value), holder_noun, frappe.bold(holder[0][0]))
			# msgprint so desk shows the reason in its dialog; the typed exception so API
			# callers get the code and the holder in details. `category` keeps its name for
			# callers of the category form; `holder` says which record it is on a screen.
			frappe.msgprint(message, title=_("{0} already assigned").format(_(label)), indicator="red")
			raise error(message, details={fieldname: value, "category": holder[0][0], "holder": holder[0][0]})

	_warn_unregistered_surface(doc)
	_warn_unregistered_page(doc)


def _newly_set(doc, fieldname: str) -> str | None:
	"""The value of ``fieldname`` if this save sets or changes it, else None."""
	if not doc.meta.has_field(fieldname):
		return None
	value = doc.get(fieldname)
	if not value or not doc.has_value_changed(fieldname):
		return None
	return value


def _warn_unregistered_surface(doc):
	"""A NEWLY SET surface_key the registry does not hold is saved, with a warning.

	Not refused: a value outside the registry must stay valid - one set before a key was
	renamed, or written through the API. Only a changed value warns, so an existing one
	does not nag on every save of the category.
	"""
	value = _newly_set(doc, "surface_key")
	if not value:
		return
	from pet_app.notifications.send_targets import SEND_SURFACES

	if value not in SEND_SURFACES:
		frappe.msgprint(
			_("Send surface {0} is not in the send surface registry, so no dialog will match it until one does. It was saved.").format(
				frappe.bold(value)
			),
			title=_("Unknown send surface"),
			indicator="orange",
		)


def _warn_unregistered_page(doc):
	"""A NEWLY SET page_key that matches no Page Access row is saved, with a warning.

	Not refused: a category may be set up before its page row exists in Pet App Access
	Settings. A disabled page row still counts as a match - the page exists. Compared
	case-insensitively, like the uniqueness check.
	"""
	value = _newly_set(doc, "page_key")
	if not value:
		return
	if value.lower() not in {cstr(row.page_key).strip().lower() for row in registered_pages()}:
		frappe.msgprint(
			_("Page {0} matches no row in Pet App Access Settings -> Page Access, so no screen will resolve to this category until one does. It was saved.").format(
				frappe.bold(value)
			),
			title=_("Unknown page"),
			indicator="orange",
		)


def _screen_categories(fieldname: str, key: str) -> list[str]:
	"""The enabled categories a screen lists, in the screen's own order. [] when unbound.

	Before p1_31 the category-held key answers instead - at most one category, exactly the
	pre-p1_31 behaviour - so this module works on either side of the migration.
	"""
	if not key:
		return []
	if has_screens():
		screen = frappe.db.get_value(SCREEN_DOCTYPE, {fieldname: key, "enabled": 1}, "name")
		if not screen:
			return []
		names = frappe.get_all(
			SCREEN_CATEGORY_DOCTYPE,
			filters={"parent": screen, "parenttype": SCREEN_DOCTYPE, "parentfield": SCREEN_CATEGORIES_FIELD},
			pluck="category",
			order_by="idx asc",
			ignore_permissions=True,
		)
	elif fieldname in binding_fields():
		names = frappe.get_all(CATEGORY_DOCTYPE, filters={fieldname: key}, pluck="name", limit=1, ignore_permissions=True)
	else:
		return []
	if not names:
		return []
	enabled = set(
		frappe.get_all(CATEGORY_DOCTYPE, filters={"name": ["in", names], "enabled": 1}, pluck="name", ignore_permissions=True)
	)
	ordered = []
	for name in names:
		if name in enabled and name not in ordered:
			ordered.append(name)
	return ordered


def category_bindings() -> dict[str, dict[str, list[str]]]:
	"""Every category's screens: ``{category: {"page_key": [...], "surface_key": [...]}}``.

	From the enabled screen records once p1_31 has run; from the category's own keys before.
	One query either way.
	"""
	bindings: dict[str, dict[str, list[str]]] = {}
	if has_screens():
		rows = frappe.db.sql(
			f"""
			select c.category, s.page_key, s.surface_key
			from `tab{SCREEN_CATEGORY_DOCTYPE}` c
			join `tab{SCREEN_DOCTYPE}` s on s.name = c.parent
			where c.parenttype = %s and c.parentfield = %s and s.enabled = 1
			order by s.page_key, s.surface_key
			""",
			(SCREEN_DOCTYPE, SCREEN_CATEGORIES_FIELD),
			as_dict=True,
		)
	else:
		fields = binding_fields()
		rows = [
			{"category": row.name, **{field: row.get(field) for field in fields}}
			for row in frappe.get_all(CATEGORY_DOCTYPE, fields=["name"] + fields, ignore_permissions=True)
		]
	for row in rows:
		entry = bindings.setdefault(row["category"], {"page_key": [], "surface_key": []})
		for field in ("page_key", "surface_key"):
			value = row.get(field)
			if value and value not in entry[field]:
				entry[field].append(value)
	return bindings


def category_payload(row, bindings: dict | None = None) -> dict:
	"""One category in the shape every endpoint returns.

	``page_keys`` / ``surface_keys`` are every screen the category appears on. The single
	``page_key`` / ``surface_key`` are kept for callers that read one value: the key when the
	category is on exactly one such screen, else None - one screen is the only case where a
	single value is the whole truth, which is also every category on the day p1_31 runs.
	"""
	if bindings is None:
		bindings = category_bindings()
	bound = bindings.get(row.get("name"), {})
	page_keys = list(bound.get("page_key") or [])
	surface_keys = list(bound.get("surface_key") or [])
	data = {field: row.get(field) for field in CATEGORY_FIELDS}
	data["page_key"] = page_keys[0] if len(page_keys) == 1 else None
	data["surface_key"] = surface_keys[0] if len(surface_keys) == 1 else None
	data["page_keys"] = page_keys
	data["surface_keys"] = surface_keys
	return data


def category_payloads(names: list[str], bindings: dict | None = None) -> list[dict]:
	"""``category_payload`` for these names, in this order. Missing names are dropped."""
	if not names:
		return []
	if bindings is None:
		bindings = category_bindings()
	rows = {
		row.name: row
		for row in frappe.get_all(
			CATEGORY_DOCTYPE, filters={"name": ["in", names]}, fields=list(CATEGORY_FIELDS), ignore_permissions=True
		)
	}
	return [category_payload(rows[name], bindings) for name in names if name in rows]


def resolve_category(surface_key=None, page_key=None) -> dict:
	"""The categories a screen should pass to ``list_template_options``, and why.

	ORDER: the surface's enabled categories, if its screen record lists any; otherwise the
	page's; otherwise none - the screen passes nothing and gets the full catalogue. Only
	ENABLED screens and categories count; a screen whose categories are all disabled is
	treated as unbound, so the next rule is tried.

	THE SURFACE REPLACES THE PAGE, it does not add to it. A dialog-level binding is the
	narrower, more deliberate one, and replacement is the only rule that can express a
	dialog showing FEWER categories than its page. A dialog meant to show everything its
	page shows plus more lists those categories itself. When the surface overrides a page
	with a different set, the page's set is returned in ``page_categories`` so it is visible.

	Returns ``{surface_key, page_key, categories, category, resolved_by, page_categories,
	page_category}``. ``categories`` is the full list. ``category`` and ``page_category`` are
	the single-value fields older callers read: the category when there is exactly one,
	None when there are several - see ``api.notifications.get_surface_category``.
	"""
	surface = cstr(surface_key).strip()
	page = cstr(page_key).strip()
	by_surface = _screen_categories("surface_key", surface)
	by_page = _screen_categories("page_key", page)

	names = by_surface or by_page
	resolved_by = "surface" if by_surface else ("page" if by_page else None)
	overridden = by_page if (by_surface and by_page and set(by_page) != set(by_surface)) else []

	bindings = category_bindings()
	categories = category_payloads(names, bindings)
	return {
		"surface_key": surface or None,
		"page_key": page or None,
		"category": categories[0] if len(categories) == 1 else None,
		"resolved_by": resolved_by,
		"page_category": overridden[0] if len(overridden) == 1 else None,
		"categories": categories,
		"page_categories": overridden,
	}
