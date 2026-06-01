# Pet App Healthcare Workspace Backend Contract

## Summary

`/profile` should treat `pet_app.api.workspace` as the backend source of truth for daily clinical work. The frontend should not infer relationships between visits, appointments, labs, imaging, services, procedures, billing, files, or consults. It should call the workspace APIs, render returned aggregates, and send safe action payloads.

Core center: `Vet Visit`.

Core API module: `pet_app.api.workspace`.

Permission/access module: `pet_app.api.permissions`.

Important: this is not a Frappe Desk Workspace UI contract. The word `workspace` here means an API-backed daily work queue for the Vue `/profile` page. Vue should call whitelisted API methods directly and should not depend on Frappe Desk Workspace routing, Workspace DocType records, or Desk pages for normal clinical work.

## Frontend API Calls

Use Frappe calls:

```ts
frappe.call({
  method: "pet_app.api.workspace.get_my_workspace",
  args: { mode, search, priority, status, limit, cursor }
})

frappe.call({
  method: "pet_app.api.workspace.get_record",
  args: { source_type, name }
})

frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: { source_type, name, action, payload }
})

frappe.call({
  method: "pet_app.api.workspace.add_note",
  args: { source_type, name, note }
})

frappe.call({
  method: "pet_app.api.workspace.attach_file",
  args: { source_type, name, payload }
})
```

## Source Types

Frontend may send either display `source_type` or backend doctype alias.

| UI Source Type | Backend DocType |
|---|---|
| `Visit` | `Vet Visit` |
| `Case Sheet` | `Vet Case Sheet` |
| `Appointment` | `Appointment` |
| `Service` | `PetCareService` |
| `Procedure` | `Pet Procedure` |
| `Lab` | `Lab` |
| `Radiology` / `Imaging` | `Imaging` |
| `Invoice` | `Sales Invoice` |
| `Payment` | `Payment Entry` |

## Queue API

### `get_my_workspace`

Input:

```json
{
  "mode": "doctor | service | coordinator | diagnostics | accounting | management | all",
  "user": "optional user email, admin only for other users",
  "search": "optional text",
  "priority": "Low | Normal | Urgent | Emergency",
  "status": "optional exact status",
  "limit": 50,
  "cursor": 0
}
```

Output:

```ts
type WorkspaceResponse = {
  mode: string
  user: string
  metrics: {
    total: number
    overdue: number
    urgent: number
    by_source: Record<string, number>
    by_status: Record<string, number>
  }
  items: WorkspaceItem[]
  next_cursor: number | null
  cursor: number | null
  total: number
}
```

### `WorkspaceItem`

```ts
type WorkspaceItem = {
  id: string
  name: string
  source_type: "Visit" | "Case Sheet" | "Appointment" | "Service" | "Procedure" | "Lab" | "Radiology" | "Invoice" | "Payment"
  source_doctype: string
  title: string
  subtitle?: string
  status?: string
  priority: "Low" | "Normal" | "Urgent" | "Emergency"
  pet?: PetSummary
  guardian?: GuardianSummary
  assignee?: AssigneeSummary
  scheduled_at?: string
  due_at?: string
  overdue: boolean
  next_task?: string
  modified?: string
  creation?: string
  badges: string[]

  // Common extras
  visit_id?: string
  order_id?: string
  care_service_id?: string
  case_sheet_id?: string
  sales_invoice?: string
  follow_up_status?: string
  follow_up_visit_id?: string
  follow_up_of_visit_id?: string
  procedure_template?: string
}
```

## Queue Behavior By Mode

| Mode | Queue Includes |
|---|---|
| `doctor` | assigned visits, visits with open consults for doctor, open procedures for doctor |
| `service` | assigned/open `PetCareService`, procedures assigned to provider |
| `coordinator` | open appointments, unconverted case sheets, unassigned services, open procedures |
| `diagnostics` | open lab and imaging records |
| `accounting` | unpaid/draft invoices |
| `management` | cross-role queues |
| `all` | all supported queues |

Open statuses used by backend:

```ts
const OPEN_VISIT_STATUSES = ["Draft", "In Progress", "Follow-up Needed"]
const OPEN_CASE_STATUSES = ["Draft", "Waiting Doctor", "In Consultation"]
const OPEN_SERVICE_STATUSES = ["pending", "overdue", "Pending", "Overdue"]
const OPEN_PROCEDURE_STATUSES = ["Pending", "In Progress"]
const OPEN_DIAGNOSTIC_STATUSES = ["Pending", "In Progress"]
const OPEN_CONSULT_STATUSES = ["Requested", "Accepted"]
```

## Aggregate API

### `get_record`

Input:

```json
{
  "source_type": "Visit | Case Sheet | Appointment | Service | Procedure | Lab | Radiology | Invoice | Payment",
  "name": "record-name"
}
```

Output always tries to return the parent visit aggregate if the record links to a visit.

