# Doctor CRUD API

## Overview

`Doctor` is the medical staff record used by the backend and referenced by `Vet Visit`.

Relationship:

- `Doctor` -> `User`
- `1 Doctor = 1 User`
- one `User` cannot be linked to multiple `Doctor` records

The backend manages the linked `User` automatically during standard Frappe REST CRUD.

## Create Doctor

Endpoint:

```http
POST /api/resource/Doctor
```

Required fields:

- `doctor_name`
- `phone`

Behavior:

- if `user` is not provided, the system auto-creates a linked `User`
- generated user email:
  - `doctor.<digits>@petapp.local` from phone digits
  - fallback: `slug(doctor_name)@petapp.local`
- linked user is set with:
  - `first_name`
  - `full_name`
  - `mobile_no`
  - `enabled = 1`
- assigned roles:
  - `Doctor`
  - `Healthcare`

Example request:

```bash
curl -X POST https://your-site.com/api/resource/Doctor \
  -H "Authorization: token api_key:api_secret" \
  -H "Content-Type: application/json" \
  -d '{
    "doctor_name": "Dr Ahmed Hassan",
    "phone": "07712345678",
    "specialization": "Surgery"
  }'
```

Example response:

```json
{
  "data": {
    "name": "DOC-.00001",
    "doctor_name": "Dr Ahmed Hassan",
    "phone": "07712345678",
    "user": "doctor.07712345678@petapp.local",
    "specialization": "Surgery"
  }
}
```

## Get Doctor

Single doctor:

```http
GET /api/resource/Doctor/{name}
```

Example:

```bash
curl -X GET "https://your-site.com/api/resource/Doctor/DOC-.00001" \
  -H "Authorization: token api_key:api_secret"
```

List doctors:

```http
GET /api/resource/Doctor?fields=["name","doctor_name","phone","specialization"]
```

Example:

```bash
curl -X GET "https://your-site.com/api/resource/Doctor?fields=[\"name\",\"doctor_name\",\"phone\",\"specialization\"]" \
  -H "Authorization: token api_key:api_secret"
```

Example list response:

```json
{
  "data": [
    {
      "name": "DOC-.00001",
      "doctor_name": "Dr Ahmed Hassan",
      "phone": "07712345678",
      "specialization": "Surgery"
    },
    {
      "name": "DOC-.00002",
      "doctor_name": "Dr Sara Ali",
      "phone": "07798765432",
      "specialization": "Dermatology"
    }
  ]
}
```

## Update Doctor

Endpoint:

```http
PUT /api/resource/Doctor/{name}
```

Behavior:

- updates the `Doctor` record
- syncs linked `User` fields:
  - `full_name`
  - `mobile_no`
- keeps `1 Doctor = 1 User`
- blocks changing the linked `user` to one already used by another doctor

Example request:

```bash
curl -X PUT https://your-site.com/api/resource/Doctor/DOC-.00001 \
  -H "Authorization: token api_key:api_secret" \
  -H "Content-Type: application/json" \
  -d '{
    "doctor_name": "Dr Ahmed H. Hassan",
    "phone": "07712340000",
    "specialization": "Emergency"
  }'
```

Example response:

```json
{
  "data": {
    "name": "DOC-.00001",
    "doctor_name": "Dr Ahmed H. Hassan",
    "phone": "07712340000",
    "user": "doctor.07712345678@petapp.local",
    "specialization": "Emergency"
  }
}
```

## Delete Doctor

Endpoint:

```http
DELETE /api/resource/Doctor/{name}
```

Behavior:

- deletes only the `Doctor` record
- does not delete the linked `User`
- disables the linked `User`

Example:

```bash
curl -X DELETE https://your-site.com/api/resource/Doctor/DOC-.00001 \
  -H "Authorization: token api_key:api_secret"
```

Example response:

```json
{
  "message": "Deleted"
}
```

## Validation Rules

- `phone` is required
- `doctor_name` is required
- `user` must be unique per doctor
- a linked `User` must exist if provided manually

## Security

Allowed roles:

- `System Manager`
- `Healthcare Administrator`

Standard Frappe REST on `Doctor` should only be used by users with those roles.

## Notes

- do not create the linked doctor user manually unless you need a specific existing `User`
- if `user` is omitted, the system creates and links the `User` automatically
- the system assigns `Doctor` and `Healthcare` roles automatically
