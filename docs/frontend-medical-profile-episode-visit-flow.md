# Frontend Guide: Medical Profile, Episodes, Visits, Orders, And Billing

This document explains how the Vue frontend should understand the pet clinical flow. The important idea is simple:

```text
Pet
  has one permanent Pet Medical Profile
  has many Pet Care Episodes over time
    each episode is one open/closed medical case or treatment course
      each episode can have many case sheets, visits, plan items, orders, diagnostics, procedures, and invoices
```

Do not treat `Pet Medical Profile` as the case itself. Treat it as the pet's current medical dashboard.

## Core Purpose

### Pet Medical Profile

`Pet Medical Profile` is one record per pet. It should stay alive for the lifetime of the pet.

Frontend use:

- Show permanent medical facts: allergies, chronic conditions, alerts, microchip, diet notes, behavior notes, vaccination/deworming notes.
- Show current clinical snapshot: current status, active case, active diagnosis summary, active treatment summary, pending orders summary, latest vitals, latest visit.
- Link the user to the active episode when there is an open case.
- Show "No Active Case" when no episode is active.

Do not use it as:

- A visit note.
- A treatment course.
- A submitted/finalized medical document.
- A replacement for episode history.

### Pet Care Episode

`Pet Care Episode` is the case/treatment course.

Frontend use:

- Show the current open case for the pet.
- Show the course status: `Open`, `Under Diagnosis`, `Pending Diagnostics`, `Under Treatment`, `Monitoring`, `Follow-up Scheduled`, `Follow-up Due`, `Referred`, `Resolved`, `Closed`, `Deceased`, `Cancelled`.
- Group visits, plan items, diagnoses, medications, orders, and linked records under one course.
- When a course ends, close the episode through the backend API.
- When the pet has a new problem later, a new episode should be created while the same medical profile remains.

Active episode statuses:

```ts
const ACTIVE_EPISODE_STATUSES = [
  "Open",
  "Under Diagnosis",
  "Pending Diagnostics",
  "Under Treatment",
  "Monitoring",
  "Follow-up Scheduled",
  "Follow-up Due",
  "Referred"
]
```

Terminal/non-active episode statuses:

```ts
const CLOSED_EPISODE_STATUSES = [
  "Resolved",
  "Closed",
  "Deceased",
  "Cancelled"
]
```

### Vet Case Sheet

`Vet Case Sheet` is intake/triage. It captures why the pet came in and the initial symptoms/background.

Frontend use:

- Create it from appointment/walk-in intake.
- Convert it to a `Vet Visit`.
- After conversion, show it as the intake source of the visit.

Important statuses:

```ts
const CASE_SHEET_STATUSES = [
  "Draft",
  "Waiting Practitioner",
  "In Consultation",
  "Converted to Visit",
  "Closed"
]
```

### Vet Visit

`Vet Visit` is the doctor's real working record.

Frontend use:

- This is the main clinical workspace.
- Store examination notes, assessment, diagnosis, treatment plan, prescriptions, orders, billable rows, follow-up, consults, and invoice status here.
- It links back to case sheet and episode.
- It does not automatically become a case. The doctor/backend case choice decides whether it is wellness, a continuation of an active case, or a new case.

Important statuses:

```ts
const VISIT_STATUSES = [
  "Draft",
  "In Progress",
  "Follow-up Needed",
  "Completed",
  "Cancelled"
]
```

## Main Frontend Screens

### Pet Medical Profile Page

Use:

```ts
frappe.call({
  method: "pet_app.api.medical_profile.get_pet_medical_profile",
  args: { pet: petId }
})
```

Render:

- `profile`: permanent pet medical dashboard.
- `active_episode`: current open case, if any.
- `active_plan_items`: active treatment/follow-up items.
- `latest_visits`: recent visits.
- `latest_vitals`: current/latest vitals.

Recommended UI sections:

- Permanent alerts: allergies, chronic conditions, special alerts, contraindications.
- Current case card: show active episode status, diagnosis/treatment summaries, current doctor, current visit.
- Active treatment plan: active plan items.
- Visit history.
- Previous episode history: fetch from timeline and/or list `Pet Care Episode` records for this pet.

### Visit Workbench

Use either the workspace aggregate:

```ts
frappe.call({
  method: "pet_app.api.workspace.get_record",
  args: { source_type: "Visit", name: visitId }
})
```

Or the visit workbench API:

```ts
frappe.call({
  method: "pet_app.api.visit_workbench.get_visit_workbench",
  args: { visit: visitId }
})
```

Render:

