# Frontend Handoff: Case Follow-up Table

This page is for the doctor view that shows open medical cases and the plan/follow-up items inside each case.

## Mental Model

```text
Pet Medical Profile
  permanent dashboard for one pet

Pet Care Episode
  one medical case / treatment course
  one pet can have many episodes over time
  normally only one active episode at a time

Pet Care Plan Item
  one actionable item inside an episode
  examples: medication course, lab recheck, imaging recheck, follow-up visit, monitoring, owner instruction

Vet Visit
  one doctor encounter
  visits can create items and later continue the same episode
```

Do not model "case has many episodes". In this backend, the episode is the case. If the UI needs a parent across many cases, use the `Pet Medical Profile` as the parent and show multiple `Pet Care Episode` rows under it.

## Main Table Endpoint

Use this endpoint for the doctor page table:

```ts
frappe.call({
  method: "pet_app.api.care_plan.get_case_follow_up_table",
  args: {
    filters: {
      active_only: 1,
      include_closed_items: 1,
      doctor: practitionerId,
      pet: petId,
      follow_up_state: "overdue"
    },
    limit_start: 0,
    limit_page_length: 50
  }
})
```

All filters are optional.

Important filters:

- `active_only`: default `1`. Returns active episodes only. Pass `0` to include resolved/closed history.
- `include_closed_items`: default `1`. Keep this on when the table needs `Done`, `Cancelled`, or `Came` rows.
- `doctor`: filters cases where the practitioner is primary doctor or assigned on plan items, and filters item rows to that doctor.
- `pet`, `guardian`, `customer`: narrow the case table.
- `episode_status` / `case_status`: filter by episode status.
- `plan_type`: filter plan item type.
- `item_status` / `plan_status`: filter raw `Pet Care Plan Item.status`.
- `follow_up_state`: filter computed UI state.
- `date_from`, `date_to`, `due_date`: filter by plan item `due_date`.

Response shape:

```ts
type CaseFollowUpTableResponse = {
  ok: true
  data: {
    cases: Array<{
      case: PetCareEpisode
      episode: PetCareEpisode
      items: CaseFollowUpItem[]
      visits: VetVisit[]
      follow_up_summary: {
        total_items: number
        open_items: number
        completed_items: number
        cancelled_items: number
        converted_items: number
        overdue_items: number
        due_today_items: number
        missed_items: number
        next_due_date?: string
        next_follow_up_date?: string
        follow_up_status?: string
        visit_count: number
        last_visit?: string
        last_visit_status?: string
      }
    }>
    rows: CaseFollowUpFlatRow[]
    metrics: {
      total_cases: number
      total_items: number
      open_items: number
      overdue_items: number
      due_today_items: number
      completed_items: number
      cancelled_items: number
      converted_items: number
      by_state: Record<string, number>
    }
  }
  meta: {
    total: number
    returned: number
    limit_start: number
    limit_page_length: number
  }
}
```

`data.cases` is best for expandable case rows. `data.rows` is best for a simple flat table.

`meta.total` is the full count of cases matching the filters across **all** pages (not the page size),
independent of `limit_start` / `limit_page_length` — use it for page count and the "X–Y of TOTAL"
label. `meta.returned` is the number of cases on the current page (`== data.cases.length`). The total
stays exact even when item-level filters (`plan_type`, `item_status`/`plan_status`, `follow_up_state`,
date filters) are active: it counts only the episodes that actually have a matching item, matching what
the paged result returns.

## Item State Rules

The backend computes `follow_up_state` and `follow_up_label` for every item.

```ts
type FollowUpState =
  | "open"
  | "in_progress"
  | "scheduled"
  | "due_today"
  | "overdue"
  | "missed"
  | "completed"
  | "cancelled"
  | "converted_to_visit"
```

Display mapping:

- `open`: item exists but is not scheduled/due yet.
- `scheduled`: item has appointment or scheduled status.
- `due_today`: due date is today.
- `overdue`: due date is before today, or raw status is `Overdue`.
- `completed`: raw status is `Done`.
- `cancelled`: raw status or appointment is cancelled.
- `converted_to_visit`: pet/owner came back and the item was converted to a visit. Use `patient_came = true`, `linked_visit`, and `linked_visit_status`.

For a converted follow-up where the linked visit is completed, `follow_up_label` is `Came / Visit Completed`.

## Existing Supporting Endpoints

Full case profile for one episode by id alone (case profile page / deep links):

