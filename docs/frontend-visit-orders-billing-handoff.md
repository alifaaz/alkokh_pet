# Frontend Handoff: Visit Orders, Work Flows, and Billable Items

## Purpose

This document explains how the Vue visit page should handle clinical orders, linked operational work, and billing rows.

The main rule:

```txt
Frontend creates orders.
Backend creates linked operational records.
Backend creates billable rows where the linked backend DocType owns billing sync.
Frontend reloads and displays the result.
```

Do not make Vue manually create both `orders` and `billable_items` for the same clinical work. That will create duplicate tracking and can create duplicate charges.

## Mental Model

`Vet Visit.orders` answers:

```txt
What work did the doctor request?
```

Examples:

- CBC lab test
- X-Ray
- Grooming/service task
- Procedure

Medication is different. Use `Vet Visit.prescribed_medications` for prescriptions, not `create_orders`.

`Vet Visit.billable_items` answers:

```txt
What will be invoiced?
```

Examples:

- Lab item code, qty, rate, amount
- Imaging item code, qty, rate, amount
- Service item code, qty, rate, amount, when saved through the visit service billing path
- Procedure item code, qty, rate, amount

Sales Invoice is created later from `billable_items`.

## Important Backend Behavior

When Vue calls:

```txt
pet_app.api.workspace.perform_action
action = create_orders
```

the backend will automatically:

1. Add a row to `Vet Visit.orders`.
2. Create the linked operational record:
   - `Lab` for `kind: "lab"`
   - `Imaging` for `kind: "radiology"`
   - `PetCareService` for `kind: "service"`
   - `Pet Procedure` for `kind: "procedure"`
3. Link the order and operational record with:
   - `order_id`
   - `linked_doctype`
   - `linked_name`
4. Create or update a matching `Vet Visit.billable_items` row for linked records whose DocType owns billing sync:
   - `Lab`
   - `Imaging`
   - `Pet Procedure`

The backend creates lab, imaging, and procedure visit billable rows automatically. It does **not** create a new ERPNext `Item` master automatically.

The selected `CareService template` must already have:

- `item_code`
- `default_price`

If either is missing, backend validation will reject the order.

### Current Service Billing Caveat

As implemented in the app code today, `kind: "service"` creates and links a `PetCareService` record, and service workflow actions update the linked order status. `PetCareService` itself does **not** currently create a `billable_items` row on insert/update.

If a generic service must create a charge in the current backend, use the existing `Vet Visit.care_services` save path, which the Visit validation converts into `billable_items` with `item_type: "Service"`.

Do **not** make Vue manually append a service `billable_items` row after `create_orders`. Either use the backend-owned visit service billing path, or patch the backend so `PetCareService` syncs service-order billables the same way `Lab`, `Imaging`, and `Pet Procedure` do.

Medication does **not** use this order auto-link path in the current backend. For medication, Vue must save a row in `Vet Visit.prescribed_medications`. Visit validation then creates or updates the matching `billable_items` row with `item_type: "Medication"`.

## Load Visit Data

Use the workspace aggregate for normal visit rendering:

```ts
frappe.call({
  method: "pet_app.api.workspace.get_record",
  args: {
    source_type: "Visit",
    name: visitName
  }
}).then((r) => {
  const visit = r.message

  const orders = visit.orders || []
  const billableItems = visit.billing?.billable_items || []
  const billingTotal = visit.billing?.total
  const isBilled = Boolean(visit.billing?.billed)
})
```

If the page needs the raw visit document plus permission flags:

```ts
frappe.call({
  method: "pet_app.api.visit_workbench.get_visit_workbench",
  args: {
    visit: visitName
  }
}).then((r) => {
  const data = r.message.data

  const orders = data.orders || []
  const medications = data.medications || []
  const billableItems = data.billables || []
  const permissions = data.permissions || {}
})
```

## Create Orders

Create lab, imaging, procedure, and operational service work through `perform_action`. Billable-only generic services use the Visit `care_services` save path in the current backend.

### Lab Order

```ts
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Visit",
    name: visitName,
    action: "create_orders",
    payload: {
      orders: [
        {
          kind: "lab",
          title: "CBC",
          template_id: "CareService-00004",
          priority: "Normal",
          note: "CBC requested by doctor"
        }
      ]
    }
  }
}).then((r) => {
  const updatedVisit = r.message
  renderVisit(updatedVisit)
})
```

