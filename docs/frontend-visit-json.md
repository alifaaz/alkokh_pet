# Frontend Visit JSON

This is the focused handoff for rendering and working with `Vet Visit` on the frontend.

Use `Vet Visit` as the doctor's working clinical record. Most frontend screens should read the workspace aggregate, then use workspace actions for status changes, clinical notes, diagnoses, orders, follow-ups, and consults.

## Read APIs

List visits from the doctor workspace:

```js
frappe.call({
  method: "pet_app.api.workspace.get_my_workspace",
  args: {
    mode: "doctor",
    source_type: "Visit",
    limit: 50
  }
})
```

List item shape:

```json
{
  "id": "Visit:VVT-2026-00001",
  "name": "VVT-2026-00001",
  "source_type": "Visit",
  "source_doctype": "Vet Visit",
  "title": "Luna - Gastrointestinal - VVT-2026-00001",
  "subtitle": "Omar Hassan",
  "status": "In Progress",
  "priority": "Normal",
  "pet": {
    "id": "PET-00001",
    "name": "PET-00001",
    "name_label": "Luna",
    "pet_name": "Luna",
    "species": "Cat",
    "breed": "Domestic Shorthair",
    "gender": "Female",
    "weight": 4.2,
    "image": null
  },
  "guardian": {
    "id": "GUARDIAN-00001",
    "name": "GUARDIAN-00001",
    "name_label": "Omar Hassan",
    "full_name": "Omar Hassan",
    "phone": "+96500000000",
    "email": null,
    "customer_id": "CUST-00001",
    "image": null
  },
  "assignee": {
    "id": "HCP-00001",
    "name": "HCP-00001",
    "name_label": "Dr. Sara Ahmed",
    "practitioner_type": "Doctor",
    "user": "doctor@example.com",
    "user_id": "doctor@example.com",
    "specialization": "General Practice",
    "image": null
  },
  "scheduled_at": "2026-05-15 09:45:00",
  "due_at": null,
  "overdue": false,
  "next_task": "Complete case",
  "modified": "2026-05-15 10:10:00",
  "creation": "2026-05-15 09:45:00",
  "case_sheet_id": "VCS-2026-00001",
  "sales_invoice": null,
  "visit_type": "Consultation",
  "billing_status": "Unbilled",
  "follow_up_status": "Not Needed",
  "follow_up_visit_id": null,
  "consult_requested_for_user": 0,
  "badges": ["Visit", "Normal"]
}
```

Detail view:

```js
frappe.call({
  method: "pet_app.api.workspace.get_record",
  args: { source_type: "Visit", name: visitId }
})
```

Use this aggregate for cards, header panels, SOAP notes, diagnoses, orders, consults, linked records, billing snapshot, notes, attachments, timeline, and routing.

