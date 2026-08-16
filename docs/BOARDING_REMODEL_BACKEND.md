# Boarding Re-model — Backend Build Plan

**Repo:** `pet_app` (Frappe/ERPNext v16), branch `develop`
**Status:** design settled, nothing built
**Date:** 2026-08-16

---

## 1. The model

A boarding is a **booking made by one guardian**, holding up to **7 of their pets**.
Each pet has its own room, its own arrival, and its own departure. Money follows the
**pet**, not the room.

| Decision | Value |
|---|---|
| Pricing unit | **Per pet, per night** |
| Rate source | **CareService catalogue** — nothing hardcoded, no rate in settings |
| Rate selected by | Pet's `animal_type` **+** travel/medical. **The room does not affect price** |
| Species coverage | The eight existing `animal_type` values, plus an **"All" template** as fallback |
| Travel vs medical | **Chosen by staff.** The system may suggest, never switch silently |
| Medications | Ordered **from the boarding** → included, not charged<br>Prescribed **on the linked visit** → charged normally |
| Labs / imaging / services | Always charged separately, unchanged |
| Room | Normally **one room holding all the booking's pets**. `service_room` lives on the occupant row so a pet *can* be moved alone, but sharing is the common case |
| Room transfer | **Per pet**, and the **exception** — isolating an aggressive or sick animal. The booking then spans two rooms. Purely a location record — **no financial effect** |
| Capacity | 7 per booking, held in **Pet Boarding Settings**, not a constant |
| Joining | A pet may join mid-stay; it pays from its own arrival |
| Departure | A pet may leave individually; it pays to its own departure |
| Death | Closes **that occupant only**. The booking continues |
| Last occupant | **Cannot depart individually** — staff must check the booking out |
| Mid-stay type change | **Not supported.** Check the pet out and re-admit it |
| Invoice | **Itemised per pet** — each has its own nights and rate |
| Guardian exclusivity | Two guardians never share a room |

### The room invariant is one guardian, not one booking

`service_room` on the occupant does **not** mean a room per pet. A booking is normally one
room holding all its pets; per-pet transfer is the exception that splits it.

Two consequences, and both contradict the obvious reading:

- **A room is free when no pet remains in it** — not when the booking ends. One pet moving
  out of a shared room frees nothing; the last one moving out frees it
- **A booking may legitimately hold two rooms.** Any check shaped "one room per booking" is
  wrong. The invariant is **one guardian per room**

### Why the room doesn't affect price

Every room costs the same. Transfer is therefore a location record only — a pet that
moves on night 3 of 5 still pays 5 nights at its own rate. This is what makes per-pet
transfer cheap to build.

### Why mid-stay type change isn't supported

Splitting a stay at the switch point needs a rate-period history — a new table, period
arithmetic, and a rule for which period applies. The owner's alternative achieves the
same result with existing tools: **check the pet out, re-admit it as the new type.**
Two clean records, two correct rates, and an invoice that shows both.

**But the current silent failure must be closed.** `boarding_type` is editable from
Desk today, and changing it mid-stay causes `_ensure_room_stay_billable_item` to return
early on a `linked_service_id` mismatch — leaving the old item, old rate, and a `qty`
of 1 from reserve time. The guardian is billed for **one night at the wrong rate**
regardless of actual stay length. No live record has triggered it yet.

---

## 2. Two defects to fix first, independent of everything else

Neither depends on the re-model. Both are live.

### 2.1 `start_visit_boarding` forces "Treatment"

`boarding.py:402` hardcodes `"boarding_type": "Treatment"` when a boarding starts from a
visit. The operator is never asked. This is exactly the silent switch the owner rejected,
and it explains all 21 medical bookings that came from visits.

**Fix:** make it a default the operator confirms, not a value they never see.

### 2.2 The early-return under-bill

`_ensure_room_stay_billable_item` (`boarding.py:2351`):

```python
if not existing_row and not add_if_missing:
    return
```