```ts
type WorkspaceRecordDetail = {
  summary: RecordSummary
  pet: PetSummary
  guardian: GuardianSummary
  assignee: AssigneeSummary
  clinical: VisitClinical
  follow_up: VisitFollowUp
  consult_requests: VisitConsultRequest[]
  diagnoses: WorkspaceDiagnosis[]
  orders: WorkspaceOrder[]
  linked_records: WorkspaceLinkedRecord[]
  notes: WorkspaceNote[]
  attachments: WorkspaceAttachment[]
  billing: WorkspaceBilling
  timeline: WorkspaceTimelineEvent[]
  raw?: { doctype: string; name: string }
  focus_source?: FocusSource
  pending_follow_up_appointment?: string
}
```

### Summary Types

```ts
type RecordSummary = {
  name: string
  source_type: string
  source_doctype: string
  title: string
  status?: string
  priority?: string
  case_sheet_id?: string
  visit_datetime?: string
  next_task?: string
  creation?: string
  modified?: string
}

type PetSummary = {
  id?: string
  name?: string
  name_label?: string
  pet_name?: string
  species?: string
  breed?: string
  gender?: string
  weight?: number
  image?: string
}

type GuardianSummary = {
  id?: string
  name?: string
  name_label?: string
  full_name?: string
  phone?: string
  email?: string
  customer_id?: string
  image?: string
}

type AssigneeSummary = {
  id?: string
  name?: string
  name_label?: string
  user?: string
  specialization?: string
  image?: string
}
```

## Visit Clinical Aggregate

```ts
type VisitClinical = {
  chief_complaint?: string
  intake_summary?: string
  overview?: string
  examination?: string
  assessment?: string
  plan?: string
  instructions?: string
  doctor_notes?: string
  case_summary?: string
  illness?: string
  diagnosis?: string
  differential_diagnosis?: string

  follow_up_required?: 0 | 1
  follow_up_reason?: string
  follow_up_preferred_date?: string
  follow_up_date?: string
  follow_up_status?: "Not Needed" | "Requested" | "Scheduled" | "Seen" | "Cancelled"

  vitals: {
    weight?: number
    temperature?: number
    heart_rate?: number
    respiratory_rate?: number
  }
}
```

## Follow-Up Aggregate

```ts
type VisitFollowUp = {
  required: 0 | 1
  reason?: string
  preferred_date?: string
  date?: string
  appointment_id?: string
  visit_id?: string
  of_visit_id?: string
  status: "Not Needed" | "Requested" | "Scheduled" | "Seen" | "Cancelled"
}
```

Important behavior:

- `request_follow_up` creates/reuses an `Appointment`.
- It does not create a new visit immediately.
- `convert_follow_up_to_visit` creates the linked follow-up `Vet Visit`.
- Follow-up actions do not create a consultation billable line.

## Consult Aggregate

```ts
type VisitConsultRequest = {
  name: string
  requested_doctor: string
  requested_doctor_label?: string
  requested_by: string
  reason?: string
  status: "Requested" | "Accepted" | "Completed" | "Cancelled"
  consult_note?: string
  requested_at?: string
  completed_at?: string
}
```

Important behavior:

- Consults stay inside the same `Vet Visit`.
- They do not create a billable consultation charge.
- Duplicate open consults for the same doctor are rejected.

## Diagnosis Aggregate

```ts
type WorkspaceDiagnosis = {
  name?: string
  disease?: string
  disease_name?: string
  diagnosis_text?: string
  is_primary: 0 | 1
  severity?: "Mild" | "Moderate" | "Severe" | "Critical"
  note?: string
}
```

`assessment`/`diagnosis` text remains readable clinical text. Structured rows power reporting/search.

## Orders Aggregate

```ts
type WorkspaceOrder = {
  name?: string
  order_id: string
  kind: "lab" | "radiology" | "service" | "procedure" | "medication" | "other"
  title: string
  item_code?: string
  template_id?: string
  status: "Draft" | "Ordered" | "In Progress" | "Completed" | "Cancelled"
  priority: "Low" | "Normal" | "Urgent" | "Emergency"
  qty?: number
  price?: number
  note?: string
  linked_doctype?: string
  linked_name?: string
}
```

Order linking rules:

| Order Kind | Created Record | Billing Source |
|---|---|---|
| `lab` | `Lab` | `CareService template` |
| `radiology` | `Imaging` | `CareService template` |
| `service` | `PetCareService` | existing service behavior |
| `procedure` | `Pet Procedure` | `Procedure Template.billing_care_service` |
| `medication` | no workspace-linked record in this milestone | existing visit medication rows |
| `other` | no linked record | none |

## Linked Records Aggregate

```ts
type WorkspaceLinkedRecord = {
  source_type: "Lab" | "Radiology" | "Service" | "Procedure"
  source_doctype: "Lab" | "Imaging" | "PetCareService" | "Pet Procedure"
  name: string
  status?: string
  order_id?: string
  title?: string
  provider?: string
  care_service_id?: string
  procedure_template?: string
  start_date?: string
  end_date?: string
  scheduled_at?: string
  started_at?: string
  completed_at?: string
  closed_at?: string
  modified?: string
}
```

## Procedure Detail

Opening `source_type = "Procedure"` returns the parent visit aggregate plus:

