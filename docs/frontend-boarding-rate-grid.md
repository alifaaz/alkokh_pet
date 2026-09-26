# Boarding rates: a Cat/Dog × Travel/Treatment grid, and per-pet estimate rates

Two bugs, one cause. The boarding screens read the **deprecated** Pet Boarding Settings
fields `travel_boarding_item` / `treatment_boarding_item`. Those point at the **Cat**
items. The invoice has not used them for a long time: it prices from the CareService
boarding catalogue by the pet's `animal_type` and the boarding type.

| Bug | What staff see |
| --- | --- |
| **Every dog is estimated at the cat rate** | `useBoardingDailyRates` keys the rate by boarding type only. BRD-00237 (a dog, 4 days Travel) shows 4 × 15,000 = 60,000 and "15,000 in credit". The invoice bills 4 × 25,000 = 100,000, so 25,000 is owed. |
| **A rate edited in Settings never reaches an invoice** | `BoardingSettingsTab` writes an Item Price on those items. The invoice reads the catalogue row's `default_price`. |

After this change there is one place a boarding rate lives, and both screens read it.

Backend: `pet_app/api/healthcare/boarding_rates.py` and
`boarding.py::_occupant_daily_rate`. Tests: `pet_app/tests/test_boarding_rates.py`.

---

## 1. Backend contract

### `get_boarding_detail`: each roster occupant gains two keys

| Key | Meaning |
| --- | --- |
| `daily_rate` | What one day of **this pet's** stay bills at. It is the pet's priced Room Stay row if there is one (check-out never reprices it, so staff corrections are included), otherwise the catalogue rate for its species and boarding type. `null` = unknown (catalogue gap), **never 0**. |
| `daily_rate_item` | The item that rate belongs to, e.g. `Dog\Regular Boarding`. |

Only the booking roster (`occupants` on the detail read) has these keys. Room-hub occupant
rows do **not**, so read "key absent" as "old backend or hub", not as "unknown".

### `pet_app.api.healthcare.boarding_rates.get_boarding_rate_grid` (GET)

This is the standard `{ok, data}` envelope. It needs read on Pet Boarding Settings.

```json
{
  "category": "CategoryCareServices-0016",
  "boarding_types": ["Travel", "Treatment"],
  "species": ["Cat", "Dog"],
  "fallback_animal_type": "All",
  "rates": [
    {"template": "CareService-00016", "service_name": "Cat\\Regular Boarding",
     "item_code": "Travel Boarding", "animal_type": "Cat", "boarding_type": "Travel",
     "rate": 15000, "modified": "2026-07-15 14:06:08.720723"}
  ],
  "issues": []
}
```

- `species` is sorted, with the `"All"` row last if one exists. An **All** row is the
  rate for any species without its own row (Bird, Rabbit, Horse). Label it "Other
  animals".
- `rate: null` means not priced.
- `issues[]` lists catalogue faults to show, not hide:
  - `NO_PRICE`, `NO_ITEM`: a row with no price or no item.
  - `DUPLICATE`: two rows for the same species and type. Check-out is **blocked** for that
    pair until one is disabled.

### `pet_app.api.healthcare.boarding_rates.set_boarding_rates` (POST)

```json
{ "rates": [ {"template": "CareService-00020", "rate": 27000, "modified": "<from the grid>"} ] }
```

- It needs write on Pet Boarding Settings, the same gate the tab already uses.
- **All or nothing.** It is refused, with nothing written, when any entry is:
  - an unknown or disabled template,
  - a rate that is not positive,
  - a template sent twice,
  - or a `modified` that no longer matches ("changed by someone else. Reload").
- Show `errors[0].message` verbatim.
- On success it returns the **fresh grid**. Replace your state with it, including the new
  `modified` values.
- The Standard Selling Item Price is kept equal to the rate automatically.
- **Stays already checked in keep their rate.** Only rows priced afterwards use the new one.

---

## 2. `src/api/healthcareApi.utils.ts`: `mapBoardingOccupant`

```ts
    /**
     * The server's per-pet daily rate: this animal's species and boarding type, or its
     * staff-corrected room row. `undefined` = key absent (older backend / hub row);
     * `null` = the catalogue cannot price it. Never coerce either to 0.
     */
    dailyRate: raw && "daily_rate" in raw
      ? (raw.daily_rate === null || !Number.isFinite(Number(raw.daily_rate)) ? null : Number(raw.daily_rate))
      : undefined,
    dailyRateItem: toNullableString(raw, ["daily_rate_item"]),
```

(This file has no `toNullableNumber` helper, hence the inline parse.)

Add `dailyRate?: number | null` and `dailyRateItem?: string | null` to `BoardingOccupant`.

