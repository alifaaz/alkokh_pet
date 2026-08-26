from __future__ import annotations

import json
import re
from decimal import Decimal

import frappe
from frappe import _
from frappe.utils import cstr

from pet_app.notifications.context import mask_sensitive_context


def _get_message_jenv():
	"""A message-body Jinja environment that renders missing variables as blanks.

	The shared Frappe environment uses ``DebugUndefined``, which raises on any
	attribute/item access against an undefined value (e.g. ``{{ pet.pet_name }}``
	when there is no related pet) and leaks raw ``{{ ... }}`` tokens into the
	message for a bare undefined. Neither is acceptable for a WhatsApp message
	sent to a guardian, so we render with a forgiving undefined instead.
	"""
	jenv = getattr(frappe.local, "pet_app_message_jenv", None)
	if jenv is not None:
		return jenv

	from jinja2 import ChainableUndefined

	class BlankUndefined(ChainableUndefined):
		"""Undefined that quietly renders as an empty string.

		``ChainableUndefined`` already makes ``pet.pet_name`` / ``pet['name']``
		safe when ``pet`` is missing; overriding ``__str__`` ensures the final
		render is a blank rather than a leaked token.
		"""

		__slots__ = ()

		def __str__(self):
			return ""

	base = frappe.get_jenv()
	jenv = base.overlay(undefined=BlankUndefined)
	frappe.local.pet_app_message_jenv = jenv
	return jenv


def render_message(body: str, context: dict | None = None) -> str:
	"""Render a message body, tolerating missing pet/guardian/etc. variables."""
	body = cstr(body)
	if not body:
		return ""
	if ".__" in body:
		frappe.throw(frappe._("Illegal template"))
	try:
		compiled = _get_message_jenv().from_string(body)
		return compiled.render(context or {})
	except Exception:
		frappe.log_error(
			title="WhatsApp template render failed",
			message=frappe.get_traceback(),
		)
		# Never let a broken template crash the send; drop unresolved tokens.
		return _strip_jinja(body)


def _strip_jinja(body: str) -> str:
	import re

	body = re.sub(r"{%.*?%}", "", body, flags=re.DOTALL)
	body = re.sub(r"{{.*?}}", "", body, flags=re.DOTALL)
	return body.strip()


def render_preview(template_doc, context: dict | None = None, *, mask_sensitive: bool = True) -> str:
	body = cstr(template_doc.get("body_preview") or template_doc.get("template_name") or template_doc.get("template_key"))
	render_context = mask_sensitive_context(context or {}) if mask_sensitive else (context or {})
	return render_message(body, render_context)


# Meta's declared parameter formats. POSITIONAL is also the assumed default when a
# template declares no parameter_format at all: it is Meta's historical default, and
# every template on this WABA (16/16) declares it explicitly. Refusing on an absent
# value would block templates that have no placeholders and would have sent fine.
POSITIONAL_FORMAT = "POSITIONAL"
NAMED_FORMAT = "NAMED"

# Where an operator's ordered parameter values live inside the notification context.
# The context stays a dict - coerce_context is unchanged - and the list lives under
# this one reserved key.
CONTEXT_PARAMETERS_KEY = "parameters"

# Declared component types this builder can emit parameters for, mapped to the
# lowercase type Meta expects on the wire. A component that declares placeholders and
# is not in here is refused rather than guessed at; no template on this WABA has one.
PARAMETERISABLE_COMPONENTS = {"BODY": "body", "HEADER": "header"}

PLACEHOLDER_PATTERN = re.compile(r"{{\s*([A-Za-z0-9_]+)\s*}}")


def declared_placeholders(component) -> list[str]:
	"""The placeholder tokens a declared component carries, in order of appearance."""
	seen: list[str] = []
	for token in PLACEHOLDER_PATTERN.findall(cstr(component.get("text"))):
		if token not in seen:
			seen.append(token)
	return seen


def declared_parameter_count(components) -> int:
	"""How many values a declared template expects, across all its components.

	The same walk build_declared_parameters does, exposed on its own so a stored slot
	map can be checked against the template at save time rather than at send time.
	"""
	return sum(len(tokens) for _wire_type, tokens in _declared_plan(components, _("this template")))


def build_declared_parameters(*, components, parameter_format, context, template_label="") -> list:
	"""Outgoing Meta components, built from what Meta declares about the template.

	The declared shape - which components take parameters, how many, and whether they
	are positional - comes from the mirror row and nothing else. It is never inferred
	from the local template row, and the parameter *format* is never guessed from the
	presence of braces in the text.

	Values come from ``context["parameters"]``, an ordered list. When a template
	declares parameters across more than one component, the list is consumed in Meta's
	own declared component order (header before body, as the components array orders
	them), because Meta numbers each component's placeholders from 1 independently.

	Counts must match exactly in both directions. Too few and the message renders with
	holes; too many and values an operator typed would be silently dropped. Both refuse.
	"""
	from pet_app.notifications.meta_templates import MetaTemplateNotSendable

	label = cstr(template_label) or _("this template")
	fmt = cstr(parameter_format).strip() or POSITIONAL_FORMAT

	if fmt == NAMED_FORMAT:
		# Deliberately unimplemented rather than speculatively implemented: no template
		# on this WABA is NAMED, and a guessed shape would fail at Meta after the fact.
		raise MetaTemplateNotSendable(
			_(
				"Template {0} uses named parameters, which this app cannot send yet. "
				"Use a template with positional parameters, or report this template."
			).format(label),
			code="META_TEMPLATE_PARAM_INVALID",
			details={"parameter_format": fmt},
		)
	if fmt != POSITIONAL_FORMAT:
		raise MetaTemplateNotSendable(
			_("Template {0} declares an unrecognised parameter format {1}.").format(label, fmt),
			code="META_TEMPLATE_PARAM_INVALID",
			details={"parameter_format": fmt},
		)

	plan = _declared_plan(components, label)
	declared_total = sum(len(tokens) for _out_type, tokens in plan)
	values = _supplied_values(context, label)

	if len(values) != declared_total:
		raise MetaTemplateNotSendable(
			_(
				"Template {0} expects {1} parameter value(s) and {2} were supplied. "
				"Send exactly {1}, in the order they appear in the message."
			).format(label, declared_total, len(values)),
			code="META_TEMPLATE_PARAM_COUNT",
			details={"declared": declared_total, "supplied": len(values)},
		)

	if not declared_total:
		return []

	texts = [_scalar_text(value, index, label) for index, value in enumerate(values)]

	built = []
	cursor = 0
	for out_type, tokens in plan:
		chunk = texts[cursor : cursor + len(tokens)]
		cursor += len(tokens)
		built.append({"type": out_type, "parameters": [{"type": "text", "text": text} for text in chunk]})
	return built