```json
{
  "summary": {
    "name": "VVT-2026-00001",
    "source_type": "Visit",
    "source_doctype": "Vet Visit",
    "title": "Luna - Gastrointestinal - VVT-2026-00001",
    "status": "In Progress",
    "priority": "Normal",
    "case_sheet_id": "VCS-2026-00001",
    "visit_datetime": "2026-05-15 09:45:00",
    "next_task": "Complete case",
    "creation": "2026-05-15 09:45:00",
    "modified": "2026-05-15 10:10:00"
  },
  "pet": {
    "id": "PET-00001",
    "name": "PET-00001",
    "name_label": "Luna",
    "pet_name": "Luna",
    "species": "Cat",
    "breed": "Domestic Shorthair",
    "gender": "Female",
    "weight": 4.2,
    "image": null
  },
  "guardian": {
    "id": "GUARDIAN-00001",
    "name": "GUARDIAN-00001",
    "name_label": "Omar Hassan",
    "full_name": "Omar Hassan",
    "phone": "+96500000000",
    "email": null,
    "customer_id": "CUST-00001",
    "image": null
  },
  "assignee": {
    "id": "HCP-00001",
    "name": "HCP-00001",
    "name_label": "Dr. Sara Ahmed",
    "practitioner_type": "Doctor",
    "user": "doctor@example.com",
    "user_id": "doctor@example.com",
    "specialization": "General Practice",
    "image": null
  },
  "case_context": {
    "doctor_case_choice": "wellness",
    "case_choice_required": false,
    "visit_care_episode": null,
    "profile_active_episode": null,
    "active_episode": null,
    "episode_status": null,
    "can_continue_case": false,
    "can_open_new_case": true
  },
  "clinical": {
    "chief_complaint": "Vomiting",
    "intake_summary": "Vomiting twice since morning.",
    "overview": "Bright, alert, responsive.",
    "examination": "Hydration adequate. Abdomen soft.",
    "assessment": "Acute mild gastrointestinal upset.",
    "plan": "Antiemetic injection and bland diet.",
    "instructions": "Return if vomiting continues or appetite drops.",
    "doctor_notes": "Owner advised on red flags.",
    "case_summary": "Vomiting twice since morning.",
    "illness": "Gastrointestinal",
    "diagnosis": "Acute gastritis",
    "differential_diagnosis": "Dietary indiscretion, parasite burden",
    "follow_up_required": 0,
    "follow_up_reason": null,
    "follow_up_preferred_date": null,
    "follow_up_date": null,
    "follow_up_status": "Not Needed",
    "vitals": {
      "weight": 4.2,
      "temperature": 38.4,
      "heart_rate": 150,
      "respiratory_rate": 28
    },
    "vital_signs": [
      {
        "name": "row-0001",
        "recorded_at": "2026-05-15 09:55:00",
        "recorded_by": "doctor@example.com",
        "temperature": 38.4,
        "heart_rate": 150,
        "respiratory_rate": 28,
        "weight": 4.2,
        "body_condition_score": "5",
        "hydration_status": "Normal",
        "mucous_membrane": "Pink",
        "capillary_refill_time": "<2 sec",
        "pain_score": "1",
        "blood_pressure": null,
        "spo2": null,
        "notes": null
      }
    ]
  },
  "follow_up": {
    "required": 0,
    "reason": null,
    "preferred_date": null,
    "date": null,
    "appointment_id": null,
    "visit_id": null,
    "of_visit_id": null,
    "status": "Not Needed"
  },
  "consult_requests": [],
  "diagnoses": [
    {
      "name": "row-0002",
      "disease": "Acute Gastritis",
      "disease_name": "Acute Gastritis",
      "diagnosis_text": "Acute gastritis",
      "is_primary": 1,
      "severity": "Mild",
      "note": "Owner reports possible food change."
    }
  ],
  "orders": [
    {
      "name": "row-0003",
      "order_id": "VVT-2026-00001-ORD-A1B2C3D4E5",
      "kind": "lab",
      "title": "CBC",
      "item_code": null,
      "template_id": "CARE-SERVICE-CBC",
      "status": "Ordered",
      "priority": "Normal",
      "qty": 1,
      "price": 0,
      "note": "Run if vomiting persists.",
      "linked_doctype": "Lab",
      "linked_name": "LAB-2026-00001"
    }
  ],
  "addenda": [],
  "linked_records": [
    {
      "source_type": "Lab",
      "source_doctype": "Lab",
      "name": "LAB-2026-00001",
      "status": "Ordered",
      "order_id": "VVT-2026-00001-ORD-A1B2C3D4E5",
      "title": "CBC",
      "modified": "2026-05-15 10:05:00"
    }
  ],
  "notes": [],
  "attachments": [],
  "billing": {
    "billing_status": "Unbilled",
    "sales_invoice": null,
    "total": 0,
    "paid": 0,
    "balance": 0,
    "currency": null,
    "billed": 0,
    "billable_items": []
  },
  "timeline": [
    {
      "type": "status",
      "at": "2026-05-15 10:10:00",
      "label": "Visit status: In Progress",
      "source_doctype": "Vet Visit",
      "name": "VVT-2026-00001"
    }
  ],
  "raw": {
    "doctype": "Vet Visit",
    "name": "VVT-2026-00001"
  }
}
```

Use the full DocType record when rendering an editable raw Visit form:

```js
frappe.call({
  method: "frappe.client.get",
  args: { doctype: "Vet Visit", name: visitId }
})
```

Use the visit workbench when Vue needs raw visit JSON plus profile, case sheet, active episode, permission flags, medications, billables, and related records in one call:

```js
frappe.call({
  method: "pet_app.api.visit_workbench.get_visit_workbench",
  args: { visit: visitId }
})
```

Workbench response shape:

```json
{
  "ok": true,
  "data": {
    "visit": { "doctype": "Vet Visit", "name": "VVT-2026-00001" },
    "case_sheet": { "doctype": "Vet Case Sheet", "name": "VCS-2026-00001" },
    "pet": { "doctype": "Pet", "name": "PET-00001" },
    "guardian": { "doctype": "Guardian", "name": "GUARDIAN-00001" },
    "medical_profile": {},
    "active_episode": {},
    "case_context": {},
    "active_plan_items": [],
    "diagnoses": [],
    "orders": [],
    "medications": [],
    "billables": [],
    "followups": {},
    "consults": [],
    "billing": {},
    "permissions": {
      "can_start_consultation": true,
      "can_set_case_choice": true,
      "can_save_clinical_note": true,
      "can_save_diagnoses": true,
      "can_create_orders": true,
      "can_complete_case": true,
      "can_request_follow_up": true,
      "can_add_plan_item": true,
      "can_schedule_plan_item": true,
      "can_convert_plan_item_to_visit": true,
      "is_billed": 0,
      "is_cancelled": 0
    }
  },
  "meta": {},
  "errors": []
}
```

