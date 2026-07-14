# Frontend Handoff: Procedure, Lab, Imaging, and Pet Care Services

## Purpose

This handoff is only for the four frontend builders:

- Lab order
- Imaging / Radiology order
- Pet care service order
- Procedure order

The important catalog link is:

```txt
CategoryCareServices
-> CareService template
-> Lab / Imaging / PetCareService / Procedure Template billing
```

Medication is not part of this handoff.

## Backend Update Log: 2026-05-27

Today changed the Care Service and Service Provider backend contract in these areas.

### Care Service Billing And Data Integrity

- `Care Service Billing Option` data issues were fixed with patches:
  - Cat grooming options with `category_care_services = "CategoryCareServices-0008"`, `option_label = "Cats"`, and `min_weight = 0`, `max_weight = 0` are corrected to `min_weight = 0`, `max_weight = 100`.
  - Test billing option data such as `option_label = "1"` is disabled instead of being offered in the UI.
- `PetCareService.category` is required and validation now prevents saving care service records without a category.
- Billing option selection remains explicit. The backend does not do species, weight, or size matching for options in the create wizard; the frontend should still choose the correct `Care Service Billing Option`.

### Pet Weight Sync

- Starting a `PetCareService` with `weight > 0` saves the weight on the service record and syncs it back to the linked `Pet.weight`.
- The workspace `start_service` action uses direct `frappe.db.set_value("Pet", pet_id, "weight", weight, update_modified=False)` and commits the update.
- `PetCareService` also has a save hook so a positive service weight can update the linked Pet weight without overwriting the Pet with `0`.

### Provider Category Assignment

Healthcare Practitioner now has a child table for service provider category coverage:

```txt
Healthcare Practitioner.service_categories
-> Healthcare Practitioner Service Category.category
-> CategoryCareServices.name
```

New child DocType:

```txt
Healthcare Practitioner Service Category
- category: Link to CategoryCareServices
```

The table field is only shown for `practitioner_type == "Service Provider"`.

### Provider Filtering API

`pet_app.api.care_service.get_providers_for_category(category)` returns providers for a selected service category.

- Medical categories such as lab, radiology, medication, sonar, checkup, and general return active Doctors and Nurses.
- Non-medical categories return active `Healthcare Practitioner` records with `practitioner_type = "Service Provider"`.
- If a Service Provider has no `service_categories` rows, they are treated as handling all non-medical categories.
- If rows exist, only providers with a matching `Healthcare Practitioner Service Category.category` are returned.

### Bulk Create API

`pet_app.api.care_service.bulk_create_pet_care_services(entries=None, services=None)` accepts either `entries` or the older `services` argument.

Expected payload is a JSON string or list of service rows. Each row may include:

```json
{
  "pet_id": "PET-00001",
  "care_service_id": "CareService-00001",
  "provider": "HCP-00001",
  "service_option": "CSBO-00001",
  "category": "CategoryCareServices-0008",
  "guardian_id": "GUARDIAN-00107",
  "due_date": "2026-05-27"
}
```

The backend fills `pet_service_name`, `category`, `item_code`, and `price` from the linked `CareService template` and selected `Care Service Billing Option` where possible.

### Service Workspace Response

`pet_app.api.workspace.get_my_workspace(mode="service")` returns `PetCareService` rows through `_service_item()`.

Each service item now includes guardian identity at the top level:

```json
{
  "guardian_id": "GUARDIAN-00107",
  "guardian_id.full_name": "Mostafa",
  "guardian": {
    "id": "GUARDIAN-00107",
    "name": "GUARDIAN-00107",
    "full_name": "Mostafa",
    "name_label": "Mostafa"
  }
}
```

The dotted key is intentionally top-level because the frontend mapper reads `raw["guardian_id.full_name"]` directly.

### Service Provider Visibility

PetCareService now has a DocType-level `permission_query_conditions` hook:

```txt
PetCareService
-> pet_app.permissions.petcareservice.get_permission_query_conditions
```

Strict provider visibility:

```sql
`tabPetCareService`.`provider` = <current user Healthcare Practitioner name>
```

Roles filtered this way:

- `Service Provider`
- `Groomer`
- `Nursing User`
- `Service Provider Manager`

Full-access roles that still see all PetCareService rows:

- `System Manager`
- `Administrator`
- `Pet App Admin`
- `Healthcare Administrator`
- `Coordinator`
- `Coordinatorr`
- `Doctor`
- `Lab Admin`
- `Radiology Admin`

Important backend detail: `_service_items()` and `_unassigned_service_items()` use permission-aware `frappe.get_list()` for `PetCareService`. They must not use `frappe.get_all(..., ignore_permissions=True)` for service provider queues, because that bypasses `permission_query_conditions` and can expose unassigned services.

### Operational Notes

- `bench --site frappe.localhost migrate` completed successfully after the schema and hook changes.
- `python -m py_compile` passed for touched Python files.
- `bench restart` was attempted, but the local environment failed at `sudo supervisorctl status`; `bench --site frappe.localhost clear-cache` completed successfully.


## Category And Template Model

`CategoryCareServices` is the category master.

```json
{
  "doctype": "CategoryCareServices",
  "name": "CategoryCareServices-0001",
  "category_name": "Lab",
  "active": 1,
  "description": "Laboratory tests"
}
```

`CareService template` is the billable service catalog. Every Lab, Imaging, and PetCareService order points directly to one `CareService template`.

```json
{
  "doctype": "CareService template",
  "name": "CareService-00004",
  "service_name": "CBC",
  "animal_species": "Mammal",
  "frequency": "onetime",
  "category_id": "CategoryCareServices-0001",
  "category_name": "Lab",
  "item_code": "CBC",
  "default_price": 25.0,
  "price_list": "Standard Selling",
  "disabled": 0,
  "specimen": "Blood",
  "estimated_turnaround": "2 hours",
  "modality": null,
  "body_part": null,
  "service_area": null,
  "description": "Complete blood count"
}
```

Procedure has one extra master:

```txt
Procedure Template
-> billing_care_service
-> CareService template
-> category_id
-> CategoryCareServices
```

`Procedure Template.category` is a text grouping field for procedure templates. The billable/category link is `billing_care_service`.

## Category Rules For Vue

Use `category_name` case-insensitively:

| Builder | What Vue should select |
|---|---|
| Lab | `CareService template` where linked category name is `Lab` |
| Imaging / Radiology | `CareService template` where linked category name is `Imaging` or `Radiology` |
| Pet care service | `CareService template` where category is not `Lab`, `Imaging`, `Radiology`, or `Procedure`, unless the clinic intentionally has a service category such as `Grooming` or `Service` |
| Procedure | `Procedure Template`; backend gets billing from `Procedure Template.billing_care_service` |

The backend does not infer category for Vue. The frontend should filter/select the correct template category before sending `create_orders`.

## Safe Catalog Reads

Use Frappe list/get calls for the catalog. The current `pet_app.api.care_service` file is not a normalized frontend catalog endpoint.

### List Categories

```json
{
  "method": "frappe.client.get_list",
  "args": {
    "doctype": "CategoryCareServices",
    "filters": {
      "active": 1
    },
    "fields": [
      "name",
      "category_name",
      "description",
      "active"
    ],
    "order_by": "category_name asc",
    "limit_page_length": 100
  }
}
```

Expected frontend category option:

```json
{
  "name": "CategoryCareServices-0001",
  "category_name": "Lab",
  "active": 1,
  "description": "Laboratory tests"
}
```

### List Care Service Templates

```json
{
  "method": "frappe.client.get_list",
  "args": {
    "doctype": "CareService template",
    "filters": {
      "disabled": 0,
      "category_id": "CategoryCareServices-0001",
      "animal_species": "Mammal"
    },
    "fields": [
      "name",
      "service_name",
      "animal_species",
      "frequency",
      "category_id",
      "item_code",
      "default_price",
      "price_list",
      "specimen",
      "estimated_turnaround",
      "modality",
      "body_part",
      "service_area",
      "description",
      "disabled"
    ],
    "order_by": "service_name asc",
    "limit_page_length": 100
  }
}
```

The frontend can enrich each service with its category label from the category map:

```json
{
  "name": "CareService-00004",
  "service_name": "CBC",
  "category_id": "CategoryCareServices-0001",
  "category_name": "Lab",
  "item_code": "CBC",
  "default_price": 25.0,
  "animal_species": "Mammal",
  "frequency": "onetime",
  "specimen": "Blood",
  "estimated_turnaround": "2 hours"
}
```

### List Procedure Templates