- `visit`
- `case_sheet`
- `pet`
- `guardian`
- `medical_profile`
- `active_episode`
- `case_context`
- `active_plan_items`
- `diagnoses`
- `orders`
- `medications`
- `billables`
- `followups`
- `consults`
- `billing`
- `permissions`

## Visit JSON Data Contract

`pet_app.api.visit_workbench.get_visit_workbench` returns the raw visit under `data.visit`. `pet_app.api.workspace.get_record` returns a normalized aggregate with `summary`, `clinical`, `diagnoses`, `orders`, `linked_records`, `billing`, and `raw`.

Use `get_visit_workbench` when Vue needs the raw visit JSON below. Use `get_record` when Vue needs the full workspace layout and linked records. Some fields are read-only from the frontend because the backend computes or locks them.

Workspace aggregate shape:

```ts
type VisitWorkspaceAggregate = {
  summary: {
    name: string
    source_type: "Visit"
    source_doctype: "Vet Visit"
    title: string
    status: VetVisitJson["status"]
    priority: VetVisitJson["priority"]
    case_sheet_id?: string
    visit_datetime?: string
    next_task?: string
    creation?: string
    modified?: string
  }
  pet: Record<string, any>
  guardian: Record<string, any>
  assignee: Record<string, any>
  case_context: VisitCaseContext
  clinical: {
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
    follow_up_status?: string
    vitals?: {
      weight?: number
      temperature?: number
      heart_rate?: number
      respiratory_rate?: number
    }
    vital_signs?: VetVisitVitalSign[]
  }
  follow_up: Record<string, any>
  consult_requests: VisitConsultRequest[]
  diagnoses: VisitDiagnosis[]
  orders: VisitOrder[]
  addenda: Record<string, any>[]
  linked_records: LinkedClinicalRecord[]
  notes: Record<string, any>[]
  attachments: Record<string, any>[]
  billing: BillingSnapshot
  timeline: Record<string, any>[]
  raw: { doctype: "Vet Visit"; name: string }
}

type VisitCaseContext = {
  doctor_case_choice: "wellness" | "continue_case" | "new_case" | null
  case_choice_required: boolean
  visit_care_episode: string | null
  profile_active_episode: string | null
  active_episode: PetCareEpisode | null
  episode_status: string | null
  can_continue_case: boolean
  can_open_new_case: boolean
}

type LinkedClinicalRecord = {
  source_type: "Lab" | "Radiology" | "Service" | "Procedure"
  source_doctype: "Lab" | "Imaging" | "PetCareService" | "Pet Procedure"
  name: string
  status?: string
  order_id?: string
  title?: string
  provider?: string
  procedure_template?: string
  care_service_id?: string
  scheduled_at?: string
  started_at?: string
  completed_at?: string
  closed_at?: string
  modified?: string
}
```

Raw visit shape from `get_visit_workbench.data.visit`:

```ts
type VetVisitJson = {
  doctype: "Vet Visit"
  name: string

  // Status / billing header
  status: "Draft" | "In Progress" | "Completed" | "Cancelled" | "Follow-up Needed"
  priority: "Low" | "Normal" | "Urgent" | "Emergency"
  billed: 0 | 1
  visit_datetime: string
  sales_invoice?: string
  total_billable_amount?: number
  billing_status?: "Unbilled" | "Draft Invoice" | "Partially Paid" | "Paid" | "Follow-up" | "Cancelled"
  paid_amount?: number
  balance_amount?: number

  // Identity links
  case_sheet: string
  appointment?: string
  guardian?: string
  customer: string
  animal_patient: string
  doctor: string
  visit_type: "Consultation" | "Follow-up" | "Vaccination" | "Emergency" | "Procedure" | "Recheck"

  // Optional custom field created by backend patch
  care_episode?: string
  doctor_case_choice?: "wellness" | "continue_case" | "new_case"
  case_choice_by?: string
  case_choice_at?: string
  case_choice_note?: string

  // Vitals
  weight?: number
  temperature?: number
  heart_rate?: number
  respiratory_rate?: number
  vital_signs?: VetVisitVitalSign[]

  // Clinical notes
  examination_notes?: string
  case_summary?: string
  intake_summary?: string
  overview?: string
  illness?: string
  diagnosis?: string
  assessment?: string
  differential_diagnosis?: string
  diagnoses?: VisitDiagnosis[]

  // Plan and orders
  care_services?: VisitCareServiceRow[]
  treatment_plan?: string
  doctor_notes?: string
  instructions?: string
  prescribed_medications?: VetVisitMedicationItem[]
  orders?: VisitOrder[]
  billable_items?: BillableItem[]

  // Follow-up
  follow_up_required?: 0 | 1
  follow_up_status?: "Not Needed" | "Requested" | "Scheduled" | "Contacted" | "Missed" | "Seen" | "Cancelled"
  follow_up_contacted_at?: string
  follow_up_contacted_by?: string
  follow_up_reason?: string
  follow_up_contact_note?: string
  missed_reason?: string
  follow_up_preferred_date?: string
  follow_up_date?: string
  follow_up_appointment_id?: string
  follow_up_visit_id?: string
  follow_up_of_visit_id?: string

  // Consults
  consult_requests?: VisitConsultRequest[]
}
```