def preview_declared_message(components, values) -> str:
	"""Human-readable preview of a declared template with its values substituted.

	Lives next to build_declared_parameters and walks the components with the same
	declared_placeholders / PARAMETERISABLE_COMPONENTS rules, in the same order, so a
	preview cannot show values in one order while the send puts them in another.

	Deliberately tolerant, because a preview is not a send: a short value list leaves
	the remaining placeholders standing rather than raising, which is how an
	unresolved slot stays visible instead of turning into a blank.
	"""
	rendered = []
	cursor = 0
	for component in components or []:
		text = cstr(component.get("text"))
		if not text:
			continue
		tokens = declared_placeholders(component)
		if tokens and PARAMETERISABLE_COMPONENTS.get(cstr(component.get("type")).upper()):
			for token in tokens:
				if cursor >= len(values):
					break
				text = re.sub(r"{{\s*" + re.escape(token) + r"\s*}}", lambda _m, v=cstr(values[cursor]): v, text)
				cursor += 1
		rendered.append(text)
	return "\n\n".join(rendered)


def _declared_plan(components, label) -> list[tuple[str, list[str]]]:
	"""(wire type, tokens) for each declared component that takes parameters."""
	from pet_app.notifications.meta_templates import MetaTemplateNotSendable

	plan: list[tuple[str, list[str]]] = []
	for component in components or []:
		tokens = declared_placeholders(component)
		if not tokens:
			continue
		declared_type = cstr(component.get("type")).upper()
		wire_type = PARAMETERISABLE_COMPONENTS.get(declared_type)
		if not wire_type:
			raise MetaTemplateNotSendable(
				_(
					"Template {0} takes parameters in its {1} component, which this app "
					"cannot fill. Use a template whose variables are in the body."
				).format(label, declared_type or "?"),
				code="META_TEMPLATE_PARAM_INVALID",
				details={"component": declared_type},
			)
		_assert_contiguous_positions(tokens, declared_type, label)
		plan.append((wire_type, tokens))
	return plan


def _assert_contiguous_positions(tokens, declared_type, label):
	"""A positional component must number its placeholders 1..n with no gaps.

	Without this, {{1}} {{3}} would be filled by order of appearance and the operator's
	second value would land in the third slot.
	"""
	from pet_app.notifications.meta_templates import MetaTemplateNotSendable

	if [token for token in tokens if not token.isdigit()] or sorted(int(t) for t in tokens) != list(
		range(1, len(tokens) + 1)
	):
		raise MetaTemplateNotSendable(
			_(
				"Template {0} numbers the variables in its {1} component as {2}, which is "
				"not a complete 1-to-{3} sequence. Fix the template on Meta and re-sync."
			).format(label, declared_type, ", ".join(tokens), len(tokens)),
			code="META_TEMPLATE_PARAM_INVALID",
			details={"component": declared_type, "tokens": list(tokens)},
		)


def _supplied_values(context, label) -> list:
	"""The ordered values an operator supplied, from context["parameters"]."""
	from pet_app.notifications.meta_templates import MetaTemplateNotSendable

	if not isinstance(context, dict):
		return []
	raw = context.get(CONTEXT_PARAMETERS_KEY)
	if raw is None:
		return []
	if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
		raise MetaTemplateNotSendable(
			_(
				"Template {0} needs its parameter values as an ordered list under "
				'"parameters", for example ["Ali", "Leo"].'
			).format(label),
			code="META_TEMPLATE_PARAM_INVALID",
			details={"supplied_type": type(raw).__name__},
		)
	return list(raw)


def _scalar_text(value, index, label) -> str:
	"""One parameter value as text, or a refusal.

	A dict or a list is a caller error, not something to serialize: JSON-dumping it
	would put a raw blob into a customer's message. None is a missing value, not an
	empty one. Numbers are coerced; booleans are not, because "True" in a message is
	always a bug.
	"""
	from pet_app.notifications.meta_templates import MetaTemplateNotSendable

	if isinstance(value, str):
		return value
	if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
		return cstr(value)

	raise MetaTemplateNotSendable(
		_(
			"Parameter {0} for template {1} is a {2}. Parameter values must be text or "
			"numbers - build the value before sending it."
		).format(index + 1, label, type(value).__name__),
		code="META_TEMPLATE_PARAM_INVALID",
		details={"position": index + 1, "type": type(value).__name__},
	)