```json
{
  "method": "frappe.client.get_list",
  "args": {
    "doctype": "Procedure Template",
    "filters": {
      "active": 1
    },
    "fields": [
      "name",
      "procedure_name",
      "code",
      "species",
      "category",
      "billing_care_service",
      "default_duration_minutes",
      "consent_required",
      "consent_template",
      "anesthesia_required",
      "active"
    ],
    "order_by": "procedure_name asc",
    "limit_page_length": 100
  }
}
```

Fetch the full procedure template if Vue needs checklist steps:

```json
{
  "method": "frappe.client.get",
  "args": {
    "doctype": "Procedure Template",
    "name": "Minor Wound Care"
  }
}
```

Expected procedure template option:

```json
{
  "name": "Minor Wound Care",
  "procedure_name": "Minor Wound Care",
  "code": "PROC-WOUND-MINOR",
  "species": "Mammal",
  "category": "Wound Care",
  "billing_care_service": "CareService-00020",
  "billing_care_service_label": "Minor Wound Care Procedure",
  "billing_category_name": "Procedure",
  "default_duration_minutes": 30,
  "consent_required": 1,
  "consent_template": "Minor Procedure Consent",
  "anesthesia_required": 0,
  "active": 1,
  "steps": [
    {
      "step_title": "Confirm consent",
      "required": 1,
      "default_note": "",
      "sort_order": 1
    },
    {
      "step_title": "Clean wound",
      "required": 1,
      "default_note": "",
      "sort_order": 2
    }
  ]
}
```

## Shared Create Order API

All four work builders start from:

```json
{
  "method": "pet_app.api.workspace.perform_action",
  "args": {
    "source_type": "Visit",
    "name": "VVT-2026-00001",
    "action": "create_orders",
    "payload": {
      "orders": []
    }
  }
}
```

After every create/update action, reload:

```json
{
  "method": "pet_app.api.workspace.get_record",
  "args": {
    "source_type": "Visit",
    "name": "VVT-2026-00001"
  }
}
```

## 1. Lab Builder

### Lab Catalog JSON

Use a `CareService template` from category `Lab`.

```json
{
  "builder": "lab",
  "category": {
    "doctype": "CategoryCareServices",
    "name": "CategoryCareServices-0001",
    "category_name": "Lab"
  },
  "care_service": {
    "doctype": "CareService template",
    "name": "CareService-00004",
    "service_name": "CBC",
    "animal_species": "Mammal",
    "category_id": "CategoryCareServices-0001",
    "category_name": "Lab",
    "item_code": "CBC",
    "default_price": 25.0,
    "price_list": "Standard Selling",
    "specimen": "Blood",
    "estimated_turnaround": "2 hours"
  }
}
```

