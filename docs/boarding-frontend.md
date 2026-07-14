# Healthcare Boarding Frontend Contract

Backend module: `pet_app.api.healthcare.boarding`

Frappe HTTP responses wrap return values in `message`. Examples below show the HTTP body shape.

## Boarding Rooms Page

### List rooms

`GET /api/method/pet_app.api.healthcare.boarding.list_boarding_units`

Params:

- `search`: optional room/pet/guardian text search.
- `occupancy`: optional `Available`, `Reserved`, or `Occupied`.
- `date`: accepted for compatibility. Room occupancy is still derived from active boarding records.

Response:

```json
{
  "message": {
    "data": [
      {
        "room_id": "ROOM-001",
        "room_code": "ROOM-001",
        "room_name": "Room 1",
        "room_type": "Kennel",
        "image": null,
        "status": "Active",
        "occupancy": "Available",
        "active_boarding": null,
        "boarding_id": null
      }
    ],
    "total": 1
  }
}
```

Occupancy mapping:

- No room-assigned active boarding: `Available`
- Active `record_status = Pending Room`: no room occupancy until a room is assigned
- Active `record_status = Reserved`: `Reserved`
- Active `record_status = Checked In`: `Occupied`

### Room/boarding detail

`GET /api/method/pet_app.api.healthcare.boarding.get_boarding_detail`

Params:

- `boarding_id`: optional `Pet Boarding.name`.
- `room_id`: optional `Service Room.name`.
- `name`: compatibility alias. The backend checks `Pet Boarding` first, then `Service Room`.

Empty rooms still return room details and `billable_items: []`, so the UI can open a reserve form.

### Reserve

`POST /api/method/pet_app.api.healthcare.boarding.reserve_room`

Payload:

```json
{
  "roomId": "ROOM-001",
  "petId": "PET-.00001",
  "guardianId": "GUARDIAN-.00001",
  "checkIn": "2026-04-20 09:00:00",
  "checkOut": "2026-04-22 09:00:00",
  "note": "Front desk note",
  "boardingType": "Travel"
}
```

`boardingType` is optional and defaults from `Pet Boarding Settings.default_boarding_type`. The backend default is `Travel`; clients must read the setting and must not invent `Treatment` when the value is absent.

Response:

```json
{
  "message": {
    "success": true,
    "boarding_id": "BRD-.00001",
    "room_id": "ROOM-001",
    "record_status": "Reserved",
    "occupancy": "Reserved"
  }
}
```

### Check in

`POST /api/method/pet_app.api.healthcare.boarding.check_in_boarding`

Payload:

```json
{ "boarding_id": "BRD-.00001" }
```

Only `Reserved` records can be checked in.

### Sync billable items

`POST /api/method/pet_app.api.healthcare.boarding.sync_billable_items`

Payload:

```json
{
  "boarding_id": "BRD-.00001",
  "billable_items": [
    {
      "name": "optional-existing-child-row-name",
      "item_name": "Bath Service",
      "item_code": "ITEM-001",
      "qty": 1,
      "rate": 50,
      "amount": 50,
      "note": "Optional line note",
      "status": "Billable",
      "linked_service_id": "optional-service-id"
    }
  ]
}
```

The frontend sends the full final `billable_items` array. Existing child rows are matched by `name`; omitted existing rows are deleted, rows without `name` are created, and totals are recalculated. This action does not check in, check out, create an invoice, or change boarding lifecycle state.

### Check out

`POST /api/method/pet_app.api.healthcare.boarding.check_out_boarding`

Payload:

```json
{ "boarding_id": "BRD-.00001" }
```

Only `Checked In` records can be checked out. Checkout adds the room-stay item if missing, creates a `Sales Invoice`, marks the boarding `Checked Out`, sets `status = Closed`, submits the boarding document, and releases the room because it is no longer active.

## Boarding Records Page

`GET /api/method/pet_app.api.healthcare.boarding.list_boarding_records`

Params:

- `search`
- `status`
- `pet_id`
- `guardian_id`
- `date_from`
- `date_to`
- `limit_start`
- `limit_page_length`
- `order_by`

Allowed `order_by` values:

- `modified desc`
- `modified asc`
- `creation desc`
- `creation asc`
- `check_in desc`
- `check_in asc`
- `check_out desc`
- `check_out asc`
- `total_cost desc`
- `total_cost asc`
- `record_status asc`
- `record_status desc`

Response:

```json
{
  "message": {
    "data": [],
    "total": 0
  }
}
```

## Required Settings

Before checkout, configure:

- `Pet Boarding Settings.travel_boarding_item`
- `Pet Boarding Settings.treatment_boarding_item`

These `Item` records provide the room-stay invoice item and price. `Service Room` never stores price or occupancy.