```ts
type FocusSource = {
  name: string
  source_type: string
  source_doctype: string
  title: string
  status?: string
  priority: string
  creation?: string
  modified?: string
  detail?: ProcedureDetail
}

type ProcedureDetail = {
  name: string
  visit: string
  order_id?: string
  pet: string
  guardian?: string
  doctor?: string
  doctor_label?: string
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
  attachments: WorkspaceAttachment[]
  notes: WorkspaceNote[]
}

type ProcedureChecklistItem = {
  name: string
  step_title: string
  required: 0 | 1
  done: 0 | 1
  note?: string
}
```

## Billing Aggregate

```ts
type WorkspaceBilling = {
  billing_status?: "Unbilled" | "Draft Invoice" | "Partially Paid" | "Paid" | "Follow-up" | "Cancelled"
  sales_invoice?: string
  total: number
  paid: number
  balance: number
  currency?: string
  billed: 0 | 1
  billable_items: BillableItem[]
}

type BillableItem = {
  name: string
  item_name?: string
  item_code?: string
  item_type: "Room Stay" | "Service" | "Medication" | "Lab" | "Imaging" | "Procedure" | "Product" | "Other"
  qty: number
  rate: number
  amount: number
  status: "Draft" | "Billable" | "Billed" | "Cancelled"
  note?: string
  linked_service_id?: string
}
```

Procedure billing behavior:

- Creating `Pet Procedure` creates/updates exactly one `Procedure` billable row.
- Documentation actions do not add extra charges.
- Cancelling a procedure cancels its unbilled billable row.
- Billed visits reject procedure billing changes.

## Notes, Attachments, Timeline

```ts
type WorkspaceNote = {
  name: string
  content: string
  owner: string
  creation: string
  modified: string
}

type WorkspaceAttachment = {
  name: string
  file_name: string
  file_url: string
  is_private: 0 | 1
  owner: string
  creation: string
  modified: string
}

type WorkspaceTimelineEvent = {
  type: "created" | "status" | "linked_record" | "note"
  at?: string
  label: string
  source_doctype?: string
  name?: string
}
```

## Workspace Actions

## Vue API Action Map

All queue reads, record opens, mutations, notes, files, and permissions are mapped to API methods.

| Vue Intent | API Method | Required Args | Returns |
|---|---|---|---|
| Load daily queue | `pet_app.api.workspace.get_my_workspace` | `mode`, optional filters | `WorkspaceResponse` |
| Open any queue item | `pet_app.api.workspace.get_record` | `source_type`, `name` | `WorkspaceRecordDetail` |
| Run any workflow action | `pet_app.api.workspace.perform_action` | `source_type`, `name`, `action`, `payload` | updated aggregate |
| Add note | `pet_app.api.workspace.add_note` | `source_type`, `name`, `note` | updated aggregate |
| Attach file | `pet_app.api.workspace.attach_file` | `source_type`, `name`, file payload | updated aggregate |
| Load current permissions | `pet_app.api.permissions.get_current_access` | none | role/resource/action access |

Supported `perform_action` values:

| Action | Source Type | Purpose |
|---|---|---|
| `start_consultation` | `Visit` | start visit |
| `save_clinical_note` | `Visit` | save SOAP/clinical fields |
| `save_diagnoses` | `Visit` | save structured diagnosis rows |
| `create_orders` | `Visit` | create lab/radiology/service/procedure orders |
| `complete_case` | `Visit` | complete visit after required records are done |
| `request_follow_up` | `Visit` | create/reuse follow-up appointment |
| `convert_follow_up_to_visit` | `Visit` or `Appointment` | convert follow-up appointment into linked visit |
| `request_consult` | `Visit` | request second-doctor consult inside same visit |
| `complete_consult` | `Visit` | save consult note inside same visit |
| `start_service` | `Service` | start service work |
| `finish_service` | `Service` | finish service work |
| `close_service` | `Service` | close service work |
| `start_test` | `Lab` or `Radiology` | mark linked order in progress |
| `save_result` | `Lab` or `Radiology` | save lab result/radiology report |
| `release` | `Lab` or `Radiology` | complete diagnostic record/order |
| `start_procedure` | `Procedure` | start procedure |
| `save_procedure_note` | `Procedure` | save checklist and documentation |
| `complete_procedure` | `Procedure` | complete procedure and linked order |
| `close_procedure` | `Procedure` | close procedure |
| `cancel_procedure` | `Procedure` | cancel procedure and unbilled charge |
| `assign` | `Visit`, `Lab`, `Radiology`, `Service`, `Procedure` | assign doctor/provider |
| `reassign` | `Visit`, `Lab`, `Radiology`, `Service`, `Procedure` | reassign doctor/provider |
| `convert_to_visit` | `Appointment` or `Case Sheet` | create linked visit |
| `convert_to_service` | `Appointment` | create linked service |
| `submit_invoice` | `Invoice` | submit draft invoice |
| `mark_follow_up` | `Invoice` or linked billing source | mark billing follow-up note |

### Common action call

```json
{
  "source_type": "Visit",
  "name": "VVT-2026-00003",
  "action": "create_orders",
  "payload": {}
}
```