### Lab Create JSON

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
          "kind": "lab",
          "title": "CBC",
          "template_id": "CareService-00004",
          "priority": "Normal",
          "note": "CBC requested by doctor"
        }
      ]
    }
  }
}
```

### Lab Returned Order JSON

```json
{
  "order_id": "VVT-2026-00001-ORD-AB12CD34EF",
  "kind": "lab",
  "title": "CBC",
  "template_id": "CareService-00004",
  "status": "Ordered",
  "priority": "Normal",
  "qty": 1,
  "price": 0,
  "note": "CBC requested by doctor",
  "linked_doctype": "Lab",
  "linked_name": "LAB-00001"
}
```

### Lab Work Record JSON

```json
{
  "doctype": "Lab",
  "name": "LAB-00001",
  "visit": "VVT-2026-00001",
  "order_id": "VVT-2026-00001-ORD-AB12CD34EF",
  "pet": "PET-00001",
  "doctor": "HLC-PRAC-00001",
  "care_service": "CareService-00004",
  "item_code": "CBC",
  "rate": 25.0,
  "status": "Ordered",
  "result": null,
  "sample_collected_by": null,
  "sample_collected_at": null,
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

### Lab Billable JSON

```json
{
  "item_name": "CBC",
  "item_code": "CBC",
  "item_type": "Lab",
  "qty": 1,
  "rate": 25.0,
  "amount": 25.0,
  "status": "Billable",
  "linked_service_id": "Lab::LAB-00001",
  "linked_doctype": "Lab",
  "linked_name": "LAB-00001",
  "order_id": "VVT-2026-00001-ORD-AB12CD34EF",
  "note": "CBC"
}
```

## 2. Imaging / Radiology Builder

### Imaging Catalog JSON

Use a `CareService template` from category `Imaging` or `Radiology`.

```json
{
  "builder": "imaging",
  "category": {
    "doctype": "CategoryCareServices",
    "name": "CategoryCareServices-0002",
    "category_name": "Imaging"
  },
  "care_service": {
    "doctype": "CareService template",
    "name": "CareService-00005",
    "service_name": "Chest X-Ray",
    "animal_species": "Mammal",
    "category_id": "CategoryCareServices-0002",
    "category_name": "Imaging",
    "item_code": "XRAY-CHEST",
    "default_price": 45.0,
    "price_list": "Standard Selling",
    "modality": "X-Ray",
    "body_part": "Chest",
    "estimated_turnaround": "1 hour"
  }
}
```

### Imaging Create JSON

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

### Imaging Returned Order JSON

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

### Imaging Work Record JSON

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

### Imaging Billable JSON

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

## 3. Pet Care Service Builder

### Pet Care Service Catalog JSON

Use a `CareService template` from a service category such as `Service`, `Grooming`, `Nursing`, or another clinic-defined category. Do not use Lab or Imaging categories here.

```json
{
  "builder": "pet_care_service",
  "category": {
    "doctype": "CategoryCareServices",
    "name": "CategoryCareServices-0003",
    "category_name": "Grooming"
  },
  "care_service": {
    "doctype": "CareService template",
    "name": "CareService-00006",
    "service_name": "Full Grooming",
    "animal_species": "Mammal",
    "category_id": "CategoryCareServices-0003",
    "category_name": "Grooming",
    "item_code": "FULL-GROOMING",
    "default_price": 30.0,
    "price_list": "Standard Selling",
    "service_area": "Grooming",
    "frequency": "onetime"
  }
}
```

### Pet Care Service Create JSON

This creates an operational `PetCareService` record and links it to the Visit order.

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
          "kind": "service",
          "title": "Full Grooming",
          "care_service_id": "CareService-00006",
          "provider": "provider@example.com",
          "due_date": "2026-05-18",
          "note": "Full grooming requested by doctor"
        }
      ]
    }
  }
}
```

### Pet Care Service Returned Order JSON

```json
{
  "order_id": "VVT-2026-00001-ORD-EF56AB78CD",
  "kind": "service",
  "title": "Full Grooming",
  "template_id": "CareService-00006",
  "status": "Ordered",
  "priority": "Normal",
  "qty": 1,
  "price": 0,
  "note": "Full grooming requested by doctor",
  "linked_doctype": "PetCareService",
  "linked_name": "PetCareService-00001"
}
```

### Pet Care Service Work Record JSON

```json
{
  "doctype": "PetCareService",
  "name": "PetCareService-00001",
  "pet_service_name": "Full Grooming",
  "pet_id": "PET-00001",
  "guardian_id": "GUARD-00001",
  "care_service_id": "CareService-00006",
  "item_code": null,
  "price": null,
  "status": "pending",
  "doctor": "HLC-PRAC-00001",
  "provider": "provider@example.com",
  "user": null,
  "visit": "VVT-2026-00001",
  "order_id": "VVT-2026-00001-ORD-EF56AB78CD",
  "due_date": "2026-05-18",
  "start_date": null,
  "end_date": null,
  "description": "Full grooming requested by doctor"
}
```

### Pet Care Service Billing JSON

Current backend behavior:

```json
{
  "create_orders_kind_service_creates_billable": false,
  "reason": "PetCareService currently creates the operational record and order tracking, but does not sync a PetCareService billable row."
}
```

If this service is billable in the current backend, fetch the full `Vet Visit`, append a `care_services` child row, then save the full document:

```json
{
  "step": "get_visit",
  "method": "frappe.client.get",
  "args": {
    "doctype": "Vet Visit",
    "name": "VVT-2026-00001"
  }
}
```

```json
{
  "step": "append_and_save_full_visit_doc",
  "method": "frappe.client.save",
  "args": {
    "doc": "<full Vet Visit doc with care_services appended>"
  },
  "append_row": {
    "care_service_id": "CareService-00006"
  }
}
```

Expected service billable after Visit save/reload:

```json
{
  "item_name": "Full Grooming",
  "item_code": "FULL-GROOMING",
  "item_type": "Service",
  "qty": 1,
  "rate": 30.0,
  "amount": 30.0,
  "status": "Billable",
  "linked_service_id": "visit-care-service::<visit child row name>",
  "linked_doctype": null,
  "linked_name": null,
  "order_id": null,
  "note": "Full Grooming"
}
```

Do not manually append `billable_items` from Vue.

## 4. Procedure Builder

### Procedure Billing Care Service JSON

The procedure charge starts from a `CareService template`, usually category `Procedure`.

```json
{
  "builder": "procedure_billing_care_service",
  "category": {
    "doctype": "CategoryCareServices",
    "name": "CategoryCareServices-0004",
    "category_name": "Procedure"
  },
  "care_service": {
    "doctype": "CareService template",
    "name": "CareService-00020",
    "service_name": "Minor Wound Care Procedure",
    "animal_species": "Mammal",
    "category_id": "CategoryCareServices-0004",
    "category_name": "Procedure",
    "item_code": "PROC-WOUND-MINOR",
    "default_price": 65.0,
    "price_list": "Standard Selling",
    "frequency": "onetime"
  }
}
```

### Procedure Template JSON

```json
{
  "doctype": "Procedure Template",
  "name": "Minor Wound Care",
  "procedure_name": "Minor Wound Care",
  "code": "PROC-WOUND-MINOR",
  "species": "Mammal",
  "category": "Wound Care",
  "billing_care_service": "CareService-00020",
  "billing_care_service_label": "Minor Wound Care Procedure",
  "billing_category_name": "Procedure",
  "default_duration_minutes": 30,
  "consent_required": 1,
  "consent_template": "Minor Procedure Consent",
  "anesthesia_required": 0,
  "active": 1,
  "steps": [
    {
      "step_title": "Confirm consent",
      "required": 1,
      "default_note": "",
      "sort_order": 1
    },
    {
      "step_title": "Clean wound",
      "required": 1,
      "default_note": "",
      "sort_order": 2
    },
    {
      "step_title": "Apply dressing",
      "required": 1,
      "default_note": "",
      "sort_order": 3
    }
  ]
}
```

### Procedure Create JSON

The frontend sends the `Procedure Template`. `care_service` is optional because backend falls back to `Procedure Template.billing_care_service`.

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
          "kind": "procedure",
          "title": "Minor Wound Care",
          "procedure_template": "Minor Wound Care",
          "care_service": "CareService-00020",
          "provider": "provider@example.com",
          "scheduled_at": "2026-05-18 10:00:00",
          "priority": "Normal",
          "indication": "Small open wound",
          "note": "Clean and dress wound"
        }
      ]
    }
  }
}
```