## Pet Profile And Episode Flow

The visit screen is part of the pet medical profile system. Treat these as three different layers:

- `Pet Medical Profile`: one permanent dashboard per pet. It stores the current clinical snapshot, latest vitals, active case pointer, summaries, and recent visit links.
- `Pet Care Episode`: one active case or treatment course. It is created only when the doctor chooses `new_case`; it is reused when the doctor chooses `continue_case`.
- `Vet Visit`: the doctor working record for today. It may be a wellness visit with no episode, a continuation of an active episode, or the first visit of a new episode.

Profile page read:

```js
frappe.call({
  method: "pet_app.api.medical_profile.get_pet_medical_profile",
  args: { pet: petId }
})
```

Profile response shape:

```json
{
  "ok": true,
  "data": {
    "profile": {
      "doctype": "Pet Medical Profile",
      "name": "PMP-00001",
      "pet": "PET-00001",
      "active_care_episode": "PCE-2026-00001",
      "current_visit": "VVT-2026-00001",
      "current_case_sheet": "VCS-2026-00001",
      "current_doctor": "HCP-00001",
      "current_clinical_status": "Under Diagnosis",
      "current_case_status": "Under Diagnosis",
      "active_problem_summary": "Vomiting twice since morning.",
      "active_diagnosis_summary": "Acute gastritis",
      "active_treatment_summary": "Antiemetic, bland diet, monitor.",
      "active_medication_summary": null,
      "pending_orders_summary": "CBC",
      "next_follow_up_date": null,
      "follow_up_status": "Not Needed",
      "last_visit": "VVT-2026-00001",
      "last_completed_visit": null,
      "last_vitals_at": "2026-05-15 09:55:00",
      "last_weight": 4.2,
      "last_temperature": 38.4,
      "last_heart_rate": 150,
      "last_respiratory_rate": 28
    },
    "active_episode": {
      "doctype": "Pet Care Episode",
      "name": "PCE-2026-00001",
      "pet": "PET-00001",
      "episode_title": "Vomiting",
      "episode_status": "Under Diagnosis",
      "primary_doctor": "HCP-00001",
      "current_visit": "VVT-2026-00001",
      "opened_visit": "VVT-2026-00001",
      "last_visit": "VVT-2026-00001",
      "primary_diagnosis": "Acute gastritis",
      "diagnosis_summary": "Acute gastritis",
      "treatment_summary": "Antiemetic, bland diet, monitor.",
      "requires_follow_up": 0,
      "next_follow_up_date": null,
      "follow_up_status": "Not Needed"
    },
    "active_plan_items": [],
    "latest_visits": [
      {
        "name": "VVT-2026-00001",
        "visit_datetime": "2026-05-15 09:45:00",
        "status": "In Progress",
        "doctor": "HCP-00001",
        "diagnosis": "Acute gastritis",
        "follow_up_date": null,
        "follow_up_status": "Not Needed"
      }
    ],
    "latest_vitals": {
      "weight": 4.2,
      "temperature": 38.4,
      "heart_rate": 150,
      "respiratory_rate": 28,
      "recorded_at": "2026-05-15 09:55:00"
    }
  },
  "meta": {},
  "errors": []
}
```

Recommended frontend integration:

1. On the pet profile page, call `get_pet_medical_profile(pet)` and render the profile dashboard, active episode card, active plan items, latest visits, and latest vitals.
2. From a case sheet or appointment, convert to `Vet Visit`. If the doctor already picked the case path, send `doctor_case_choice` in the conversion payload.
3. On Visit open, read `case_context`. If `case_context.case_choice_required` is true, show the doctor the case choice prompt before the clinical workflow continues.
4. For `wellness`, render the visit as routine care and do not show episode-level treatment-course controls.
5. For `continue_case`, show the active episode context from `case_context.active_episode` or the profile response.
6. For `new_case`, let the backend create the episode, then render the returned `case_context.visit_care_episode`.
7. After any visit action, replace the local visit state with the returned aggregate. If a pet profile panel is visible, refresh `get_pet_medical_profile(pet)` so current status, summaries, latest vitals, and active episode stay aligned.

Backend sync behavior the frontend should rely on:

- Case sheet intake updates the profile as `Waiting Intake` and sets `current_case_sheet`, priority, and problem summary.
- Visit conversion/start updates `current_visit`, `current_doctor`, `last_visit`, and the current clinical status.
- `set_case_choice` is the only normal frontend path that creates or links an episode.
- `save_diagnoses` updates visit diagnoses and, when the visit has an episode, the active diagnosis summary.
- `create_orders` updates pending order summary and may move the episode/profile to pending diagnostics.
- Treatment plan or prescribed medication changes update active treatment and medication summaries when the visit has an episode.
- Follow-up request updates `next_follow_up_date`, follow-up status, and the linked follow-up appointment.
- Completing the visit updates `last_completed_visit`; if the visit has an episode, the episode may become `Resolved`, `Monitoring`, `Follow-up Scheduled`, `Referred`, or `Deceased` based on outcome and follow-up.

