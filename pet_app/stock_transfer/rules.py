"""Contract validation, independent of the database and transport."""

import hashlib
import json
import math
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlsplit


class TransferError(Exception):
	def __init__(self, message, code="VALIDATION_ERROR"):
		super().__init__(message)
		self.code = code


def fail(message, code="VALIDATION_ERROR"):
	raise TransferError(message, code)


def quantity(value, *, precision=6, whole=False):
	if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
		fail("Quantities must be JSON numbers.")
	result = float(value)
	if not math.isfinite(result) or result < 0:
		fail("Quantities must be finite and nonnegative.")
	if Decimal(str(value)) != Decimal(str(value)).quantize(Decimal(10) ** -precision):
		fail(f"Quantity exceeds the permitted precision of {precision}.")
	if whole and not result.is_integer():
		fail("This unit requires whole quantities.")
	return result


def stock_units(qty, factor):
	return float(Decimal(str(qty)) * Decimal(str(factor)))


def same(a, b):
	return abs(a - b) < 1e-8


def rows(value):
	if not isinstance(value, list) or any(not isinstance(r, dict) for r in value):
		fail("items must be an array of objects.")
	ids = [r.get("line_id") for r in value]
	if any(not isinstance(i, str) or not i or len(i) > 140 for i in ids) or len(set(ids)) != len(ids):
		fail("Each line must have a unique, nonempty line_id.")
	return value


def snapshot(value, lines, complete=False):
	result = rows(value)
	ids = {r["line_id"] for r in result}
	if not ids <= set(lines) or (complete and ids != set(lines)):
		fail("Provide this order's line IDs; a complete snapshot is required for preparation and reduction.")
	return result


def instant(value, label):
	if not isinstance(value, str):
		fail(f"{label} must be an ISO-8601 instant with a timezone.")
	try:
		dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
	except ValueError:
		fail(f"{label} must be an ISO-8601 instant with a timezone.")
	if dt.tzinfo is None:
		fail(f"{label} requires a timezone offset.")
	return dt


def shipment(payload, previous=None):
	fields = (
		"driver_name",
		"driver_contact",
		"vehicle",
		"packages",
		"seal",
		"departure_time",
		"expected_arrival",
	)
	if set(payload) != set(fields):
		fail("Provide the complete shipment snapshot.")
	result = dict(payload)
	for key in ("driver_name", "driver_contact", "vehicle", "seal"):
		if not isinstance(result[key], str):
			fail(f"{key} must be text.")
		result[key] = result[key].strip()
	if not result["driver_name"]:
		fail("Driver name is required for dispatch.")
	departure = instant(result["departure_time"], "Departure time")
	if previous and departure != instant(previous["departure_time"], "Departure time"):
		fail("The recorded departure time cannot be changed.")
	if previous:
		result["departure_time"] = previous["departure_time"]
	if result["expected_arrival"] and instant(result["expected_arrival"], "Expected arrival") < departure:
		fail("Expected arrival cannot precede departure.")
	if result["packages"] is not None:
		quantity(result["packages"], whole=True)
	return result


def public_url(value):
	try:
		parts = urlsplit(value or "")
		valid = (
			parts.scheme in ("http", "https")
			and parts.hostname
			and not parts.username
			and not parts.password
			and not parts.query
			and parts.fragment in ("", "/")
			and not any(c.isspace() for c in value)
		)
		_ = parts.port
	except (TypeError, ValueError):
		valid = False
	if not valid:
		fail("Configure an HTTP(S) public frontend base URL without credentials or query.")
	return value.rstrip("/") + "/"


def digest(value):
	return hashlib.sha256(
		json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
	).hexdigest()