Child row shapes used inside `VetVisitJson`:

```ts
type VetVisitVitalSign = {
  recorded_at?: string
  recorded_by?: string
  temperature?: number
  heart_rate?: number
  respiratory_rate?: number
  weight?: number
  body_condition_score?: "" | "1" | "2" | "3" | "4" | "5" | "6" | "7" | "8" | "9"
  hydration_status?: "" | "Normal" | "Mild Dehydration" | "Moderate Dehydration" | "Severe Dehydration"
  mucous_membrane?: string
  capillary_refill_time?: string
  pain_score?: "" | "0" | "1" | "2" | "3" | "4" | "5" | "6" | "7" | "8" | "9" | "10"
  blood_pressure?: string
  spo2?: number
  notes?: string
}

type VisitCareServiceRow = {
  pet_care_service_id?: string
  care_service_id?: string
}

type VetVisitMedicationItem = {
  medication_item: string
  medication?: string
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
  dispensed_by?: string
  dispensed_at?: string
  batch_no?: string
  expiry_date?: string
  stock_entry?: string
  sales_invoice_item?: string
}

type BillableItem = {
  item_name?: string
  item_code?: string
  item_type?: "Room Stay" | "Service" | "Medication" | "Lab" | "Imaging" | "Procedure" | "Product" | "Other"
  qty: number
  rate?: number
  amount?: number
  status?: "Draft" | "Billable" | "Billed" | "Cancelled"
  note?: string
  linked_service_id?: string
  linked_doctype?: string
  linked_name?: string
  order_id?: string
}

type VisitConsultRequest = {
  requested_doctor: string
  requested_by?: string
  reason?: string
  status: "Requested" | "Accepted" | "Completed" | "Cancelled"
  consult_note?: string
  requested_at?: string
  completed_at?: string
}
```

Required visit fields when creating a visit directly:

```ts
type CreateVisitPayload = {
  case_sheet: string
  customer: string
  animal_patient: string
  doctor: string
  status?: "Draft" | "In Progress"
  priority?: "Low" | "Normal" | "Urgent" | "Emergency"
  visit_type?: "Consultation" | "Follow-up" | "Vaccination" | "Emergency" | "Procedure" | "Recheck"
  visit_datetime?: string
  guardian?: string
  appointment?: string
  doctor_case_choice?: "wellness" | "continue_case" | "new_case"
  care_episode?: string
  case_choice_note?: string
}
```

Preferred frontend flow:

- Create or load `Vet Case Sheet`.
- Convert it to a visit with `pet_app.api.workspace.perform_action` action `convert_to_visit`.
- Let the backend fill `guardian`, `customer`, `animal_patient`, and `case_sheet`.
- Send `doctor_case_choice` during conversion when the doctor has already chosen the case path, or call `set_case_choice` immediately after the visit opens.
- Treat `case_context` returned by the backend as final truth for visit episode link, profile active episode, and episode status.
- Use the visit aggregate after every action instead of locally patching linked fields.

Convert case sheet to visit:

```ts
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Case Sheet",
    name: caseSheetId,
    action: "convert_to_visit",
    payload: {
      doctor: practitionerId,
      priority: "Normal",
      doctor_case_choice: "wellness" // optional: wellness | continue_case | new_case
    }
  }
})
```

## Visit Case Choice

The frontend may ask the doctor one question at visit start:

```ts
type DoctorCaseChoice = "wellness" | "continue_case" | "new_case"
```

Meaning:

- `wellness`: routine visit/checkup. Backend keeps `visit.care_episode = null` and does not create a new episode.
- `continue_case`: backend links the visit to the current active episode, or to the provided active episode.
- `new_case`: backend creates a new active episode only when the pet has no other active episode.

Set case choice:

```ts
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Visit",
    name: visitId,
    action: "set_case_choice",
    payload: {
      doctor_case_choice: "new_case",
      care_episode: activeEpisodeId, // optional, only for continue_case
      case_choice_note: "Vomiting course opened after exam"
    }
  }
})
```

Backend-owned result:

- `wellness` never creates a `Pet Care Episode`.
- `continue_case` fails if there is no active episode to continue.
- `new_case` fails if another active episode already exists for the pet.
- The response always includes `case_context`; Vue should reload/render that instead of deciding `care_episode` locally.

