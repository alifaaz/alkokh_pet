# Vet Visit Reference

Last verified: 2026-07-19 against `frappe.localhost`.

This is a backend reference for the `Vet Visit` DocType only. It documents the schema, child tables, link graph, lifecycle, write paths, workbench payload, and known traps. Source citations use repository file paths plus line numbers. Live-data counts were collected read-only with `bench --site frappe.localhost execute frappe.db.sql` on 2026-07-19.

## 1. What A Vet Visit Is

`Vet Visit` is the clinical encounter record. A case sheet captures intake before the doctor starts; converting that case sheet or appointment creates a visit, copies the patient/guardian/customer/weight context, assigns a doctor, and moves the workflow into `In Progress` consultation state. The conversion paths are in `pet_app/pet_app/doctype/vet_case_sheet/vet_case_sheet.py:232` and `pet_app/api/workspace.py:2391`; both create a `Vet Visit` with `status = "In Progress"` and a visit type derived from case sheet/payload context (`pet_app/pet_app/doctype/vet_case_sheet/vet_case_sheet.py:317`, `pet_app/api/workspace.py:2412`).

The visit sits between the case sheet, the pet's ongoing care episode, and billing. A visit can be linked to a `Pet Care Episode` through the custom `care_episode` field created by the medical-core patch (`pet_app/patches/p0_5_medical_core_schema.py:14`), and doctor case choice routes a visit into wellness, continuing an active case, or opening a new case (`pet_app/utils/medical_profile.py:86`). Billing is generated from visit `billable_items`; completion creates a draft `Sales Invoice`, marks billables as billed, stores the invoice link on the visit, and then syncs the medical profile/episode (`pet_app/api/workspace.py:1769`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:899`, `pet_app/utils/medical_profile.py:332`).

The visit is not submitted (`is_submittable = 0` in live meta and the DocType JSON), but it is heavily locked by status, billing, active boarding, and linked clinical records. Controller validation enforces workflow transitions, completion requirements, billable integrity, prescription/billable synchronization, and case-sheet uniqueness (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:87`).

### Live Counts

As of 2026-07-19, there are 155 live `Vet Visit` rows. Status counts: `Completed` 58, `Follow-up Needed` 11, `In Progress` 86, `Draft` 0, `Cancelled` 0. These status values come from the DocType select options (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:97`).

As of 2026-07-19, linked `Pet Care Episode` status counts are: `Cancelled` 15, `Deceased` 14, `Follow-up Scheduled` 3, `Open` 1, `Pending Diagnostics` 2, `Resolved` 24, `Under Diagnosis` 4, `Under Treatment` 10. Episode statuses are defined on `Pet Care Episode` (`pet_app/pet_app/doctype/pet_care_episode/pet_care_episode.json:96`).

## 2. Every Field On Vet Visit