### Procedure Returned Order JSON

```json
{
  "order_id": "VVT-2026-00001-ORD-78CD90EF12",
  "kind": "procedure",
  "title": "Minor Wound Care",
  "template_id": "CareService-00020",
  "status": "Ordered",
  "priority": "Normal",
  "qty": 1,
  "price": 0,
  "note": "Clean and dress wound",
  "linked_doctype": "Pet Procedure",
  "linked_name": "PROC-00001"
}
```

### Procedure Work Record JSON

When opening the linked procedure through `get_record(source_type = "Procedure")`, read `focus_source.detail`.

```json
{
  "name": "PROC-00001",
  "visit": "VVT-2026-00001",
  "order_id": "VVT-2026-00001-ORD-78CD90EF12",
  "pet": "PET-00001",
  "guardian": "GUARD-00001",
  "doctor": "HLC-PRAC-00001",
  "doctor_label": "Dr. Sara Ahmed",
  "provider": "provider@example.com",
  "procedure_template": "Minor Wound Care",
  "procedure_template_label": "Minor Wound Care",
  "care_service": "CareService-00020",
  "care_service_label": "Minor Wound Care Procedure",
  "item_code": "PROC-WOUND-MINOR",
  "rate": 65.0,
  "status": "Pending",
  "scheduled_at": "2026-05-18 10:00:00",
  "started_at": null,
  "completed_at": null,
  "closed_at": null,
  "indication": "Small open wound",
  "consent_obtained": 0,
  "anesthesia_used": 0,
  "procedure_note": null,
  "findings": null,
  "outcome": null,
  "complications": null,
  "aftercare_instructions": null,
  "checklist": [
    {
      "name": "row-0001",
      "step_title": "Confirm consent",
      "required": 1,
      "done": 0,
      "note": ""
    },
    {
      "name": "row-0002",
      "step_title": "Clean wound",
      "required": 1,
      "done": 0,
      "note": ""
    }
  ],
  "attachments": [],
  "notes": []
}
```