All `perform_action` calls return an updated aggregate.

## Doctor Actions

### `start_consultation`

Source: `Visit`

Payload:

```json
{}
```

Effect:

- Sets visit from `Draft` to `In Progress`.
- Adds a comment.

### `save_clinical_note`

Source: `Visit`

Payload fields:

```json
{
  "intake_summary": "string",
  "overview": "string",
  "examination": "string",
  "assessment": "string",
  "diagnosis": "string",
  "differential_diagnosis": "string",
  "plan": "string",
  "instructions": "string",
  "doctor_notes": "string",
  "illness": "string",
  "follow_up_required": 1,
  "follow_up_reason": "string",
  "follow_up_preferred_date": "YYYY-MM-DD",
  "follow_up_date": "YYYY-MM-DD",
  "follow_up_status": "Requested",
  "weight": 12.5,
  "temperature": 38.4,
  "heart_rate": 90,
  "respiratory_rate": 24,
  "status": "In Progress"
}
```

### `save_diagnoses`

Source: `Visit`

```json
{
  "diagnoses": [
    {
      "disease": "optional existing Disease id",
      "disease_name": "Otitis externa",
      "diagnosis_text": "Clinical diagnosis text",
      "is_primary": 1,
      "severity": "Moderate",
      "note": "optional"
    }
  ]
}
```

### `create_orders`

Source: `Visit`

Lab:

```json
{
  "orders": [
    {
      "kind": "lab",
      "title": "CBC",
      "template_id": "CareService-00004",
      "priority": "Normal",
      "note": "reason"
    }
  ]
}
```

Radiology:

```json
{
  "orders": [
    {
      "kind": "radiology",
      "title": "X-Ray",
      "template_id": "CareService-00005",
      "priority": "Urgent",
      "note": "reason"
    }
  ]
}
```

Service:

```json
{
  "orders": [
    {
      "kind": "service",
      "title": "Grooming",
      "care_service_id": "CareService-00006",
      "provider": "provider@example.com",
      "due_date": "YYYY-MM-DD",
      "note": "instructions"
    }
  ]
}
```

Procedure:

```json
{
  "orders": [
    {
      "kind": "procedure",
      "procedure_template": "Smoke Minor Procedure",
      "title": "Minor wound care",
      "provider": "provider@example.com",
      "scheduled_at": "2026-05-03 10:00:00",
      "priority": "Normal",
      "note": "indication"
    }
  ]
}
```

### `complete_case`

Source: `Visit`

Payload may include clinical note fields and diagnoses.

```json
{
  "overview": "string",
  "assessment": "string",
  "plan": "string",
  "instructions": "string",
  "diagnoses": []
}
```

Backend requires required clinical completion fields and no pending lab/imaging/procedure records.

## Follow-Up Actions

### `request_follow_up`

Source: `Visit`

```json
{
  "reason": "Recheck wound",
  "follow_up_preferred_date": "2026-05-10",
  "scheduled_time": "2026-05-10 14:30:00",
  "doctor": "DOC-00001"
}
```

Notes:

- If `scheduled_time` is missing, backend uses `09:00` on preferred date.
- Creates/reuses an open `Appointment`.
- Sets original visit follow-up status to `Scheduled`.

### `convert_follow_up_to_visit`

Source: `Appointment` or original `Visit`

```json
{
  "appointment_id": "APMT-0001",
  "doctor": "DOC-00001",
  "priority": "Normal",
  "chief_complaint": "Follow-up",
  "intake_notes": "optional"
}
```

Notes:

- Creates new `Vet Case Sheet`.
- Creates new `Vet Visit` with `visit_type = Follow-up`.
- New visit has `follow_up_of_visit_id = original visit`.
- Idempotent: if already converted, returns existing linked visit aggregate.

## Consult Actions

### `request_consult`

Source: `Visit`

```json
{
  "requested_doctor": "DOC-00002",
  "reason": "Need second opinion"
}
```

### `complete_consult`

Source: `Visit`

```json
{
  "consult_request_name": "child-row-name",
  "consult_note": "Consult note text"
}
```

Alternative matching payload:

```json
{
  "requested_doctor": "DOC-00002",
  "consult_note": "Consult note text"
}
```

## Service Actions

### `start_service`

Source: `Service`

```json
{
  "provider": "provider@example.com",
  "description": "optional note"
}
```

### `finish_service`

Source: `Service`

```json
{
  "description": "completed service note"
}
```

### `close_service`

Source: `Service`

```json
{
  "description": "closing note"
}
```

## Diagnostic Actions

### `start_test`

Source: `Lab` or `Radiology`

```json
{}
```

Current backend mainly keeps status as pending/in-progress unless result/release is saved.

### `save_result`

Lab:

```json
{
  "result": "Lab result text"
}
```

Radiology:

```json
{
  "report": "Radiology report text",
  "image": "/files/image.png"
}
```

### `release`

Source: `Lab` or `Radiology`

```json
{
  "result": "final result text",
  "report": "final report text",
  "image": "/files/image.png"
}
```

Effect:

- Sets diagnostic status to `Completed`.
- Updates linked Visit Order to `Completed`.

