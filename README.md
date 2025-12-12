# 🐾 Pet App API

This document describes the **actual API behavior and responses** of the Pet App backend, based on live endpoints.

All examples below are **real responses**, not simplified or mocked.

---

## Base URL

```
{{url}}/api
```

---

# 🐶 Pets

## Get Single Pet

### Endpoint

```
GET /method/pet_app.api.pet.get_pet?pet_id=PET-00017
```

### Response

```json
{
  "message": {
    "data": {
      "name": "PET-00017",
      "pet_name": "Milo",
      "animal_species": "Mammal",
      "animal_type": "Cat",
      "breed": "Persian",
      "status": "Available",
      "birth_date": "2023-08-31",
      "registration_date": "2025-12-10",

      "owner_customer": "CUST-0001",
      "customer_name": "Mostafa",

      "color": "Orange",
      "gender": "Male",
      "weight": 4.2,
      "hight": 28.0,
      "blood_type": "A+",

      "play": "Likes to play with small balls and strings",
      "activity_exercise": "Medium Activity, active in the morning and evening",

      "food_brand": "FB-0002",
      "brand_name": "mybrand",

      "food_type": "FT-0001",
      "type_name": "test1213",

      "description": "Friendly Persian cat, calm temperament, requires weekly grooming, fully vaccinated, and healthy.",
      "note": "Needs to avoid wet/cold places due to sensitive fur.",

      "images": [
        {
          "name": "5cc299a0b0",
          "file_url": "/files/download.jpeg",
          "file_name": "download.jpeg",
          "custom_is_default": 1
        },
        {
          "name": "3e12e52238",
          "file_url": "/files/3646d8423e0f31b82d9b4212d5cdfd29.jpg",
          "file_name": "3646d8423e0f31b82d9b4212d5cdfd29.jpg",
          "custom_is_default": 0
        }
      ],

      "image": "/files/download.jpeg"
    }
  }
}
```

### Notes

* `food_brand` / `food_type` → IDs
* `brand_name` / `type_name` → resolved names
* `image` → derived from `images` where `custom_is_default = 1`

---

# 🗂️ Category Care Services

## Get Single Category

```
GET /resource/CategoryCareServices/CategoryCareServices-0008
```

```json
{
  "data": {
    "name": "CategoryCareServices-0008",
    "category_name": "type 1",
    "description": "dsadasdasd",
    "doctype": "CategoryCareServices"
  }
}
```

---

## List Categories (Minimal Fields)

```
GET /resource/CategoryCareServices?fields=["name","category_name","description"]
```

```json
{
  "data": [
    {
      "name": "CategoryCareServices-0008",
      "category_name": "type 1",
      "description": "dsadasdasd"
    },
    {
      "name": "CategoryCareServices-0009",
      "category_name": "tata",
      "description": "abcd"
    }
  ]
}
```

---

# 🩺 Care Service

## Get Single Care Service

```
GET /resource/CareService/CareService-00004
```

```json
{
  "data": {
    "name": "CareService-00004",
    "service_name": "acd",
    "frequency": "onetime",
    "animal_species": "Mammal",
    "category_id": "CategoryCareServices-0008",
    "description": "abcd",
    "doctype": "CareService"
  }
}
```

---

## List Care Services (With Category Name)

```
GET /resource/CareService?fields=[
  "name",
  "service_name",
  "frequency",
  "category_id",
  "category_id.category_name",
  "animal_species",
  "description"
]
```

> ⚠️ Note
> Frappe **does not flatten linked fields automatically** in this endpoint.
> Returned object still follows the CareService structure.

---

# 🐾 Pet Care Service (Pet ↔ Care Service)

## Get Single Pet Care Service

```
GET /resource/PetCareService/PetCareService-00002
```

```json
{
  "data": {
    "name": "PetCareService-00002",
    "pet_service_name": "12334asx",
    "pet_id": "PET-00017",
    "care_service_id": "CareService-00004",
    "status": "pending",
    "due_date": "2025-12-10",
    "provider": null,
    "description": null,
    "send_reminder": 0,
    "doctype": "PetCareService"
  }
}
```

---

## List All Pet Care Services

```
GET /resource/PetCareService?fields=[
  "name",
  "pet_service_name",
  "pet_id",
  "pet_id.pet_name",
  "care_service_id",
  "care_service_id.service_name",
  "description",
  "status",
  "due_date",
  "provider"
]
```

```json
{
  "data": [
    {
      "name": "PetCareService-00002",
      "pet_service_name": "12334asx",
      "pet_id": "PET-00017",
      "pet_name": "Milo",
      "care_service_id": "CareService-00004",
      "service_name": "acd",
      "status": "pending",
      "due_date": "2025-12-10",
      "provider": null,
      "description": null
    }
  ]
}
```

---

## Get Care Services for a Specific Pet

```
GET /resource/PetCareService
  ?limit_page_length=100
  &filters=[["pet_id","=","PET-00017"]]
  &fields=[
    "pet_id",
    "pet_id.pet_name",
    "name",
    "pet_service_name",
    "care_service_id",
    "care_service_id.service_name",
    "status",
    "due_date",
    "provider",
    "description"
  ]
```

```json
{
  "data": [
    {
      "pet_id": "PET-00017",
      "pet_name": "Milo",
      "name": "PetCareService-00002",
      "pet_service_name": "12334asx",
      "care_service_id": "CareService-00004",
      "service_name": "acd",
      "status": "pending",
      "due_date": "2025-12-10",
      "provider": null,
      "description": null
    }
  ]
}
```

---

# 🔑 Key Design Rules (Current Reality)

* IDs are **never replaced** by names
* Names are **returned alongside IDs**
* Responses are **flat**, frontend-ready
* No hidden joins
* No extra computed fields beyond what you see
* Everything matches actual Frappe behavior