Do not create, link, close, or clear `Pet Care Episode` directly from the Visit UI. Use `doctor_case_choice`, `set_case_choice`, `request_follow_up`, `complete_case`, or the dedicated medical profile APIs.

## Create APIs

Preferred flow: create a `Vet Case Sheet`, then convert it to a visit. This keeps intake, queue ticket, guardian, customer, pet, and visit links consistent.

```js
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Case Sheet",
    name: caseSheetId,
    action: "convert_to_visit",
    payload: {
      doctor: practitionerId,
      priority: "Normal",
      visit_type: "Consultation",
      doctor_case_choice: "wellness"
    }
  }
})
```

`doctor_case_choice` can be `wellness`, `continue_case`, or `new_case`.

Direct DocType insert is only for admin/back-office tools that already have a valid `case_sheet`. Backend pulls guardian/customer/pet identity from the case sheet and locks identity after creation.

```js
frappe.call({
  method: "frappe.client.insert",
  args: { doc: visitDoc }
})
```

```json
{
  "doctype": "Vet Visit",
  "status": "Draft",
  "priority": "Normal",
  "visit_datetime": "2026-05-15 09:45:00",
  "case_sheet": "VCS-2026-00001",
  "appointment": null,
  "doctor": "HCP-00001",
  "visit_type": "Consultation",
  "weight": 4.2,
  "temperature": 38.4,
  "heart_rate": 150,
  "respiratory_rate": 28,
  "intake_summary": "Vomiting twice since morning.",
  "overview": "Bright, alert, responsive.",
  "illness": "Gastrointestinal",
  "diagnosis": "Acute gastritis",
  "assessment": "Likely dietary indiscretion.",
  "differential_diagnosis": "Parasites, foreign body if signs worsen.",
  "treatment_plan": "Antiemetic, bland diet, monitor.",
  "doctor_notes": "Owner advised to return if vomiting continues.",
  "instructions": "Small meals for 24 hours."
}
```

## Render Schema

Use this JSON to render the fixed visit form. Use `get_record` for normalized cards and `frappe.client.get` or `get_visit_workbench` for full raw editing.