### Imaging / Radiology Order

```ts
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Visit",
    name: visitName,
    action: "create_orders",
    payload: {
      orders: [
        {
          kind: "radiology",
          title: "X-Ray",
          template_id: "CareService-00005",
          priority: "Urgent",
          note: "Chest X-Ray"
        }
      ]
    }
  }
}).then((r) => {
  const updatedVisit = r.message
  renderVisit(updatedVisit)
})
```

### Service Order

```ts
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Visit",
    name: visitName,
    action: "create_orders",
    payload: {
      orders: [
        {
          kind: "service",
          title: "Grooming",
          care_service_id: "CareService-00006",
          provider: "provider@example.com",
          due_date: "2026-05-18",
          note: "Full grooming"
        }
      ]
    }
  }
}).then((r) => {
  const updatedVisit = r.message
  renderVisit(updatedVisit)
})
```

### Procedure Order

```ts
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Visit",
    name: visitName,
    action: "create_orders",
    payload: {
      orders: [
        {
          kind: "procedure",
          title: "Minor wound care",
          procedure_template: "Minor Wound Care",
          provider: "provider@example.com",
          scheduled_at: "2026-05-18 10:00:00",
          priority: "Normal",
          note: "Clean and dress wound"
        }
      ]
    }
  }
}).then((r) => {
  const updatedVisit = r.message
  renderVisit(updatedVisit)
})
```

## Work Flow After Order Creation

All four work types use the same outer Vue loop:

```txt
Doctor creates order from Visit
-> Backend creates Visit Order + linked operational record
-> Vue reloads the Visit aggregate
-> Vue reads order.linked_doctype and order.linked_name
-> Operator opens the linked record screen
-> Operator runs work actions against the linked record
-> Vue reloads the Visit aggregate and billing preview
```

Use this source type mapping when opening linked work:

| `linked_doctype` | Vue `source_type` | Work screen |
|---|---|---|
| `Lab` | `Lab` | Lab result workflow |
| `Imaging` | `Radiology` | Imaging report workflow |
| `PetCareService` | `Service` | Service provider workflow |
| `Pet Procedure` | `Procedure` | Procedure workflow |

Shared helper:

```ts
function sourceTypeForLinkedDoc(doctype?: string) {
  if (doctype === "Imaging") return "Radiology"
  if (doctype === "PetCareService") return "Service"
  if (doctype === "Pet Procedure") return "Procedure"
  return doctype
}

function openLinkedOrder(order: VisitOrder) {
  if (!order.linked_doctype || !order.linked_name) return

  openWorkspaceRecord({
    source_type: sourceTypeForLinkedDoc(order.linked_doctype),
    name: order.linked_name
  })
}
```

### Lab Flow

Create the lab from the Visit:

```txt
Visit create_orders kind=lab
-> Vet Visit.orders row status = Ordered
-> Lab record status = Ordered
-> Vet Visit.billable_items row item_type = Lab
```

Required order payload:

```ts
type CreateLabOrderPayload = {
  kind: "lab"
  title: string
  template_id: string
  priority?: "Low" | "Normal" | "Urgent" | "Emergency"
  note?: string
}
```

`template_id` must be an existing `CareService template` with `item_code` and `default_price`.

The linked `Lab` record is created with:

```ts
type LabRecord = {
  name: string
  visit: string
  order_id?: string
  pet: string
  doctor?: string
  care_service: string
  item_code?: string
  rate?: number
  status: "Ordered" | "Sample Collected" | "In Progress" | "Result Entered" | "Released" | "Cancelled"
  result?: string
  result_visibility?: "Clinical Team" | "Guardian Visible"
  attachment_required?: 0 | 1
}
```

Lab operator actions:

```ts
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Lab",
    name: labName,
    action: "start_test",
    payload: {}
  }
})
```

```ts
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Lab",
    name: labName,
    action: "save_result",
    payload: {
      result: "CBC result text"
    }
  }
})
```

```ts
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Lab",
    name: labName,
    action: "release",
    payload: {}
  }
}).then(() => {
  reloadVisit()
})
```