## Visit Clinical Fields

The visit is the real doctor record. Vue should edit these through workspace actions, not by guessing side effects.

Main clinical fields:

```ts
type VisitClinicalFields = {
  overview?: string
  examination_notes?: string
  assessment?: string
  differential_diagnosis?: string
  diagnosis?: string
  treatment_plan?: string
  doctor_notes?: string
  instructions?: string
  weight?: number
  temperature?: number
  heart_rate?: number
  respiratory_rate?: number
}
```

Save notes:

```ts
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Visit",
    name: visitId,
    action: "save_clinical_note",
    payload: {
      overview,
      examination_notes,
      assessment,
      differential_diagnosis,
      treatment_plan,
      doctor_notes,
      instructions,
      weight,
      temperature,
      heart_rate,
      respiratory_rate
    }
  }
})
```

## Diagnoses

Current backend model:

- `Disease` is the diagnosis master/catalog.
- `Visit Diagnosis` is a child table inside `Vet Visit`.
- `Vet Visit.differential_diagnosis` is currently free text for suspected/differential diagnoses.
- `Pet Medical Profile.active_diagnosis_summary` is only a summary of the current active diagnosis, not the full history.

Current diagnosis row:

```ts
type VisitDiagnosis = {
  disease?: string
  disease_name?: string
  diagnosis_text?: string
  is_primary?: 0 | 1
  severity?: "" | "Mild" | "Moderate" | "Severe" | "Critical"
  note?: string
}
```

Save diagnoses:

```ts
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Visit",
    name: visitId,
    action: "save_diagnoses",
    payload: {
      diagnoses: [
        {
          disease: "Otitis Externa",
          diagnosis_text: "Left ear infection",
          is_primary: 1,
          severity: "Moderate",
          note: "Pending cytology if not improving"
        }
      ]
    }
  }
})
```

Frontend rule for now:

- Use `differential_diagnosis` for suspected/differential text.
- Use `diagnoses[]` for chosen/confirmed visit diagnoses.
- Mark one row as `is_primary = 1`.
- The primary diagnosis updates the visit. If the visit has an episode, backend also updates episode/profile active diagnosis summaries.

Recommended next backend improvement:

Add `diagnosis_status` to `Visit Diagnosis`:

```ts
type DiagnosisStatus =
  | "Suspected"
  | "Differential"
  | "Confirmed"
  | "Ruled Out"
  | "Historical"
```

Until that exists, Vue should not invent this as a persisted field.

## Treatment Plan / Course Items

There are two treatment-plan levels:

- `Vet Visit.treatment_plan`: doctor narrative plan text for this visit.
- `Pet Care Plan Item`: actionable course item linked to the active episode.

Use `Pet Care Plan Item` when the plan has tasks, dates, reminders, follow-up visits, lab rechecks, imaging rechecks, procedures, medication courses, monitoring, vaccination, deworming, or owner instructions.

Backend rule:

- A visit must be linked to an episode through `new_case` or `continue_case` before Vue can create `Pet Care Plan Item` rows.
- Wellness visits can still have notes, vitals, routine orders, billables, vaccination/deworming records, and invoices, but they do not own episode-level care plan items.

Plan item statuses:

```ts
const PLAN_ITEM_STATUSES = [
  "Planned",
  "Scheduled",
  "In Progress",
  "Done",
  "Missed",
  "Overdue",
  "Cancelled",
  "Changed",
  "Converted To Visit"
]
```

Add plan item from a visit:

```ts
frappe.call({
  method: "pet_app.api.care_plan.add_plan_item_from_visit",
  args: {
    visit: visitId,
    data: {
      plan_type: "Medication",
      title: "Antibiotic course",
      instructions: "Give twice daily after food",
      start_date: "2026-05-11",
      due_date: "2026-05-16",
      duration_days: 5,
      priority: "Normal"
    }
  }
})
```

Complete plan item:

```ts
frappe.call({
  method: "pet_app.api.care_plan.complete_plan_item",
  args: {
    plan_item: planItemId,
    note: "Course completed"
  }
})
```

Other plan APIs:

- `pet_app.api.care_plan.update_plan_item`
- `pet_app.api.care_plan.cancel_plan_item`
- `pet_app.api.care_plan.schedule_plan_item_appointment`
- `pet_app.api.care_plan.convert_plan_item_to_visit`
- `pet_app.api.care_plan.get_pet_active_plan`

Frontend rule:

- Plan item completion updates plan/profile status.
- It must not be treated as full episode closure.
- The doctor closes the episode only when the whole course/case is finished.