When `boarding_type` has changed, `linked_service_id` no longer matches the existing row,
so at checkout (`:794`, called with `add_if_missing=False`) the function returns and does
nothing. The row keeps its reserve-time `qty` of 1 and its old rate.

On the paths where `add_if_missing=True`, the same mismatch appends a **second** Room Stay
row — a double charge.

**Fix regardless of any other decision.** Verified absent from live data (zero Room Stay
rows with `qty < stay_days` where `stay_days > 1`) — latent, not active.

---

## 3. What the catalogue needs

Boarding pricing today comes from **Pet Boarding Settings → Item → Item Price**, not from
the catalogue at all. Two CareService templates exist under `CategoryCareServices-0016`
("Boarding") and are **completely bypassed**:

| Template | Service | Item | Price | `animal_species` |
|---|---|---|---|---|
| CareService-00016 | Regular Boarding | Regular Bording | 15,000 | All |
| CareService-00017 | Treatment Boarding | Treatment Boarding | 25,000 | All |

Note the travel template points at `Regular Bording` (sic) while settings point at
`Travel Boarding` — they have already diverged.

### The blocker: the catalogue can't tell a cat from a dog

`CareService.animal_species` is **taxonomic class** — `Mammal / Bird / Reptile / Fish /
Amphibian / Insect / Arachnid / Crustacean`. Cat and dog are both `Mammal`. The pricing
distinction the owner needs **cannot be expressed** with it.

`Pet.animal_type` is the practical field — `Dog / Cat / Bird / Fish / Reptile / Horse /
Rabbit / Other`, mandatory, fully populated across 2,173 pets (Cat 1,753, Dog 408,
Bird 8, Rabbit 3, Horse 1).

**Required:** add an `animal_type` dimension to the CareService template, mirroring
`Pet.animal_type`, with **"All"** as a valid value.

**Resolution rule:** match on category = Boarding, `animal_type` = the pet's type, and
travel/medical. Fall back to the **"All"** row when no exact match exists. Throw with an
actionable message only when neither resolves.

That makes cats and dogs four catalogue rows, bird boarding two more, and a horse or
rabbit falls to "All" — **no code change for any of it.**

---

## 4. Staged build

Each stage is independently verifiable and leaves the system no worse than before it.

### Stage 0 — the two standalone defects

§2.1 and §2.2. No model dependency. Ship first.

### Stage 1 — clinical orders name their pet

`create_order` copies `boarding.pet` onto every order. With several pets in one booking a
lab for one animal would be filed against another.

- **`pet` becomes a required parameter** on `create_order`, threaded through to
  `_create_boarding_order_doc`. Required, not optional — optional lets every existing
  call site keep compiling while silently sending nothing
- **`order_id` must include the pet.** Today
  `f"{boarding.name}-{kind}-{care_service}"` has none, and `_find_recent_duplicate_order`
  (`:1666-1679`) filters on source and care service within a 120-second window. Order the
  same test for two pets within two minutes and **the second call returns the first pet's
  order and creates nothing** — no clinical record, no charge, no error
- **Backfill the 5 existing boarding-sourced orders** rather than teaching the matcher two
  formats. `order_id` is a match key in `care_service.py:339` and `workspace.py:2502`
- Update the frontend caller in the same change, so the contract moves once
- `_active_episode_name_for_pet` must follow the **order's** pet, not the booking's

**Note:** these are *latent* today — one booking holds one pet, so `boarding.pet` is the
right pet and the collision cannot fire. They are wrong only under the new model. Doing
them first is about contract lead time, not correctness.

### Stage 2 — the model

**New: `Pet Boarding Occupant`** — child of Pet Boarding:

| Field | Notes |
|---|---|
| `pet` | Link → Pet, validated against the guardian per row |
| `service_room` | Link → Service Room — **the room belongs to the pet** |
| `joined_at` | Datetime |
| `departed_at` | Datetime, nullable |
| `status` | Active / Departed / Deceased |
| `boarding_type` | Travel / Medical — per occupant |

