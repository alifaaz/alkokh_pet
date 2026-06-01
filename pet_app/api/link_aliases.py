from __future__ import annotations

from collections.abc import Iterable

import frappe
from frappe.utils import cstr


PET_FIELDS = ("pet_id", "pet", "animal_patient", "custom_pet")
GUARDIAN_FIELDS = ("guardian_id", "guardian", "primary_guardian", "custom_guardian")
DOCTOR_FIELDS = ("doctor", "custom_doctor", "practitioner")
PROVIDER_FIELDS = ("provider",)


def enrich_link_aliases(
	rows,
	*,
	pet_field: str | None = None,
	guardian_field: str | None = None,
	doctor_field: str | None = None,
	provider_field: str | None = None,
	include_pet: bool = True,
	include_guardian: bool = True,
	include_doctor: bool = True,
	include_provider: bool = True,
):
	"""Add flat display aliases for common clinical link fields."""
	items = _as_list(rows)
	if not items:
		return rows

	pet_fields = _fields(pet_field, PET_FIELDS)
	guardian_fields = _fields(guardian_field, GUARDIAN_FIELDS)
	doctor_fields = _fields(doctor_field, DOCTOR_FIELDS)
	provider_fields = _fields(provider_field, PROVIDER_FIELDS)

	pet_ids = _collect(items, pet_fields) if include_pet else []
	guardian_ids = _collect(items, guardian_fields) if include_guardian else []
	doctor_ids = _collect(items, doctor_fields) if include_doctor else []
	provider_ids = _collect(items, provider_fields) if include_provider else []

	pet_names = _display_map("Pet", pet_ids, "pet_name")
	guardian_names = _display_map("Guardian", guardian_ids, "full_name")
	practitioner_names = _display_map("Healthcare Practitioner", sorted(set(doctor_ids + provider_ids)), "practitioner_name")

	for row in items:
		if include_pet:
			pet_id = _pick(row, pet_fields)
			if pet_id:
				row["pet_id"] = pet_id
				row["pet_name"] = pet_names.get(pet_id) or pet_id
		if include_guardian:
			guardian_id = _pick(row, guardian_fields)
			if guardian_id:
				row["guardian_id"] = guardian_id
				row["guardian_name"] = guardian_names.get(guardian_id) or guardian_id
		if include_doctor:
			doctor_id = _pick(row, doctor_fields)
			if doctor_id:
				row["doctor"] = doctor_id
				row["doctor_name"] = practitioner_names.get(doctor_id) or doctor_id
		if include_provider:
			provider_id = _pick(row, provider_fields)
			if provider_id:
				row["provider"] = provider_id
				row["provider_name"] = practitioner_names.get(provider_id) or provider_id
	return rows


def with_link_aliases(payload: dict, **kwargs) -> dict:
	enrich_link_aliases([payload], **kwargs)
	return payload


def pet_display_map(pet_ids: Iterable[str]) -> dict[str, str]:
	return _display_map("Pet", pet_ids, "pet_name")


def guardian_display_map(guardian_ids: Iterable[str]) -> dict[str, str]:
	return _display_map("Guardian", guardian_ids, "full_name")


def practitioner_display_map(practitioner_ids: Iterable[str]) -> dict[str, str]:
	return _display_map("Healthcare Practitioner", practitioner_ids, "practitioner_name")


def _as_list(rows) -> list:
	if rows is None:
		return []
	if isinstance(rows, dict):
		return [rows]
	return list(rows)


def _fields(primary: str | None, defaults: tuple[str, ...]) -> tuple[str, ...]:
	if primary:
		return (primary, *[field for field in defaults if field != primary])
	return defaults


def _collect(rows: list, fields: tuple[str, ...]) -> list[str]:
	return sorted({value for row in rows if (value := _pick(row, fields))})


def _pick(row, fields: tuple[str, ...]) -> str | None:
	for fieldname in fields:
		value = row.get(fieldname)
		if isinstance(value, dict):
			continue
		value = cstr(value).strip()
		if value:
			return value
	return None


def _display_map(doctype: str, names: Iterable[str], display_field: str) -> dict[str, str]:
	names = sorted({cstr(name).strip() for name in names if cstr(name).strip()})
	if not names:
		return {}
	return {
		row.name: row.get(display_field) or row.name
		for row in frappe.get_all(
			doctype,
			filters={"name": ["in", names]},
			fields=["name", display_field],
			ignore_permissions=True,
		)
	}