## Orders

Orders are created from the visit. An order can create linked work records:

| Order kind | Linked record |
|---|---|
| `lab` | `Lab` |
| `radiology` | `Imaging` |
| `service` | `PetCareService` |
| `procedure` | `Pet Procedure` |
| `medication` | Usually handled through prescribed medications |
| `other` | Visit order row only |

Order row shape:

```ts
type VisitOrder = {
  order_id?: string
  kind: "lab" | "radiology" | "service" | "procedure" | "medication" | "other"
  title: string
  item_code?: string
  template_id?: string
  care_service?: string
  care_service_id?: string
  procedure_template?: string
  status?: "Draft" | "Ordered" | "In Progress" | "Completed" | "Cancelled"
  priority?: "Low" | "Normal" | "Urgent" | "Emergency"
  qty?: number
  price?: number
  note?: string
  provider?: string
  due_date?: string
  scheduled_at?: string
}
```

Create orders:

```ts
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Visit",
    name: visitId,
    action: "create_orders",
    payload: {
      orders: [
        {
          kind: "lab",
          title: "CBC",
          care_service: labCareServiceId,
          priority: "Normal"
        },
        {
          kind: "procedure",
          title: "Dental cleaning",
          procedure_template: procedureTemplateId
        }
      ]
    }
  }
})
```

Order behavior:

- Backend creates/stamps `order_id` if missing.
- Backend creates linked `Lab`, `Imaging`, `PetCareService`, or `Pet Procedure` records where appropriate.
- The order row stores `linked_doctype` and `linked_name`.
- `create_orders` returns the updated visit aggregate, so Vue can read final `orders[].linked_doctype` and `orders[].linked_name` immediately from the response.
- Linked record status updates the order status.
- Profile `pending_orders_summary` updates when there are pending orders.
- Orders are visit-centered and do not create episodes. A wellness visit can order lab/imaging/procedure/service records without making the pet medically active.

## Linked Order Records

The frontend should think of `Visit Order` as the request row, and the linked record as the execution workspace.

```text
Visit Order row
  order_id
  kind
  linked_doctype
  linked_name

Linked record
  Lab / Imaging / PetCareService / Pet Procedure
```

After `create_orders`, reload the visit aggregate and read:

- `orders[]` for request status.
- `linked_records[]` from `pet_app.api.workspace.get_record`.
- `orders[].linked_doctype` and `orders[].linked_name` to open the exact linked workspace.

Example routing:

```ts
function routeOrder(order: VisitOrder) {
  if (order.linked_doctype === "Lab") return { source_type: "Lab", name: order.linked_name }
  if (order.linked_doctype === "Imaging") return { source_type: "Radiology", name: order.linked_name }
  if (order.linked_doctype === "Pet Procedure") return { source_type: "Procedure", name: order.linked_name }
  if (order.linked_doctype === "PetCareService") return { source_type: "Service", name: order.linked_name }
  return { source_type: "Visit", name: visitId }
}
```

### Catalog Lookup JSON

Use `Procedure Template` for procedure choices.

```ts
type ProcedureTemplateJson = {
  name: string
  procedure_name: string
  code?: string
  species?: string
  category?: string
  billing_care_service: string
  default_duration_minutes?: number
  consent_required?: 0 | 1
  consent_template?: string
  anesthesia_required?: 0 | 1
  active?: 0 | 1
  steps?: ProcedureTemplateStep[]
}

type ProcedureTemplateStep = {
  step_title: string
  required?: 0 | 1
  default_note?: string
  sort_order?: number
}
```

Use `CareService template` for billable lab, imaging, service, and procedure billing services.

```ts
type CareServiceTemplateJson = {
  name: string
  service_name: string
  animal_species: "Mammal" | "Bird" | "Reptile" | "Amphibian" | "Fish" | "Insect" | "Arachnid" | "Crustacean"
  frequency: "onetime" | "monthly" | "quarterly" | "biannual" | "annul"
  category_id: string
  item_code?: string
  default_price: number
  price_list?: string
  disabled?: 0 | 1

  // Useful for diagnostic catalogs
  specimen?: string
  modality?: string
  body_part?: string
  service_area?: string
  estimated_turnaround?: string
}
```

Suggested catalog calls:

```ts
frappe.call({
  method: "frappe.client.get_list",
  args: {
    doctype: "Procedure Template",
    filters: { active: 1 },
    fields: [
      "name",
      "procedure_name",
      "code",
      "species",
      "category",
      "billing_care_service",
      "default_duration_minutes",
      "consent_required",
      "anesthesia_required"
    ],
    limit_page_length: 100
  }
})

frappe.call({
  method: "frappe.client.get_list",
  args: {
    doctype: "CareService template",
    filters: { disabled: 0 },
    fields: [
      "name",
      "service_name",
      "animal_species",
      "category_id",
      "item_code",
      "default_price",
      "specimen",
      "modality",
      "body_part",
      "service_area",
      "estimated_turnaround"
    ],
    limit_page_length: 100
  }
})
```