```json
{
  "doctype": "Vet Visit",
  "title": "Visit",
  "statuses": ["Draft", "In Progress", "Completed", "Cancelled", "Follow-up Needed"],
  "priorities": ["Low", "Normal", "Urgent", "Emergency"],
  "visit_types": ["Consultation", "Follow-up", "Vaccination", "Emergency", "Procedure", "Recheck"],
  "billing_statuses": ["Unbilled", "Draft Invoice", "Partially Paid", "Paid", "Follow-up", "Cancelled"],
  "illness_options": ["Gastrointestinal", "Dermatological", "Respiratory", "Musculoskeletal", "Dental", "Ophthalmic", "Otic", "Parasitic", "Infectious", "Nutritional", "Endocrine", "Urinary", "Reproductive", "Neurological", "Behavioral", "Preventive Care", "Post-Surgical", "Other"],
  "required": ["status", "priority", "visit_datetime", "case_sheet", "customer", "animal_patient", "doctor", "visit_type"],
  "read_only": ["billed", "sales_invoice", "total_billable_amount", "billing_status", "paid_amount", "balance_amount", "case_summary", "follow_up_contacted_at", "follow_up_contacted_by", "follow_up_appointment_id", "follow_up_visit_id", "follow_up_of_visit_id"],
  "identity_locked_after_create": ["case_sheet", "appointment", "guardian", "customer", "animal_patient"],
  "completion_required": ["illness", "diagnosis", "treatment_plan", "doctor_notes"],
  "conditional_required": [
    {"field": "follow_up_preferred_date", "when": {"follow_up_required": 1}},
    {"field": "follow_up_date", "when": {"follow_up_required": 1}}
  ],
  "sections": [
    {
      "key": "header",
      "label": "Header",
      "fields": [
        {"name": "status", "label": "Visit Status", "type": "Select", "options": ["Draft", "In Progress", "Completed", "Cancelled", "Follow-up Needed"], "default": "Draft"},
        {"name": "priority", "label": "Priority", "type": "Select", "options": ["Low", "Normal", "Urgent", "Emergency"], "default": "Normal"},
        {"name": "visit_datetime", "label": "Visit Date / Time", "type": "Datetime", "default": "Now"},
        {"name": "billed", "label": "Billed", "type": "Check", "read_only": true},
        {"name": "sales_invoice", "label": "Sales Invoice", "type": "Link", "options": "Sales Invoice", "read_only": true},
        {"name": "billing_status", "label": "Billing Status", "type": "Select", "options": ["Unbilled", "Draft Invoice", "Partially Paid", "Paid", "Follow-up", "Cancelled"], "read_only": true},
        {"name": "total_billable_amount", "label": "Total Billable Amount", "type": "Currency", "read_only": true},
        {"name": "paid_amount", "label": "Paid Amount", "type": "Currency", "read_only": true},
        {"name": "balance_amount", "label": "Balance Amount", "type": "Currency", "read_only": true}
      ]
    },
    {
      "key": "basic_info",
      "label": "Basic Info",
      "fields": [
        {"name": "case_sheet", "label": "Case Sheet", "type": "Link", "options": "Vet Case Sheet", "read_only_after_create": true},
        {"name": "appointment", "label": "Appointment", "type": "Link", "options": "Appointment", "read_only_after_create": true},
        {"name": "guardian", "label": "Guardian", "type": "Link", "options": "Guardian", "read_only_after_create": true},
        {"name": "customer", "label": "Customer", "type": "Link", "options": "Customer", "read_only_after_create": true},
        {"name": "animal_patient", "label": "Pet", "type": "Link", "options": "Pet", "read_only_after_create": true},
        {"name": "doctor", "label": "Healthcare Practitioner", "type": "Link", "options": "Healthcare Practitioner"},
        {"name": "visit_type", "label": "Visit Type", "type": "Select", "options": ["Consultation", "Follow-up", "Vaccination", "Emergency", "Procedure", "Recheck"], "default": "Consultation"},
        {"name": "weight", "label": "Weight", "type": "Float"}
      ]
    },
    {
      "key": "vitals",
      "label": "Vitals",
      "fields": [
        {"name": "temperature", "label": "Temperature", "type": "Float"},
        {"name": "heart_rate", "label": "Heart Rate", "type": "Int"},
        {"name": "respiratory_rate", "label": "Respiratory Rate", "type": "Int"},
        {"name": "vital_signs", "label": "Repeated Vitals", "type": "Table", "options": "Vet Visit Vital Sign", "columns": [
          {"name": "recorded_at", "label": "Recorded At", "type": "Datetime"},
          {"name": "recorded_by", "label": "Recorded By", "type": "Link", "options": "User"},
          {"name": "temperature", "label": "Temperature", "type": "Float"},
          {"name": "heart_rate", "label": "Heart Rate", "type": "Int"},
          {"name": "respiratory_rate", "label": "Respiratory Rate", "type": "Int"},
          {"name": "weight", "label": "Weight", "type": "Float"},
          {"name": "body_condition_score", "label": "Body Condition Score", "type": "Select", "options": ["1", "2", "3", "4", "5", "6", "7", "8", "9"]},
          {"name": "hydration_status", "label": "Hydration Status", "type": "Select", "options": ["Normal", "Mild Dehydration", "Moderate Dehydration", "Severe Dehydration"]},
          {"name": "mucous_membrane", "label": "Mucous Membrane", "type": "Data"},
          {"name": "capillary_refill_time", "label": "Capillary Refill Time", "type": "Data"},
          {"name": "pain_score", "label": "Pain Score", "type": "Select", "options": ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10"]},
          {"name": "blood_pressure", "label": "Blood Pressure", "type": "Data"},
          {"name": "spo2", "label": "SpO2", "type": "Percent"},
          {"name": "notes", "label": "Notes", "type": "Small Text"}
        ]}
      ]
    },
    {
      "key": "clinical",
      "label": "Clinical",
      "fields": [
        {"name": "case_summary", "label": "Case Summary", "type": "Small Text", "read_only": true},
        {"name": "intake_summary", "label": "Intake Summary", "type": "Text Editor"},
        {"name": "overview", "label": "Overview", "type": "Text Editor"},
        {"name": "examination_notes", "label": "Clinical Examination", "type": "Text Editor"},
        {"name": "illness", "label": "Illness / Condition", "type": "Select", "options": ["Gastrointestinal", "Dermatological", "Respiratory", "Musculoskeletal", "Dental", "Ophthalmic", "Otic", "Parasitic", "Infectious", "Nutritional", "Endocrine", "Urinary", "Reproductive", "Neurological", "Behavioral", "Preventive Care", "Post-Surgical", "Other"]},
        {"name": "assessment", "label": "Assessment", "type": "Text Editor"},
        {"name": "diagnosis", "label": "Diagnosis", "type": "Text Editor"},
        {"name": "differential_diagnosis", "label": "Differential Diagnosis", "type": "Small Text"}
      ]
    },
    {
      "key": "diagnoses",
      "label": "Diagnoses",
      "fields": [
        {"name": "diagnoses", "label": "Diagnoses", "type": "Table", "options": "Visit Diagnosis", "columns": [
          {"name": "disease", "label": "Disease", "type": "Link", "options": "Disease"},
          {"name": "diagnosis_text", "label": "Diagnosis Text", "type": "Small Text"},
          {"name": "is_primary", "label": "Primary", "type": "Check"},
          {"name": "severity", "label": "Severity", "type": "Select", "options": ["Mild", "Moderate", "Severe", "Critical"]},
          {"name": "note", "label": "Note", "type": "Small Text"}
        ]}
      ]
    },
    {
      "key": "plan",
      "label": "Plan",
      "fields": [
        {"name": "treatment_plan", "label": "Treatment Plan", "type": "Text Editor"},
        {"name": "doctor_notes", "label": "Practitioner Notes", "type": "Text Editor"},
        {"name": "instructions", "label": "Instructions", "type": "Text Editor"}
      ]
    },
    {
      "key": "orders_actions",
      "label": "Orders / Actions",
      "fields": [
        {"name": "prescribed_medications", "label": "Prescribed Medications", "type": "Table", "options": "Vet Visit Medication Item", "columns": [
          {"name": "medication", "label": "Medication", "type": "Link", "options": "Medication"},
          {"name": "medication_item", "label": "Medication Item", "type": "Data", "read_only": true},
          {"name": "qty", "label": "Qty", "type": "Float", "default": 1},
          {"name": "rate", "label": "Rate", "type": "Currency"},
          {"name": "amount", "label": "Amount", "type": "Currency", "read_only": true},
          {"name": "dosage", "label": "Dosage", "type": "Data"},
          {"name": "frequency", "label": "Frequency", "type": "Data"},
          {"name": "duration_days", "label": "Duration (Days)", "type": "Int"},
          {"name": "instructions", "label": "Instructions", "type": "Small Text"},
          {"name": "warehouse", "label": "Warehouse", "type": "Link", "options": "Warehouse"},
          {"name": "dispense_status", "label": "Dispense Status", "type": "Select", "options": ["Prescribed", "Pending Dispense", "Dispensed", "Partially Dispensed", "Cancelled", "Returned"], "default": "Prescribed"}
        ]},
        {"name": "orders", "label": "Orders", "type": "Table", "options": "Visit Order", "columns": [
          {"name": "order_id", "label": "Order ID", "type": "Data", "read_only": true},
          {"name": "kind", "label": "Kind", "type": "Select", "options": ["lab", "radiology", "service", "procedure", "medication", "other"]},
          {"name": "title", "label": "Title", "type": "Data"},
          {"name": "item_code", "label": "Item Code", "type": "Link", "options": "Item"},
          {"name": "template_id", "label": "Template", "type": "Link", "options": "CareService template"},
          {"name": "status", "label": "Status", "type": "Select", "options": ["Draft", "Ordered", "In Progress", "Completed", "Cancelled"], "default": "Draft"},
          {"name": "priority", "label": "Priority", "type": "Select", "options": ["Low", "Normal", "Urgent", "Emergency"], "default": "Normal"},
          {"name": "qty", "label": "Qty", "type": "Float", "default": 1},
          {"name": "price", "label": "Price", "type": "Currency"},
          {"name": "note", "label": "Note", "type": "Small Text"},
          {"name": "linked_doctype", "label": "Linked DocType", "type": "Link", "options": "DocType", "read_only": true},
          {"name": "linked_name", "label": "Linked Name", "type": "Dynamic Link", "options": "linked_doctype", "read_only": true}
        ]},
        {"name": "billable_items", "label": "Billable Items", "type": "Table", "options": "Pet Billable Item", "read_only_after_billed": true}
      ]
    },
    {
      "key": "follow_up",
      "label": "Follow-up",
      "fields": [
        {"name": "follow_up_required", "label": "Follow-up Required", "type": "Check"},
        {"name": "follow_up_status", "label": "Follow-up Status", "type": "Select", "options": ["Not Needed", "Requested", "Scheduled", "Contacted", "Missed", "Seen", "Cancelled"], "default": "Not Needed"},
        {"name": "follow_up_reason", "label": "Follow-up Reason", "type": "Small Text", "show_when": {"follow_up_required": 1}},
        {"name": "follow_up_preferred_date", "label": "Follow-up Preferred Date", "type": "Date", "show_when": {"follow_up_required": 1}},
        {"name": "follow_up_date", "label": "Follow-up Date", "type": "Date", "show_when": {"follow_up_required": 1}},
        {"name": "follow_up_contact_note", "label": "Contact Note", "type": "Small Text"},
        {"name": "missed_reason", "label": "Missed Reason", "type": "Small Text"},
        {"name": "follow_up_contacted_at", "label": "Contacted At", "type": "Datetime", "read_only": true},
        {"name": "follow_up_contacted_by", "label": "Contacted By", "type": "Link", "options": "User", "read_only": true},
        {"name": "follow_up_appointment_id", "label": "Follow-up Appointment", "type": "Link", "options": "Appointment", "read_only": true},
        {"name": "follow_up_visit_id", "label": "Follow-up Visit", "type": "Link", "options": "Vet Visit", "read_only": true},
        {"name": "follow_up_of_visit_id", "label": "Follow-up Of Visit", "type": "Link", "options": "Vet Visit", "read_only": true}
      ]
    },
    {
      "key": "consults",
      "label": "Consults",
      "fields": [
        {"name": "consult_requests", "label": "Consult Requests", "type": "Table", "options": "Visit Consult Request", "columns": [
          {"name": "requested_doctor", "label": "Requested Healthcare Practitioner", "type": "Link", "options": "Healthcare Practitioner"},
          {"name": "requested_by", "label": "Requested By", "type": "Link", "options": "User", "read_only": true},
          {"name": "reason", "label": "Reason", "type": "Small Text"},
          {"name": "status", "label": "Status", "type": "Select", "options": ["Requested", "Accepted", "Completed", "Cancelled"], "default": "Requested"},
          {"name": "consult_note", "label": "Consult Note", "type": "Text Editor"},
          {"name": "requested_at", "label": "Requested At", "type": "Datetime", "read_only": true},
          {"name": "completed_at", "label": "Completed At", "type": "Datetime", "read_only": true}
        ]}
      ]
    }
  ]
}
```