```ts
frappe.call({
  method: "pet_app.api.care_plan.get_care_episode_detail",
  args: { episode: episodeId }
})
```

Returns exactly one `data.cases[]` element of the main table — `data` is
`{ case, episode, items, visits, follow_up_summary }` with the same shapes documented above — but keyed
by the episode id with **no pet filter required**. Use this for the case profile page instead of
composing from `get_case_follow_up_table` filtered by pet; it loads deep links / bookmarks to a case
that has no pet context, including closed/resolved episodes that the board's default `active_only`
filter would exclude. Optional item filters (`plan_type`, `item_status`/`plan_status`,
`follow_up_state`, `date_from`/`date_to`) narrow the returned `items` the same way as the table.
Permission-scoped like the other episode endpoints (caller must be allowed to see the pet's clinical
data). Returns `{ ok: false, meta: { code: "NOT_FOUND" } }` for an unknown episode and
`{ code: "VALIDATION_ERROR" }` when `episode` is omitted.

Case item list for one episode:

```ts
frappe.call({
  method: "pet_app.api.care_plan.get_care_episode_plan",
  args: { episode: episodeId }
})
```

Active items for one pet:

```ts
frappe.call({
  method: "pet_app.api.care_plan.get_pet_active_plan",
  args: { pet: petId }
})
```

Due active items:

```ts
frappe.call({
  method: "pet_app.api.care_plan.list_due_plan_items",
  args: {
    filters: {
      doctor: practitionerId,
      date_to: "2026-07-05"
    }
  }
})
```

Visit workbench, including active episode and active plan items for the current visit:

```ts
frappe.call({
  method: "pet_app.api.visit_workbench.get_visit_workbench",
  args: { visit: visitId }
})
```

Legacy visit-level follow-ups:

```ts
frappe.call({
  method: "pet_app.api.follow_up.list_due_follow_ups",
  args: { doctor: practitionerId, date_to: "2026-07-05" }
})
```

## Mutations

Add item from an episode-linked visit:

```ts
frappe.call({
  method: "pet_app.api.care_plan.add_plan_item_from_visit",
  args: {
    visit: visitId,
    data: {
      plan_type: "Follow-up Visit",
      title: "Recheck appetite",
      due_date: "2026-07-12",
      priority: "Important"
    }
  }
})
```

Update item:

```ts
frappe.call({
  method: "pet_app.api.care_plan.update_plan_item",
  args: {
    plan_item: planItemId,
    data: { status: "In Progress" }
  }
})
```

Mark done:

```ts
frappe.call({
  method: "pet_app.api.care_plan.complete_plan_item",
  args: {
    plan_item: planItemId,
    note: "Course completed"
  }
})
```

Cancel:

```ts
frappe.call({
  method: "pet_app.api.care_plan.cancel_plan_item",
  args: {
    plan_item: planItemId,
    reason: "Owner declined"
  }
})
```

Schedule appointment:

```ts
frappe.call({
  method: "pet_app.api.care_plan.schedule_plan_item_appointment",
  args: {
    plan_item: planItemId,
    appointment_data: {
      scheduled_time: "2026-07-12 10:00:00"
    }
  }
})
```

Convert the item to a visit when the pet/owner comes:

```ts
frappe.call({
  method: "pet_app.api.care_plan.convert_plan_item_to_visit",
  args: {
    plan_item: planItemId,
    data: {
      doctor: practitionerId
    }
  }
})
```

After conversion, the plan item status becomes `Converted To Visit`, and the new visit continues the same `care_episode`.

## Frontend Display Guidance

Recommended columns:

- Case: `episode_title`, `episode_status`, `pet_name`, `guardian_name`, `doctor_name`.
- Case summary: `primary_diagnosis`, `treatment_summary`, `next_follow_up_date`.
- Item: `plan_type`, `title`, `follow_up_label`, `due_date`, `scheduled_datetime`, `assigned_to_name`.
- Result: `patient_came`, `linked_visit`, `linked_visit_status`, `completed_on`, `completion_note`.

Recommended table behavior:

1. Load `get_case_follow_up_table` for the doctor landing page.
2. Show one case row with summary counts.
3. Expand the case to show its `items`.
4. Use `follow_up_state` for badges and filters.
5. Use `convert_plan_item_to_visit` when the pet arrives for that follow-up item.
6. Refresh the table after every mutation.

Important rule: completing one item does not close the case. Close the episode only when the whole treatment course is finished.