### Procedure Order

Place a procedure order from the visit:

```ts
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Visit",
    name: visitId,
    action: "create_orders",
    payload: {
      orders: [
        {
          kind: "procedure",
          title: "Dental cleaning",
          procedure_template: procedureTemplateId,
          care_service: billingCareServiceId,
          provider: providerUserId,
          scheduled_at: "2026-05-11 10:00:00",
          indication: "Dental tartar and gingivitis",
          priority: "Normal",
          note: "Owner approved estimate"
        }
      ]
    }
  }
})
```

If `care_service` is omitted, backend tries to use `Procedure Template.billing_care_service`.

Linked procedure JSON:

```ts
type PetProcedureJson = {
  doctype: "Pet Procedure"
  name: string
  visit: string
  order_id?: string
  pet: string
  guardian?: string
  doctor?: string
  provider?: string
  procedure_template: string
  care_service: string
  item_code?: string
  rate?: number
  status: "Pending" | "In Progress" | "Completed" | "Closed" | "Cancelled"
  scheduled_at?: string
  started_at?: string
  completed_at?: string
  closed_at?: string
  indication?: string
  consent_obtained?: 0 | 1
  anesthesia_used?: 0 | 1
  checklist?: ProcedureChecklistItem[]
  procedure_note?: string
  findings?: string
  outcome?: string
  complications?: string
  aftercare_instructions?: string
}

type ProcedureChecklistItem = {
  step_title: string
  required?: 0 | 1
  done?: 0 | 1
  note?: string
}
```

Procedure actions:

```ts
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Procedure",
    name: procedureId,
    action: "start_procedure",
    payload: { provider: providerUserId }
  }
})

frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Procedure",
    name: procedureId,
    action: "complete_procedure",
    payload: {
      consent_obtained: 1,
      anesthesia_used: 1,
      checklist: [
        { step_title: "Pre-op check", done: 1 },
        { step_title: "Owner instructions", done: 1, note: "Explained aftercare" }
      ],
      procedure_note: "Procedure completed without complication",
      findings: "Moderate tartar",
      outcome: "Completed",
      aftercare_instructions: "Soft food for 24 hours"
    }
  }
})

frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Procedure",
    name: procedureId,
    action: "close_procedure",
    payload: {
      outcome: "Closed after review",
      aftercare_instructions: "Recheck if appetite drops"
    }
  }
})
```

Procedure billing:

- `Procedure Template.billing_care_service` points to `CareService template`.
- `Pet Procedure.care_service` resolves `item_code` and `rate`.
- Backend syncs one visit billable row linked to `Pet Procedure`.
- Cancelling the procedure cancels the linked billable item if the visit is not billed.

### Lab Order

Place a lab order from the visit:

```ts
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Visit",
    name: visitId,
    action: "create_orders",
    payload: {
      orders: [
        {
          kind: "lab",
          title: "CBC",
          care_service: labCareServiceId,
          priority: "Urgent",
          note: "Fever and lethargy"
        }
      ]
    }
  }
})
```

Linked lab JSON:

```ts
type LabJson = {
  doctype: "Lab"
  name: string
  visit: string
  order_id?: string
  pet: string
  doctor?: string
  care_service: string
  item_code?: string
  rate?: number
  status: "Ordered" | "Sample Collected" | "In Progress" | "Result Entered" | "Released" | "Cancelled"
  sample_collected_at?: string
  sample_collected_by?: string
  result_entered_by?: string
  result_entered_at?: string
  released_by?: string
  released_at?: string
  doctor_reviewed?: 0 | 1
  doctor_reviewed_at?: string
  result_visibility?: "Clinical Team" | "Guardian Visible"
  attachment_required?: 0 | 1
  result?: string
}
```

Lab actions:

```ts
frappe.call({
  method: "pet_app.api.diagnostics.collect_sample",
  args: { lab: labId }
})

frappe.call({
  method: "pet_app.api.diagnostics.save_lab_result",
  args: {
    lab: labId,
    data: {
      result: "WBC elevated",
      doctor_reviewed: 1,
      result_visibility: "Clinical Team"
    }
  }
})

frappe.call({
  method: "pet_app.api.diagnostics.release_lab_result",
  args: {
    lab: labId,
    result_visibility: "Guardian Visible"
  }
})
```