## Procedure Actions

### `start_procedure`

Source: `Procedure`

```json
{
  "provider": "provider@example.com"
}
```

Effect:

- Sets status `In Progress`.
- Stamps `started_at`.
- Updates linked Visit Order to `In Progress`.

### `save_procedure_note`

Source: `Procedure`

```json
{
  "scheduled_at": "2026-05-03 10:00:00",
  "provider": "provider@example.com",
  "indication": "reason for procedure",
  "consent_obtained": 1,
  "anesthesia_used": 0,
  "procedure_note": "procedure note",
  "findings": "findings text",
  "outcome": "outcome text",
  "complications": "none",
  "aftercare_instructions": "aftercare text",
  "checklist": [
    {
      "name": "optional-child-row-name",
      "step_title": "Confirm consent",
      "done": 1,
      "note": "Consent confirmed"
    }
  ]
}
```

### `complete_procedure`

Source: `Procedure`

```json
{
  "consent_obtained": 1,
  "procedure_note": "done",
  "findings": "normal",
  "outcome": "completed",
  "complications": "none",
  "aftercare_instructions": "monitor",
  "checklist": [
    { "step_title": "Confirm consent", "done": 1 },
    { "step_title": "Document findings", "done": 1 }
  ]
}
```

Effect:

- Saves documentation/checklist.
- Sets status `Completed`.
- Stamps `completed_at`.
- Updates Visit Order to `Completed`.

### `close_procedure`

Source: `Procedure`

```json
{}
```

Effect:

- Sets status `Closed`.
- Stamps `closed_at`.
- Keeps linked Visit Order `Completed`.

### `cancel_procedure`

Source: `Procedure`

```json
{}
```

Effect:

- Sets status `Cancelled`.
- Updates Visit Order to `Cancelled`.
- Cancels the unbilled procedure billable item.

## Coordinator Actions

### `assign` / `reassign`

Visit:

```json
{
  "doctor": "DOC-00001"
}
```

Lab/Radiology:

```json
{
  "doctor": "DOC-00001"
}
```

Service:

```json
{
  "provider": "provider@example.com",
  "user": "user@example.com",
  "doctor": "DOC-00001"
}
```

Procedure:

```json
{
  "provider": "provider@example.com",
  "doctor": "DOC-00001"
}
```

### `convert_to_visit`

Source: `Appointment` or `Case Sheet`

```json
{
  "doctor": "DOC-00001",
  "priority": "Normal",
  "visit_type": "Consultation",
  "chief_complaint": "Checkup",
  "intake_notes": "intake"
}
```

### `convert_to_service`

Source: `Appointment`

```json
{
  "template_id": "CareService-00006",
  "pet": "PET-00001",
  "doctor": "DOC-00001",
  "provider": "provider@example.com",
  "due_date": "YYYY-MM-DD",
  "description": "service appointment"
}
```

## Accounting Actions

### `submit_invoice`

Source: `Invoice`

```json
{}
```

### `mark_follow_up`

Source: usually `Invoice` or visit-linked billing source.

```json
{
  "note": "Need payment follow-up"
}
```

## Notes And Attachments

### `add_note`

```json
{
  "source_type": "Visit",
  "name": "VVT-2026-00003",
  "note": "Visible workspace note"
}
```

### `attach_file`

```json
{
  "source_type": "Procedure",
  "name": "PROC-00001",
  "payload": {
    "file_url": "/files/photo.jpg",
    "file_name": "photo.jpg",
    "is_private": 1
  }
}
```

Base64 upload:

```json
{
  "payload": {
    "file_name": "photo.jpg",
    "filedata": "data:image/jpeg;base64,...",
    "is_private": 1
  }
}
```

## Backend DocType JSON Summary

### `Vet Visit`

Main clinical source of truth.

| Field | Type | Notes |
|---|---|---|
| `status` | Select | `Draft`, `In Progress`, `Completed`, `Cancelled`, `Follow-up Needed` |
| `priority` | Select | `Low`, `Normal`, `Urgent`, `Emergency` |
| `visit_datetime` | Datetime | defaults now |
| `case_sheet` | Link `Vet Case Sheet` | required |
| `guardian` | Link `Guardian` | from case sheet |
| `customer` | Link `Customer` | required |
| `animal_patient` | Link `Pet` | required |
| `doctor` | Link `Doctor` | required |
| `visit_type` | Select | `Consultation`, `Follow-up`, `Vaccination`, `Emergency`, `Procedure`, `Recheck` |
| `examination_notes` | Text Editor | rendered as clinical examination |
| `case_summary` | Small Text | readonly |
| `intake_summary` | Text Editor | intake |
| `overview` | Text Editor | summary |
| `illness` | Select | clinical category |
| `diagnosis` | Text Editor | readable diagnosis |
| `assessment` | Text Editor | SOAP assessment |
| `differential_diagnosis` | Small Text | optional |
| `treatment_plan` | Text Editor | plan |
| `doctor_notes` | Text Editor | note |
| `instructions` | Text Editor | discharge/instructions |
| `diagnoses` | Table `Visit Diagnosis` | structured diagnoses |
| `orders` | Table `Visit Order` | linked orders |
| `billable_items` | Table `Pet Billable Item` | billing snapshot source |
| `follow_up_required` | Check | follow-up flag |
| `follow_up_status` | Select | `Not Needed`, `Requested`, `Scheduled`, `Seen`, `Cancelled` |
| `follow_up_reason` | Small Text | reason |
| `follow_up_preferred_date` | Date | new preferred date |
| `follow_up_date` | Date | kept for compatibility |
| `follow_up_appointment_id` | Link `Appointment` | appointment created by request |
| `follow_up_visit_id` | Link `Vet Visit` | converted follow-up visit |
| `follow_up_of_visit_id` | Link `Vet Visit` | set on new follow-up visit |
| `consult_requests` | Table `Visit Consult Request` | second-doctor consults |

