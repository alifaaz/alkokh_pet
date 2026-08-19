# `scheduled_datetime` — one name, both routes

**Status:** live on `frappe.localhost` (migrated). One field, one type, one spelling,
everywhere a clinical order can be raised.

## The name

| | |
|---|---|
| **Parameter you send** | `scheduled_datetime` |
| **Key echoed back** | `scheduled_datetime` |
| **Type** | Datetime — `"YYYY-MM-DD HH:MM:SS"` |
| **Required?** | No. Never. |

The send name and the echo name are the same string on every endpoint below. Nothing to
infer, and nothing that changes shape depending on which order kind you are raising.

## Where it works

**Boarding** — `pet_app.api.healthcare.boarding.create_order` now takes **14** parameters;
`scheduled_datetime` is the fourteenth. It applies to all four kinds (`lab`, `radiology`,
`service`, `medication`). For lab, radiology and service it lands on the order document
*and* on the billable row; for `medication` there is no order document — the billable row
**is** the order — so it lands there.

**Visit** — the same key on the order payload for all four kinds (`lab`, `radiology`,
`service`, `procedure`).

**Read back** — every billable row from `get_boarding_detail`, `create_order`,
`sync_billable_items`, `dispense_medication` and the rest now carries
`"scheduled_datetime"`, null when unset. The procedure payload carries it too.

```json
{ "boarding_id": "BRD-00109", "kind": "lab", "pet": "PET-00129",
  "template_id": "CS-0001", "scheduled_datetime": "2026-09-01 14:30:00" }
```

```json
{ "name": "af3jrgatgk", "item_code": "...", "qty": 1, "rate": 5000,
  "scheduled_datetime": "2026-09-01 14:30:00" }
```

## Absence is a real answer

Most orders are "do it now" and will carry `null` forever. That is correct and supported —
do not send a value to fill the field in.

**Nothing defaults it.** There is no `or now()` anywhere on either route, deliberately: a
stamp written automatically is indistinguishable from one a person chose, and the moment
every row carries one, nothing can tell a scheduled order from an unscheduled one. That is
the whole reason the field exists, so it is the one thing the backend will not do for you.

- `null`, `""` and whitespace all mean **not scheduled** and are stored as null. A cleared
  picker and a picker never touched send the same thing and mean the same thing.
- A value that is present but unparseable **throws** — it is not silently dropped, because
  a dropped schedule would leave your UI showing a time the record does not hold.

## It has no billing effect

`scheduled_datetime` never touches `qty`, `rate` or `amount`, never changes a total, and
never reaches the Sales Invoice. Verified: invoice lines carry no schedule key at all. It
is recorded and read; it does not price anything.

It also **survives a billing sync**. `sync_billable_items` will not blank it — it is a
server-owned column like `dosage`, `warehouse` and the stock trail, and is not in that
endpoint's writable set. You do not need to echo it back when syncing billable rows.

## What happened to the old fields

**`scheduled_at` (Pet Procedure) — being retired, still accepted.** It is the same concept
under an older name. On the visit path, `scheduled_at` is still read as a deprecated alias
if `scheduled_datetime` is absent, and both columns are still written, so a client that has
not moved yet keeps working. **Move to `scheduled_datetime`;** `scheduled_at` will be
dropped in a later change, and the deprecated alias goes with it. Its 4 live rows were
copied across.

**`due_date` (PetCareService) — kept, and it is not the schedule.** It stays exactly as it
is: required, a Date, defaulted to today by all four of its writers. It answers *"which day
is this owed"*, not *"when is it booked"* — 98.9% of live rows hold nothing but their own
creation date, so it can never tell you whether anyone scheduled anything. Keep sending it
if you send it today; keep sorting on it if you sort on it today. It is a different
question and it has a different answer.

Only the 31 service rows whose `due_date` genuinely differed from their creation date were
migrated, and those landed at **`00:00:00`** — a Date has no time, so there was none to
migrate. Midnight is the marker for *"date known, time not"*; treat it as a date, not as a
booking at midnight.

## One behaviour change to expect in the worklist

The workspace worklist used to report a **service's** `scheduled_at` as its `start_date` —
the moment the work *started*, stamped by the server with microseconds on 96.9% of rows.
That column now reads `scheduled_datetime` and will be **null until someone actually
schedules a service**. Fewer services will show a time than before; that is the correction,
not a regression. The same column for a procedure meant something entirely different, which
is what convergence fixes.

Imaging work items in the user-performance list previously pointed at a `start_at` column
that does not exist on Imaging at all, so they never showed a time and never errored. They
now read `scheduled_datetime` like everything else.