If the lab screen needs sample collection, result visibility, doctor review, or attachment-required validation, use the diagnostics API and then reload the Visit:

```ts
frappe.call({
  method: "pet_app.api.diagnostics.collect_sample",
  args: { lab: labName }
})
```

```ts
frappe.call({
  method: "pet_app.api.diagnostics.save_lab_result",
  args: {
    lab: labName,
    result: "CBC result text",
    data: {
      doctor_reviewed: 1,
      result_visibility: "Clinical Team",
      attachment_required: 0
    }
  }
})
```

```ts
frappe.call({
  method: "pet_app.api.diagnostics.release_lab_result",
  args: {
    lab: labName,
    result_visibility: "Guardian Visible"
  }
}).then(() => {
  reloadVisit()
})
```

Status sync:

| Lab status | Visit order status |
|---|---|
| `Ordered` | `Ordered` |
| `Sample Collected`, `In Progress`, `Result Entered` | `In Progress` |
| `Released` | `Completed` |
| `Cancelled` | `Cancelled` |

Billing match:

```ts
const labBillable = billableItems.find((row) =>
  row.item_type === "Lab" &&
  row.linked_doctype === "Lab" &&
  row.linked_name === labName
)
```

### Imaging / Radiology Flow

Create imaging from the Visit:

```txt
Visit create_orders kind=radiology
-> Vet Visit.orders row status = Ordered
-> Imaging record status = Ordered
-> Vet Visit.billable_items row item_type = Imaging
```

Required order payload:

```ts
type CreateImagingOrderPayload = {
  kind: "radiology"
  title: string
  template_id: string
  priority?: "Low" | "Normal" | "Urgent" | "Emergency"
  note?: string
}
```

`template_id` must be an existing `CareService template` with `item_code` and `default_price`.

The linked `Imaging` record is created with:

```ts
type ImagingRecord = {
  name: string
  visit: string
  order_id?: string
  pet: string
  doctor?: string
  care_service: string
  item_code?: string
  rate?: number
  status: "Ordered" | "Scheduled" | "In Progress" | "Reported" | "Released" | "Cancelled"
  report?: string
  image?: string
  result_visibility?: "Clinical Team" | "Guardian Visible"
  attachment_required?: 0 | 1
}
```

Radiology operator actions:

```ts
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Radiology",
    name: imagingName,
    action: "start_test",
    payload: {}
  }
})
```

```ts
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Radiology",
    name: imagingName,
    action: "save_result",
    payload: {
      report: "Thoracic radiology report text",
      image: "/files/xray.png"
    }
  }
})
```

```ts
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Radiology",
    name: imagingName,
    action: "release",
    payload: {}
  }
}).then(() => {
  reloadVisit()
})
```

If the imaging screen needs report visibility, doctor review, or attachment/image-required validation, use the diagnostics API and then reload the Visit:

```ts
frappe.call({
  method: "pet_app.api.diagnostics.save_imaging_report",
  args: {
    imaging: imagingName,
    report: "Thoracic radiology report text",
    image: "/files/xray.png",
    data: {
      doctor_reviewed: 1,
      result_visibility: "Clinical Team",
      attachment_required: 0
    }
  }
})
```

```ts
frappe.call({
  method: "pet_app.api.diagnostics.release_imaging_report",
  args: {
    imaging: imagingName,
    result_visibility: "Guardian Visible"
  }
}).then(() => {
  reloadVisit()
})
```

Status sync:

| Imaging status | Visit order status |
|---|---|
| `Ordered`, `Scheduled` | `Ordered` |
| `In Progress`, `Reported` | `In Progress` |
| `Released` | `Completed` |
| `Cancelled` | `Cancelled` |

Billing match:

```ts
const imagingBillable = billableItems.find((row) =>
  row.item_type === "Imaging" &&
  row.linked_doctype === "Imaging" &&
  row.linked_name === imagingName
)
```

### Procedure Flow

Create the procedure from the Visit:

```txt
Visit create_orders kind=procedure
-> Vet Visit.orders row status = Ordered
-> Pet Procedure record status = Pending
-> Procedure template checklist is copied
-> Vet Visit.billable_items row item_type = Procedure
```