Live meta check on 2026-07-19 confirms these non-layout fields exist on `Vet Visit`: `naming_series`, `status`, `outcome`, `death_during_visit`, `death_record`, `priority`, `billed`, `visit_datetime`, `sales_invoice`, `total_billable_amount`, `billing_status`, `paid_amount`, `balance_amount`, `case_sheet`, `care_episode`, `doctor_case_choice`, `case_choice_by`, `case_choice_at`, `case_choice_note`, `appointment`, `guardian`, `guardian_name`, `customer`, `animal_patient`, `doctor`, `primary_practitioner`, `doctor_name`, `visit_type`, `weight`, `temperature`, `heart_rate`, `respiratory_rate`, `vital_signs`, `examination_notes`, `case_summary`, `intake_summary`, `overview`, `illness`, `diagnosis`, `assessment`, `differential_diagnosis`, `diagnoses`, `care_services`, `treatment_plan`, `doctor_notes`, `instructions`, `prescribed_medications`, `orders`, `billable_items`, `follow_up_required`, `follow_up_status`, `follow_up_contacted_at`, `follow_up_contacted_by`, `follow_up_reason`, `follow_up_contact_note`, `missed_reason`, `follow_up_preferred_date`, `follow_up_date`, `follow_up_appointment_id`, `follow_up_visit_id`, `follow_up_of_visit_id`, `consult_requests`, `referrals`, `last_synced_at`, `idempotency_key`, `client_request_id`. The base JSON owns the built-in fields (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:87`), and the custom fields are added by patches (`pet_app/patches/p0_5_medical_core_schema.py:14`, `pet_app/patches/visit_case_choice_schema.py:14`, `pet_app/patches/p2_product_growth_schema.py:558`).

### State, Naming, And Billing

| Field | Type / Options | Purpose | Writes | Reads | Source / status |
|---|---|---|---|---|---|
| `naming_series` | `Select`; `VVT-.YYYY.-.#####` | Names visits with the `VVT` yearly sequence. | Frappe autoname uses the field because the DocType naming rule is by fieldname (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:534`). | Everywhere that loads a visit by name; workbench requires a visit name (`pet_app/api/visit_workbench.py:37`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:87`). |
| `status` | `Select`; `Draft`, `In Progress`, `Completed`, `Cancelled`, `Follow-up Needed` | Main visit lifecycle state. | Defaults in controller, start/conversion paths set `In Progress`, completion transitions to `Completed`, death cascade may set `Cancelled` (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:119`, `pet_app/api/workspace.py:2412`, `pet_app/api/workspace.py:1777`, `pet_app/utils/boarding_death_cascade.py:358`). | Clinical-state transitions, action permissions, workbench permissions, dashboards, follow-up/task labels (`pet_app/workflows/clinical_state.py:16`, `pet_app/workflows/clinical_state.py:68`, `pet_app/api/visit_workbench.py:257`, `pet_app/api/dashboard.py:611`, `pet_app/api/workspace.py:3129`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:97`). Live counts above. |
| `outcome` | `Select`; blank, `Recovered`, `Improved`, `Stable`, `Referred`, `Follow-up Required`, `Death`, `Euthanasia`, `Unknown` | Captures clinical close/completion outcome. | Added by medical-core patch; mortality finalization sets `Death`/`Euthanasia`; completion sync reads payload outcome (`pet_app/patches/p0_5_medical_core_schema.py:24`, `pet_app/api/mortality.py:432`, `pet_app/utils/medical_profile.py:332`). | Completion sync maps `Referred` to active `Referred`, death/euthanasia to `Deceased`, follow-up to active follow-up/monitoring, and ordinary completion leaves episode status unchanged (`pet_app/utils/medical_profile.py:335`). | Custom field. Live meta options match `p0_5`, not the older `p2` options (`pet_app/patches/p2_product_growth_schema.py:572`). Live non-empty count: 2. |
| `death_during_visit` | `Check` | Marks that a death record came from this visit. | Mortality `_link_source` sets it for source `Vet Visit` (`pet_app/api/mortality.py:428`). | Raw visit/workbench payload returns it; death cascade/source logic relies on the `death_record` link more than this flag (`pet_app/api/visit_workbench.py:157`). | Custom field (`pet_app/patches/p0_5_medical_core_schema.py:31`). Live count checked: 7 set. |
| `death_record` | `Link` to `Pet Death Record` | Links the visit to the mortality record. | Mortality `_link_source` writes it (`pet_app/api/mortality.py:432`). | Raw visit/workbench payload and mortality flows use the link (`pet_app/api/visit_workbench.py:157`, `pet_app/api/mortality.py:428`). | Custom field (`pet_app/patches/p0_5_medical_core_schema.py:37`). Live count checked: 7 set. |
| `priority` | `Select`; `Low`, `Normal`, `Urgent`, `Emergency` | Visit triage priority. | Default `Normal`; conversion copies payload/case-sheet priority; start consultation backfills from case sheet if empty (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:126`, `pet_app/api/workspace.py:2419`, `pet_app/api/workspace.py:1600`). | Workspace summaries and conversion/default logic read it (`pet_app/api/workspace.py:1136`, `pet_app/api/workspace.py:1968`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:106`). |
| `billed` | `Check` | Boolean lock once invoice is created. | `_mark_visit_invoiced` sets it; Sales Invoice cancel hook clears billing state through visit billing utilities (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:899`, `pet_app/utils/visit_billing.py:198`). | Billing lock, workbench permissions, and clinical-state billed checks (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:334`, `pet_app/api/visit_workbench.py:257`, `pet_app/workflows/clinical_state.py:197`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:115`). Live count checked: 58 set. |
| `visit_datetime` | `Datetime` | Timestamp for the encounter. | Controller defaults to now; conversion/start paths set now or carry context (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:119`, `pet_app/pet_app/doctype/vet_case_sheet/vet_case_sheet.py:317`). | Workbench summary, pharmacy pending dispense order, reports/performance use it (`pet_app/api/workspace.py:1138`, `pet_app/api/pharmacy.py:59`, `pet_app/api/user_performance.py:617`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:127`). |
| `sales_invoice` | `Link` to `Sales Invoice` | Stores the draft invoice created from the visit. | `_mark_visit_invoiced` writes it; Sales Invoice cancel clears it (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:904`, `pet_app/utils/visit_billing.py:198`). | Billing snapshot loads invoice fields from it; billing lock blocks save if set (`pet_app/api/workspace.py:1567`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:348`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:134`). Live count checked: 58 set. |
| `total_billable_amount` | `Currency` | Cached total of visit billables. | Controller recalculates via `apply_billable_item_amounts`; `_mark_visit_invoiced` stores invoice total (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:375`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:906`). | Billing snapshot uses invoice total if invoice exists, otherwise this cached total (`pet_app/api/workspace.py:1577`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:146`). Live non-zero count checked: 104. |
| `billing_status` | `Select`; `Unbilled`, `Draft Invoice`, `Partially Paid`, `Paid`, `Follow-up`, `Cancelled` | Cached billing state; mostly derived by billing snapshot, with one explicit follow-up setter. | Default `Unbilled`; `_mark_follow_up` sets `Follow-up` on linked visits (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:153`, `pet_app/api/workspace.py:2528`). | Billing snapshot returns computed status and includes visit billing status fallback (`pet_app/api/workspace.py:1583`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:153`). Live non-empty count checked: 95. |
| `paid_amount` | `Currency` | Intended payment cache, but current backend billing derives paid amount from `Sales Invoice`, not this field. | No `Vet Visit` writer found in `pet_app` on 2026-07-19; live non-zero count checked: 0. | Workbench billing reads invoice `paid_amount`, not visit `paid_amount` (`pet_app/api/workspace.py:1573`). Raw visit payload still includes the field (`pet_app/api/visit_workbench.py:157`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:160`). Legacy/dead for behavior. |
| `balance_amount` | `Currency` | Intended balance cache, but current backend derives balance from `Sales Invoice.outstanding_amount` or total. | No `Vet Visit` writer found in `pet_app` on 2026-07-19; live non-zero count checked: 0. | Workbench billing computes balance from invoice/total, not this field (`pet_app/api/workspace.py:1577`). Raw visit payload still includes the field (`pet_app/api/visit_workbench.py:157`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:166`). Legacy/dead for behavior. |

### Identity And Routing

| Field | Type / Options | Purpose | Writes | Reads | Source / status |
|---|---|---|---|---|---|
| `case_sheet` | `Link` to `Vet Case Sheet`, unique | The intake record that produced the visit. | Case-sheet start and workspace conversion set it; controller keeps the reverse `Vet Case Sheet.vet_visit` synced (`pet_app/pet_app/doctype/vet_case_sheet/vet_case_sheet.py:276`, `pet_app/api/workspace.py:2414`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:692`). | Workbench returns linked case sheet; profile sync stores current case sheet; uniqueness validator checks one visit per case sheet (`pet_app/api/visit_workbench.py:53`, `pet_app/utils/medical_profile.py:188`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:205`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:181`). |
| `care_episode` | `Link` to `Pet Care Episode` | Current care case for this visit. | Case-choice logic sets/clears it; follow-up conversion carries it; creation/update episode sync may set it (`pet_app/utils/medical_profile.py:86`, `pet_app/api/workspace.py:1997`, `pet_app/utils/medical_profile.py:49`). | Workbench active episode/case context, profile sync, plan item filters, deletion protection (`pet_app/api/visit_workbench.py:79`, `pet_app/utils/medical_profile.py:145`, `pet_app/api/visit_workbench.py:97`, `pet_app/utils/medical_profile.py:377`). | Custom field (`pet_app/patches/p0_5_medical_core_schema.py:16`). Live count checked: 69 set. |
| `doctor_case_choice` | `Select`; blank, `wellness`, `continue_case`, `new_case` | Records the doctor's explicit case routing choice. | `set_visit_case_choice` writes it through `_stamp_case_choice` (`pet_app/utils/medical_profile.py:86`, `pet_app/utils/medical_profile.py:584`). | Case context decides whether a case choice is required and whether new/continue is allowed (`pet_app/utils/medical_profile.py:145`). | Custom field (`pet_app/patches/visit_case_choice_schema.py:16`). Live count checked: 47 set. |
| `case_choice_by` | `Link` to `User` | Audit user for case choice. | `_stamp_case_choice` writes the session user (`pet_app/utils/medical_profile.py:584`). | No behavioral reader found; raw visit payload returns it (`pet_app/api/visit_workbench.py:157`). | Custom field (`pet_app/patches/visit_case_choice_schema.py:24`). Written-but-not-read for behavior; live count checked: 47. |
| `case_choice_at` | `Datetime` | Audit timestamp for case choice. | `_stamp_case_choice` writes `now_datetime()` (`pet_app/utils/medical_profile.py:589`). | No behavioral reader found; raw visit payload returns it (`pet_app/api/visit_workbench.py:157`). | Custom field (`pet_app/patches/visit_case_choice_schema.py:32`). Written-but-not-read for behavior; live count checked: 47. |
| `case_choice_note` | `Small Text` | Optional note explaining case routing. | `_stamp_case_choice` writes it if supplied (`pet_app/utils/medical_profile.py:591`). | Raw visit payload/case context only; no gating logic found (`pet_app/api/visit_workbench.py:157`, `pet_app/utils/medical_profile.py:145`). | Custom field (`pet_app/patches/visit_case_choice_schema.py:39`). Live count checked: 3. |
| `appointment` | `Link` to `Appointment` | Appointment converted into the visit, if any. | Case-sheet start maps it from case sheet; workspace appointment conversion and follow-up conversion set it (`pet_app/pet_app/doctype/vet_case_sheet/vet_case_sheet.py:282`, `pet_app/api/workspace.py:2425`, `pet_app/api/workspace.py:1995`). | Workspace conversion checks appointment `custom_linked_visit_id`; queue sync can find ticket by appointment (`pet_app/api/workspace.py:2394`, `pet_app/api/workspace.py:2548`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:190`). |
| `guardian` | `Link` to `Guardian` | Pet owner/contact on the visit. | Conversion copies from case sheet/original visit; controller validates identity consistency and immutability (`pet_app/api/workspace.py:2416`, `pet_app/api/workspace.py:1985`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:169`). | Workbench linked guardian, billing/customer derivation, profile sync, permissions/restrictions (`pet_app/api/visit_workbench.py:55`, `pet_app/utils/medical_profile.py:184`, `pet_app/api/workspace.py:484`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:198`). |
| `guardian_name` | `Data`, fetches `guardian.full_name` | Cached display name for the linked guardian. | Frappe fetches it from `Guardian.full_name`; typo patch renamed old `gurdian_name` to this field (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:517`, `pet_app/patches/rename_guardian_typo_fields.py:13`). | Workbench visit payload uses it or falls back to Guardian full name (`pet_app/api/visit_workbench.py:166`). | Base JSON plus rename patch history. Live meta has `guardian_name`; live meta does not have `gurdian_name`. |
| `customer` | `Link` to `Customer`, fetches from guardian | Billing customer. | Conversion resolves from case sheet or creates from guardian; controller validates identity consistency (`pet_app/api/workspace.py:2415`, `pet_app/pet_app/doctype/vet_case_sheet/vet_case_sheet.py:246`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:169`). | Invoice creation requires it; workbench/profile/billing use it (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:849`, `pet_app/api/visit_workbench.py:157`, `pet_app/utils/medical_profile.py:184`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:207`). |
| `animal_patient` | `Link` to `Pet` | The pet being treated. | Conversion copies from case sheet/original visit; controller validates immutability (`pet_app/api/workspace.py:2417`, `pet_app/api/workspace.py:1986`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:243`). | Almost every clinical/profile/episode path reads it; workbench aliases it as `pet` and `pet_id` (`pet_app/utils/medical_profile.py:49`, `pet_app/api/visit_workbench.py:159`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:217`). |
| `doctor` | `Link` to `Healthcare Practitioner` | Current responsible practitioner. | Conversion/start sets it; referral can change it through `set_visit_practitioner`; defaults from `primary_practitioner` when missing (`pet_app/api/workspace.py:2418`, `pet_app/api/visit_referral.py:78`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:129`). | Access control, workbench labels, profile sync, order creation, reports, notifications (`pet_app/api/workspace.py:509`, `pet_app/api/visit_workbench.py:169`, `pet_app/utils/medical_profile.py:190`, `pet_app/api/workspace.py:2094`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:53`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:230`). |
| `primary_practitioner` | `Link` to `Healthcare Practitioner` | Original/primary practitioner slot separate from mutable `doctor`. | Controller copies it into `doctor` if doctor is blank; case-assignment patch backfilled it from doctor (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:129`, `pet_app/patches/case_assignment_schema.py:17`). | Case-team backfill and practitioner utilities look at it for episode teams (`pet_app/patches/case_assignment_schema.py:47`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:237`). |
| `doctor_name` | `Data`, fetches `doctor.practitioner_name` | Cached doctor display name. | Frappe fetches it from doctor; workbench also falls back to practitioner name (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:523`, `pet_app/api/visit_workbench.py:169`). | Raw/workbench display only; no behavioral reader found beyond payload enrichment (`pet_app/api/visit_workbench.py:157`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:523`). |
| `visit_type` | `Select`; `Consultation`, `Follow-up`, `Vaccination`, `Emergency`, `Procedure`, `Recheck` | Classifies the encounter. | Controller defaults `Consultation`; case-sheet start derives from complaint/priority; follow-up conversion sets `Follow-up` (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:126`, `pet_app/pet_app/doctype/vet_case_sheet/vet_case_sheet.py:326`, `pet_app/api/workspace.py:1990`). | Raw/workbench display and reports can read it; no lifecycle gate found (`pet_app/api/visit_workbench.py:157`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:245`). |

### Clinical Scalars And Notes

| Field | Type / Options | Purpose | Writes | Reads | Source / status |
|---|---|---|---|---|---|
| `weight` | `Float` | Snapshot weight on this visit. | Case-sheet/follow-up conversion copies weight; `save_clinical_note` writes it; latest vital row sync overwrites scalar from child vitals (`pet_app/api/workspace.py:2423`, `pet_app/api/workspace.py:1992`, `pet_app/api/workspace.py:1639`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:300`). | Profile vitals snapshot and workbench clinical/vitals read it (`pet_app/utils/medical_profile.py:351`, `pet_app/api/workspace.py:1165`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:252`). |
| `temperature` | `Float` | Latest temperature scalar for this visit. | `save_clinical_note` writes it; latest child vital row sync writes it (`pet_app/api/workspace.py:1640`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:314`). | Profile vitals snapshot and workspace aggregate read it (`pet_app/utils/medical_profile.py:351`, `pet_app/api/workspace.py:1167`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:266`). |
| `heart_rate` | `Int` | Latest heart-rate scalar for this visit. | `save_clinical_note` writes it; latest child vital row sync writes it (`pet_app/api/workspace.py:1641`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:316`). | Profile vitals snapshot and workspace aggregate read it (`pet_app/utils/medical_profile.py:351`, `pet_app/api/workspace.py:1168`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:271`). |
| `respiratory_rate` | `Int` | Latest respiratory-rate scalar for this visit. | `save_clinical_note` writes it; latest child vital row sync writes it (`pet_app/api/workspace.py:1642`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:318`). | Profile vitals snapshot and workspace aggregate read it (`pet_app/utils/medical_profile.py:351`, `pet_app/api/workspace.py:1169`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:276`). |
| `vital_signs` | `Table` to `Vet Visit Vital Sign` | Time-series vitals rows for this visit; canonical for repeated readings. | Vitals API appends/updates rows; controller copies the latest row into scalar fields (`pet_app/api/vitals.py:34`, `pet_app/api/vitals.py:59`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:300`). | Vitals API list, workspace aggregate, and raw workbench visit payload read it (`pet_app/api/vitals.py:96`, `pet_app/api/workspace.py:1278`, `pet_app/api/visit_workbench.py:157`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:281`). Live child count: 75 rows on 49 visits. |
| `examination_notes` | `Text Editor` | Doctor examination text. | `save_clinical_note` writes it from `examination` or `examination_notes` aliases (`pet_app/api/workspace.py:1624`). | Workspace aggregate exposes it as `clinical.examination`; raw workbench exposes field as-is (`pet_app/api/workspace.py:1151`, `pet_app/api/visit_workbench.py:157`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:291`). |
| `case_summary` | `Small Text`, read-only | Intake/case-sheet summary copied onto the visit. | Case-sheet mapping and conversion defaults write it (`pet_app/pet_app/doctype/vet_case_sheet/vet_case_sheet.py:317`, `pet_app/api/workspace.py:2422`). | Workspace aggregate uses it as intake fallback and profile sync uses it in active problem summary (`pet_app/api/workspace.py:1149`, `pet_app/utils/medical_profile.py:208`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:305`). |
| `intake_summary` | `Text Editor` | Editable intake narrative on the visit. | `save_clinical_note` writes it (`pet_app/api/workspace.py:1622`). | Workspace aggregate returns it, falling back to `case_summary` (`pet_app/api/workspace.py:1149`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:311`). |
| `overview` | `Text Editor` | Visit overview narrative. | `save_clinical_note` writes it (`pet_app/api/workspace.py:1623`). | Workspace aggregate returns it (`pet_app/api/workspace.py:1150`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:316`). |
| `illness` | `Select`; condition categories | Structured illness/condition category required for completion. | `save_clinical_note` writes it (`pet_app/api/workspace.py:1633`). | Completion validators require it; workbench summary title can include it (`pet_app/api/workspace.py:1800`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:257`, `pet_app/api/workspace.py:1134`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:321`). |
| `diagnosis` | `Text Editor` | Primary diagnosis text. | `save_clinical_note` writes it; `save_diagnoses` sets it from primary diagnosis row; completion can derive it from child diagnoses (`pet_app/api/workspace.py:1627`, `pet_app/api/workspace.py:1677`, `pet_app/api/workspace.py:1761`). | Completion validators require it; diagnosis sync pushes it to episode/profile (`pet_app/api/workspace.py:1803`, `pet_app/utils/medical_profile.py:217`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:327`). |
| `assessment` | `Text Editor` | Clinical assessment text separate from diagnosis. | `save_clinical_note` writes it when field exists (`pet_app/api/workspace.py:1626`). | Workspace aggregate returns `assessment` or falls back to `diagnosis` (`pet_app/api/workspace.py:1152`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:332`). |
| `differential_diagnosis` | `Small Text` | Differential diagnosis note. | `save_clinical_note` writes it (`pet_app/api/workspace.py:1628`). | Workspace aggregate returns it (`pet_app/api/workspace.py:1159`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:341`). |
| `diagnoses` | `Table` to `Visit Diagnosis` | Structured diagnosis rows. | `save_diagnoses` replaces rows from payload and then syncs episode/profile diagnosis (`pet_app/api/workspace.py:1655`, `pet_app/utils/medical_profile.py:217`). | Workbench/aggregate returns rows, with scalar fallback if rows absent (`pet_app/api/visit_workbench.py:61`, `pet_app/api/workspace.py:1246`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:346`). Live child count: 114 rows on 51 visits. |
| `care_services` | `Table` to `custom services` | Selected non-lab/non-imaging services billed from the visit. | Direct visit saves can add/remove/update rows; controller syncs them to billables (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:539`). | Controller billable sync and deletion blockers read it (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:542`, `pet_app/utils/medical_profile.py:627`). | Base JSON points to a child DocType literally named `custom services` (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:352`). Live child count: 15 rows on 10 visits. |
| `treatment_plan` | `Text Editor` | Free-text treatment plan. | `save_clinical_note` writes it from `plan` or `treatment_plan`; controller/profile sync pushes it to episode treatment summary (`pet_app/api/workspace.py:1629`, `pet_app/utils/medical_profile.py:273`). | Workbench/aggregate and episode/profile sync read it (`pet_app/api/workspace.py:1153`, `pet_app/utils/medical_profile.py:281`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:367`). |
| `doctor_notes` | `Text Editor` | Clinical note/instructions fallback required for completion. | `save_clinical_note` writes it; completion copies `instructions` into it if empty (`pet_app/api/workspace.py:1632`, `pet_app/api/workspace.py:1765`). | Completion validators require it; workspace aggregate returns it (`pet_app/api/workspace.py:1805`, `pet_app/api/workspace.py:1155`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:372`). |
| `instructions` | `Text Editor` | Owner/clinical instructions. | `save_clinical_note` writes it from `instructions`, falling back to `doctor_notes` if the field did not exist (`pet_app/api/workspace.py:1631`). | Workspace aggregate returns it or falls back to `doctor_notes`; completion can copy it into `doctor_notes` (`pet_app/api/workspace.py:1154`, `pet_app/api/workspace.py:1765`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:377`). |

### Orders, Medications, Billables, Consults, Referrals

| Field | Type / Options | Purpose | Writes | Reads | Source / status |
|---|---|---|---|---|---|
| `prescribed_medications` | `Table` to `Vet Visit Medication Item` | Medication prescription rows embedded in the visit. | Direct visit save can add rows; controller fills dose-option defaults, audits qty/rate changes, syncs medication billables; workspace can cancel rows; pharmacy can dispense/return rows (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:378`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:439`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:483`, `pet_app/api/workspace.py:1811`, `pet_app/api/pharmacy.py:76`). | Workbench medications list, billing invoice item creation, treatment sync, pharmacy pending dispense (`pet_app/api/visit_workbench.py:192`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:911`, `pet_app/utils/medical_profile.py:273`, `pet_app/api/pharmacy.py:18`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:391`). Live child count: 110 rows on 84 visits. |
| `orders` | `Table` to `Visit Order` | Embedded order rows for labs, imaging/radiology, services, procedures, medication/other. | `create_orders` appends/upserts rows and creates linked records; linked records update order status (`pet_app/api/workspace.py:1686`, `pet_app/api/workspace.py:2074`, `pet_app/api/workspace.py:2533`). | Workbench orders, pending-orders profile sync, order transition validator (`pet_app/api/visit_workbench.py:62`, `pet_app/utils/medical_profile.py:246`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:273`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:397`). Live child count: 92 rows on 44 visits. |
| `billable_items` | `Table` to `Pet Billable Item` | Embedded billing rows that become Sales Invoice items. | Controller auto-upserts medication/service billables; visit-billing utilities add/cancel linked billables; invoice marking sets non-cancelled rows to `Billed` (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:483`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:539`, `pet_app/utils/visit_billing.py:88`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:899`). | Invoice item creation, billing snapshot, workbench billables, billed-row lock (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:911`, `pet_app/api/workspace.py:1567`, `pet_app/api/visit_workbench.py:65`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:615`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:403`). Live child count: 200 rows on 105 visits. |
| `consult_requests` | `Table` to `Visit Consult Request` | Embedded consult request rows. | Workspace `request_consult` appends rows; `complete_consult` updates status/note/completed time (`pet_app/api/workspace.py:2020`, `pet_app/api/workspace.py:2051`). | Workbench returns them; access control lets requested doctors access active consult visits (`pet_app/api/visit_workbench.py:68`, `pet_app/api/workspace.py:3017`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:504`). Live child count: 0 rows on 0 visits; backend support exists but no live use found. |
| `referrals` | `Table` to `Visit Referral` | Embedded practitioner referral/transfer history. | `create_visit_referral` appends a row and changes visit practitioner (`pet_app/api/visit_referral.py:27`, `pet_app/api/visit_referral.py:68`). | Workbench returns sorted referral payload; `can_refer_visit` gates permission (`pet_app/api/visit_workbench.py:69`, `pet_app/api/visit_referral.py:102`, `pet_app/api/visit_referral.py:128`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:510`). Live child count: 12 rows on 7 visits. |

### Follow-Up Fields

| Field | Type / Options | Purpose | Writes | Reads | Source / status |
|---|---|---|---|---|---|
| `follow_up_required` | `Check` | Marks that the completed/current visit needs follow-up. | `save_clinical_note`, `request_follow_up`, follow-up contact/reschedule APIs set it (`pet_app/api/workspace.py:1634`, `pet_app/api/workspace.py:1898`, `pet_app/api/follow_up.py:91`, `pet_app/api/follow_up.py:109`). | `sync_completed_visit`, follow-up list filters, workspace aggregate/workbench follow-up payload read it (`pet_app/utils/medical_profile.py:342`, `pet_app/api/follow_up.py:19`, `pet_app/api/workspace.py:1328`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:419`). Live count checked: 18 set. |
| `follow_up_status` | `Select`; `Not Needed`, `Requested`, `Scheduled`, `Contacted`, `Missed`, `Seen`, `Cancelled` | State of the follow-up workflow. | `request_follow_up`, follow-up APIs, follow-up conversion update it (`pet_app/api/workspace.py:1905`, `pet_app/api/follow_up.py:95`, `pet_app/api/follow_up.py:110`, `pet_app/api/workspace.py:2015`). | Follow-up board/list filters, profile sync, workbench follow-up payload (`pet_app/api/follow_up.py:26`, `pet_app/utils/medical_profile.py:315`, `pet_app/api/workspace.py:1328`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:425`). |
| `follow_up_contacted_at` | `Datetime` | Timestamp when follow-up contact occurred. | `mark_follow_up_contacted` writes it (`pet_app/api/follow_up.py:105`). | Follow-up list returns it (`pet_app/api/follow_up.py:55`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:431`). |
| `follow_up_contacted_by` | `Link` to `User` | User who contacted the guardian. | `mark_follow_up_contacted` writes session user (`pet_app/api/follow_up.py:112`). | Follow-up list returns it and enriches display names (`pet_app/api/follow_up.py:56`, `pet_app/api/follow_up.py:152`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:437`). |
| `follow_up_reason` | `Small Text` | Reason for follow-up. | `save_clinical_note` and `request_follow_up` write it (`pet_app/api/workspace.py:1635`, `pet_app/api/workspace.py:1901`). | Follow-up list, workbench follow-up payload, appointment customer details (`pet_app/api/follow_up.py:51`, `pet_app/api/workspace.py:1331`, `pet_app/api/workspace.py:1881`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:445`). |
| `follow_up_contact_note` | `Small Text` | Contact note. | `mark_follow_up_contacted` and `reschedule_follow_up` can write it (`pet_app/api/follow_up.py:114`, `pet_app/api/follow_up.py:96`). | Follow-up list returns it (`pet_app/api/follow_up.py:57`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:450`). |
| `missed_reason` | `Small Text` | Reason follow-up contact/visit was missed. | `mark_follow_up_missed` writes it (`pet_app/api/follow_up.py:115`). | Follow-up list returns it (`pet_app/api/follow_up.py:58`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:455`). |
| `follow_up_preferred_date` | `Date` | Doctor/preferred target date. | `save_clinical_note`, `request_follow_up`, `reschedule_follow_up`, care-plan scheduling write it (`pet_app/api/workspace.py:1636`, `pet_app/api/workspace.py:1903`, `pet_app/api/follow_up.py:93`, `pet_app/api/care_plan.py:1096`). | Follow-up payload prefers it over `follow_up_date`; list returns it (`pet_app/api/workspace.py:1332`, `pet_app/api/follow_up.py:52`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:461`). |
| `follow_up_date` | `Date` | Scheduled follow-up date. | `save_clinical_note`, `request_follow_up`, `reschedule_follow_up` write it (`pet_app/api/workspace.py:1637`, `pet_app/api/workspace.py:1898`, `pet_app/api/follow_up.py:91`). | Follow-up list filters/order, episode/profile sync, completion sync (`pet_app/api/follow_up.py:31`, `pet_app/utils/medical_profile.py:314`, `pet_app/utils/medical_profile.py:342`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:468`). |
| `follow_up_appointment_id` | `Link` to `Appointment` | Appointment scheduled for follow-up. | `request_follow_up` writes it (`pet_app/api/workspace.py:1900`). | Follow-up conversion and payload use it (`pet_app/api/workspace.py:1915`, `pet_app/api/workspace.py:1334`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:474`). Live count checked: 14 set. |
| `follow_up_visit_id` | `Link` to `Vet Visit` | Follow-up visit created from this visit. | Follow-up conversion updates the original visit when a new visit is created (`pet_app/api/workspace.py:2015`). | Conversion idempotency and payload read it (`pet_app/api/workspace.py:1946`, `pet_app/api/workspace.py:1335`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:481`). Live count checked: 0 set. |
| `follow_up_of_visit_id` | `Link` to `Vet Visit` | Reverse pointer on the new follow-up visit to the original visit. | Follow-up conversion sets it (`pet_app/api/workspace.py:1993`). | Conversion idempotency searches by it; payload returns it (`pet_app/api/workspace.py:1951`, `pet_app/api/workspace.py:1336`). | Base JSON (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:488`). Live count checked: 4 set. |

### Offline / Sync Fields

| Field | Type / Options | Purpose | Writes | Reads | Source / status |
|---|---|---|---|---|---|
| `client_request_id` | `Data` | Intended offline client request identity. | Generic offline helper can write `client_request_id` to its own `Offline Request` DocType, but no `Vet Visit` writer was found on 2026-07-19 (`pet_app/utils/offline.py:18`). | No `Vet Visit` reader found; raw visit payload returns it if populated (`pet_app/api/visit_workbench.py:157`). | Custom field from common offline patch (`pet_app/patches/p2_product_growth_schema.py:558`). Live count checked: 0. Legacy/dead for current visit behavior. |
| `idempotency_key` | `Data` | Intended offline/idempotency key on visit. | No `Vet Visit` writer found on 2026-07-19; scheduling uses appointment custom idempotency fields instead (`pet_app/api/scheduling.py:41`, `pet_app/api/scheduling.py:79`). | No `Vet Visit` reader found; raw visit payload returns it if populated (`pet_app/api/visit_workbench.py:157`). | Custom field (`pet_app/patches/p2_product_growth_schema.py:560`). Live count checked: 0. Legacy/dead for current visit behavior. |
| `last_synced_at` | `Datetime` | Intended offline sync timestamp on visit. | Generic offline helper writes its own request record and can stamp a doc passed to it, but no `Vet Visit` endpoint uses it on 2026-07-19 (`pet_app/utils/offline.py:47`, `pet_app/utils/offline.py:58`). | No `Vet Visit` reader found; raw visit payload returns it if populated (`pet_app/api/visit_workbench.py:157`). | Custom field (`pet_app/patches/p2_product_growth_schema.py:561`). Live count checked: 0. Legacy/dead for current visit behavior. |

Layout-only fields are presentation fields in the DocType JSON: tab breaks, section breaks, and column breaks such as `basic_info_tab`, `section_break_basic_info`, `examination_tab`, `assessment_tab`, `plan_tab`, `orders_actions_tab`, `follow_up_tab`, and `consults_tab` (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:122`, `pet_app/pet_app/doctype/vet_visit/vet_visit.json:172`, `pet_app/pet_app/doctype/vet_visit/vet_visit.json:257`, `pet_app/pet_app/doctype/vet_visit/vet_visit.json:296`, `pet_app/pet_app/doctype/vet_visit/vet_visit.json:358`, `pet_app/pet_app/doctype/vet_visit/vet_visit.json:382`, `pet_app/pet_app/doctype/vet_visit/vet_visit.json:409`, `pet_app/pet_app/doctype/vet_visit/vet_visit.json:495`).

## 3. Every Child Table

### `vital_signs` -> `Vet Visit Vital Sign`

Real child DocType: `Vet Visit Vital Sign`, `istable = 1` (`pet_app/pet_app/doctype/vet_visit_vital_sign/vet_visit_vital_sign.json:107`).

Fields:

| Field | Type / Options | Purpose | Source |
|---|---|---|---|
| `recorded_at` | `Datetime` | Reading timestamp. | `pet_app/pet_app/doctype/vet_visit_vital_sign/vet_visit_vital_sign.json:26` |
| `recorded_by` | `Link` to `User` | User who recorded the reading. | `pet_app/pet_app/doctype/vet_visit_vital_sign/vet_visit_vital_sign.json:32` |
| `temperature` | `Float` | Temperature reading. | `pet_app/pet_app/doctype/vet_visit_vital_sign/vet_visit_vital_sign.json:38` |
| `heart_rate` | `Int` | Heart-rate reading. | `pet_app/pet_app/doctype/vet_visit_vital_sign/vet_visit_vital_sign.json:44` |
| `respiratory_rate` | `Int` | Respiratory-rate reading. | `pet_app/pet_app/doctype/vet_visit_vital_sign/vet_visit_vital_sign.json:50` |
| `weight` | `Float` | Weight reading. | `pet_app/pet_app/doctype/vet_visit_vital_sign/vet_visit_vital_sign.json:56` |
| `body_condition_score` | `Select`; `1` to `9` | Body condition score. | `pet_app/pet_app/doctype/vet_visit_vital_sign/vet_visit_vital_sign.json:62` |
| `hydration_status` | `Select`; `Normal`, `Mild Dehydration`, `Moderate Dehydration`, `Severe Dehydration` | Hydration assessment. | `pet_app/pet_app/doctype/vet_visit_vital_sign/vet_visit_vital_sign.json:68` |
| `mucous_membrane` | `Data` | Mucous membrane observation. | `pet_app/pet_app/doctype/vet_visit_vital_sign/vet_visit_vital_sign.json:74` |
| `capillary_refill_time` | `Data` | CRT observation. | `pet_app/pet_app/doctype/vet_visit_vital_sign/vet_visit_vital_sign.json:79` |
| `pain_score` | `Select`; `0` to `10` | Pain score. | `pet_app/pet_app/doctype/vet_visit_vital_sign/vet_visit_vital_sign.json:84` |
| `blood_pressure` | `Data` | Blood pressure text. | `pet_app/pet_app/doctype/vet_visit_vital_sign/vet_visit_vital_sign.json:90` |
| `spo2` | `Percent` | Oxygen saturation. | `pet_app/pet_app/doctype/vet_visit_vital_sign/vet_visit_vital_sign.json:95` |
| `notes` | `Small Text` | Notes for the reading. | `pet_app/pet_app/doctype/vet_visit_vital_sign/vet_visit_vital_sign.json:100` |

Creates/updates/deletes/guards:

- `add_visit_vital` appends a row, saves the visit, and syncs latest profile vitals (`pet_app/api/vitals.py:34`).
- `update_visit_vital` updates only keys in `VITAL_FIELDS`; unknown keys are silently ignored by that loop (`pet_app/api/vitals.py:16`, `pet_app/api/vitals.py:80`).
- Controller `_sync_latest_vital_signs` fills missing `recorded_at`/`recorded_by` and copies the latest row to scalar `temperature`, `heart_rate`, `respiratory_rate`, and `weight` (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:300`).
- Vitals are blocked on `Cancelled` visits and billed visits (`pet_app/api/vitals.py:116`).
- No delete endpoint for individual vital rows was found; direct DocType saves could remove rows unless prevented by generic billed/cancelled locks.

Live data: 75 rows across 49 visits as of 2026-07-19.

### `diagnoses` -> `Visit Diagnosis`

Real child DocType: `Visit Diagnosis`, `istable = 1` (`pet_app/pet_app/doctype/visit_diagnosis/visit_diagnosis.json:51`).

Fields:

| Field | Type / Options | Purpose | Source |
|---|---|---|---|
| `disease` | `Link` to `Disease` | Structured disease link. | `pet_app/pet_app/doctype/visit_diagnosis/visit_diagnosis.json:17` |
| `diagnosis_text` | `Data` / text | Diagnosis free text. | `pet_app/pet_app/doctype/visit_diagnosis/visit_diagnosis.json:24` |
| `is_primary` | `Check` | Marks primary diagnosis. | `pet_app/pet_app/doctype/visit_diagnosis/visit_diagnosis.json:31` |
| `severity` | `Select` | Severity. | `pet_app/pet_app/doctype/visit_diagnosis/visit_diagnosis.json:37` |
| `note` | `Small Text` | Per-diagnosis note. | `pet_app/pet_app/doctype/visit_diagnosis/visit_diagnosis.json:44` |

Creates/updates/deletes/guards:

- Workspace `save_diagnoses` clears the table and appends replacement rows from payload (`pet_app/api/workspace.py:1655`).
- It creates/ensures a `Disease` when needed and writes scalar `visit.diagnosis` from the primary row (`pet_app/api/workspace.py:1663`, `pet_app/api/workspace.py:1677`).
- It calls `sync_diagnoses_from_visit`, which writes episode/profile diagnosis and sets episode status `Under Diagnosis` (`pet_app/utils/medical_profile.py:217`).
- Replacement deletes old child rows by table reset; no special delete guard exists beyond visit action/status/billing gates (`pet_app/workflows/clinical_state.py:72`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:334`).

Live data: 114 rows across 51 visits as of 2026-07-19.

### `orders` -> `Visit Order`

Real child DocType: `Visit Order`, `istable = 1` (`pet_app/pet_app/doctype/visit_order/visit_order.json:110`).

Fields:

| Field | Type / Options | Purpose | Source |
|---|---|---|---|
| `order_id` | `Data` | Stable order identifier used to sync child row with linked records. | `pet_app/pet_app/doctype/visit_order/visit_order.json:24` |
| `kind` | `Select`; `lab`, `radiology`, `service`, `procedure`, `medication`, `other` | Order category. | `pet_app/pet_app/doctype/visit_order/visit_order.json:31` |
| `title` | `Data` | Display title. | `pet_app/pet_app/doctype/visit_order/visit_order.json:39` |
| `item_code` | `Link` to `Item` | Billable item reference. | `pet_app/pet_app/doctype/visit_order/visit_order.json:46` |
| `template_id` | `Link` to `CareService template` | Care-service template used to create lab/imaging/service records. | `pet_app/pet_app/doctype/visit_order/visit_order.json:53` |
| `status` | `Select`; `Draft`, `Ordered`, `In Progress`, `Completed`, `Cancelled` | Order lifecycle state. | `pet_app/pet_app/doctype/visit_order/visit_order.json:61` |
| `priority` | `Select` | Order priority. | `pet_app/pet_app/doctype/visit_order/visit_order.json:69` |
| `qty` | Numeric | Quantity for billing/order context. | `pet_app/pet_app/doctype/visit_order/visit_order.json:77` |
| `price` | Currency | Price for order context. | `pet_app/pet_app/doctype/visit_order/visit_order.json:83` |
| `note` | `Small Text` | Order note. | `pet_app/pet_app/doctype/visit_order/visit_order.json:89` |
| `linked_doctype` | `Link` to `DocType` | Target record type created from the order. | `pet_app/pet_app/doctype/visit_order/visit_order.json:94` |
| `linked_name` | `Dynamic Link` via `linked_doctype` | Target record name. | `pet_app/pet_app/doctype/visit_order/visit_order.json:101` |

Creates/updates/deletes/guards:

- Workspace `create_orders` appends/upserts `orders` rows, then creates linked `Lab`, `Imaging`, `PetCareService`, or `Pet Procedure` records for supported order kinds (`pet_app/api/workspace.py:1686`, `pet_app/api/workspace.py:2074`).
- Lab/imaging/service/procedure action handlers update the linked order row status through `_update_order_status_for_link` (`pet_app/api/workspace.py:2249`, `pet_app/api/workspace.py:2533`).
- Controller validates order status transitions and duplicate `order_id` values (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:273`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:285`).
- No direct order delete endpoint was found. Direct deletion by saving the parent is possible before billing unless business code blocks it; order-status transitions are guarded (`pet_app/workflows/clinical_state.py:23`).

Live data: 92 rows across 44 visits as of 2026-07-19.

### `prescribed_medications` -> `Vet Visit Medication Item`

Real child DocType: `Vet Visit Medication Item`, `istable = 1` (`pet_app/pet_app/doctype/vet_visit_medication_item/vet_visit_medication_item.json:241`).

Fields:

| Field | Type / Options | Purpose | Source |
|---|---|---|---|
| `dose_option` | `Link` to `Medication Dose Option` | Dose template. | `pet_app/pet_app/doctype/vet_visit_medication_item/vet_visit_medication_item.json:43` |
| `medication_item` | `Link` to `Item` | Stock/billable item. | `pet_app/pet_app/doctype/vet_visit_medication_item/vet_visit_medication_item.json:50` |
| `qty` | `Float` | Prescribed quantity or dose count. | `pet_app/pet_app/doctype/vet_visit_medication_item/vet_visit_medication_item.json:60` |
| `dispense_uom` | `Link` to `UOM` | Dispense unit. | `pet_app/pet_app/doctype/vet_visit_medication_item/vet_visit_medication_item.json:67` |
| `stock_uom` | `Link` to `UOM` | Stock unit. | `pet_app/pet_app/doctype/vet_visit_medication_item/vet_visit_medication_item.json:73` |
| `conversion_factor` | `Float` | Stock conversion/deduction factor. | `pet_app/pet_app/doctype/vet_visit_medication_item/vet_visit_medication_item.json:81` |
| `rate` | `Currency` | Unit price. | `pet_app/pet_app/doctype/vet_visit_medication_item/vet_visit_medication_item.json:86` |
| `amount` | `Currency` | Computed amount. | `pet_app/pet_app/doctype/vet_visit_medication_item/vet_visit_medication_item.json:92` |
| `dosage` | `Data` | Dosage instructions. | `pet_app/pet_app/doctype/vet_visit_medication_item/vet_visit_medication_item.json:99` |
| `frequency` | `Data` | Frequency instructions. | `pet_app/pet_app/doctype/vet_visit_medication_item/vet_visit_medication_item.json:105` |
| `duration_days` | `Int` | Duration in days. | `pet_app/pet_app/doctype/vet_visit_medication_item/vet_visit_medication_item.json:111` |
| `instructions` | `Small Text` | Medication-specific instructions. | `pet_app/pet_app/doctype/vet_visit_medication_item/vet_visit_medication_item.json:117` |
| `medication` | `Link` to `Medication` | Medication master. | `pet_app/pet_app/doctype/vet_visit_medication_item/vet_visit_medication_item.json:122` |
| `warehouse` | `Link` to `Warehouse` | Dispense warehouse. | `pet_app/pet_app/doctype/vet_visit_medication_item/vet_visit_medication_item.json:133` |
| `dispense_status` | `Select`; `Prescribed`, `Pending Dispense`, `Dispensed`, `Partially Dispensed`, `Cancelled`, `Returned` | Pharmacy lifecycle. | `pet_app/pet_app/doctype/vet_visit_medication_item/vet_visit_medication_item.json:141` |
| `dispensed_qty` | `Float` | Quantity dispensed. | `pet_app/pet_app/doctype/vet_visit_medication_item/vet_visit_medication_item.json:149` |
| `return_qty` | `Float` | Quantity returned. | `pet_app/pet_app/doctype/vet_visit_medication_item/vet_visit_medication_item.json:156` |
| `dispensed_by` | `Link` to `User` | User who dispensed. | `pet_app/pet_app/doctype/vet_visit_medication_item/vet_visit_medication_item.json:165` |
| `dispensed_at` | `Datetime` | Dispense timestamp. | `pet_app/pet_app/doctype/vet_visit_medication_item/vet_visit_medication_item.json:172` |
| `returned_by` | `Link` to `User` | User who recorded return. | `pet_app/pet_app/doctype/vet_visit_medication_item/vet_visit_medication_item.json:178` |
| `returned_at` | `Datetime` | Return timestamp. | `pet_app/pet_app/doctype/vet_visit_medication_item/vet_visit_medication_item.json:185` |
| `batch_no` | `Link` to `Batch` | Batch used for dispensing. | `pet_app/pet_app/doctype/vet_visit_medication_item/vet_visit_medication_item.json:191` |
| `expiry_date` | `Date` | Batch/medicine expiry. | `pet_app/pet_app/doctype/vet_visit_medication_item/vet_visit_medication_item.json:197` |
| `quantity_modified_by` | `Link` to `User` | Audit user for qty edits. | `pet_app/pet_app/doctype/vet_visit_medication_item/vet_visit_medication_item.json:207` |
| `quantity_modified_at` | `Datetime` | Audit timestamp for qty edits. | `pet_app/pet_app/doctype/vet_visit_medication_item/vet_visit_medication_item.json:214` |
| `rate_modified_by` | `Link` to `User` | Audit user for rate edits. | `pet_app/pet_app/doctype/vet_visit_medication_item/vet_visit_medication_item.json:220` |
| `rate_modified_at` | `Datetime` | Audit timestamp for rate edits. | `pet_app/pet_app/doctype/vet_visit_medication_item/vet_visit_medication_item.json:227` |
| `sales_invoice_item` | `Data` | Intended invoice item reference. | `pet_app/pet_app/doctype/vet_visit_medication_item/vet_visit_medication_item.json:233` |

Creates/updates/deletes/guards:

- No workspace action creates prescription rows directly. Rows are expected from direct parent saves or other callers; the defined `_sync_medication_to_visit` helper appends a medication row but has no call sites in `care_plan.py` as of 2026-07-19 (`pet_app/api/care_plan.py:1006`).
- Controller fills dose-option-derived medication item/UOM/conversion data (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:378`).
- Controller syncs non-cancelled medication rows to `Pet Billable Item` rows with `linked_service_id = medication::<row.name>` and cancels stale unbilled medication billables (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:483`).
- `cancel_medication` sets row `dispense_status = "Cancelled"` if nothing has been dispensed (`pet_app/api/workspace.py:1811`).
- Pharmacy APIs update `dispensed_qty`, `return_qty`, status, users/timestamps, batch metadata, save the visit, and create `Medication Dispense Ledger` rows (`pet_app/api/pharmacy.py:76`, `pet_app/api/pharmacy.py:129`, `pet_app/api/pharmacy.py:206`).
- Deleting or cancelling medication rows linked to active care plan items is blocked by `assert_no_active_plan_items_linked_to` (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:465`).

Live data: 110 rows across 84 visits as of 2026-07-19.

### `billable_items` -> `Pet Billable Item`

Real child DocType: `Pet Billable Item`, reused by visits and boarding. Visit rows have `parenttype = "Vet Visit"` (`pet_app/pet_app/doctype/pet_billable_item/pet_billable_item.json:103`).

Fields:

| Field | Type / Options | Purpose | Source |
|---|---|---|---|
| `item_name` | `Data` | Display item name. | `pet_app/pet_app/doctype/pet_billable_item/pet_billable_item.json:24` |
| `item_code` | `Link` to `Item` | Invoice item. | `pet_app/pet_app/doctype/pet_billable_item/pet_billable_item.json:30` |
| `item_type` | `Select`; `Room Stay`, `Service`, `Medication`, `Lab`, `Imaging`, `Procedure`, `Product`, `Other` | Billing category. | `pet_app/pet_app/doctype/pet_billable_item/pet_billable_item.json:38` |
| `qty` | `Float` | Quantity. | `pet_app/pet_app/doctype/pet_billable_item/pet_billable_item.json:46` |
| `rate` | `Currency` | Unit rate. | `pet_app/pet_app/doctype/pet_billable_item/pet_billable_item.json:53` |
| `amount` | `Currency` | Computed amount. | `pet_app/pet_app/doctype/pet_billable_item/pet_billable_item.json:59` |
| `status` | `Select`; `Draft`, `Billable`, `Billed`, `Cancelled` | Billing lifecycle. | `pet_app/pet_app/doctype/pet_billable_item/pet_billable_item.json:67` |
| `note` | `Small Text` | Billing note. | `pet_app/pet_app/doctype/pet_billable_item/pet_billable_item.json:74` |
| `linked_service_id` | `Data` | App-defined stable link key. | `pet_app/pet_app/doctype/pet_billable_item/pet_billable_item.json:79` |
| `linked_doctype` | `Link` to `DocType` | Linked source record type. | `pet_app/pet_app/doctype/pet_billable_item/pet_billable_item.json:84` |
| `linked_name` | `Dynamic Link` via `linked_doctype` | Linked source record name. | `pet_app/pet_app/doctype/pet_billable_item/pet_billable_item.json:90` |
| `order_id` | `Data` | Visit order id that generated this billable. | `pet_app/pet_app/doctype/pet_billable_item/pet_billable_item.json:96` |

Creates/updates/deletes/guards:

- Medication rows and care-service rows auto-upsert/cancel billable rows in the visit controller (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:483`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:539`).
- `upsert_visit_billable_item` appends/updates visit billable rows from linked records (`pet_app/utils/visit_billing.py:88`).
- Invoice creation requires at least one billable and uses all non-cancelled rows to build invoice items (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:911`).
- `_mark_visit_invoiced` sets all non-cancelled rows to `Billed` and then the billed-row lock prevents changing billed child row fields (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:899`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:615`).
- Manual billable items are allowed but audited with a comment/log (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:667`).

Live data: 200 rows across 105 visits as of 2026-07-19.

### `care_services` -> `custom services`

Real child DocType: `custom services`, including the space and lowercase name in the DocType display name (`pet_app/pet_app/doctype/custom_services/custom_services.json:41`). This name is an oddity; the field on `Vet Visit` uses exactly `options = "custom services"` (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:352`).

Fields:

| Field | Type / Options | Purpose | Source |
|---|---|---|---|
| `pet_care_service_id` | `Link` to `PetCareService` | Optional linked concrete service record. | `pet_app/pet_app/doctype/custom_services/custom_services.json:15` |
| `care_service_id` | `Link` to `CareService template` | Service template/master used for billable lookup. | `pet_app/pet_app/doctype/custom_services/custom_services.json:21` |
| `status` | `Select`; `Active`, `Cancelled` | Selection lifecycle. | `pet_app/pet_app/doctype/custom_services/custom_services.json:28` |

Creates/updates/deletes/guards:

- Direct parent saves create/update/remove rows; no dedicated workspace action creates `care_services` rows.
- Controller `_sync_care_services_billables` reads non-cancelled rows, rejects lab/imaging service categories, and upserts `Pet Billable Item` rows with `linked_service_id = visit-care-service::<row.name>` (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:539`).
- Removing or cancelling a row cancels its stale unbilled billable row; billed rows remain locked (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:583`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:615`).
- Deletion of a visit that opened an active episode is blocked when care-service rows exist (`pet_app/utils/medical_profile.py:627`).

Live data: 15 rows across 10 visits as of 2026-07-19.

### `consult_requests` -> `Visit Consult Request`

Real child DocType: `Visit Consult Request`, `istable = 1` (`pet_app/pet_app/doctype/visit_consult_request/visit_consult_request.json:70`).

Fields:

| Field | Type / Options | Purpose | Source |
|---|---|---|---|
| `requested_doctor` | `Link` to `Healthcare Practitioner` | Practitioner asked to consult. | `pet_app/pet_app/doctype/visit_consult_request/visit_consult_request.json:19` |
| `requested_by` | `Link` to `User` | User who requested consult. | `pet_app/pet_app/doctype/visit_consult_request/visit_consult_request.json:27` |
| `reason` | `Small Text` | Consult reason. | `pet_app/pet_app/doctype/visit_consult_request/visit_consult_request.json:35` |
| `status` | `Select`; `Requested`, `Accepted`, `Completed`, `Cancelled` | Consult lifecycle. | `pet_app/pet_app/doctype/visit_consult_request/visit_consult_request.json:42` |
| `consult_note` | `Text` | Completion note. | `pet_app/pet_app/doctype/visit_consult_request/visit_consult_request.json:50` |
| `requested_at` | `Datetime` | Request timestamp. | `pet_app/pet_app/doctype/visit_consult_request/visit_consult_request.json:55` |
| `completed_at` | `Datetime` | Completion timestamp. | `pet_app/pet_app/doctype/visit_consult_request/visit_consult_request.json:62` |

Creates/updates/deletes/guards:

- `request_consult` appends a row and prevents duplicate open consults for the same requested doctor (`pet_app/api/workspace.py:2020`).
- `complete_consult` finds an open row, checks permission, and writes `Completed`, `consult_note`, and `completed_at` (`pet_app/api/workspace.py:2051`, `pet_app/api/workspace.py:3006`).
- Access control treats active consult rows as a reason a requested doctor can access a visit (`pet_app/api/workspace.py:3017`).
- No delete endpoint was found. There are live zero rows as of 2026-07-19, so this path is implemented but appears unused in live data.

Live data: 0 rows across 0 visits as of 2026-07-19.

### `referrals` -> `Visit Referral`

Real child DocType: `Visit Referral`, `istable = 1` (`pet_app/pet_app/doctype/visit_referral/visit_referral.json:58`).

Fields:

| Field | Type / Options | Purpose | Source |
|---|---|---|---|
| `from_practitioner` | `Link` to `Healthcare Practitioner` | Referring/current doctor. | `pet_app/pet_app/doctype/visit_referral/visit_referral.json:17` |
| `to_practitioner` | `Link` to `Healthcare Practitioner` | Receiving doctor. | `pet_app/pet_app/doctype/visit_referral/visit_referral.json:26` |
| `note` | `Small Text` | Referral note. | `pet_app/pet_app/doctype/visit_referral/visit_referral.json:34` |
| `referred_by` | `Link` to `User` | User who made the referral. | `pet_app/pet_app/doctype/visit_referral/visit_referral.json:41` |
| `referred_at` | `Datetime` | Referral timestamp. | `pet_app/pet_app/doctype/visit_referral/visit_referral.json:49` |

Creates/updates/deletes/guards:

- `create_visit_referral` appends a row, validates receiving practitioner, changes visit practitioner, and adds the receiving practitioner to the episode team when an episode exists (`pet_app/api/visit_referral.py:27`, `pet_app/api/visit_referral.py:68`, `pet_app/api/visit_referral.py:81`).
- Referral is blocked if the visit is not eligible through `_assert_visit_can_receive_referral` / `_visit_locked_for_referral` (`pet_app/api/visit_referral.py:185`, `pet_app/api/visit_referral.py:192`).
- Workbench returns `referrals` from `visit_referrals_payload` (`pet_app/api/visit_workbench.py:69`, `pet_app/api/visit_referral.py:102`).
- No delete endpoint was found.

Live data: 12 rows across 7 visits as of 2026-07-19.

## 4. The Link Graph

### A. Outbound Links On `Vet Visit`

| Visit field | Target DocType | What sets it | Source |
|---|---|---|---|
| `case_sheet` | `Vet Case Sheet` | Case-sheet start and workspace conversion. | `pet_app/pet_app/doctype/vet_visit/vet_visit.json:181`, `pet_app/pet_app/doctype/vet_case_sheet/vet_case_sheet.py:276`, `pet_app/api/workspace.py:2414` |
| `care_episode` | `Pet Care Episode` | Case-choice/episode sync and follow-up conversion. | `pet_app/patches/p0_5_medical_core_schema.py:16`, `pet_app/utils/medical_profile.py:86`, `pet_app/api/workspace.py:1997` |
| `appointment` | `Appointment` | Appointment/case-sheet/follow-up conversion. | `pet_app/pet_app/doctype/vet_visit/vet_visit.json:190`, `pet_app/api/workspace.py:2425`, `pet_app/api/workspace.py:1995` |
| `guardian` | `Guardian` | Conversion copies from case sheet/original visit. | `pet_app/pet_app/doctype/vet_visit/vet_visit.json:198`, `pet_app/api/workspace.py:2416` |
| `customer` | `Customer` | Conversion copies or creates from guardian. | `pet_app/pet_app/doctype/vet_visit/vet_visit.json:207`, `pet_app/api/workspace.py:2415` |
| `animal_patient` | `Pet` | Conversion copies from case sheet/original visit. | `pet_app/pet_app/doctype/vet_visit/vet_visit.json:217`, `pet_app/api/workspace.py:2417` |
| `doctor` | `Healthcare Practitioner` | Conversion/start/referral. | `pet_app/pet_app/doctype/vet_visit/vet_visit.json:230`, `pet_app/api/workspace.py:2418`, `pet_app/api/visit_referral.py:78` |
| `primary_practitioner` | `Healthcare Practitioner` | Backfill/default slot for primary practitioner. | `pet_app/pet_app/doctype/vet_visit/vet_visit.json:237`, `pet_app/patches/case_assignment_schema.py:17` |
| `sales_invoice` | `Sales Invoice` | `_mark_visit_invoiced`. | `pet_app/pet_app/doctype/vet_visit/vet_visit.json:134`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:899` |
| `death_record` | `Pet Death Record` | Mortality `_link_source`. | `pet_app/patches/p0_5_medical_core_schema.py:37`, `pet_app/api/mortality.py:432` |
| `follow_up_appointment_id` | `Appointment` | `request_follow_up`. | `pet_app/pet_app/doctype/vet_visit/vet_visit.json:474`, `pet_app/api/workspace.py:1900` |
| `follow_up_visit_id` | `Vet Visit` | Follow-up conversion updates original visit. | `pet_app/pet_app/doctype/vet_visit/vet_visit.json:481`, `pet_app/api/workspace.py:1946` |
| `follow_up_of_visit_id` | `Vet Visit` | Follow-up conversion writes new visit pointer to original. | `pet_app/pet_app/doctype/vet_visit/vet_visit.json:488`, `pet_app/api/workspace.py:1993` |
| `follow_up_contacted_by` | `User` | Follow-up contacted API. | `pet_app/pet_app/doctype/vet_visit/vet_visit.json:437`, `pet_app/api/follow_up.py:112` |

### B. Child Rows Inside `Vet Visit`

The embedded child tables are `vital_signs`, `diagnoses`, `orders`, `prescribed_medications`, `billable_items`, `care_services`, `consult_requests`, and `referrals` (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:281`, `pet_app/pet_app/doctype/vet_visit/vet_visit.json:346`, `pet_app/pet_app/doctype/vet_visit/vet_visit.json:397`, `pet_app/pet_app/doctype/vet_visit/vet_visit.json:391`, `pet_app/pet_app/doctype/vet_visit/vet_visit.json:403`, `pet_app/pet_app/doctype/vet_visit/vet_visit.json:352`, `pet_app/pet_app/doctype/vet_visit/vet_visit.json:504`, `pet_app/pet_app/doctype/vet_visit/vet_visit.json:510`).

### C. Reverse Links Pointing At `Vet Visit`

| Doctype | Field(s) | What creates / updates the link | Source |
|---|---|---|---|
| `Vet Case Sheet` | `vet_visit` | `start_visit` and workspace conversion set it; visit controller keeps it synced. | `pet_app/pet_app/doctype/vet_case_sheet/vet_case_sheet.json:128`, `pet_app/pet_app/doctype/vet_case_sheet/vet_case_sheet.py:301`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:692` |
| `Pet Care Episode` | `opened_visit`, `current_visit`, `last_visit`; plus `opened_from_doctype/name` can point to a visit | New/continue case logic and episode touch sync write these links. | `pet_app/pet_app/doctype/pet_care_episode/pet_care_episode.json:126`, `pet_app/utils/medical_profile.py:428`, `pet_app/utils/medical_profile.py:720` |
| `Pet Medical Profile` | `current_visit`, `last_visit`, `last_completed_visit` | `update_profile_for_visit` writes current/last and last completed. | `pet_app/pet_app/doctype/pet_medical_profile/pet_medical_profile.json:145`, `pet_app/pet_app/doctype/pet_medical_profile/pet_medical_profile.json:205`, `pet_app/pet_app/doctype/pet_medical_profile/pet_medical_profile.json:211`, `pet_app/utils/medical_profile.py:170` |
| `Appointment` | `custom_linked_visit_id`, `custom_follow_up_of_visit_id` | Conversion stamps linked visit; follow-up scheduling links appointment to source visit. | `pet_app/fixtures/custom_field.json:2025`, `pet_app/fixtures/custom_field.json:2202`, `pet_app/api/workspace.py:3358`, `pet_app/api/workspace.py:1891` |
| `Lab` | `visit` | Visit order creation creates Lab with `visit = visit.name`. | `pet_app/pet_app/doctype/lab/lab.json:37`, `pet_app/api/workspace.py:2087` |
| `Imaging` | `visit` | Visit order creation creates Imaging with `visit = visit.name`. | `pet_app/pet_app/doctype/imaging/imaging.json:36`, `pet_app/api/workspace.py:2099` |
| `Pet Procedure` | `visit` | Procedure order creation creates Pet Procedure with `visit = visit.name`. | `pet_app/pet_app/doctype/pet_procedure/pet_procedure.json:46`, `pet_app/api/workspace.py:2128` |
| `PetCareService` | `visit` | Service order creation creates PetCareService with `visit = visit.name`. | `pet_app/pet_app/doctype/petcareservice/petcareservice.json:146`, `pet_app/api/workspace.py:2111` |
| `Pet Boarding` | `visit` | Boarding can be started from a visit and reverse-link back to it. | `pet_app/pet_app/doctype/pet_boarding/pet_boarding.json:261`, `pet_app/api/healthcare/boarding.py:368` |
| `Pet Queue Ticket` | `visit` | Queue sync finds/updates tickets by visit; appointment/case-sheet paths create queue tickets. | `pet_app/pet_app/doctype/pet_queue_ticket/pet_queue_ticket.json:84`, `pet_app/api/workspace.py:2548` |
| `Medication Dispense Ledger` | `visit` | Pharmacy dispense/return creates ledger rows for the visit. | `pet_app/pet_app/doctype/medication_dispense_ledger/medication_dispense_ledger.json:45`, `pet_app/api/pharmacy.py:206` |
| `Pet Care Plan Item` | `source_visit`, `converted_visit` | Adding plan items from a visit writes `source_visit`; conversion marks `converted_visit`. | `pet_app/pet_app/doctype/pet_care_plan_item/pet_care_plan_item.json:75`, `pet_app/pet_app/doctype/pet_care_plan_item/pet_care_plan_item.json:229`, `pet_app/api/care_plan.py:103`, `pet_app/api/care_plan.py:1293` |
| `Pet Care Episode Problem` | `source_visit` | Diagnosis/problem sync can source episode problems from visits. | `pet_app/pet_app/doctype/pet_care_episode_problem/pet_care_episode_problem.json:15`, `pet_app/utils/medical_profile.py:783` |
| `Pet Care Episode Medication` | `source_visit` | Treatment sync copies medication summary/details from visit prescriptions. | `pet_app/pet_app/doctype/pet_care_episode_medication/pet_care_episode_medication.json:19`, `pet_app/utils/medical_profile.py:793` |
| `Pet Vaccination Record` | `visit` | Doctype has a visit link; no live linked rows as of 2026-07-19. | `pet_app/pet_app/doctype/pet_vaccination_record/pet_vaccination_record.json:41` |
| `Pet Deworming Record` | `visit` | Doctype has a visit link; no live linked rows as of 2026-07-19. | `pet_app/pet_app/doctype/pet_deworming_record/pet_deworming_record.json:42` |
| `Pet Consent Form` | `visit` | Doctype has a visit link; no live linked rows as of 2026-07-19. | `pet_app/pet_app/doctype/pet_consent_form/pet_consent_form.json:34` |
| `Vet Visit Addendum` | `visit` | Addenda are reverse-linked to visits and returned by workspace aggregate. | `pet_app/pet_app/doctype/vet_visit_addendum/vet_visit_addendum.json:29`, `pet_app/api/workspace.py:1303` |
| `File` | `attached_to_doctype`, `attached_to_name` | Workspace attachment API creates `File` rows attached to `Vet Visit`; aggregate attachment lookup reads those fields. | `pet_app/api/workspace.py:390`, `pet_app/api/workspace.py:2733` |

Live reverse-link counts as of 2026-07-19: `Lab` 61, `Imaging` 13, `Pet Procedure` 0, `PetCareService` 15, `Pet Boarding` 14, `Pet Queue Ticket` 55, `Medication Dispense Ledger` 6, `Pet Vaccination Record` 0, `Pet Deworming Record` 0, `Pet Consent Form` 0, `Vet Visit Addendum` 0.

ASCII shape:

```text
Appointment --custom_linked_visit_id/custom_follow_up_of_visit_id--> Vet Visit <-- Vet Case Sheet.vet_visit
      |                                                                  |
      | conversion                                                        | outbound links
      v                                                                  v
Vet Case Sheet ----------------------------------------------------> Pet / Guardian / Customer / Healthcare Practitioner
                                                                         |
Vet Visit.care_episode -------------------------------------------------> Pet Care Episode
      |                                                                  ^
      | child tables                                                     |
      +-- vital_signs / diagnoses / orders / medications / billables     |
      +-- care_services / consult_requests / referrals                   |
      |
      +-- orders create reverse-linked Lab / Imaging / PetCareService / Pet Procedure
      +-- billing creates Sales Invoice and marks billable_items
      +-- pharmacy creates Medication Dispense Ledger
      +-- mortality links Pet Death Record
      +-- files attach through File.attached_to_doctype/name
      +-- profile sync writes Pet Medical Profile.current/last/completed visit
```

## 5. The Lifecycle

### Status Values

| Status | Meaning | Evidence |
|---|---|---|
| `Draft` | Default controller state if a visit is created without a status; allowed to move to `In Progress`, `Completed`, `Follow-up Needed`, or `Cancelled`. | `pet_app/pet_app/doctype/vet_visit/vet_visit.py:123`, `pet_app/workflows/clinical_state.py:16` |
| `In Progress` | Active consultation. Normal creation paths set this immediately. | `pet_app/pet_app/doctype/vet_case_sheet/vet_case_sheet.py:317`, `pet_app/api/workspace.py:2420` |
| `Follow-up Needed` | Non-terminal open status whose next task is scheduling follow-up. No dedicated writer was found besides generic/direct status save. | `pet_app/workflows/clinical_state.py:19`, `pet_app/api/workspace.py:3138` |
| `Completed` | Terminal completed visit; reached by `complete_case`. | `pet_app/api/workspace.py:1777`, `pet_app/workflows/clinical_state.py:110` |
| `Cancelled` | Terminal cancelled visit; death cascade and direct status save can create it. | `pet_app/utils/boarding_death_cascade.py:358`, `pet_app/workflows/clinical_state.py:110` |

Transition table:

- `Draft` -> `In Progress`, `Completed`, `Follow-up Needed`, `Cancelled` (`pet_app/workflows/clinical_state.py:16`).
- `In Progress` -> `Completed`, `Follow-up Needed`, `Cancelled` (`pet_app/workflows/clinical_state.py:18`).
- `Follow-up Needed` -> `In Progress`, `Completed`, `Cancelled` (`pet_app/workflows/clinical_state.py:19`).
- `Completed` and `Cancelled` are terminal (`pet_app/workflows/clinical_state.py:20`, `pet_app/workflows/clinical_state.py:110`).

### Creation Paths

- Case sheet direct start: `start_visit(case_sheet_name, practitioner, doctor_case_choice, care_episode, case_choice_note)` maps `Vet Case Sheet` fields into a `Vet Visit`, sets doctor, inserts, optionally applies case choice, updates medical profile, comments, and writes `Vet Case Sheet.vet_visit/status` (`pet_app/pet_app/doctype/vet_case_sheet/vet_case_sheet.py:232`, `pet_app/pet_app/doctype/vet_case_sheet/vet_case_sheet.py:276`, `pet_app/pet_app/doctype/vet_case_sheet/vet_case_sheet.py:293`, `pet_app/pet_app/doctype/vet_case_sheet/vet_case_sheet.py:301`).
- Workspace case-sheet conversion: `_convert_to_visit("Vet Case Sheet", ...)` reuses an existing visit if present, otherwise creates a visit with case sheet, customer, guardian, pet, doctor, priority, `status = "In Progress"`, visit type, case summary, and weight (`pet_app/api/workspace.py:2391`, `pet_app/api/workspace.py:2404`, `pet_app/api/workspace.py:2412`).
- Workspace appointment conversion: `_convert_to_visit("Appointment", ...)` first creates/fetches a case sheet from appointment custom pet/guardian/customer fields, then creates the visit and stamps appointment conversion (`pet_app/api/workspace.py:2394`, `pet_app/api/workspace.py:2458`, `pet_app/api/workspace.py:2435`).
- Follow-up conversion: `_convert_follow_up_to_visit` creates a new follow-up case sheet and visit, copies original customer/guardian/pet/doctor/priority/weight, sets `visit_type = "Follow-up"`, `follow_up_of_visit_id`, and carries `care_episode` when present (`pet_app/api/workspace.py:1915`, `pet_app/api/workspace.py:1965`, `pet_app/api/workspace.py:1981`, `pet_app/api/workspace.py:1997`).
- Walk-in is represented by case-sheet creation followed by conversion in this backend; no separate `create_walk_in_visit` writer was found in `pet_app` on 2026-07-19. The conversion code allows case sheets without appointments (`pet_app/api/workspace.py:2391`).

### Completion Path

`complete_case` is a workspace action. The exact sequence is:

1. `perform_action` routes `complete_case` to `_complete_case` after access checks (`pet_app/api/workspace.py:271`).
2. `_complete_case` opens a database savepoint and delegates to `_complete_case_atomic` (`pet_app/api/workspace.py:1736`).
3. `_complete_case_atomic` loads the visit, blocks active checked-in boarding, and returns if already completed (`pet_app/api/workspace.py:1748`).
4. If the payload includes clinical note or diagnoses fields, it saves those first (`pet_app/api/workspace.py:1753`).
5. It checks `complete_case` is allowed from the current status, derives scalar diagnosis from child rows if needed, copies `instructions` to `doctor_notes` if needed, and validates illness/diagnosis/clinical note requirements (`pet_app/api/workspace.py:1758`, `pet_app/api/workspace.py:1800`).
6. It creates a draft `Sales Invoice` using `_create_sales_invoice_for_visit`; this requires customer, Sales Invoice permission, and at least one active billable item (`pet_app/api/workspace.py:1769`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:833`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:911`).
7. It transitions visit status to `Completed` (`pet_app/api/workspace.py:1777`).
8. `_mark_visit_invoiced` marks non-cancelled billables `Billed`, writes `sales_invoice`, `billed = 1`, `total_billable_amount`, and saves with billing lock ignored (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:899`).
9. `sync_completed_visit` syncs clinical/profile/episode status. Current rule: `Referred` -> episode `Referred`; `Death`/`Euthanasia` -> `Deceased`; follow-up -> `Follow-up Scheduled` or `Monitoring`; otherwise `episode_status = None`, so ordinary visit completion does not close the episode (`pet_app/utils/medical_profile.py:332`).
10. It syncs the queue ticket to `Completed` and comments on the visit (`pet_app/api/workspace.py:1780`).

### Deletion

- `VetVisit.on_trash` calls `protect_care_episode_before_visit_delete` (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:116`).
- Deleting a visit that opened an active care episode is allowed only when the episode can be detached/repointed or has no blockers. Blockers include episode problems/medications/monitoring, visit diagnoses/medications/orders/care services, care plan items, lab/imaging/procedure/service/boarding records (`pet_app/utils/medical_profile.py:377`, `pet_app/utils/medical_profile.py:616`).
- If another visit exists in the episode, episode/profile visit links are repointed to it; otherwise a content-free episode opened by this visit is cancelled/detached (`pet_app/utils/medical_profile.py:384`, `pet_app/utils/medical_profile.py:408`, `pet_app/utils/medical_profile.py:668`, `pet_app/utils/medical_profile.py:689`).

### Gates And Locks

| Gate | What it blocks | Source |
|---|---|---|
| Clinical-state actions | `set_case_choice`, `start_consultation`, `save_clinical_note`, `save_diagnoses`, `create_orders`, `cancel_medication`, `complete_case`, `request_consult`, `complete_consult` only run in `Draft`, `In Progress`, or `Follow-up Needed`; `request_follow_up` can also run on `Completed`. | `pet_app/workflows/clinical_state.py:68` |
| Terminal statuses | `Completed` and `Cancelled` are terminal visit statuses. | `pet_app/workflows/clinical_state.py:110` |
| Billing lock | Billed visits cannot be changed except through paths that set `ignore_billing_lock`; sales-invoice-linked visits throw `Visit is locked after billing`. | `pet_app/pet_app/doctype/vet_visit/vet_visit.py:334`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:348` |
| Billed child-row lock | `Billed` billable rows cannot have item/qty/rate/link/status/note fields changed. | `pet_app/pet_app/doctype/vet_visit/vet_visit.py:615` |
| Checked-in boarding lock | Clinical write actions and completion are blocked while a linked boarding record is checked in. | `pet_app/api/workspace.py:484`, `pet_app/api/workspace.py:1784` |
| Completion requirements | Illness, diagnosis, and clinical note are required; strict mode also requires no pending lab/imaging/procedure records. | `pet_app/api/workspace.py:1800`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:354` |
| Vitals guard | Cancelled or billed visits cannot accept vital signs. | `pet_app/api/vitals.py:116` |
| Care-plan guard | Cancelled or billed visits cannot accept care plan items. | `pet_app/api/care_plan.py:1000` |
| New-case guard | A pet with another active episode cannot open a new case; error names the open case. | `pet_app/utils/medical_profile.py:102` |
| Deceased guard | `Vet Visit.before_insert` runs deceased-document validation through hooks. | `pet_app/hooks.py:254` |

## 6. What Writes A Visit

### Controller Hooks

- `before_insert`: sets defaults, pulls case-sheet values, rejects legacy payload (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:39`).
- `after_insert`: updates profile, optionally syncs treatment, syncs latest vitals, and notifies doctor (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:44`).
- `validate`: runs defaults, identity consistency/immutability, workflow transition validation, order validation, vitals scalar sync, sales-invoice/billing locks, medication/billable sync, billable validation, case-sheet uniqueness, follow-up validation, and completion rules (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:87`).
- `on_update`: syncs case sheet, medical profile snapshot, and audits manual billables (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:111`).
- `on_trash`: protects care episodes before deletion (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:116`).
- App hooks also run deceased validation before insert and medication counter sync on update/trash (`pet_app/hooks.py:254`).

### Workspace `perform_action`

`perform_action` is the main workbench mutator. The visit-relevant actions are routed in one list (`pet_app/api/workspace.py:271`):

| Action | Writes |
|---|---|
| `convert_to_visit` | Creates or returns a visit from `Appointment` or `Vet Case Sheet` (`pet_app/api/workspace.py:2391`). |
| `convert_follow_up_to_visit` | Creates a follow-up case sheet and visit, links original/new visits and appointment (`pet_app/api/workspace.py:1915`). |
| `set_case_choice` | Writes doctor case choice and `care_episode` through `set_visit_case_choice` (`pet_app/api/workspace.py:1608`, `pet_app/utils/medical_profile.py:86`). |
| `start_consultation` | Transitions to `In Progress`, fills priority, updates profile/episode to diagnosis context, syncs queue (`pet_app/api/workspace.py:1595`). |
| `save_clinical_note` | Writes clinical note fields, vitals scalars, follow-up fields, and optional status; updates profile and treatment sync (`pet_app/api/workspace.py:1617`). |
| `save_diagnoses` | Replaces diagnosis child rows and syncs diagnosis to episode/profile (`pet_app/api/workspace.py:1655`). |
| `create_orders` | Appends/upserts order child rows and creates linked lab/imaging/service/procedure records (`pet_app/api/workspace.py:1686`, `pet_app/api/workspace.py:2074`). |
| `cancel_medication` | Sets a medication row `dispense_status` to `Cancelled` when not dispensed (`pet_app/api/workspace.py:1811`). |
| `complete_case` | Completes the visit, creates invoice, bills rows, syncs episode/profile/queue (`pet_app/api/workspace.py:1736`). |
| `request_follow_up` | Creates/updates follow-up appointment and writes visit follow-up fields (`pet_app/api/workspace.py:1838`). |
| `request_consult` | Appends consult request row (`pet_app/api/workspace.py:2020`). |
| `complete_consult` | Completes consult request row (`pet_app/api/workspace.py:2051`). |
| `mark_follow_up` | For linked diagnostic/service/procedure records, sets the linked visit `billing_status = "Follow-up"` (`pet_app/api/workspace.py:365`, `pet_app/api/workspace.py:2528`). |

### Other Whitelisted APIs And Utilities

- `pet_app.api.vitals`: appends, updates, and lists `vital_signs`; save triggers scalar/profile vitals sync (`pet_app/api/vitals.py:34`, `pet_app/api/vitals.py:59`, `pet_app/api/vitals.py:96`).
- `pet_app.api.follow_up`: lists due follow-ups and marks/reschedules visit follow-up fields (`pet_app/api/follow_up.py:16`, `pet_app/api/follow_up.py:71`, `pet_app/api/follow_up.py:83`).
- `pet_app.api.visit_referral`: appends referral rows, changes `doctor`, and updates the episode team (`pet_app/api/visit_referral.py:27`, `pet_app/api/visit_referral.py:78`, `pet_app/api/visit_referral.py:81`).
- `pet_app.api.pharmacy`: reads pending medication rows and mutates dispense/return fields; creates medication dispense ledgers (`pet_app/api/pharmacy.py:18`, `pet_app/api/pharmacy.py:76`, `pet_app/api/pharmacy.py:129`, `pet_app/api/pharmacy.py:206`).
- `pet_app.api.care_plan`: creates `Pet Care Plan Item` rows linked to visits and converts plan items back to visits; its `_sync_medication_to_visit` helper exists but no call site was found in that file (`pet_app/api/care_plan.py:103`, `pet_app/api/care_plan.py:279`, `pet_app/api/care_plan.py:1006`).
- `pet_app.api.mortality`: when a death record has source `Vet Visit`, it writes `death_record`, `death_during_visit`, and finalized outcome (`pet_app/api/mortality.py:428`).
- `pet_app.utils.visit_billing`: upserts/cancels linked billable rows and responds to Sales Invoice cancellation (`pet_app/utils/visit_billing.py:88`, `pet_app/utils/visit_billing.py:198`).
- `pet_app.api.healthcare.boarding`: visit-linked boarding can block visit completion/editing and creates reverse `Pet Boarding.visit` links (`pet_app/api/healthcare/boarding.py:368`, `pet_app/api/workspace.py:1784`).

## 7. What `get_visit_workbench` Returns

`get_visit_workbench` is a whitelisted endpoint that requires visit read permission and record access, loads the visit, builds linked records, and returns an `ok` payload (`pet_app/api/visit_workbench.py:36`).

Exact top-level keys:

| Key | Contents | Source |
|---|---|---|
| `visit` | Raw `doc.as_dict(no_nulls=False)` plus aliases `pet`, `pet_id`, `pet_name`, `guardian_id`, `guardian_name`, `doctor_name`. | `pet_app/api/visit_workbench.py:52`, `pet_app/api/visit_workbench.py:157` |
| `case_sheet` | Linked `Vet Case Sheet` doc payload or `{}`. | `pet_app/api/visit_workbench.py:53`, `pet_app/api/visit_workbench.py:175` |
| `pet` | Linked `Pet` doc payload or `{}`. | `pet_app/api/visit_workbench.py:54` |
| `guardian` | Linked `Guardian` doc payload or `{}`. | `pet_app/api/visit_workbench.py:55` |
| `medical_profile` | Linked `Pet Medical Profile` for the pet or `{}`. | `pet_app/api/visit_workbench.py:56`, `pet_app/api/visit_workbench.py:150` |
| `active_episode` | Visit `care_episode` if set, otherwise most recently modified active episode for the pet. | `pet_app/api/visit_workbench.py:57`, `pet_app/api/visit_workbench.py:79` |
| `case_context` | Doctor case choice, visit episode, profile active episode, active episode doc, episode status, `can_continue_case`, `can_open_new_case`. | `pet_app/api/visit_workbench.py:58`, `pet_app/utils/medical_profile.py:145` |
| `active_plan_items` | Active non-terminal plan items for the visit episode or pet. | `pet_app/api/visit_workbench.py:59`, `pet_app/api/visit_workbench.py:97` |
| `plan_items` | Plan items where `source_visit` equals this visit. | `pet_app/api/visit_workbench.py:60`, `pet_app/api/visit_workbench.py:116` |
| `diagnoses` | Structured diagnosis rows, with scalar fallback in shared workspace helper. | `pet_app/api/visit_workbench.py:61`, `pet_app/api/workspace.py:1246` |
| `orders` | Visit order rows plus synthesized orders from linked records missing child rows. | `pet_app/api/visit_workbench.py:62`, `pet_app/api/workspace.py:1318`, `pet_app/api/workspace.py:1547` |
| `linked_records` | Linked Lab, Imaging, PetCareService, and Pet Procedure records. | `pet_app/api/visit_workbench.py:63`, `pet_app/api/workspace.py:1381` |
| `medications` | Normalized prescribed medication child rows. | `pet_app/api/visit_workbench.py:64`, `pet_app/api/visit_workbench.py:192` |
| `billables` | Non-cancelled billable child rows. | `pet_app/api/visit_workbench.py:65`, `pet_app/api/visit_workbench.py:245` |
| `cancelled_billables` | Cancelled billable child rows. | `pet_app/api/visit_workbench.py:66`, `pet_app/api/visit_workbench.py:249` |
| `followups` | Visit follow-up fields normalized into a small dict. | `pet_app/api/visit_workbench.py:67`, `pet_app/api/workspace.py:1328` |
| `consults` | Visit consult request child rows. | `pet_app/api/visit_workbench.py:68`, `pet_app/api/workspace.py:1341` |
| `referrals` | Visit referral child rows enriched by visit-referral API. | `pet_app/api/visit_workbench.py:69`, `pet_app/api/visit_referral.py:102` |
| `boarding` | Visit boarding payload. | `pet_app/api/visit_workbench.py:70` |
| `billing` | Billing snapshot with invoice-derived totals, paid, balance, billable rows. | `pet_app/api/visit_workbench.py:71`, `pet_app/api/workspace.py:1567` |
| `permissions` | Per-action booleans and doctor/team context. | `pet_app/api/visit_workbench.py:72`, `pet_app/api/visit_workbench.py:257` |

Keys that the older workspace aggregate returns but `get_visit_workbench` does not return as top-level keys:

- `summary`, `assignee`, `clinical`, `addenda`, `notes`, `attachments`, `timeline`, and `raw` exist in `_visit_aggregate`, not `get_visit_workbench` (`pet_app/api/workspace.py:1129`, `pet_app/api/workspace.py:1177`, `pet_app/api/workspace.py:1179`, `pet_app/api/workspace.py:1180`, `pet_app/api/workspace.py:1182`, `pet_app/api/workspace.py:1183`).
- `get_visit_workbench` still returns the underlying data for many clinical fields inside the raw `visit` object, but it does not wrap them in a top-level `clinical` object (`pet_app/api/visit_workbench.py:157`).
- Visit attachments are not top-level in `get_visit_workbench`; attachment lookup lives in workspace aggregate `_attachments_for` (`pet_app/api/workspace.py:2733`).

Keys returned that appear backend-only or underused from live data:

- `consults` is returned, and backend access/action support exists, but live `Visit Consult Request` row count is 0 as of 2026-07-19 (`pet_app/api/visit_workbench.py:68`, `pet_app/api/workspace.py:2020`).
- `cancelled_billables` is deliberately returned separately from active `billables`; no backend concern was found with this split (`pet_app/api/visit_workbench.py:65`, `pet_app/api/visit_workbench.py:66`).
- I did not audit frontend consumption for this document because the scope is backend-only; any statement about frontend usage would be outside the evidence set.

## 8. Known Oddities And Traps

- There are two vitals stores: scalar fields on `Vet Visit` (`weight`, `temperature`, `heart_rate`, `respiratory_rate`) and repeated rows in `vital_signs`. The child table is the time-series store; controller sync copies the latest child row to scalars, and profile sync copies latest vitals to `Pet Medical Profile` (`pet_app/pet_app/doctype/vet_visit/vet_visit.py:300`, `pet_app/utils/medical_profile.py:351`).
- Live meta confirms `Vet Visit Vital Sign` has `mucous_membrane`, not `mucous_membrane_color`. Code writes/returns `mucous_membrane`; no backend field named `mucous_membrane_color` was found on 2026-07-19 (`pet_app/api/vitals.py:25`, `pet_app/api/workspace.py:1292`, `pet_app/pet_app/doctype/vet_visit_vital_sign/vet_visit_vital_sign.json:74`).
- `guardian_name` is valid on `Vet Visit`, but the old typo `gurdian_name` has been renamed/dropped by a patch; do not use the typo (`pet_app/patches/rename_guardian_typo_fields.py:7`, `pet_app/patches/rename_guardian_typo_fields.py:13`, `pet_app/pet_app/doctype/vet_visit/vet_visit.json:517`).
- `guardian_name` is a visit display/cache field fetched from `Guardian.full_name`; it is not evidence that `Guardian` itself has a `guardian_name` field (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:517`).
- `doctor` and `primary_practitioner` are not identical: `doctor` is the mutable/current responsible practitioner and can change on referral, while `primary_practitioner` is a primary/original slot backfilled from doctor and used by case-team migration logic (`pet_app/api/visit_referral.py:78`, `pet_app/pet_app/doctype/vet_visit/vet_visit.py:129`, `pet_app/patches/case_assignment_schema.py:17`).
- The `care_services` child table points at a child DocType literally named `custom services`, with a space and lowercase name. Use the exact DocType name in Frappe table references (`pet_app/pet_app/doctype/vet_visit/vet_visit.json:352`, `pet_app/pet_app/doctype/custom_services/custom_services.json:41`).
- Clinical aliases matter: `save_clinical_note` maps incoming `examination` to `examination_notes`, `plan` to `treatment_plan`, and `instructions` to `instructions` or `doctor_notes` depending on field existence (`pet_app/api/workspace.py:1620`).
- Current completion behavior does not auto-resolve ordinary care episodes. `sync_completed_visit` leaves `episode_status = None` for no-follow-up/no-referral/non-death completion, while follow-up, referred, and deceased paths still set active/deceased statuses (`pet_app/utils/medical_profile.py:332`).
- `Referred` is active episode state in the care-case system. Active episode lookup includes active statuses, and new-case guard blocks opening another case when one exists (`pet_app/utils/medical_profile.py:455`, `pet_app/utils/medical_profile.py:102`).
- Visit invoice payment/balance is not stored in `Vet Visit.paid_amount` or `Vet Visit.balance_amount`; billing snapshot reads `Sales Invoice.paid_amount` and `Sales Invoice.outstanding_amount` (`pet_app/api/workspace.py:1567`). The visit fields are live-meta-present but have zero non-zero rows as of 2026-07-19.
- Offline fields `client_request_id`, `idempotency_key`, and `last_synced_at` exist on live `Vet Visit`, but no current visit writer/reader was found; the active offline/idempotency flow is elsewhere (`pet_app/patches/p2_product_growth_schema.py:558`, `pet_app/utils/offline.py:18`, `pet_app/api/scheduling.py:41`).
- `Follow-up Needed` is a valid open visit status and appears in live data, but no dedicated writer was found besides direct/generic status mutation; follow-up request logic writes follow-up fields and appointments rather than transitioning visit status to `Follow-up Needed` (`pet_app/workflows/clinical_state.py:19`, `pet_app/api/workspace.py:1838`).
- The current production process uses gunicorn with `--preload`; plain `.py` edits will not affect running web workers until the service is restarted (`config/supervisor.conf:18`).
- Patch history is not enough proof that a field exists. This document uses live meta checks for the risky fields; the source patches for custom fields are `p0_5_medical_core_schema`, `visit_case_choice_schema`, and `p2_product_growth_schema` (`pet_app/patches/p0_5_medical_core_schema.py:14`, `pet_app/patches/visit_case_choice_schema.py:14`, `pet_app/patches/p2_product_growth_schema.py:558`).
- Workbench split trap: `get_visit_workbench` does not return the same shape as `_visit_aggregate`. The former returns `visit` plus focused top-level sections; the latter returns `clinical`, `attachments`, `timeline`, and `raw` (`pet_app/api/visit_workbench.py:50`, `pet_app/api/workspace.py:1129`).