**New: `Pet Boarding Room Stint`** — per-pet room history for transfers. Location record
only; no financial effect.

**`Pet Billable Item` gains a nullable `pet` Link.** Required now, not optional: the
invoice itemises per pet, since each has its own nights and rate.

**`reserve_room` must accept several pets.** It takes a single `petId` today; the owner may
book two pets in one request, so it takes a **list**. One booking, one room, N occupant rows.

**Capacity** lives in Pet Boarding Settings, default 7, checked against **active**
occupants — a booking that held 7 with one departed must accept another.

**Migration:** all 58 records convert mechanically. Each becomes a booking with exactly
one occupant (`pet` → occupant, `check_in`/`check_out` → `joined_at`/`departed_at`) and one
room stint. No ambiguity — every existing record genuinely *is* a one-pet booking.

**`pet` stays populated on the booking but unread** through Stages 2–4, dropped in
Stage 5. Twenty-five read sites across nine modules on a submittable doctype with 30
submitted records is not a single release.

**Concurrency:** the capacity check is read-decide-write. Two staff adding pets
simultaneously can both see 6 and both insert. It needs the same advisory-lock discipline
as `_service_room_lock`.

### Stage 3 — pricing

- Room-stay charge becomes **one row per occupant**, `qty` from that occupant's own
  joined/departed window
- Rate resolved from the catalogue per §3
- `_ensure_room_stay_billable_item` changes from a single-row upsert into a **per-occupant
  reconciliation**
- Deprecate the boarding items in Pet Boarding Settings

### Stage 4 — the medication rule

Medications ordered **through the boarding** are included in the medical rate. Those
prescribed **on the linked visit** are charged normally.

**Create the row, exclude it at invoicing.** Not "never create", not "create at zero":

- **Never create** is disqualified — the billable row *is* the dispensing record.
  `dispense_medication` finds its row through `_find_boarding_billable_row` and writes
  `dispensed_qty`, `dispensed_by`, `dispensed_at`, `dispense_status` onto it. Suppressing
  the row destroys a clinical control
- **Create at zero** destroys the true cost — you lose any way to know what the inclusive
  rate absorbed, which is the number that tells you whether the price is right. It also
  makes an included medication indistinguishable from an unpriced-item bug

**New `Included` status on Pet Billable Item**, wired into three exclusion points and
deliberately kept out of a fourth:

| Site | Change |
|---|---|
| `_build_sales_invoice_items:2411` | skip `Included` alongside `Cancelled` |
| `_compute_totals:145` | skip it, or `total_cost` and `balance` overstate what's owed |
| `_normalize_billable_item_row` | accept the new status |
| `dispense_medication:1414` | **must not block on it** — `Included` rows stay dispensable |

Do **not** reuse `Cancelled` — it breaks the dispense guard and misrepresents a medication
that was actually given.

Applies only when the occupant's type is medical.

**Also:** add a comment at `ORDER_ITEM_TYPES` (`order_billing.py:59-64`) recording that
Medication's absence is now load-bearing, so a future change adding a Medication order
type doesn't quietly route around the inclusive rule.

### Stage 5 — death, departure, transfer

**Death — the sharpest consequence of the settled rule.** Today `_close_boarding` sets
`check_out` and `record_status = "Checked Out"`, then `_settle_boarding_billing` raises
the invoice. Under the new rule that invoice would **permanently block checkout of the
remaining pets**, because `check_out_boarding` refuses to run once `sales_invoice` is set
(`:781`). A hard failure, not cosmetic.

Split on **is this the last active occupant**:

- **Occupants remain:** close the occupant row, run `_close_open_clinical_docs(pet, …)`
  and `_cancel_orders(…)` — both already pet-scoped — mark the pet deceased. Do **not**
  touch `check_out`, `record_status`, `billing_status`, `sales_invoice`, or the room
- **Last occupant:** today's path is right — set check-out, close, settle

The deceased pet's already-performed clinical charges stay and bill normally. Open orders
are cancelled by the existing cascade.