Required order payload:

```ts
type CreateProcedureOrderPayload = {
  kind: "procedure"
  title?: string
  procedure_template: string
  care_service?: string
  care_service_id?: string
  provider?: string
  scheduled_at?: string
  priority?: "Low" | "Normal" | "Urgent" | "Emergency"
  indication?: string
  note?: string
}
```

`procedure_template` must be an active `Procedure Template`. The backend uses `care_service` / `care_service_id` from the payload, or falls back to `Procedure Template.billing_care_service`. That care service must have `item_code` and `default_price`.

Open the linked procedure:

```ts
frappe.call({
  method: "pet_app.api.workspace.get_record",
  args: {
    source_type: "Procedure",
    name: procedureName
  }
}).then((r) => {
  const aggregate = r.message
  const detail = aggregate.focus_source?.detail
})
```

Procedure detail shape:

```ts
type ProcedureDetail = {
  name: string
  visit: string
  order_id?: string
  pet: string
  guardian?: string
  doctor?: string
  provider?: string
  procedure_template: string
  procedure_template_label?: string
  care_service: string
  care_service_label?: string
  item_code?: string
  rate?: number
  status: "Pending" | "In Progress" | "Completed" | "Closed" | "Cancelled"
  scheduled_at?: string
  started_at?: string
  completed_at?: string
  closed_at?: string
  indication?: string
  consent_obtained: 0 | 1
  anesthesia_used: 0 | 1
  procedure_note?: string
  findings?: string
  outcome?: string
  complications?: string
  aftercare_instructions?: string
  checklist: ProcedureChecklistItem[]
}

type ProcedureChecklistItem = {
  name?: string
  step_title: string
  required: 0 | 1
  done: 0 | 1
  note?: string
}
```

Start procedure:

```ts
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Procedure",
    name: procedureName,
    action: "start_procedure",
    payload: {
      provider: "provider@example.com"
    }
  }
})
```

Save procedure documentation without completing:

```ts
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Procedure",
    name: procedureName,
    action: "save_procedure_note",
    payload: {
      consent_obtained: 1,
      anesthesia_used: 0,
      procedure_note: "Cleaned and dressed wound.",
      findings: "No foreign body seen.",
      outcome: "Stable",
      complications: "",
      aftercare_instructions: "Keep bandage dry.",
      checklist: [
        {
          name: checklistRowName,
          done: 1,
          note: "Completed"
        }
      ]
    }
  }
})
```

Complete procedure:

```ts
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Procedure",
    name: procedureName,
    action: "complete_procedure",
    payload: {
      procedure_note: "Procedure completed.",
      outcome: "Recovered well",
      checklist: completedChecklistRows
    }
  }
}).then(() => {
  reloadVisit()
})
```

Close procedure:

```ts
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Procedure",
    name: procedureName,
    action: "close_procedure",
    payload: {
      aftercare_instructions: "Return for recheck in 3 days."
    }
  }
}).then(() => {
  reloadVisit()
})
```

Cancel procedure:

```ts
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Procedure",
    name: procedureName,
    action: "cancel_procedure",
    payload: {}
  }
}).then(() => {
  reloadVisit()
})
```

Completion validation:

- If the template requires consent, `consent_obtained` must be true or a signed `Pet Consent Form` must exist.
- Every required checklist row must have `done = 1`.
- A billed Visit cannot be modified.

Status sync:

| Procedure status | Visit order status |
|---|---|
| `Pending` | `Ordered` |
| `In Progress` | `In Progress` |
| `Completed`, `Closed` | `Completed` |
| `Cancelled` | `Cancelled` |

Billing behavior:

- Creating the `Pet Procedure` creates or updates exactly one `Procedure` billable row.
- Saving notes/checklist does not add another charge.
- Cancelling the procedure cancels the unbilled billable row.
- Billed procedure rows cannot be changed.

Billing match:

```ts
const procedureBillable = billableItems.find((row) =>
  row.item_type === "Procedure" &&
  row.linked_doctype === "Pet Procedure" &&
  row.linked_name === procedureName
)
```

### Service Flow

Create the service from the Visit:

```txt
Visit create_orders kind=service
-> Vet Visit.orders row status = Ordered
-> PetCareService record status = pending
-> No automatic PetCareService billable row in current app code
```

