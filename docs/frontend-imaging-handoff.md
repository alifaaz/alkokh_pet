# Frontend Handoff: Imaging / Radiology

Scope: the **Imaging** builder only — order an X-Ray / Ultrasound / etc., enter the radiologist report, attach the image, and release the result. Everything here is verified against the current backend (`Imaging` DocType, `pet_app.api.diagnostics`, `pet_app.workflows.clinical_state`).

For the cross-builder version (Lab / Imaging / Pet Care Service / Procedure together) see [frontend-procedure-lab-imaging-pet-care-services-handoff.md](frontend-procedure-lab-imaging-pet-care-services-handoff.md). This file is the imaging-only cut so the imaging screen team doesn't have to read around the others.

---

## 1. Model at a glance

```txt
CategoryCareServices (category_name = "Imaging" or "Radiology")
  -> CareService template            (the billable catalog item: modality, body_part, price)
       -> Imaging                    (the work record, IMG-#####)
            -> Vet Visit.orders      (order tracking row)
            -> Vet Visit.billable    (auto-billed at rate from the template)
```

One imaging order = one `Imaging` record + one `Visit Order` row + one auto-created billable row. The frontend never writes the `Imaging` record or billable rows directly — it calls `create_orders` and the lifecycle actions, then reloads the Visit.

---

## 2. Catalog read (populate the order picker)

Show templates whose linked category name is `Imaging` or `Radiology` (case-insensitive).

```json
{
  "method": "frappe.client.get_list",
  "args": {
    "doctype": "CareService template",
    "filters": { "disabled": 0, "category_id": "CategoryCareServices-0002" },
    "fields": [
      "name", "service_name", "animal_species", "category_id",
      "item_code", "default_price", "price_list",
      "modality", "body_part", "service_area", "estimated_turnaround"
    ],
    "order_by": "service_name asc",
    "limit_page_length": 100
  }
}
```

Catalog option shape the picker should bind to:

```json
{
  "name": "CareService-00005",
  "service_name": "Chest X-Ray",
  "category_id": "CategoryCareServices-0002",
  "category_name": "Imaging",
  "item_code": "XRAY-CHEST",
  "default_price": 45.0,
  "modality": "X-Ray",
  "body_part": "Chest",
  "estimated_turnaround": "1 hour"
}
```

Validation to enforce in the UI before allowing select:
- `CareService template.disabled` must be `0`.
- `item_code` and `default_price` are **required** — the backend throws on save if either is missing, so gray out templates lacking them.

---

## 3. Create the imaging order

All builders go through the same Visit action. For imaging, `kind` is `"radiology"`.

```json
{
  "method": "pet_app.api.workspace.perform_action",
  "args": {
    "source_type": "Visit",
    "name": "VVT-2026-00001",
    "action": "create_orders",
    "payload": {
      "orders": [
        {
          "kind": "radiology",
          "title": "Chest X-Ray",
          "template_id": "CareService-00005",
          "priority": "Urgent",
          "note": "Rule out thoracic trauma"
        }
      ]
    }
  }
}
```

`create_orders` is blocked when the Visit is billed (`billed` set or `sales_invoice` set) or when the Visit status is terminal (`Completed` / `Cancelled`). Allowed Visit statuses: `Draft`, `In Progress`, `Follow-up Needed`.

### Returned order row

```json
{
  "order_id": "VVT-2026-00001-ORD-CD34EF56AB",
  "kind": "radiology",
  "title": "Chest X-Ray",
  "template_id": "CareService-00005",
  "status": "Ordered",
  "priority": "Urgent",
  "qty": 1,
  "price": 0,
  "note": "Rule out thoracic trauma",
  "linked_doctype": "Imaging",
  "linked_name": "IMG-00001"
}
```

### The created Imaging work record

```json
{
  "doctype": "Imaging",
  "name": "IMG-00001",
  "visit": "VVT-2026-00001",
  "order_id": "VVT-2026-00001-ORD-CD34EF56AB",
  "pet": "PET-00001",
  "doctor": "HLC-PRAC-00001",
  "care_service": "CareService-00005",
  "item_code": "XRAY-CHEST",
  "rate": 45.0,
  "status": "Ordered",
  "report": null,
  "image": null,
  "result_entered_by": null,
  "result_entered_at": null,
  "released_by": null,
  "released_at": null,
  "doctor_reviewed": 0,
  "doctor_reviewed_at": null,
  "result_visibility": "Clinical Team",
  "attachment_required": 0
}
```

`pet`, `doctor`, `item_code`, and `rate` are auto-filled from the Visit and the template — do not send them in the create payload. `pet` must match the Visit's pet or the backend throws.

### Auto-created billable

```json
{
  "item_name": "Chest X-Ray",
  "item_code": "XRAY-CHEST",
  "item_type": "Imaging",
  "qty": 1,
  "rate": 45.0,
  "amount": 45.0,
  "status": "Billable",
  "linked_service_id": "Imaging::IMG-00001",
  "linked_doctype": "Imaging",
  "linked_name": "IMG-00001",
  "order_id": "VVT-2026-00001-ORD-CD34EF56AB",
  "note": "Chest X-Ray"
}
```

Imaging is `auto_billable: true` — the billable row appears on its own. **Do not append `billable_items` from the frontend.**

---

## 4. Lifecycle — statuses and the two work actions

Imaging status flow (from the backend state machine):

```txt
Ordered ──> Scheduled ──> In Progress ──> Reported ──> Released
   │            │              │             │
   └────────────┴──────────────┴─────────────┴──> Cancelled
```

