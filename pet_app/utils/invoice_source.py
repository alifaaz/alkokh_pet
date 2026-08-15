"""Per-line provenance markers on Sales Invoice Item descriptions.

One invoice can now carry lines from several visits and boardings, so every line has to
say where it came from. The marker is a trailing line on the item description:

    Rabies Vaccine
    [alkokh-source-group:Vet Visit:HLC-VIS-2026-00042]

Appended, never replacing, because _invoice_item_matches_billable compares descriptions
when it matches a billable row to its invoice line - so the human part has to survive
intact and be recoverable with strip_markers().

The format is the one the frontend already parses (parseInvoiceItemSource); backend
lines carried nothing until now, which is what made a merged invoice an undifferentiated
list with no way to reverse one visit's charges.
"""

from __future__ import annotations

import re

from frappe.utils import cstr

MARKER_PREFIX = "alkokh-source-group"
_MARKER_RE = re.compile(r"^\[" + re.escape(MARKER_PREFIX) + r":([^:\]]+):([^\]]+)\]$")


def build_marker(doctype: str, name: str) -> str:
    return f"[{MARKER_PREFIX}:{cstr(doctype).strip()}:{cstr(name).strip()}]"


def append_marker(description, doctype: str, name: str) -> str:
    """Add the marker as the last line, unless it is already there."""
    marker = build_marker(doctype, name)
    body = cstr(description).rstrip()
    if marker in body:
        return body
    return f"{body}\n{marker}" if body else marker


def parse_markers(description) -> list[tuple[str, str]]:
    """Every (doctype, name) marker on a description, in order."""
    found = []
    for line in cstr(description).splitlines():
        match = _MARKER_RE.match(line.strip())
        if match:
            found.append((match.group(1).strip(), match.group(2).strip()))
    return found


def has_marker(description, doctype: str, name: str) -> bool:
    return (cstr(doctype).strip(), cstr(name).strip()) in parse_markers(description)


def strip_markers(description) -> str:
    """The description as a human wrote it, with provenance lines removed."""
    kept = [
        line for line in cstr(description).splitlines()
        if not _MARKER_RE.match(line.strip())
    ]
    return "\n".join(kept).strip()