Required order payload:

```ts
type CreateServiceOrderPayload = {
  kind: "service"
  title: string
  care_service_id: string
  provider?: string
  due_date?: string
  note?: string
}
```

The linked `PetCareService` record is created with:

```ts
type PetCareServiceRecord = {
  name: string
  pet_service_name: string
  pet_id: string
  guardian_id?: string
  care_service_id: string
  item_code?: string
  price?: number
  status: "pending" | "completed" | "overdue"
  doctor?: string
  provider?: string
  visit?: string
  order_id?: string
  due_date: string
  start_date?: string
  end_date?: string
  description?: string
}
```

Start service:

```ts
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Service",
    name: serviceName,
    action: "start_service",
    payload: {
      provider: "provider@example.com",
      description: "Started grooming."
    }
  }
})
```

Finish service:

```ts
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Service",
    name: serviceName,
    action: "finish_service",
    payload: {
      description: "Finished grooming."
    }
  }
}).then(() => {
  reloadVisit()
})
```

Close service uses the same backend path as finish:

```ts
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Service",
    name: serviceName,
    action: "close_service",
    payload: {}
  }
})
```

Status sync:

| PetCareService status | Visit order status |
|---|---|
| `pending`, `overdue` | `In Progress` after start |
| `completed` | `Completed` |

Service billing choices for Vue:

- For a non-billable operational service, use `create_orders kind=service` and show the linked `PetCareService`.
- For a billable generic service in the current backend, save the Visit `care_services` child table through the existing raw Visit save path and reload the Visit. Visit validation will create the `Service` billable row.
- Do not manually append a service `billable_items` row from Vue.
- If the product requirement is "service orders must auto-bill like lab/imaging/procedure", the backend should be patched so `PetCareService` syncs a billable row with `linked_doctype = "PetCareService"`, `linked_name = serviceName`, and `order_id = order.order_id`.

Existing visit service billing row source:

```ts
doc.care_services = doc.care_services || []
doc.care_services.push({
  care_service_id: "CareService-00006"
})
```

Direct Frappe save shape:

```ts
frappe.call({
  method: "frappe.client.get",
  args: {
    doctype: "Vet Visit",
    name: visitName
  }
}).then((r) => {
  const doc = r.message

  doc.care_services = doc.care_services || []
  doc.care_services.push({
    care_service_id: "CareService-00006"
  })

  return frappe.call({
    method: "frappe.client.save",
    args: { doc }
  })
}).then(() => {
  reloadVisit()
})
```

After saving and reloading:

```ts
const serviceBillables = (data.billables || []).filter((row) =>
  row.item_type === "Service" &&
  String(row.linked_service_id || "").startsWith("visit-care-service::")
)
```

## Medication Prescriptions

Medication is handled through the Visit's `prescribed_medications` child table.

Medication starts from a medication catalog record, then becomes a prescribed medication row on the Visit.

```txt
Admin defines Medication master
-> Medication is linked to an ERPNext Item
-> Doctor picks Medication on the Visit
-> Vue saves a prescribed_medications row
-> Backend creates/updates the medication billable row
```

Do **not** use `create_orders` for medication if the goal is auto billing. A `kind: "medication"` order row does not currently create a linked medication workspace or billable row.

Use this flow instead:

```txt
Doctor adds prescribed medication row
-> Vue saves the Vet Visit
-> Backend validates the visit
-> Backend creates/updates billable_items row with item_type = Medication
-> Pharmacy can dispense from the medication row
```

### Medication Master Setup

Create medication catalog records outside the Visit page.

Recommended `Medication` fields:

- `medication_name`: Display name doctors search for, for example `Amoxicillin 250mg Tablet`.
- `code`: Optional clinic code, for example `AMOX-250`.
- `linked_item`: ERPNext `Item` used for stock, billing, and invoice rows.
- `default_warehouse`: Pharmacy warehouse used when dispensing.
- `default_price`: Selling price copied to the linked Item or Item Price.
- `dosage_form_or_unit`: Tablet, bottle, ml, capsule, etc.
- `strength`: Human-readable strength, for example `250mg`.
- `default_dosage`: Example `1 tablet`.
- `default_frequency`: Example `Twice daily`.
- `default_duration_days`: Example `5`.
- `default_instructions`: Example `Give with food`.