### Procedure Billable JSON

```json
{
  "item_name": "Minor Wound Care Procedure",
  "item_code": "PROC-WOUND-MINOR",
  "item_type": "Procedure",
  "qty": 1,
  "rate": 65.0,
  "amount": 65.0,
  "status": "Billable",
  "linked_service_id": "Pet Procedure::PROC-00001",
  "linked_doctype": "Pet Procedure",
  "linked_name": "PROC-00001",
  "order_id": "VVT-2026-00001-ORD-78CD90EF12",
  "note": "Minor Wound Care Procedure"
}
```

## Cancellation Actions

Use soft cancellation. Vue should not delete linked records or manually delete billable rows. After a successful cancel action, reload the Visit aggregate and render the returned statuses.

Cancelled billables are removed from the active billing preview:

- `billing.billable_items` contains active billables only.
- `billing.cancelled_billable_items` contains cancelled billables for audit/history only.
- `data.billables` from workbench contains active billables only.
- `data.cancelled_billables` from workbench contains cancelled billables for audit/history only.

### Cancel Pet Care Service

Allowed when the linked `PetCareService.status` is `pending` or `overdue`. Backend blocks `completed` services and billed Visits.

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

Backend result:

```json
{
  "pet_care_service_status": "cancelled",
  "visit_order_status": "Cancelled",
  "cancelled_billable": {
    "item_type": "Service",
    "status": "Cancelled",
    "linked_doctype": "PetCareService",
    "linked_name": "PetCareService-00001",
    "order_id": "VVT-2026-00001-ORD-EF56AB78CD"
  }
}
```

### Cancel Procedure

Allowed when the linked `Pet Procedure.status` is `Pending` or `In Progress`.

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

Backend sets the linked Visit order to `Cancelled` and cancels the matching unbilled Procedure billable row.

## Frontend Builder Summary JSON

Vue can normalize all four builders into this shape:

```json
{
  "visit": "VVT-2026-00001",
  "builders": {
    "lab": {
      "category_names": ["Lab"],
      "catalog_doctype": "CareService template",
      "order_kind": "lab",
      "order_template_field": "template_id",
      "linked_doctype": "Lab",
      "billable_item_type": "Lab",
      "auto_billable": true
    },
    "imaging": {
      "category_names": ["Imaging", "Radiology"],
      "catalog_doctype": "CareService template",
      "order_kind": "radiology",
      "order_template_field": "template_id",
      "linked_doctype": "Imaging",
      "linked_source_type": "Radiology",
      "billable_item_type": "Imaging",
      "auto_billable": true
    },
    "pet_care_service": {
      "category_names": ["Service", "Grooming", "Nursing"],
      "catalog_doctype": "CareService template",
      "order_kind": "service",
      "order_template_field": "care_service_id",
      "linked_doctype": "PetCareService",
      "linked_source_type": "Service",
      "billable_item_type": "Service",
      "auto_billable": false,
      "billable_path": "Vet Visit.care_services"
    },
    "procedure": {
      "catalog_doctype": "Procedure Template",
      "order_kind": "procedure",
      "order_template_field": "procedure_template",
      "billing_link": "Procedure Template.billing_care_service",
      "linked_doctype": "Pet Procedure",
      "linked_source_type": "Procedure",
      "billable_item_type": "Procedure",
      "auto_billable": true
    }
  }
}
```

## Validation Rules To Show In Vue

- `CategoryCareServices.active` must be `1`.
- `CareService template.disabled` must be `0`.
- `CareService template.item_code` is required for Lab, Imaging, Procedure billing, and billable visit services.
- `CareService template.default_price` is required for Lab, Imaging, Procedure billing, and billable visit services.
- Lab builder should only show Lab category templates.
- Imaging builder should only show Imaging/Radiology category templates.
- Pet care service builder should not show Lab/Imaging diagnostic templates.
- Procedure builder should show active `Procedure Template` records.
- Procedure template must have `billing_care_service`.
- Do not manually create `billable_items` from Vue.
- Reload the Visit after every mutation and render backend `orders`, `linked_records`, and active `billing.billable_items`.
