# Frontend Handoff: Visit Cancellation Actions

## Purpose

This handoff covers the new soft-cancellation backend contract for:

- Pet care service cancellation
- Medication prescription cancellation
- Procedure cancellation

The rule:

```txt
Frontend requests cancellation.
Backend soft-cancels records and billable rows.
Frontend reloads and displays backend state.
```

Do not delete linked records from Vue. Do not manually remove or edit `billable_items`.

## Soft Cancellation Model

Cancellation means:

- Keep the original clinical or service record for audit.
- Set the operational record or child row status to cancelled.
- Set matching unbilled `Vet Visit.billable_items` row to `Cancelled`.
- Keep billed rows locked.
- Reload the Visit after the mutation.

Cancelled billable rows remain on the raw `Vet Visit` document for audit, but backend removes them from the active frontend billing lists:

- `visit.billing.billable_items`
- `data.billables`

If Vue needs audit/history, read:

- `visit.billing.cancelled_billable_items`
- `data.cancelled_billables`

## 1. Cancel Pet Care Service

Use this when cancelling a linked `PetCareService` created from a Visit service order.

Allowed:

- `PetCareService.status = "pending"`
- `PetCareService.status = "overdue"`

Blocked:

- `PetCareService.status = "completed"`
- Visit is already billed
- Matching billable row is already billed

### Request JSON

```json
{
  "method": "pet_app.api.workspace.perform_action",
  "args": {
    "source_type": "Service",
    "name": "PetCareService-00001",
    "action": "cancel_service",
    "payload": {
      "reason": "Owner declined"
    }
  }
}
```

### Backend Behavior

Backend will:

1. Set `PetCareService.status = "cancelled"`.
2. Set linked `Vet Visit.orders[].status = "Cancelled"`.
3. Cancel matching unbilled service billable row if present.
4. Add a comment with the cancellation reason.
5. Return the refreshed service workspace record.

### Matching Billable Identifiers

Backend looks for the matching service billable by:

```json
{
  "linked_service_id": "PetCareService::PetCareService-00001",
  "linked_doctype": "PetCareService",
  "linked_name": "PetCareService-00001",
  "order_id": "VVT-2026-00001-ORD-EF56AB78CD",
  "item_type": "Service"
}
```

### Expected After Reload

```json
{
  "linked_records": [
    {
      "source_type": "Service",
      "source_doctype": "PetCareService",
      "name": "PetCareService-00001",
      "status": "cancelled",
      "order_id": "VVT-2026-00001-ORD-EF56AB78CD"
    }
  ],
  "orders": [
    {
      "order_id": "VVT-2026-00001-ORD-EF56AB78CD",
      "kind": "service",
      "title": "Full Grooming",
      "status": "Cancelled",
      "linked_doctype": "PetCareService",
      "linked_name": "PetCareService-00001"
    }
  ],
  "billing": {
    "billable_items": [],
    "cancelled_billable_items": [
      {
        "item_type": "Service",
        "status": "Cancelled",
        "linked_doctype": "PetCareService",
        "linked_name": "PetCareService-00001",
        "order_id": "VVT-2026-00001-ORD-EF56AB78CD"
      }
    ]
  }
}
```

## 2. Cancel Medication Prescription

Use this when cancelling a medication child row on `Vet Visit.prescribed_medications`.

Allowed:

- Medication has not been dispensed.
- `dispensed_qty` is blank or `0`.

Blocked:

- `dispensed_qty > 0`
- Visit is already billed
- Matching billable row is already billed

If medication was partially or fully dispensed, pharmacy must return or reverse the dispensed quantity first.

### Request JSON

```json
{
  "method": "pet_app.api.workspace.perform_action",
  "args": {
    "source_type": "Visit",
    "name": "VVT-2026-00001",
    "action": "cancel_medication",
    "payload": {
      "row_name": "medication-child-row-name",
      "reason": "Owner declined"
    }
  }
}
```

`row_name` can be the child row `name`. Backend also accepts the row `idx`, but Vue should prefer `row_name`.

### Backend Behavior

Backend will:

1. Find the medication child row.
2. Block cancellation if `dispensed_qty > 0`.
3. Set `dispense_status = "Cancelled"`.
4. Cancel the matching unbilled medication billable row.
5. Add a Visit comment with the cancellation reason.
6. Return the refreshed Visit aggregate.

### Matching Billable Identifier

```json
{
  "linked_service_id": "medication::medication-child-row-name",
  "item_type": "Medication"
}
```