Workspace equivalents are also supported:

```ts
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Lab",
    name: labId,
    action: "save_result",
    payload: { result: "WBC elevated" }
  }
})
```

### Radiology / Imaging Order

Backend doctype is `Imaging`. The UI may label it `Radiology`.

Place an imaging order from the visit:

```ts
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Visit",
    name: visitId,
    action: "create_orders",
    payload: {
      orders: [
        {
          kind: "radiology",
          title: "Chest X-Ray",
          care_service: imagingCareServiceId,
          priority: "Normal",
          note: "Cough and breathing concern"
        }
      ]
    }
  }
})
```

Accepted order aliases:

```ts
const RADIOLOGY_ORDER_KIND_ALIASES = ["radiology", "imaging", "image"]
```

Linked imaging JSON:

```ts
type ImagingJson = {
  doctype: "Imaging"
  name: string
  visit: string
  order_id?: string
  pet: string
  doctor?: string
  care_service: string
  item_code?: string
  rate?: number
  status: "Ordered" | "Scheduled" | "In Progress" | "Reported" | "Released" | "Cancelled"
  sample_collected_at?: string
  sample_collected_by?: string
  result_entered_by?: string
  result_entered_at?: string
  released_by?: string
  released_at?: string
  doctor_reviewed?: 0 | 1
  doctor_reviewed_at?: string
  result_visibility?: "Clinical Team" | "Guardian Visible"
  attachment_required?: 0 | 1
  report?: string
  image?: string
}
```

Imaging actions:

```ts
frappe.call({
  method: "pet_app.api.diagnostics.save_imaging_report",
  args: {
    imaging: imagingId,
    data: {
      report: "No obvious thoracic mass",
      image: "/files/chest-xray.png",
      doctor_reviewed: 1,
      result_visibility: "Clinical Team"
    }
  }
})

frappe.call({
  method: "pet_app.api.diagnostics.release_imaging_report",
  args: {
    imaging: imagingId,
    result_visibility: "Guardian Visible"
  }
})
```

Workspace equivalent:

```ts
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Radiology",
    name: imagingId,
    action: "save_result",
    payload: {
      report: "No obvious thoracic mass",
      image: "/files/chest-xray.png"
    }
  }
})
```

Lab/imaging billing:

- `care_service` must point to a `CareService template`.
- The care service supplies `item_code` and `default_price`.
- Backend syncs a visit billable row linked to `Lab` or `Imaging`.
- Releasing the result marks the linked order as `Completed`.

## Billing

Billing is visit-centered.

Billable rows live on:

```text
Vet Visit.billable_items
```

Rows are added/synced by:

- Prescribed medications.
- Selected care services on the visit.
- Linked lab records.
- Linked imaging records.
- Linked procedure records.
- Manual billable rows if allowed in the UI/backend.

Frontend should render billing from the visit aggregate:

```ts
type BillingSnapshot = {
  billing_status: string
  sales_invoice?: string
  total: number
  paid: number
  balance: number
  currency?: string
  billed: 0 | 1
  billable_items: BillableItem[]
}
```

Create invoice:

```ts
frappe.call({
  method: "pet_app.pet_app.doctype.vet_visit.vet_visit.create_sales_invoice",
  args: { visit_name: visitId }
})
```

Billing rules:

- A visit must have `customer`.
- A visit must have at least one active billable item.
- Billable rows need `item_code`, `qty > 0`, and `rate >= 0`.
- After invoice creation, `visit.billed = 1` and billable rows become `Billed`.
- Billed visits are locked against clinical/billing edits except backend-approved follow-up updates.

## Completing A Visit

Complete case:

```ts
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Visit",
    name: visitId,
    action: "complete_case",
    payload: {
      outcome: "Recovered",
      instructions: "Continue medication for 5 days",
      diagnoses: []
    }
  }
})
```

Before completion, backend requires linked work to be finished/cancelled:

- Lab records must be released/completed/cancelled.
- Imaging records must be released/completed/cancelled.
- Procedure records must be completed/closed/cancelled.

Completion behavior:

- `Vet Visit.status` becomes `Completed`.
- `Vet Case Sheet.status` becomes `Closed`.
- If the visit has a care episode, `Pet Care Episode` may become `Resolved`, `Follow-up Scheduled`, `Monitoring`, `Referred`, or `Deceased` depending on outcome/follow-up.
- If the visit is wellness, no episode is created during completion.
- `Pet Medical Profile` updates current status, summaries, latest visit, and latest vitals.

## Closing An Episode

Use this when the doctor decides the course/case is finished:

```ts
frappe.call({
  method: "pet_app.api.medical_profile.close_care_episode",
  args: {
    episode: episodeId,
    outcome: "Recovered",
    closure_reason: "Treatment course completed"
  }
})
```

Closure behavior:

- Episode becomes `Resolved`, except `Death` or `Euthanasia` makes it `Deceased`.
- Episode gets `closed_on` and `closed_by`.
- Profile clears `active_care_episode`.
- Profile current case status becomes `No Active Case`, or `Deceased` for death.
- The same pet can later open a new episode.

Important frontend rule:

Completing one plan item is not the same as closing the episode. The doctor should close the episode when the whole course is done.

## Previous Episode History

The frontend should show history in two layers:

1. Timeline events.
2. Previous episodes/courses.

Timeline:

```ts
frappe.call({
  method: "pet_app.api.medical_profile.get_pet_medical_timeline",
  args: { pet: petId, limit: 50 }
})
```

This returns events from:

- Visits.
- Case sheets.
- Diagnoses.
- Medications.
- Follow-ups.
- Addendums.
- Invoices.
- Labs.
- Imaging.
- Procedures.

Previous episodes:

- Query `Pet Care Episode` where `pet = petId`.
- Show active episode first.
- Then show previous closed/resolved/cancelled/deceased episodes by date/modified.
- Each episode card should show status, started date, closed/resolved date, primary diagnosis, treatment summary, outcome, and related visits.

Current backend note:

- `get_pet_medical_profile` returns only the active episode, active plan items, latest visits, and latest vitals.
- `get_pet_medical_timeline` returns clinical events but does not group them by episode yet.
- Until a dedicated grouped endpoint exists, Vue can use Frappe list/resource access for `Pet Care Episode` if the user has permission.

Recommended future endpoint:

```ts
frappe.call({
  method: "pet_app.api.medical_profile.get_pet_episode_history",
  args: { pet: petId, limit: 20 }
})
```

Suggested response:

```ts
type EpisodeHistoryResponse = {
  active_episode?: PetCareEpisode
  previous_episodes: Array<{
    episode: PetCareEpisode
    visits: VetVisit[]
    plan_items: PetCarePlanItem[]
    orders: VisitOrder[]
    invoices: SalesInvoice[]
  }>
}
```

Recommended UI model:

```ts
type PetClinicalPage = {
  profile: PetMedicalProfile
  activeEpisode?: PetCareEpisode
  activePlanItems: PetCarePlanItem[]
  latestVisits: VetVisit[]
  timeline: MedicalTimelineEvent[]
  previousEpisodes: PetCareEpisode[]
}
```

## Vue State Rules

Use these display rules:

```ts
const hasActiveCase = Boolean(caseContext.profile_active_episode || activeEpisode?.name)
const caseChoiceRequired = caseContext.case_choice_required
const canContinueCase = caseContext.can_continue_case
const canOpenNewEpisode = caseContext.can_open_new_case && profile.current_case_status !== "Deceased"
const canEditVisit = !billing.billed && !["Completed", "Cancelled"].includes(visit.status)
const canBillVisit = !billing.billed && billing.billable_items.some(row => row.status !== "Cancelled")
const showHistory = !hasActiveCase || previousEpisodes.length > 0
```

Do not let Vue decide backend truth by itself. Vue should render local state optimistically only after the backend action returns the updated aggregate.

## Recommended Flow

```text
1. Guardian/pet arrives
2. Create Vet Case Sheet
3. Convert case sheet to Vet Visit
4. Doctor chooses wellness, continue case, or new case
5. Backend returns case_context as final truth
6. For new/continued cases, Medical Profile points to the active episode/current visit
7. For wellness, Medical Profile keeps no active episode unless another case is already active
8. Doctor saves notes, suspected diagnosis text, diagnosis rows
9. Doctor creates orders, and treatment plan items only when the visit has an episode
10. Lab/imaging/service/procedure work is completed
11. Billable items sync to visit
12. Invoice is created from visit billables
13. Doctor completes visit
14. If course is done, doctor closes episode
15. Profile returns to No Active Case
16. Future illness creates a new episode under the same profile
```

## Things The Frontend Should Not Do

- Do not create multiple `Pet Medical Profile` records for one pet.
- Do not use profile as the treatment course.
- Do not clear `active_care_episode` from Vue directly.
- Do not create or link `Pet Care Episode` from Vue directly; send `doctor_case_choice` and use backend `case_context`.
- Do not mark an episode closed just because one plan item is done.
- Do not create invoices by manually inserting `Sales Invoice`; use the visit billing API.
- Do not persist fake diagnosis fields that are not in the backend yet.
- Do not bypass workspace actions for clinical flow unless the backend exposes a specific API.