Example:

```txt
Medication Name: Amoxicillin 250mg Tablet
Code: AMOX-250
Linked Item: AMOX-250-TAB
Default Warehouse: Main Pharmacy
Default Price: 3.00
Dosage Form or Unit: Tablet
Strength: 250mg
Default Dosage: 1 tablet
Default Frequency: Twice daily
Default Duration Days: 5
Default Instructions: Give with food
```

The `Medication` DocType can resolve or create its linked ERPNext `Item` during validation if the user has Item permissions. The Visit page should still treat `linked_item` as already prepared.

### Medication Picker Flow

Vue should let the doctor search and pick from `Medication` records.

On selection:

1. Fetch the selected `Medication`.
2. Require `linked_item`.
3. Copy `linked_item` into the Visit row as `medication_item`.
4. Copy defaults such as dosage, frequency, duration, instructions, warehouse, and price.
5. Let the doctor confirm or adjust `qty`, dosage, and instructions.
6. Save the raw `Vet Visit`.
7. Reload the Visit and billing preview.

Suggested picker shape:

```ts
type MedicationOption = {
  name: string
  medication_name: string
  code?: string
  linked_item: string
  default_warehouse?: string
  default_price?: number
  default_dosage?: string
  default_frequency?: string
  default_duration_days?: number
  default_instructions?: string
}
```

Example selection handler:

```ts
function medicationToVisitRow(medication: MedicationOption, qty = 1) {
  if (!medication.linked_item) {
    throw new Error("This medication is missing a linked Item. Ask an administrator to complete the Medication setup before prescribing it.")
  }

  return {
    medication: medication.name,
    medication_item: medication.linked_item,
    qty,
    rate: medication.default_price,
    dosage: medication.default_dosage,
    frequency: medication.default_frequency,
    duration_days: medication.default_duration_days,
    instructions: medication.default_instructions,
    warehouse: medication.default_warehouse,
    dispense_status: "Prescribed"
  }
}
```

Medication row shape:

```ts
type VetVisitMedicationItem = {
  name?: string
  medication?: string
  medication_item: string
  qty: number
  rate?: number
  amount?: number
  dosage?: string
  frequency?: string
  duration_days?: number
  instructions?: string
  warehouse?: string
  dispense_status?: "Prescribed" | "Pending Dispense" | "Dispensed" | "Partially Dispensed" | "Cancelled" | "Returned"
  dispensed_qty?: number
  return_qty?: number
}
```

Required fields for billing:

- `medication_item`
- `qty`

Recommended fields:

- `medication`
- `dosage`
- `frequency`
- `duration_days`
- `instructions`

`medication_item` must be an existing ERPNext `Item`. If Vue lets the user choose a `Medication` master, use its `linked_item` as `medication_item`.

Example row to save on the Visit:

```ts
const medicationRow = {
  medication: "Amoxicillin 250mg Tablet",
  medication_item: "AMOX-250-TAB",
  qty: 10,
  rate: 3.0,
  dosage: "1 tablet",
  frequency: "Twice daily",
  duration_days: 5,
  instructions: "Give with food",
  dispense_status: "Prescribed"
}
```

Use the app's existing raw Visit save path for `prescribed_medications`. If using Frappe client calls directly:

```ts
frappe.call({
  method: "frappe.client.get",
  args: {
    doctype: "Vet Visit",
    name: visitName
  }
}).then((r) => {
  const doc = r.message

  doc.prescribed_medications = doc.prescribed_medications || []
  doc.prescribed_medications.push({
    medication: "Amoxicillin 250mg Tablet",
    medication_item: "AMOX-250-TAB",
    qty: 10,
    rate: 3.0,
    dosage: "1 tablet",
    frequency: "Twice daily",
    duration_days: 5,
    instructions: "Give with food",
    dispense_status: "Prescribed"
  })

  return frappe.call({
    method: "frappe.client.save",
    args: { doc }
  })
}).then(() => {
  reloadVisit()
})
```

After saving, reload the Visit and read:

```ts
const medications = data.medications || []
const medicationBillables = (data.billables || []).filter((row) => row.item_type === "Medication")
```