## Visit Actions

All workspace actions return the updated visit aggregate. Prefer them over locally mutating related records.

### Start Consultation

```js
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Visit",
    name: visitId,
    action: "start_consultation",
    payload: {}
  }
})
```

### Set Case Choice

```js
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Visit",
    name: visitId,
    action: "set_case_choice",
    payload: {
      doctor_case_choice: "continue_case",
      care_episode: activeEpisodeId,
      case_choice_note: "Continuing active vomiting case."
    }
  }
})
```

Case choice rules:

- `wellness`: routine/checkup visit. Backend keeps `care_episode` empty.
- `continue_case`: backend links to the active care episode or the provided `care_episode`.
- `new_case`: backend creates a new active care episode only if the pet has no other active episode.
- Use `case_context` from the response as the source of truth.

### Save Clinical Note

```js
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Visit",
    name: visitId,
    action: "save_clinical_note",
    payload: {
      intake_summary: "Vomiting twice since morning.",
      overview: "Bright, alert, responsive.",
      examination_notes: "Hydration adequate. Abdomen soft.",
      assessment: "Mild acute GI upset.",
      diagnosis: "Acute gastritis",
      differential_diagnosis: "Dietary indiscretion, parasites",
      treatment_plan: "Antiemetic, bland diet, monitor.",
      doctor_notes: "Owner advised on warning signs.",
      instructions: "Return if vomiting continues.",
      illness: "Gastrointestinal",
      weight: 4.2,
      temperature: 38.4,
      heart_rate: 150,
      respiratory_rate: 28
    }
  }
})
```

