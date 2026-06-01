# Frontend Handoff: Generic Rating Module

## Purpose

This handoff is for the Vue rating module.

The rating model lets the frontend attach feedback to any Frappe document using a dynamic link:

```txt
Rating
-> reference_doctype
-> reference_name
```

Ratings store:

- One overall integer rating from `1` to `5`
- Optional selected `Rating Questionnaire`
- Optional per-question answer rows
- Optional notes

Use standard Frappe APIs. No custom rating API is required for v1.

## DocType Model

`Rating Questionnaire` is the reusable template master.

```json
{
  "doctype": "Rating Questionnaire",
  "name": "Visit Experience",
  "questionnaire_name": "Visit Experience",
  "active": 1,
  "applies_to_doctype": "Vet Visit",
  "description": "Customer-facing visit feedback",
  "questions": [
    {
      "question_key": "doctor_communication",
      "question_text": "Doctor communication",
      "required": 1,
      "sort_order": 1,
      "help_text": "How clearly did the doctor explain the case?"
    },
    {
      "question_key": "staff_service",
      "question_text": "Staff service",
      "required": 0,
      "sort_order": 2,
      "help_text": ""
    }
  ]
}
```

`applies_to_doctype` is optional:

- Blank means the questionnaire can be used for any target DocType.
- Filled means the questionnaire can only be used when `Rating.reference_doctype` matches it.

`Rating` is the submitted feedback record.

```json
{
  "doctype": "Rating",
  "name": "RATING-2026-00001",
  "reference_doctype": "Vet Visit",
  "reference_name": "VVT-2026-00001",
  "questionnaire": "Visit Experience",
  "overall_rating": 5,
  "rated_by": "doctor@example.com",
  "rated_at": "2026-05-19 12:30:00",
  "notes": "Owner was very happy with the visit.",
  "answers": [
    {
      "question_key": "doctor_communication",
      "question_text": "Doctor communication",
      "required": 1,
      "rating": 5,
      "note": "Very clear explanation",
      "sort_order": 1
    },
    {
      "question_key": "staff_service",
      "question_text": "Staff service",
      "required": 0,
      "rating": 4,
      "note": "",
      "sort_order": 2
    }
  ]
}
```

## Safe Catalog Reads

### List Active Questionnaires

Fetch active questionnaires, then filter in Vue:

- Show global questionnaires where `applies_to_doctype` is blank.
- Show scoped questionnaires where `applies_to_doctype` equals the target DocType.

```json
{
  "method": "frappe.client.get_list",
  "args": {
    "doctype": "Rating Questionnaire",
    "filters": {
      "active": 1
    },
    "fields": [
      "name",
      "questionnaire_name",
      "applies_to_doctype",
      "description",
      "active"
    ],
    "order_by": "questionnaire_name asc",
    "limit_page_length": 100
  }
}
```

Expected frontend option:

```json
{
  "name": "Visit Experience",
  "questionnaire_name": "Visit Experience",
  "applies_to_doctype": "Vet Visit",
  "description": "Customer-facing visit feedback",
  "active": 1
}
```

### Fetch Questionnaire Questions

When the user selects a questionnaire, fetch the full document to get child rows.

```json
{
  "method": "frappe.client.get",
  "args": {
    "doctype": "Rating Questionnaire",
    "name": "Visit Experience"
  }
}
```

Expected response shape:

```json
{
  "name": "Visit Experience",
  "questionnaire_name": "Visit Experience",
  "active": 1,
  "applies_to_doctype": "Vet Visit",
  "questions": [
    {
      "question_key": "doctor_communication",
      "question_text": "Doctor communication",
      "required": 1,
      "sort_order": 1,
      "help_text": "How clearly did the doctor explain the case?"
    }
  ]
}
```

## Create Rating

Use `frappe.client.insert`.

Frontend should send integer values from `1` to `5`. Do not use a decimal or Frappe fraction value.

```json
{
  "method": "frappe.client.insert",
  "args": {
    "doc": {
      "doctype": "Rating",
      "reference_doctype": "Vet Visit",
      "reference_name": "VVT-2026-00001",
      "questionnaire": "Visit Experience",
      "overall_rating": 5,
      "notes": "Owner was very happy with the visit.",
      "answers": [
        {
          "question_key": "doctor_communication",
          "rating": 5,
          "note": "Very clear explanation"
        },
        {
          "question_key": "staff_service",
          "rating": 4,
          "note": ""
        }
      ]
    }
  }
}
```

Backend defaults:

- `rated_by` defaults to the current Frappe user.
- `rated_at` defaults to the current datetime.
- `question_text`, `required`, and `sort_order` in answer rows are copied from the selected questionnaire.

Expected created rating:

```json
{
  "doctype": "Rating",
  "name": "RATING-2026-00001",
  "reference_doctype": "Vet Visit",
  "reference_name": "VVT-2026-00001",
  "questionnaire": "Visit Experience",
  "overall_rating": 5,
  "rated_by": "doctor@example.com",
  "rated_at": "2026-05-19 12:30:00",
  "notes": "Owner was very happy with the visit.",
  "answers": [
    {
      "question_key": "doctor_communication",
      "question_text": "Doctor communication",
      "required": 1,
      "rating": 5,
      "note": "Very clear explanation",
      "sort_order": 1
    },
    {
      "question_key": "staff_service",
      "question_text": "Staff service",
      "required": 0,
      "rating": 4,
      "note": "",
      "sort_order": 2
    }
  ]
}
```

### Create Simple Rating Without Questionnaire

Use this when the UI only needs a quick score and notes.

```json
{
  "method": "frappe.client.insert",
  "args": {
    "doc": {
      "doctype": "Rating",
      "reference_doctype": "PetCareService",
      "reference_name": "PetCareService-00001",
      "overall_rating": 4,
      "notes": "Service completed well."
    }
  }
}
```

Do not send `answers` unless `questionnaire` is selected.

## Read Existing Ratings

### List Ratings For A Target

Use this to show the rating history for a document.

```json
{
  "method": "frappe.client.get_list",
  "args": {
    "doctype": "Rating",
    "filters": {
      "reference_doctype": "Vet Visit",
      "reference_name": "VVT-2026-00001"
    },
    "fields": [
      "name",
      "reference_doctype",
      "reference_name",
      "questionnaire",
      "overall_rating",
      "rated_by",
      "rated_at",
      "notes"
    ],
    "order_by": "rated_at desc",
    "limit_page_length": 50
  }
}
```

Expected list row:

```json
{
  "name": "RATING-2026-00001",
  "reference_doctype": "Vet Visit",
  "reference_name": "VVT-2026-00001",
  "questionnaire": "Visit Experience",
  "overall_rating": 5,
  "rated_by": "doctor@example.com",
  "rated_at": "2026-05-19 12:30:00",
  "notes": "Owner was very happy with the visit."
}
```

### Fetch Rating Detail

Use this when Vue needs answer rows.

```json
{
  "method": "frappe.client.get",
  "args": {
    "doctype": "Rating",
    "name": "RATING-2026-00001"
  }
}
```

## Update Rating

Use `frappe.client.save` with the full Rating document.

```json
{
  "method": "frappe.client.save",
  "args": {
    "doc": {
      "doctype": "Rating",
      "name": "RATING-2026-00001",
      "reference_doctype": "Vet Visit",
      "reference_name": "VVT-2026-00001",
      "questionnaire": "Visit Experience",
      "overall_rating": 4,
      "rated_by": "doctor@example.com",
      "rated_at": "2026-05-19 12:30:00",
      "notes": "Updated after follow-up call.",
      "answers": [
        {
          "question_key": "doctor_communication",
          "rating": 4,
          "note": "Updated score"
        }
      ]
    }
  }
}
```

After save, reload the rating detail or target rating list.

## Vue Normalization Shape

Vue can normalize the module into this shape:

```json
{
  "target": {
    "reference_doctype": "Vet Visit",
    "reference_name": "VVT-2026-00001"
  },
  "questionnaire": {
    "name": "Visit Experience",
    "questionnaire_name": "Visit Experience",
    "applies_to_doctype": "Vet Visit",
    "questions": [
      {
        "question_key": "doctor_communication",
        "question_text": "Doctor communication",
        "required": true,
        "sort_order": 1,
        "help_text": "How clearly did the doctor explain the case?"
      }
    ]
  },
  "draft_rating": {
    "overall_rating": 5,
    "notes": "",
    "answers": [
      {
        "question_key": "doctor_communication",
        "rating": 5,
        "note": ""
      }
    ]
  }
}
```

## Frontend Rules

- Render ratings as `1` to `5`; send integers only.
- `reference_doctype` and `reference_name` are required.
- `overall_rating` is required.
- If `questionnaire` is blank, do not send answer rows.
- If `questionnaire` is selected, show its questions in `sort_order` order.
- Required questionnaire questions must have a `rating` from `1` to `5`.
- Optional questions may be left blank or sent with a `rating` from `1` to `5`.
- Do not trust client-side scope only. Backend also validates questionnaire scope.
- One user can create only one rating for the same `reference_doctype`, `reference_name`, and `questionnaire`.
- A blank questionnaire counts as its own duplicate group.
- If the backend returns a duplicate error, reload existing ratings for that target and offer edit instead of create.
- Do not manually set `rated_by` or `rated_at` for normal user flows.

## Validation Errors To Surface In Vue

Backend may reject save when:

- The target document does not exist.
- `overall_rating` is outside `1` to `5`.
- The selected questionnaire is inactive.
- The selected questionnaire is scoped to a different DocType.
- Answer rows do not match the selected questionnaire question keys.
- A required question is missing a valid rating.
- The same user already submitted a rating for the same target and questionnaire.

## Permission Notes

- `System Manager` can manage questionnaires and ratings.
- `Pet App Admin` can manage questionnaires.
- `Desk User` can create ratings and edit/read ratings they own.