The backend creates the medication billable row with:

```txt
linked_service_id = medication::<medication child row name>
item_type = Medication
item_code = medication_item
qty = prescribed qty
rate = row rate, Clinic price, or Item standard rate
```

### Medication Master and Item Creation

The Visit page should not create ERPNext `Item` records.

If a new `Medication` master is created separately, the `Medication` DocType can resolve or create its linked ERPNext `Item` during validation, depending on permissions. But prescribing medication on the Visit expects the Item to already exist through `medication_item`.

If the selected Medication has no `linked_item`, block the prescription save and show:

```txt
This medication is missing a linked Item. Ask an administrator to complete the Medication setup before prescribing it.
```

If the Medication exists but the linked Item is disabled or missing, backend validation will reject the Visit save.

### Pharmacy Dispense Flow

Pharmacy uses the medication child row, not `orders`.

List pending dispense:

```ts
frappe.call({
  method: "pet_app.api.pharmacy.list_pending_dispense",
  args: {
    visit: visitName
  }
})
```

Dispense medication:

```ts
frappe.call({
  method: "pet_app.api.pharmacy.dispense_visit_medication",
  args: {
    visit: visitName,
    row_name: medicationRowName,
    qty: 10,
    warehouse: "Main Pharmacy"
  }
}).then(() => {
  reloadVisit()
})
```

Return dispensed medication:

```ts
frappe.call({
  method: "pet_app.api.pharmacy.return_dispensed_medication",
  args: {
    visit: visitName,
    row_name: medicationRowName,
    qty: 1
  }
}).then(() => {
  reloadVisit()
})
```

### Cancel Medication Prescription

Cancel medication through the Visit action. Do not remove the row from Vue unless the screen is hiding cancelled history.

Allowed only before dispensing starts. If `dispensed_qty > 0`, backend rejects cancellation and the pharmacy must return or reverse the dispensed quantity first.

```ts
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Visit",
    name: visitName,
    action: "cancel_medication",
    payload: {
      row_name: medicationRowName,
      reason: "Owner declined"
    }
  }
}).then(() => {
  reloadVisit()
})
```

Backend result after reload:

```json
{
  "dispense_status": "Cancelled",
  "billing": {
    "billable_items": [],
    "cancelled_billable_items": [
      {
        "item_type": "Medication",
        "status": "Cancelled",
        "linked_service_id": "medication::<medication child row name>"
      }
    ]
  }
}
```

Saving the Visit after medication cancellation will not recreate the cancelled medication billable.
Cancelled medication billables are not returned in active `billing.billable_items`; use `billing.cancelled_billable_items` only for audit/history screens.

## Track Linked Records

After `create_orders`, read the returned or reloaded `orders[]`.

Each created order should include:

```ts
type VisitOrder = {
  order_id: string
  kind: "lab" | "radiology" | "service" | "procedure" | "medication" | "other"
  title: string
  status: string
  priority?: string
  note?: string
  linked_doctype?: string
  linked_name?: string
}
```

Use this to open the linked work page:

```ts
function openLinkedOrder(order: VisitOrder) {
  if (!order.linked_doctype || !order.linked_name) return

  openWorkspaceRecord({
    source_type: sourceTypeForLinkedDoc(order.linked_doctype),
    name: order.linked_name
  })
}
```

Example after creating a lab order:

```txt
order.kind = "lab"
order.order_id = "VVT-2026-00001-ORD-001"
order.linked_doctype = "Lab"
order.linked_name = "LAB-00001"
```

For lab, imaging, and procedure, the matching billable row will have the same `order_id`, `linked_doctype`, and `linked_name`.

For service orders, the current app creates the linked `PetCareService` record and order status tracking, but does not create a `PetCareService` billable row. Use the Visit `care_services` billing path for billable generic services until backend service-order billing sync exists.

Medication billable rows do not use `order_id`. Match them by `linked_service_id`, which starts with `medication::`, or by `item_type === "Medication"`.

## Render the Visit Page

Recommended sections:

### Orders / Work

Source:

```ts
visit.orders
```

Show:

- `kind`
- `title`
- `status`
- `priority`
- `note`
- link button if `linked_doctype` and `linked_name` exist