The backend also accepts aliases: `examination` for `examination_notes`, `plan` for `treatment_plan`, and `instructions` for discharge instructions.

### Save Diagnoses

```js
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Visit",
    name: visitId,
    action: "save_diagnoses",
    payload: {
      diagnoses: [
        {
          disease: "Acute Gastritis",
          diagnosis_text: "Acute gastritis",
          is_primary: 1,
          severity: "Mild",
          note: "Owner reports possible food change."
        }
      ]
    }
  }
})
```

### Create Orders

Use `create_orders` for lab, radiology, service, and procedure orders. Backend creates/links the downstream record when possible.

```js
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
          care_service: "CARE-SERVICE-CBC",
          priority: "Normal",
          qty: 1,
          note: "Run if vomiting persists."
        },
        {
          kind: "radiology",
          title: "Abdominal X-ray",
          care_service: "CARE-SERVICE-XRAY",
          priority: "Urgent",
          note: "Rule out obstruction."
        },
        {
          kind: "procedure",
          title: "Dental cleaning",
          procedure_template: "PROC-TEMPLATE-DENTAL",
          care_service: "CARE-SERVICE-DENTAL",
          provider: "provider@example.com",
          scheduled_at: "2026-05-15 12:00:00",
          indication: "Dental tartar",
          note: "Owner approved estimate."
        }
      ]
    }
  }
})
```

Order row shape:

```json
{
  "order_id": "VVT-2026-00001-ORD-A1B2C3D4E5",
  "kind": "lab",
  "title": "CBC",
  "item_code": null,
  "template_id": "CARE-SERVICE-CBC",
  "status": "Ordered",
  "priority": "Normal",
  "qty": 1,
  "price": 0,
  "note": "Run if vomiting persists.",
  "linked_doctype": "Lab",
  "linked_name": "LAB-2026-00001"
}
```

### Request Follow-up