### `Visit Order`

Child table on `Vet Visit.orders`.

| Field | Type | Notes |
|---|---|---|
| `order_id` | Data | readonly generated id |
| `kind` | Select | `lab`, `radiology`, `service`, `procedure`, `medication`, `other` |
| `title` | Data | required |
| `item_code` | Link `Item` | optional |
| `template_id` | Link `CareService template` | care service billing template |
| `status` | Select | `Draft`, `Ordered`, `In Progress`, `Completed`, `Cancelled` |
| `priority` | Select | `Low`, `Normal`, `Urgent`, `Emergency` |
| `qty` | Float | default `1` |
| `price` | Currency | optional |
| `note` | Small Text | clinical/order note |
| `linked_doctype` | Link `DocType` | readonly |
| `linked_name` | Dynamic Link | readonly |

### `Visit Diagnosis`

| Field | Type | Notes |
|---|---|---|
| `disease` | Link `Disease` | optional existing disease |
| `diagnosis_text` | Small Text | structured diagnosis text |
| `is_primary` | Check | primary diagnosis marker |
| `severity` | Select | blank, `Mild`, `Moderate`, `Severe`, `Critical` |
| `note` | Small Text | optional |

### `Disease`

| Field | Type | Notes |
|---|---|---|
| `disease_name` | Data | required, document name |
| `species` | Select | dog/cat/bird/fish/reptile/horse/rabbit/other/mammal/etc |
| `category` | Select | clinical category |
| `active` | Check | default `1` |

### `Visit Consult Request`

| Field | Type | Notes |
|---|---|---|
| `requested_doctor` | Link `Doctor` | required |
| `requested_by` | Link `User` | readonly |
| `reason` | Small Text | consult reason |
| `status` | Select | `Requested`, `Accepted`, `Completed`, `Cancelled` |
| `consult_note` | Text Editor | second doctor note |
| `requested_at` | Datetime | readonly |
| `completed_at` | Datetime | readonly |

### `Lab`

| Field | Type | Notes |
|---|---|---|
| `visit` | Link `Vet Visit` | required |
| `order_id` | Data | readonly |
| `pet` | Link `Pet` | required |
| `doctor` | Link `Doctor` | assigned doctor |
| `care_service` | Link `CareService template` | required |
| `item_code` | Link `Item` | readonly from care service |
| `status` | Select | `Pending`, `Completed` |
| `result` | Small Text | lab result |

### `Imaging`

| Field | Type | Notes |
|---|---|---|
| `visit` | Link `Vet Visit` | required |
| `order_id` | Data | readonly |
| `pet` | Link `Pet` | required |
| `doctor` | Link `Doctor` | assigned doctor |
| `care_service` | Link `CareService template` | required |
| `item_code` | Link `Item` | readonly |
| `rate` | Float | readonly |
| `status` | Select | `Pending`, `Completed` |
| `report` | Small Text | report text |
| `image` | Attach Image | image/report attachment |

### `PetCareService`

| Field | Type | Notes |
|---|---|---|
| `status` | Select | `pending`, `completed`, `overdue` |
| `provider` | Data | provider/user text |
| `description` | Small Text | service note |
| `due_date` | Date | required |
| `pet_service_name` | Data | required |
| `pet_id` | Link `Pet` | required |
| `care_service_id` | Link `CareService template` | required |
| `doctor` | Link `Doctor` | doctor |
| `visit` | Link `Vet Visit` | linked visit |
| `order_id` | Data | linked order |
| `start_date` | Datetime | started |
| `end_date` | Datetime | finished |
| `user` | Data | fetched from doctor.user |

### `Procedure Template`

Clinical procedure master.

| Field | Type | Notes |
|---|---|---|
| `procedure_name` | Data | required, document name |
| `code` | Data | optional code |
| `species` | Data | optional |
| `category` | Data | optional |
| `billing_care_service` | Link `CareService template` | required pricing source |
| `default_duration_minutes` | Int | default `30` |
| `consent_required` | Check | completion requires consent |
| `anesthesia_required` | Check | defaults procedure anesthesia flag |
| `active` | Check | default `1` |
| `steps` | Table `Procedure Template Step` | checklist template |

### `Procedure Template Step`