Terminal statuses: `Released`, `Completed`, `Cancelled` (no transitions out).

There are exactly **two whitelisted imaging actions**. Both are `POST`.

### 4a. Save the report (status → `Reported`)

`pet_app.api.diagnostics.save_imaging_report`

```json
{
  "method": "pet_app.api.diagnostics.save_imaging_report",
  "args": {
    "imaging": "IMG-00001",
    "report": "No acute thoracic abnormality. Cardiac silhouette normal.",
    "image": "/files/img-00001-chest.png",
    "doctor_reviewed": 1,
    "result_visibility": "Clinical Team"
  }
}
```

- `report` is **required** — the call fails with `VALIDATION_ERROR` if empty.
- `image` is the file URL of an uploaded attachment (optional here, but see release rule below).
- Allowed from statuses: `Ordered`, `Scheduled`, `In Progress`, `Reported`. Re-saving while `Reported` is allowed (edits the report).
- Sets `result_entered_by` / `result_entered_at` to the current user / now.
- `result_visibility` defaults to `Clinical Team` on report.

### 4b. Release the report (status → `Released`)

`pet_app.api.diagnostics.release_imaging_report`

```json
{
  "method": "pet_app.api.diagnostics.release_imaging_report",
  "args": {
    "imaging": "IMG-00001",
    "result_visibility": "Guardian Visible"
  }
}
```

- `report` must already be set — release fails with `VALIDATION_ERROR` otherwise. So **the UI must call `save_imaging_report` before enabling Release**.
- If `attachment_required` is `1`, there must be an `image` **or** a File attachment, or release fails with `VALIDATION_ERROR`. When the order has `attachment_required`, block the Release button until an image is present.
- `result_visibility` defaults to `Guardian Visible` on release (this is what makes the result show in the guardian portal).
- Sets `released_by` / `released_at`. Status becomes `Released` (terminal) and the Visit order syncs to `Completed`.

### Response from both actions

Both return `ok({ "imaging": <payload> })` on success, or `fail(message, code)` on validation/permission failure. The `imaging` payload:

```json
{
  "name": "IMG-00001",
  "doctype": "Imaging",
  "visit": "VVT-2026-00001",
  "order_id": "VVT-2026-00001-ORD-CD34EF56AB",
  "pet": "PET-00001",
  "doctor": "HLC-PRAC-00001",
  "care_service": "CareService-00005",
  "item_code": "XRAY-CHEST",
  "status": "Released",
  "result": null,
  "report": "No acute thoracic abnormality. Cardiac silhouette normal.",
  "image": "/files/img-00001-chest.png",
  "result_entered_by": "radiologist@example.com",
  "result_entered_at": "2026-06-22 11:04:00",
  "released_by": "radiologist@example.com",
  "released_at": "2026-06-22 11:09:00",
  "doctor_reviewed": 1,
  "doctor_reviewed_at": "2026-06-22 11:04:00",
  "result_visibility": "Guardian Visible"
}
```

Note: `result` is always `null` for Imaging — the report text lives in `report`. The shared diagnostic payload includes the `result` key (it is reused by Lab), so ignore it on imaging screens. The payload is also enriched with link-alias labels (`pet.full_name`, `doctor.full_name` style keys) via `with_link_aliases`.

### Error shape

```json
{ "ok": false, "code": "VALIDATION_ERROR", "message": "Imaging report is required before release." }
```

`code` is one of `VALIDATION_ERROR`, `PERMISSION_ERROR`, or the backend exception class name. Surface `message` to the user.

---

## 5. Cancellation

Imaging has **no dedicated cancel action** in `diagnostics.py` (unlike Pet Care Service / Procedure). To stop an imaging order, cancel the underlying Visit order through the workspace so billing stays consistent — do not delete the `Imaging` record or its billable row from the frontend. `Cancelled` is reachable from any non-terminal imaging status in the state machine; route cancellation through the Visit aggregate and re-render the returned statuses.

---

## 6. After every mutation — reload the Visit

```json
{
  "method": "pet_app.api.workspace.get_record",
  "args": { "source_type": "Visit", "name": "VVT-2026-00001" }
}
```

Re-render from the reloaded aggregate: `orders`, `linked_records`, and active `billing.billable_items` (cancelled billables move to `billing.cancelled_billable_items` for history only). Don't trust the single-action response for the full screen state — use it to confirm success, then reload.

---

## 7. Imaging builder config (for a generic builder component)

```json
{
  "imaging": {
    "category_names": ["Imaging", "Radiology"],
    "catalog_doctype": "CareService template",
    "order_kind": "radiology",
    "order_template_field": "template_id",
    "linked_doctype": "Imaging",
    "linked_source_type": "Radiology",
    "billable_item_type": "Imaging",
    "auto_billable": true,
    "actions": {
      "save_report": "pet_app.api.diagnostics.save_imaging_report",
      "release_report": "pet_app.api.diagnostics.release_imaging_report"
    },
    "status_flow": ["Ordered", "Scheduled", "In Progress", "Reported", "Released"],
    "terminal_statuses": ["Released", "Completed", "Cancelled"]
  }
}
```

## 8. Frontend checklist

- Filter the catalog to `Imaging`/`Radiology` category, `disabled = 0`, with `item_code` + `default_price` present.
- Order kind is `"radiology"` (not `"imaging"`).
- Save report (`save_imaging_report`) before showing Release.
- If `attachment_required = 1`, require an `image` before Release.
- Never write `Imaging` or `billable_items` directly; never delete to cancel.
- Reload the Visit after every action and render `orders` + active billables.