```js
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Visit",
    name: visitId,
    action: "request_follow_up",
    payload: {
      preferred_date: "2026-05-22",
      scheduled_time: "2026-05-22 09:00:00",
      reason: "Recheck vomiting and appetite."
    }
  }
})
```

This creates or reuses a follow-up `Appointment` and updates `follow_up_appointment_id`.

### Convert Follow-up To Visit

From the original visit:

```js
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Visit",
    name: originalVisitId,
    action: "convert_follow_up_to_visit",
    payload: {
      appointment: followUpAppointmentId,
      doctor: practitionerId,
      priority: "Normal"
    }
  }
})
```

From the follow-up appointment:

```js
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Appointment",
    name: followUpAppointmentId,
    action: "convert_follow_up_to_visit",
    payload: {
      doctor: practitionerId,
      priority: "Normal"
    }
  }
})
```

### Request Consult

```js
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Visit",
    name: visitId,
    action: "request_consult",
    payload: {
      requested_practitioner: "HCP-00002",
      reason: "Second opinion on abdominal pain."
    }
  }
})
```

### Complete Consult

```js
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Visit",
    name: visitId,
    action: "complete_consult",
    payload: {
      requested_practitioner: "HCP-00002",
      consult_note: "No surgical signs at this time."
    }
  }
})
```

### Complete Case

```js
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Visit",
    name: visitId,
    action: "complete_case",
    payload: {
      illness: "Gastrointestinal",
      diagnosis: "Acute gastritis",
      treatment_plan: "Antiemetic, bland diet, monitor.",
      doctor_notes: "Owner advised on warning signs.",
      outcome: "Stable for discharge."
    }
  }
})
```

Backend completion rules require `illness`, `diagnosis`, `treatment_plan`, and `doctor_notes`.

## Notes, Attachments, And Addenda

Add a normal workspace note:

```js
frappe.call({
  method: "pet_app.api.workspace.add_note",
  args: {
    source_type: "Visit",
    name: visitId,
    note: "Owner called to confirm medication instructions."
  }
})
```

Attach a file:

```js
frappe.call({
  method: "pet_app.api.workspace.attach_file",
  args: {
    source_type: "Visit",
    name: visitId,
    payload: {
      file_url: "/files/luna-discharge.pdf",
      file_name: "luna-discharge.pdf",
      is_private: 1
    }
  }
})
```

After billing, the visit is locked. Use addenda for corrections or follow-up notes:

```js
frappe.call({
  method: "pet_app.api.visit_addendum.add_visit_addendum",
  args: {
    visit: visitId,
    addendum_type: "Clinical Correction",
    note: "Corrected discharge instruction wording.",
    reason: "Clarification after billing."
  }
})
```

List addenda:

```js
frappe.call({
  method: "pet_app.api.visit_addendum.list_visit_addendums",
  args: { visit: visitId, owner_safe: 0, limit: 50 }
})
```

## Frontend Rules

- Open a visit with `get_record("Visit", visitId)` for the normalized workspace layout.
- Use `get_visit_workbench` when the screen needs raw visit JSON, permission flags, active episode, medications, billables, or plan items.
- Use `frappe.client.get` when rendering the literal DocType form.
- Use the raw Visit form/workbench save path for repeated vitals and prescribed medication child rows. `save_clinical_note` only updates scalar vitals.
- Convert from `Vet Case Sheet` instead of direct inserting visits in normal intake flows.
- Do not edit `case_sheet`, `appointment`, `guardian`, `customer`, or `animal_patient` after the visit is created.
- Show `Start Consultation` when status is `Draft`.
- Show the case choice prompt when `case_context.case_choice_required` is true.
- Show `Complete Case` when status is `In Progress` or `Follow-up Needed`, but require `illness`, `diagnosis`, `treatment_plan`, and `doctor_notes`.
- Show `Request Follow-up` when the visit is not billed/cancelled and follow-up permission is true.
- Show `Convert Follow-up To Visit` when `follow_up.appointment_id` exists and `follow_up.visit_id` is empty.
- Show `Open Follow-up Visit` when `follow_up.visit_id` is set.
- Show `Request Consult` while the visit is active and not billed/cancelled.
- Show `Complete Consult` for visits where `consult_requested_for_user` is true or an open consult row belongs to the current practitioner.
- Treat `billing.billed = 1` or `billing.sales_invoice` as locked for visit edits. Use addenda instead of changing the visit.
- Use `linked_records[]` to route lab/radiology/service/procedure cards. Opening any linked record with `get_record(source_type, name)` returns that source or the parent visit aggregate with `focus_source`.
- Use `orders[]` for the doctor's order list and `linked_records[]` for operational execution cards.
- Use `billing.billable_items[]` as the billing snapshot. Do not compute invoice totals on the frontend.
- Use workspace action responses as the next UI state instead of patching local state by hand.