| Field | Type | Notes |
|---|---|---|
| `step_title` | Data | required |
| `required` | Check | required before completion |
| `default_note` | Small Text | copied to procedure checklist |
| `sort_order` | Int | ordering |

### `Pet Procedure`

Performed procedure record.

| Field | Type | Notes |
|---|---|---|
| `visit` | Link `Vet Visit` | required |
| `order_id` | Data | readonly |
| `pet` | Link `Pet` | required |
| `guardian` | Link `Guardian` | copied from visit |
| `doctor` | Link `Doctor` | copied from visit |
| `provider` | Link `User` | assigned provider |
| `procedure_template` | Link `Procedure Template` | required |
| `care_service` | Link `CareService template` | required billing template |
| `item_code` | Link `Item` | readonly from care service |
| `rate` | Currency | readonly from care service |
| `status` | Select | `Pending`, `In Progress`, `Completed`, `Closed`, `Cancelled` |
| `scheduled_at` | Datetime | optional |
| `started_at` | Datetime | stamped on start |
| `completed_at` | Datetime | stamped on complete |
| `closed_at` | Datetime | stamped on close |
| `indication` | Small Text | reason |
| `consent_obtained` | Check | required if template says consent required |
| `anesthesia_used` | Check | defaults from template if anesthesia required |
| `checklist` | Table `Procedure Checklist Item` | copied from template steps |
| `procedure_note` | Text Editor | documentation |
| `findings` | Text Editor | findings |
| `outcome` | Small Text | outcome |
| `complications` | Small Text | complications |
| `aftercare_instructions` | Text Editor | aftercare |

### `Procedure Checklist Item`

| Field | Type | Notes |
|---|---|---|
| `step_title` | Data | required |
| `required` | Check | completion gate |
| `done` | Check | frontend checkbox |
| `note` | Small Text | note |

### `Pet Billable Item`

Child table on `Vet Visit.billable_items`.

| Field | Type | Notes |
|---|---|---|
| `item_name` | Data | item display |
| `item_code` | Link `Item` | billable ERP item |
| `item_type` | Select | `Room Stay`, `Service`, `Medication`, `Lab`, `Imaging`, `Procedure`, `Product`, `Other` |
| `qty` | Float | required, default `1` |
| `rate` | Currency | price |
| `amount` | Currency | readonly qty x rate |
| `status` | Select | `Draft`, `Billable`, `Billed`, `Cancelled` |
| `note` | Small Text | bill line note |
| `linked_service_id` | Data | record name or synthetic link id |

## Appointment Custom Fields

The workspace uses custom Appointment fields:

| Field | Purpose |
|---|---|
| `custom_appointment_type` | appointment kind; supports `visit`, `service`, `follow_up` |
| `custom_pet` | linked pet |
| `custom_guardian` | linked guardian; spelling is existing backend field |
| `custom_customer` | linked customer |
| `custom_follow_up_of_visit_id` | original visit for follow-up appointment |
| `custom_linked_visit_id` | created/converted visit |
| `custom_linked_service_id` | created/converted service |
| `custom_converted_target` | `Visit` or `Service` |
| `custom_converted_at` | conversion timestamp |

## Permission Module

Module: `pet_app.api.permissions`.

### Frontend Access API

```ts
frappe.call({
  method: "pet_app.api.permissions.get_current_access"
})
```

Returns:

```ts
type CurrentAccess = {
  roles: string[]
  roleProfile?: string
  fullAccess: boolean
  fullAccessRoles: string[]
  modules: string[]
  pages: string[]
  actions: string[]
  doctypes: Record<string, Record<string, boolean>>
  restrictions: {
    warehouse: string[]
    cashier_profile: string[]
    doctor: string[]
    branch: string[]
  }
  loadedAt: string
}
```

Use this to hide/show navigation. Still call backend actions; backend remains final authority.

Vue page/module visibility is scoped by `Pet App Role Profile Workflow Rule` and then filtered by Frappe DocPerm. `Pet App Permission Rule` is deprecated and must not be used as an access source.

### Full Access Roles

```ts
const FULL_ACCESS_ROLES = ["Administrator"]
```

`Administrator` bypasses app-resource row assignments. The frontend should treat `fullAccess: true` as access to every registered module and page.

### Workspace Role Groups

```ts
const DOCTOR_ROLES = ["Doctor", "Physician", "Healthcare Practitioner"]
const COORDINATOR_ROLES = ["Visit", "Healthcare Administrator", "Healthcare", "Nursing User"]
const DIAGNOSTIC_ROLES = ["Laboratory User"]
const ACCOUNTING_ROLES = ["Accounts User", "Accounts Manager", "Sales User", "Sales Manager", "Accounting"]
const MANAGEMENT_ROLES = ["Healthcare Administrator", "Accounts Manager", "System Manager", "Pet App Admin"]
```

### Important Permission Keys For Frontend

Pages:

```ts
[
  "page.healthcare.case_sheets",
  "page.healthcare.visits",
  "page.healthcare.appointments",
  "page.healthcare.coordinator",
  "page.healthcare.labs",
  "page.healthcare.radiology",
  "page.healthcare.services",
  "page.healthcare.boarding",
  "page.healthcare.settings",
  "page.queue.doctor",
  "page.accounting.sales_invoices",
  "page.accounting.payment_entries"
]
```

