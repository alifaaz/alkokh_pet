# Alkokh Mobile Read APIs

These endpoints are Frappe method endpoints under:

```text
/api/method/pet_app.api.mobile.<module>.<method_name>
```

They are mobile DTO wrappers over existing backend data. They do not replace Frappe/ERPNext DocTypes.

## Config

| Method | Endpoint | Auth |
|---|---|---|
| `GET` | `/api/method/pet_app.api.mobile.config.get_config` | Public |
| `GET` | `/api/method/pet_app.api.mobile.config.support_contact` | Public |
| `GET` | `/api/method/pet_app.api.mobile.config.content?key=terms` | Public |

## Catalog

| Method | Endpoint | Auth |
|---|---|---|
| `GET` | `/api/method/pet_app.api.mobile.catalog.home` | Public |
| `GET` | `/api/method/pet_app.api.mobile.catalog.list_products` | Public |
| `GET` | `/api/method/pet_app.api.mobile.catalog.get_product?product=<id>` | Public |
| `GET` | `/api/method/pet_app.api.mobile.catalog.list_categories` | Public |
| `GET` | `/api/method/pet_app.api.mobile.catalog.list_brands` | Public |
| `GET` | `/api/method/pet_app.api.mobile.catalog.search?q=<text>` | Public |
| `GET` | `/api/method/pet_app.api.mobile.catalog.suggest?q=<text>` | Public |

Catalog endpoints expose only active `Product` records and read-only `Product Category` / `Brand` DTOs.

## Profile

| Method | Endpoint | Auth |
|---|---|---|
| `GET` | `/api/method/pet_app.api.mobile.profile.me` | Bearer |
| `POST` | `/api/method/pet_app.api.mobile.profile.update_me` | Bearer |

Profile endpoints are scoped to the Guardian linked to the current OAuth user.

## Addresses

| Method | Endpoint | Auth |
|---|---|---|
| `GET` | `/api/method/pet_app.api.mobile.addresses.list_addresses` | Bearer |
| `GET` | `/api/method/pet_app.api.mobile.addresses.get_address?address=<id>` | Bearer |
| `POST` | `/api/method/pet_app.api.mobile.addresses.create_address` | Bearer |
| `POST` | `/api/method/pet_app.api.mobile.addresses.update_address` | Bearer |
| `POST` | `/api/method/pet_app.api.mobile.addresses.set_default` | Bearer |
| `POST` | `/api/method/pet_app.api.mobile.addresses.delete_address` | Bearer |
| `GET` | `/api/method/pet_app.api.mobile.addresses.cities` | Public |
| `GET` | `/api/method/pet_app.api.mobile.addresses.reverse?lat=<lat>&lng=<lng>` | Public |

Address endpoints use ERPNext `Address` linked to the Guardian's resolved `Customer` through `Dynamic Link`.

`delete_address` is a soft delete: it sets `Address.disabled = 1`, clears default flags, and promotes another active address when needed. Mobile order checkout validates `shipping_address_name` against these Customer-linked addresses.

## Pets

| Method | Endpoint | Auth |
|---|---|---|
| `GET` | `/api/method/pet_app.api.mobile.pets.list_pets` | Bearer |
| `GET` | `/api/method/pet_app.api.mobile.pets.get_pet?pet=<id>` | Bearer |
| `POST` | `/api/method/pet_app.api.mobile.pets.create_pet` | Bearer |
| `POST` | `/api/method/pet_app.api.mobile.pets.update_pet` | Bearer |
| `POST` | `/api/method/pet_app.api.mobile.pets.disable_pet` | Bearer |
| `GET` | `/api/method/pet_app.api.mobile.pets.medical_timeline?pet=<id>` | Bearer |
| `GET` | `/api/method/pet_app.api.mobile.pets.documents?pet=<id>` | Bearer |

Pet endpoints are scoped through `PetGuardian`; a Guardian can only access linked pets.

`create_pet` creates a normal `Pet`, approves the mobile add request through the existing `PetAddRequest` behavior, creates the `PetGuardian` link, and ensures the medical profile exists.

`disable_pet` does not delete data. It sets `pet_status` to `Archived`; archived pets are hidden from `list_pets` unless `include_disabled=1` is supplied.
