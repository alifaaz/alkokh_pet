# Employee Of The Month Frontend Contract

This document explains how the backend decides who belongs in each Employee of the Month group for the Report Engine tab.

## Endpoint

Call the whitelisted Frappe method:

```ts
pet_app.api.employee_month.get_employee_month_dashboard
```

Params:

```ts
{
  date_from: "YYYY-MM-DD",
  date_to: "YYYY-MM-DD"
}
```

The frontend should render the returned groups directly. Do not re-detect whether a person is a doctor, service provider, coordinator, cashier, or receptionist in frontend code.

## Top-Level Groups

The backend always returns these six keys:

```ts
type EmployeeMonthResponse = {
  service_providers: EmployeeMonthGroup
  doctors: EmployeeMonthGroup
  coordinators: EmployeeMonthGroup
  cashiers: EmployeeMonthGroup
  receptionists: EmployeeMonthGroup
  other_staff: EmployeeMonthGroup
}
```

Each group has the same outer shape:

```ts
type EmployeeMonthGroup = {
  winner: EmployeeMonthRow | null
  summary: {
    total_people: number
    total_completed: number
    average_rating: number | null
    top_score: number
  }
  leaderboard: EmployeeMonthRow[]
}
```

## Detection Mechanism

There are two different population mechanisms.

## Clinical Groups: Practitioner-Based

`service_providers` and `doctors` are based on `Healthcare Practitioner` records.

Only active practitioners are considered. Disabled practitioners are excluded when the `disabled` field exists.

### Doctors

A practitioner is included in `doctors` when:

```txt
Healthcare Practitioner.practitioner_type == "Doctor"
```

Rows use:

```txt
practitioner_id = Healthcare Practitioner.name
```

### Service Providers

A practitioner is included in `service_providers` when either condition is true:

```txt
Healthcare Practitioner.practitioner_type != "Doctor"
```

or:

```txt
Healthcare Practitioner.name appears in PetCareService.provider
```

This means a doctor who also completes service work can appear in the service provider leaderboard too. The frontend should not try to prevent this; render the backend response as-is.

Rows use:

```txt
practitioner_id = Healthcare Practitioner.name
```

Clinical rows may include ratings and on-time data:

```ts
rating_average: number | null
rating_count: number
on_time_rate: number | null
```

## Staff Groups: Role-Based Users

`coordinators`, `cashiers`, `receptionists`, and `other_staff` are based on active `User` records, not `Healthcare Practitioner`.

The backend reads roles through `get_user_roles(user)`, which includes:

- roles directly assigned to the user
- roles inherited through role profiles

Only enabled `System User` records are considered.

These users are excluded from staff groups:

- `Administrator`
- `Guest`
- users already ranked in `service_providers` or `doctors`

Staff rows use:

```txt
practitioner_id = User.name
```

For staff rows, `practitioner_id` is a user id/email, not a healthcare practitioner id.

### Staff Role Priority

If a user has more than one staff role, the backend chooses one group in this order:

```txt
coordinator -> cashier -> receptionist -> other_staff
```

Role mapping:

| Group | Roles accepted by backend | Display label |
| --- | --- | --- |
| `coordinators` | `Coordinator`, `Coordinatorr` | `Coordinator` |
| `cashiers` | `Cashier`, `POS Cashier` | `Cashier` |
| `receptionists` | `Receptionist`, `Reception` | `Receptionist` |
| `other_staff` | active staff users with none of the above roles | `Other` |

`other_staff` excludes users whose only roles are framework/system roles such as:

- `All`
- `Guest`
- `Administrator`
- `System Manager`

## Staff Count Metric

Staff leaderboards rank by document activity in the selected date range.

`completed_count` means:

```txt
documents created by the user
+ submitted documents where that user submitted/modified the document
```

The backend avoids counting the same document twice for the same user.

The counted doctypes follow the same profile Scoreboard list:

- `PetCareService`
- `Vet Case Sheet`
- `Vet Visit`
- `Pet Procedure`
- `Lab`
- `Imaging`
- `Appointment`
- `Rating`

Staff rows may include:

```ts
by_doctype: Array<{
  doctype: string
  label: string
  count: number
}>
```

## Frontend Rules

Render the six groups returned by the backend. Do not filter by role on the frontend.

Use `practitioner_type` as the row label. It already contains values like `Doctor`, `Service Provider`, `Coordinator`, `Cashier`, `Receptionist`, or `Other`.

Treat staff rows differently from clinical rows when needed:

```ts
const isStaffGroup = ["coordinators", "cashiers", "receptionists", "other_staff"].includes(groupKey)
```

For staff groups:

```ts
rating_average === null
rating_count === 0
on_time_rate === null
primary_metric_label === "Documents created"
```

For clinical groups:

```ts
primary_metric_label === "Completed services" // service_providers
primary_metric_label === "Completed clinical records" // doctors
```

Use `winner === leaderboard[0]` when the leaderboard is non-empty. If `winner` is `null`, show the empty state.

Use `image` if present. If `image` is `null` or broken, fall back to initials.

## Important ID Difference

Clinical groups:

```txt
row.practitioner_id = Healthcare Practitioner.name
```

Staff groups:

```txt
row.practitioner_id = User.name
```

The frontend should treat `practitioner_id` as an opaque row id and should not assume every id is a healthcare practitioner.