## 3. `src/utils/boardingEstimate.ts`: price each line from the pet

In `buildBoardingEstimate`, replace the one line that reads `ratesByType`:

```ts
    // ⚠️ PER PET, NOT PER BOARDING TYPE. `ratesByType` came from the deprecated settings
    // items - the CAT items - and quoted every dog at the cat rate (BRD-00237). The server
    // now sends the rate it will bill for this animal. The type table is only a fallback for
    // a backend that predates `daily_rate`; a present-but-null rate stays unknown.
    const ratePerDay = occupant.dailyRate !== undefined
      ? finiteOrNull(occupant.dailyRate)
      : boardingType ? finiteOrNull(ratesByType[boardingType]) : null;
```

`unpricedTypes` keeps working as it is. Everything else in the module is unchanged: the
estimate still never enters a payload.

Once every site runs the new backend, delete the `ratesByType` fallback and the rate-loading
half of `useBoardingDailyRates`. Keep `readBoardingSlotPrice` only if something else still
uses it.

## 4. `src/components/healthcare/settings/BoardingSettingsTab.vue`: the grid

Replace the two item pickers and two rate fields with a grid. Keep the **Default Boarding
Type** select and its save through `SavePetBoardingSettings` exactly as today.

```
Boarding rates (per day)      Travel        Treatment
Cat                           [ 15,000 ]    [ 25,000 ]
Dog                           [ 25,000 ]    [ 35,000 ]
Other animals (All)           [   —    ]    [   —    ]      ← only if an "All" row exists
```

- Rows come from `species`, columns from `boarding_types`. Each cell is the `rates[]` entry
  matching `(animal_type, boarding_type)`.
- Each cell is a `PriceInputField`, with `item_code` as a small caption under it. A missing
  entry shows **"Not configured"** (read-only), never an input showing 0.
- Dirty tracking is per cell against the value last loaded. **Save** sends only changed cells
  `{template, rate, modified}` in **one** `set_boarding_rates` call, then:
  - applies the returned grid,
  - calls `invalidateBoardingDailyRates()`, while the fallback still exists.
- Show `issues[]` above the grid as a warning list.
- A note under the grid: *"Changes apply to new stays. Pets already checked in keep their
  rate."* And: *"To add another species, add a boarding row in the CareService catalogue."*
- Remove the item pickers, `UpsertItemSellingPrice` and the travel/treatment rate refs from
  this tab. They wrote to the wrong place.

New file `src/api/boardingRatesApi.ts` holds the two calls. Use the same `{ok, data}`
unwrapping as `boardingSettingsApi.ts`.

## 5. Strings (en / ar)

```
alkokh.healthcare.settings.boarding.rates.title
  en: "Boarding rates (per day)"
  ar: "أسعار الإيواء (لليوم)"
alkokh.healthcare.settings.boarding.rates.otherAnimals
  en: "Other animals"
  ar: "حيوانات أخرى"
alkokh.healthcare.settings.boarding.rates.notConfigured
  en: "Not configured"
  ar: "غير مُعدّ"
alkokh.healthcare.settings.boarding.rates.appliesToNewStays
  en: "Changes apply to new stays. Pets already checked in keep their rate."
  ar: "التغييرات تسري على الإقامات الجديدة. الحيوانات المسجلة دخولها تحتفظ بسعرها."
alkokh.healthcare.settings.boarding.rates.addSpecies
  en: "To add another species, add a boarding row in the care-service catalogue."
  ar: "لإضافة نوع حيوان آخر، أضف سطر إيواء في كتالوج الخدمات."
```

Species names use the existing animal-type translations.

## 6. Deploy order

**Backend first.** Everything is additive: an old frontend ignores `daily_rate`, and the
old settings fields are still returned. A `bench restart` is enough; no migrate.

## 7. How to check it

1. **The dog estimate.** Open BRD-00237's check-out. The panel shows 25,000/day
   (4 × 25,000 = 100,000), 75,000 paid and **25,000 due**. That is what check-out bills.
2. **A cat is unchanged.** A cat on Travel still shows 15,000/day.
3. **The grid.** Settings shows Cat/Dog × Travel/Treatment at 15k / 25k / 25k / 35k.
4. **A saved rate is billed.** Change Dog Travel to 26,000 and save. Check in a new dog:
   its room row is 26,000 and the estimate shows 26,000. A dog already checked in stays
   at 25,000. Put the rate back.
5. **Stale save.** Open Settings in two tabs, save a rate in one, then save in the other.
   You get the "changed by someone else" message and nothing is written.
6. **Other animals.** If there's no "All" row, no "Other animals" line appears, and a
   rabbit's estimate line shows "—" rather than 0.