**Tell the front desk:** today a death produces a settlement document immediately. Under
the new rule the guardian gets nothing until the booking ends.

**Individual departure** — a per-occupant action, **not** the checkout endpoint. **The
last occupant cannot depart individually**; staff must check the booking out. Without that
rule the room stays blocked and `_compute_stay_duration` keeps accruing against
`now_datetime()`.

**Transfer** — per pet, whole-booking not required. A room stint closes and another opens.
No financial effect. Two things need care:

- **Release and claim must be atomic**, or two bookings can each believe they hold the same
  room, invisibly — `get_active_boarding_for_room` uses `limit_page_length=1`, so it returns
  *a* booking, not *the* booking
- **Occupancy is per pet.** The vacated room is only free once **no pet remains in it**, and
  the destination must be empty or already hold this guardian. The check is one guardian per
  room, never one booking per room — a transferring booking legitimately holds both at once

### Stage 6 — drop `pet` from the booking

Once every read site is off it.

---

## 5. Riskiest part

**Room transfer**, and not for the obvious reason.

It is the only change touching concurrency and money attribution at once. Today's invariant
is "one active booking per room", enforced by `_validate_single_active_room_boarding` firing
on save plus `_service_room_lock` around mutating endpoints. **That invariant is being
replaced, not merely re-implemented**: it becomes *one guardian per room*, evaluated over
occupants, because a transferring booking holds two rooms and a shared room holds several
pets. Rewriting an exclusivity check while it is the only thing standing between two
guardians and the same room is what makes this the risky piece. A double-claim is
**invisible to every occupancy read in the system** and surfaces as two guardians' animals
in one room at the facility.

**Branch attribution** is frozen at insert by design — and `hooks.py:343-355` records that
this was reverted once, because a mis-attributed invoice made Frappe refuse the insert and
blocked checkout entirely. A cross-branch transfer puts the room stints in disagreement
with the booking's frozen branch. **That exact failure has already cost a production
revert.**

### What must be proven before transfer ships

1. A concurrency test that genuinely races two transfers at the same destination and
   asserts exactly one wins. If it can't be made to race reliably, prove instead that the
   lock spans the whole read-decide-write window
2. The invariant as a **data query**, in CI and on a schedule: active bookings grouped by
   room, `having count > 1`, must be empty. The application check cannot see violations it
   didn't cause
3. **Cross-branch transfer decided explicitly before any code.** If attribution stays
   frozen, a test that a transferred booking still invoices to its original branch and
   still checks out
4. For the cascade: an idempotency test under occupant scoping — re-running a death on one
   occupant must not close a second

---

## 6. Open items

- **Species beyond the eight.** `animal_type` is a fixed Select in `pet.json`. Bird, Fish,
  Reptile, Horse, Rabbit and Other already exist, so bird boarding is a catalogue entry.
  A ferret is a schema change. Acceptable for now; note it
- **Cross-branch transfer** — see §5.3
- **Does the medical rate absorb anything beyond medications?** Currently no
- **A pet joining on day 6 pays from day 6.** Correct under per-pet pricing, but tell the
  desk before they discover it

---

## 7. Ground truth as of this plan

| Measure | Value |
|---|---|
| Pet Boarding records | 58 |
| Travel / Treatment | 23 / 35 |
| Boardings with a visit link | 21 — **all** are Treatment; no Travel boarding has one |
| Distinct guardians / pets / rooms | 30 / 38 / 24 |
| Service Rooms | 37 (35 active) |
| Room Stay billable rows | 49 |
| Medication billable rows | 1 |
| Boarding-sourced clinical orders | 5 |
| Pets that have ever boarded | Cat 31 pets / 40 stays · Dog 8 pets / 18 stays. Nothing else |
| Guardians holding 8+ pets | 5 — cannot fit one booking at a cap of 7 |
| Genuine concurrent multi-pet stays in history | **1** (BRD-00040 + BRD-00042, and it used two rooms) |

Historic dog stays were billed at the cat rate — one price served every species.
**Owner decision: leave historic records alone.**