This section is for clinical tracking.

### Prescribed Medications

Source:

```ts
data.medications
```

or raw visit:

```ts
visit.prescribed_medications
```

Show:

- `medication`
- `medication_item`
- `qty`
- `dosage`
- `frequency`
- `duration_days`
- `instructions`
- `dispense_status`
- `dispensed_qty`

This section is for prescription and pharmacy tracking.

### Billing Preview

Source:

```ts
visit.billing.billable_items
```

or from workbench:

```ts
data.billables
```

Show:

- `item_name`
- `item_code`
- `item_type`
- `qty`
- `rate`
- `amount`
- `status`

Use backend totals:

```ts
visit.billing.total
visit.billing.balance
visit.billing.paid
visit.billing.billing_status
```

Do not compute final invoice totals in Vue.

## Create Visit Invoice

When the visit is ready to invoice, Vue should call:

```ts
frappe.call({
  method: "pet_app.api.v1.billing.create_visit_invoice",
  args: {
    visit_name: visitName
  }
}).then((r) => {
  const result = r.message.data || r.message
  reloadVisit()
})
```

The backend will:

- Use active `billable_items`.
- Create and submit the `Sales Invoice`.
- Mark billable rows as `Billed`.
- Set `visit.sales_invoice`.
- Set `visit.billed = 1`.
- Lock billed visit changes.

## Frontend Rules

Do:

- Use `create_orders` for lab, imaging, and procedure when the doctor requests work and billing should follow the linked record.
- Use `create_orders` for service when the doctor needs a linked `PetCareService` operational task.
- Use `Vet Visit.care_services` for billable generic services in the current backend.
- Use `prescribed_medications` for medication.
- Reload visit data after every mutation.
- Render `orders` as workflow tracking.
- Render `prescribed_medications` as prescription/pharmacy tracking.
- Render `billable_items` as billing preview.
- Use `linked_doctype` and `linked_name` for navigation.
- Disable order creation and billing edits when the visit is billed.

Do not:

- Do not manually create a `billable_items` row after creating an order.
- Do not assume `create_orders kind=service` creates a service billable row in the current app code.
- Do not use `create_orders` for medication billing.
- Do not create ERPNext `Item` records from the Visit page.
- Do not calculate final invoice totals in Vue.
- Do not invoice directly from `orders`.
- Do not edit `Billed` billable rows.

## UI Disable Logic

Disable create-order actions when:

```ts
const disabled =
  Boolean(visit.billing?.billed) ||
  visit.summary?.status === "Cancelled"
```

If using workbench permissions:

```ts
const canCreateOrders =
  Boolean(data.permissions?.can_create_orders) &&
  !Boolean(data.permissions?.is_billed) &&
  !Boolean(data.permissions?.is_cancelled)
```

Disable invoice button when:

```ts
const canInvoice =
  !visit.billing?.billed &&
  (visit.billing?.billable_items || []).some((row) => row.status !== "Cancelled")
```

Backend will still do final validation, including pending lab/imaging/procedure checks.

## Error Handling

Show backend validation messages directly to the user.

Common errors:

- Care Service is missing `item_code`.
- Care Service is missing `default_price`.
- Procedure Template is missing `billing_care_service`.
- Required procedure consent or checklist item is missing.
- Medication row is missing `medication_item`.
- Medication row has invalid `qty`.
- Visit is already billed.
- Pending lab/imaging/procedure must be completed before invoicing.
- User does not have permission.

## Final Flow

```txt
Doctor opens Visit
-> Vue loads workspace aggregate
-> Doctor adds order with perform_action/create_orders
-> Backend creates order + linked record
-> Backend auto-creates billable rows for Lab, Imaging, and Procedure
-> Generic service billing uses Vet Visit.care_services until PetCareService billing sync is added
-> Doctor adds medications in prescribed_medications
-> Backend creates medication billable item when Visit is saved
-> Vue reloads Visit
-> Staff works linked Lab/Imaging/Service/Procedure
-> Pharmacy dispenses prescribed medication rows
-> Billing Preview updates from billable_items
-> Cashier creates visit invoice
-> Backend creates Sales Invoice from billable_items
-> Visit becomes billed and locked
```