Deprecated matrix responses returned by `get_access_matrix` include `fullAccessRoles`, plus read-only matrix rows shaped like:

```ts
type AppResourceMatrixRow = {
  key: string
  label: string
  resourceType: "Module" | "Page" | "Action"
  moduleKey?: string
  path?: string
  status: "Draft" | "Active" | "Disabled"
  allowedRoles: string[]
}
```

`register_frontend_resources` is deprecated and returns read-only compatibility output. Do not use it to grant access:

```ts
frappe.call({
  method: "pet_app.api.permissions.register_frontend_resources",
  args: {
    resources: [
      {
        key: "page.healthcare.coordinator",
        resourceType: "Page",
        label: "Coordinator",
        moduleKey: "module.healthcare",
        path: "/healthcare/coordinator"
      }
    ]
  }
})
```

Missing resources are created as `Draft` with no allowed roles. Non-admin users receive a resource only when it is `Active` and one of their roles is assigned.

Actions:

```ts
[
  "action.healthcare.appointments.create",
  "action.healthcare.appointments.convert_to_visit",
  "action.accounting.sales_invoices.create",
  "action.accounting.sales_invoices.update",
  "action.accounting.sales_invoices.submit",
  "action.accounting.sales_invoices.add_payment",
  "action.accounting.payment_entries.create",
  "action.accounting.payment_entries.submit"
]
```

Restrictions:

```ts
type RestrictionType = "warehouse" | "cashier_profile" | "doctor" | "branch"
```

## Frontend Rendering Guidance

- `/profile` should be the daily workspace.
- `/healthcare/coordinator` should be shown only when `pages` contains `page.healthcare.coordinator`.
- The coordinator queue should call `get_my_workspace` with `mode: "coordinator"`.
- Queue cards should use `WorkspaceItem.source_type`, not guessed doctype names.
- Opening any linked source should call `get_record(source_type, name)`.
- For linked lab/radiology/service/procedure rows, do not navigate away for normal work.
- Use `focus_source` to open the relevant tab/section when the aggregate came from a linked record.
- For `Procedure`, use `focus_source.detail` as the primary procedure form source.
- Use `billing.billable_items` for display only unless implementing explicit billing actions.
- `actions` from `get_current_access` is intentionally empty. Use `doctypes[doctype]` permissions for controls, and trust backend errors as final permission truth.

## Suggested Vue Stores

```ts
type WorkspaceState = {
  mode: string
  metrics: WorkspaceResponse["metrics"]
  items: WorkspaceItem[]
  selectedItem?: WorkspaceItem
  selectedRecord?: WorkspaceRecordDetail
  loadingQueue: boolean
  loadingRecord: boolean
  actionPending: boolean
  error?: string
}
```

Core methods:

```ts
loadQueue(filters)
openRecord(item)
performWorkspaceAction(sourceType, name, action, payload)
addNote(sourceType, name, note)
attachFile(sourceType, name, filePayload)
refreshSelectedRecord()
```

## UI Tabs By Source

Doctor visit detail:

- `Summary`
- `SOAP / Assessment`
- `Diagnoses`
- `Orders`
- `Procedures`
- `Attachments`
- `Timeline`
- `Billing`

Service provider detail:

- `Checklist / service note`
- `Start / finish / close`
- `Photos / attachments`
- `Timeline`

Coordinator detail:

- `Intake`
- `Assignment`
- `Conversion`
- `Routing timeline`

Diagnostics detail:

- `Result / report`
- `Image`
- `Release readiness`

Accounting detail:

- `Invoice`
- `Payment`
- `Follow-up`

Procedure detail:

- `Schedule`
- `Checklist`
- `Documentation`
- `Aftercare`
- `Billing snapshot`
- `Timeline`

## Known Backend Test Data From Smoke Run

These exist in the local dev database from backend verification:

| Record | Meaning |
|---|---|
| `Smoke Minor Procedure` | Procedure Template |
| `PROC-00001` | Closed procedure linked to `VVT-2026-00003` |
| `PROC-00002` | Cancelled procedure linked to `VVT-2026-00003` |
| `VVT-2026-00003-ORD-002` | Completed procedure Visit Order |
| `VVT-2026-00003-ORD-003` | Cancelled procedure Visit Order |

Do not hardcode these in frontend. They are useful for local manual checks only.

## Frontend Acceptance Checks

- Doctor can open `/profile`, see assigned visits and procedures.
- Opening a visit shows diagnoses, orders, linked lab/imaging/service/procedure records, billing, notes, attachments, timeline.
- Creating a procedure order creates a linked `Pet Procedure` and shows it in linked records.
- Opening `Procedure` returns visit aggregate with `focus_source.detail`.
- Procedure checklist can be edited and completed.
- Completing required checklist + consent completes procedure.
- Cancelling procedure marks order cancelled and billable row cancelled.
- Coordinator can convert follow-up appointment to visit.
- Diagnostics can save/release lab and radiology.
- Accounting can see invoice/payment follow-up actions.