### Expected After Reload

```json
{
  "medications": [
    {
      "name": "medication-child-row-name",
      "medication_item": "AMOX-250-TAB",
      "qty": 10,
      "dispense_status": "Cancelled",
      "dispensed_qty": 0
    }
  ],
  "billing": {
    "billable_items": [],
    "cancelled_billable_items": [
      {
        "item_type": "Medication",
        "status": "Cancelled",
        "linked_service_id": "medication::medication-child-row-name"
      }
    ]
  }
}
```

Saving the Visit after cancellation will not recreate the medication billable row.

## 3. Cancel Procedure

Procedure cancellation already uses the same soft-cancel pattern.

Allowed:

- `Pet Procedure.status = "Pending"`
- `Pet Procedure.status = "In Progress"`

Blocked:

- `Completed`
- `Closed`
- `Cancelled`
- Visit is already billed

### Request JSON

```json
{
  "method": "pet_app.api.workspace.perform_action",
  "args": {
    "source_type": "Procedure",
    "name": "PROC-00001",
    "action": "cancel_procedure",
    "payload": {
      "reason": "Owner declined"
    }
  }
}
```

### Backend Behavior

Backend will:

1. Set `Pet Procedure.status = "Cancelled"`.
2. Set linked `Vet Visit.orders[].status = "Cancelled"`.
3. Cancel the matching unbilled Procedure billable row.
4. Return the refreshed Procedure workspace record.

### Expected After Reload

```json
{
  "billing": {
    "billable_items": [],
    "cancelled_billable_items": [
      {
        "item_type": "Procedure",
        "status": "Cancelled",
        "linked_service_id": "Pet Procedure::PROC-00001",
        "linked_doctype": "Pet Procedure",
        "linked_name": "PROC-00001",
        "order_id": "VVT-2026-00001-ORD-78CD90EF12"
      }
    ]
  }
}
```

## Reload After Cancel

After any cancellation, Vue should reload the Visit:

```json
{
  "method": "pet_app.api.workspace.get_record",
  "args": {
    "source_type": "Visit",
    "name": "VVT-2026-00001"
  }
}
```

Render from the reloaded backend state:

- `orders[]` for order status.
- `linked_records[]` for linked service/procedure status.
- `medications[]` or `prescribed_medications[]` for medication status.
- `billing.billable_items[]` for active billable rows only.
- `billing.cancelled_billable_items[]` for cancelled billable audit/history.
- `data.billables[]` from workbench for active billable rows only.
- `data.cancelled_billables[]` from workbench for cancelled billable audit/history.

## Frontend Display Rules

Do:

- Show backend validation messages directly.
- Keep cancelled rows visible in history/audit views.
- Hide cancelled medications from pending dispense lists.
- Use `billing.billable_items` or `data.billables` for the active billing preview.
- Use `billing.cancelled_billable_items` or `data.cancelled_billables` only in cancelled/history sections.
- Disable cancel buttons when Visit is billed.

Do not:

- Do not delete `PetCareService`.
- Do not delete `Pet Procedure`.
- Do not remove medication rows to cancel them.
- Do not manually set `billable_items.status`.
- Do not invoice cancelled billable rows.

## Error Messages To Expect

Common backend validation errors:

```txt
Medication must be returned or reversed before cancellation.
This visit is already billed and cannot be modified.
Action cancel_service is not allowed when PetCareService is completed.
Action cancel_procedure is not allowed when Pet Procedure is Completed.
Medication row was not found.
```

## Frontend Action Summary JSON

```json
{
  "actions": {
    "cancel_service": {
      "source_type": "Service",
      "action": "cancel_service",
      "required": ["name"],
      "payload": {
        "reason": "string optional"
      },
      "cancelled_status": "cancelled",
      "order_status": "Cancelled",
      "billable_status": "Cancelled"
    },
    "cancel_medication": {
      "source_type": "Visit",
      "action": "cancel_medication",
      "required": ["visit name", "row_name"],
      "payload": {
        "row_name": "medication child row name",
        "reason": "string optional"
      },
      "cancelled_status": "Cancelled",
      "billable_linked_service_id": "medication::<row_name>",
      "billable_status": "Cancelled"
    },
    "cancel_procedure": {
      "source_type": "Procedure",
      "action": "cancel_procedure",
      "required": ["name"],
      "payload": {
        "reason": "string optional"
      },
      "cancelled_status": "Cancelled",
      "order_status": "Cancelled",
      "billable_status": "Cancelled"
    }
  }
}
```
