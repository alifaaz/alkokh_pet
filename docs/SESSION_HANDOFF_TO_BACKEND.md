# Session handling — frontend handoff to the backend agent

**Date:** 2026-07-26
**Audience:** an agent working on the Frappe/ERPNext backend (`pet_app`) who cannot read the frontend repo.
**Purpose:** give you everything needed to design a backend-side fix for *silent session expiry*, without guessing at frontend behaviour.

The frontend is a Vue 3 SPA (Vite, Vue Router in history mode, Pinia, axios). It is **not** a Frappe Desk app and does not use Frappe's own JS client. It talks to your server purely over `/api/resource/*` and `/api/method/*`.

**Deployment:** frontend is served from `clinic.kokh-vet.com`; the API is `admin.kokh-vet.com`. **These are different origins**, so every API call is cross-origin and carries a CORS preflight (`OPTIONS`) unless `Access-Control-Max-Age` is set. As far as I can tell it currently is not — that is a separate, cheap win worth taking.

Throughout, I mark things I could not verify as **[UNVERIFIED]**. Where I say something is true, I read it in the code.

---

## 1. What the app does on boot

Exact order.

1. **Vue app is created and plugins register.** Relevant ones, in load order: the router, then Pinia. When Pinia installs it calls `auth.loadFromStorage()`, which rehydrates auth state from `localStorage` (see §2). **No network call happens here.**
2. **The app mounts.** The layout — sidebar, header, footer — renders immediately, before any API call resolves. This matters: the chrome is always visible even when the content area is empty, which is why users report "the page is blank but the menu works".
3. **Two independent access loads fire.** They are not coordinated with each other, and this is a known wart:
   - `GetPermission()` is called **fire-and-forget** immediately after mount (not awaited, failures only `console.warn`).
   - The **router navigation guard** runs on the first navigation and calls its own access load.

   Both call the same endpoint, so **a normal cold boot issues `pet_app.api.permissions.get_current_access` twice.** [UNVERIFIED: whether they always overlap or one usually wins the race — either way you will see two.]

4. **The access load itself**, in both paths, is:
   ```
   GET /api/method/pet_app.api.permissions.get_current_access
     ├─ success → normalise to an "access snapshot", persist to localStorage, done
     └─ failure → fall back to:
        GET /api/method/frappe.core.doctype.user.user.get_roles
             → build a snapshot from role names alone
   ```
   The fallback exists to answer exactly one question: *"is the dynamic-access backend deployed yet?"* It is **not** meant to handle "the backend refused me" — as of today's changes, a `401`/`403` from `get_current_access` no longer falls through to `get_roles`; it propagates as a real failure.

5. **The router guard blocks navigation on this, with a 4000 ms timeout.** The guard `await`s the access load raced against a 4 s timer. If the timer wins, the guard **proceeds anyway** using whatever snapshot is in `localStorage`, logging a warning. It does not block the user and does not show them anything.

   For context: you measured `get_current_access` at **0.13 s** for a real user. The 4 s timeout therefore only fires when something is badly wrong. It has no recorded rationale — it arrived in a bulk commit. We have deliberately kept it but **stripped it of authority**: it no longer marks access as "loaded", so the next navigation retries. It exists only to stop a slow network white-screening the app.

### The access snapshot

- Stored in `localStorage` under `alkokh_access_snapshot`, plus a separate `user_permissions` key holding role names.
- Shape: `{ roles[], modules[], pages[], actions[], doctypes{}, restrictions{}, source, loadedAt }`. `pages[]` is what the router checks to decide whether a user may open a route; `doctypes{}` drives per-doctype read/write UI gating.
- **`source`** is `"backend"` when it came from `get_current_access`, or a roles-derived fallback otherwise.
- **TTL: 8 hours**, added today. Before that it had **no expiry at all** — `loadedAt` was written and never read, so a snapshot survived browser restarts indefinitely. On shared clinic workstations that nobody logs out of, one could be months old. On expiry the snapshot is deleted and re-fetched; when the backend is healthy this is invisible (0.13 s).

---

## 2. How the app decides a user is logged in

**It reads `localStorage` only.** The check is:

```
authenticated  =  localStorage has an OAuth access_token
               OR localStorage has a refresh_token
               OR localStorage flag "session authenticated" is true
               OR localStorage has both api_key and api_secret
```

**What it does NOT do:**

- It does **not** read or inspect the `sid` cookie. It cannot — the cookie is `HttpOnly` [UNVERIFIED, but the frontend never attempts to read it either way].
- It does **not** call the backend to confirm the session is alive.
- It does **not** re-verify after boot. **This is the crux of the whole problem:** once the app has booted with a snapshot in `localStorage`, it can run for an unbounded time — hours, a whole shift — without ever asking your server whether the user is still authenticated. There is no heartbeat and no periodic re-validation of identity.

### Which credential is actually used

Three login paths exist in the code:

| Path | Endpoint | Result |
|---|---|---|
| OAuth2 | `frappe.integrations.oauth2.get_token` / `pet_app.api.auth_api.login_and_get_oauth_token` | access + refresh token in `localStorage`; requests send `Authorization: Bearer <token>` |
| Session cookie | `/api/method/login` | relies on the `sid` cookie; a `sessionAuthenticated` flag is set in `localStorage` |
| API key | `pet_app.api.auth_api.login_and_get_api_keys` | `Authorization: token <key>:<secret>` |

**The request interceptor prefers Bearer if a token exists**, falling back to `token key:secret`, otherwise sending nothing explicit (cookie only). If a refresh token exists and the access token is expired (checked against a stored expiry timestamp, purely client-side), it attempts a refresh **before** the request.

**[UNVERIFIED — and I would like your input]:** which path production actually uses. All three are implemented. This matters a great deal to you: if users are on Bearer tokens, then `sid` expiry is irrelevant to them and the meaningful expiry is the OAuth token's. If they are on the cookie, `sid` lifetime governs. A practical consequence we hit while debugging: **deleting the `sid` cookie does not log out a Bearer-authenticated user**, and a raw `fetch()` from the browser console sends cookies but no `Authorization` header, so its 403 says nothing about what the app itself experiences.

---

## 3. What happens today when the backend answers `200` with `sid=Guest`

This is the failure mode the owner reports, walked through end to end:

1. The user's server-side session dies (timeout, restart, eviction). **The frontend is not told.** Nothing pushes; there is no socket carrying session state.
2. `localStorage` still holds tokens/flags and the access snapshot. So `authenticated` is still `true` (§2) and the router guard admits the user to any route the stale snapshot grants.
3. The page component mounts and issues its data calls — `/api/resource/Appointment?...` and similar.
4. **If your server answers `200` with an empty or Guest-scoped result**, the frontend has *no signal at all*. It treats the response as valid data. Empty list → empty table. No error, no toast, no redirect, no console error.
5. The content area renders empty while the sidebar, header and footer render normally, because those live outside the routed view and don't depend on that data.
6. The user sees a blank page and assumes the system is broken. Staff have been reloading, and a hard reload does not help because `localStorage` survives it.

**Where the app has no way to detect this:** anywhere you return `200`. The frontend's *only* signal that a session is dead is a non-2xx status. A `200` with `user='Guest'` and an empty payload is indistinguishable from "this clinic genuinely has no appointments today". There is no field the frontend inspects for identity — it does not look for a `user` key in list responses, and adding one would not help unless the frontend also learned to read it (which it currently does not).

This is worth stating plainly: **if any endpoint answers a Guest with `200`, we cannot fix that from the frontend.**

---

## 4. What the app already handles correctly (build for this)

A **403 session probe** was built today. If you produce a `403`, the app already does the right thing — so the cheapest correct design is one that makes your server return `403`/`401`.

**Trigger:** any `403` from a non-auth endpoint.

**Behaviour:**

1. The axios response interceptor catches the `403`.
2. It issues **one** probe: `GET /api/method/frappe.auth.get_logged_user`. This is **de-duplicated by a shared in-flight promise** — a burst of ten parallel `403`s produces exactly **one** probe, not ten. Please do not design around seeing a probe per failed request.
3. Verdict:
   - probe returns `"Guest"`, or the probe itself returns `401`/`403` → **treat as session expired**: clear tokens, clear the access snapshot, show a translated (en/ar) toast *"Your session has ended. Please sign in again."*, and redirect to `/login` preserving the intended destination.
   - probe returns a real username → **treat as a genuine permission denial**: show a "not authorized" banner and leave the user logged in.
4. The probe endpoint is excluded from the `401` and `403` handlers so it cannot recurse.

**Why the probe exists at all:** Frappe answers a dead session with `403 PermissionError`, not `401`. The app's `401` handler (refresh → retry → logout) was correct but **unreachable** for session expiry. And `exc_type` alone cannot discriminate: a genuine permission denial for a live user carries the same `PermissionError`. Asking who the backend thinks we are is the only reliable test we found.

**Important for your design:** this is written so that **if you change the backend to return `401` for Guest, the existing `401` handler takes over and the probe simply stops being reached.** It becomes dead code, not a rewrite. `401` is fully supported today.

### A trap we fell into, so you don't

`pet_app.api.permissions.get_current_access` returning `403` to a Guest produces a `_server_messages` containing:

> `You are not permitted to access this resource. Login to access Function pet_app.api.permissions.get_current_access is not whitelisted.`

The frontend had a predicate that treated *any* error mentioning `pet_app.api.permissions` as "this backend method isn't deployed", converting the `403` into a silent `null`. That is fixed (it now classifies by status, and `401`/`403` are never "missing backend"). Flagging it because **the wording is genuinely misleading**: "is not whitelisted" here means *whitelisted but not `allow_guest`*, not *missing `@frappe.whitelist()`*. We know the method exists and works — you measured it at 0.13 s for a real user. If you can make that message less ambiguous, it would help future debugging, but it is not required.

---

## 5. Backend options — what each costs us

### (a) `get_current_access` returns `403` for Guest

**Frontend work: none.** This already flows into the probe → logout → redirect path described in §4.
**Risk:** low. The one thing to check is that it returns `403` and not a `200` with an error envelope — several `pet_app` endpoints use an `{ok, data, meta, errors}` envelope which the frontend collapses on success and rejects on `ok:false`. An envelope with `ok:false` would surface as a generic error toast, **not** as session expiry.
**Caveat:** this only helps at the moments the app happens to call `get_current_access` — boot, and after the 8 h TTL. It does **not** catch expiry mid-session unless something else also 403s.

### (b) `401` for Guest (my preference — see §6)

**Frontend work: none.** The `401` handler already refreshes, retries, and on repeat failure logs out with *"Session expired. Please login again."* and redirects.
**Risk:** low, with one thing to verify: the frontend will attempt an OAuth **token refresh** on a `401` before giving up. If your `401` is returned for a Guest *cookie* session while a valid Bearer token exists, the refresh will succeed and the retry will still be Guest — a wasted round trip, not a loop (there is a `_retry` flag preventing a second attempt).

### (c) A new lightweight session-check endpoint

**Frontend work: small but real** — perhaps 20–40 lines: a periodic caller (on an interval and/or `visibilitychange`), plus wiring its answer into the existing expiry path.
**Value:** this is the only option that catches expiry **mid-session without waiting for a user action**. Everything else is reactive.
**What we'd want:** something cheap enough to call every 1–5 minutes, `allow_guest=True` so it answers rather than 403s, returning at minimum `{ authenticated: bool, user: string }`. If it also returned a snapshot version/etag we could skip re-fetching access when nothing changed.
**Risk:** adds a periodic request per tab (see §7 for what that costs at 20 machines).

### (d) A response header signalling session state

e.g. `X-Session-User: Guest` or `X-Session-Expired: 1` on every response.
**Frontend work: small** — one axios response interceptor reading the header and calling the existing expiry handler. Maybe 10 lines.
**Attraction:** it works on **every** response, so expiry is detected on the very next request the user makes, with **zero extra traffic**. That is strictly better than polling.
**Risk / blocker:** CORS. We are cross-origin, so any custom header must be listed in **`Access-Control-Expose-Headers`** or the browser will hide it from JavaScript. Easy to get wrong and it fails silently. Also [UNVERIFIED] whether your nginx/proxy layer strips or forwards custom headers.

### (e) What I would rather have — see §6.

---

## 6. What we want from the backend, plainly

**Ranked.**

**1. Return `401` (preferred) or `403` (also fine) whenever the session is Guest, on every `/api/*` endpoint — never `200`.**

This is the single highest-value change and it needs **no frontend work at all**; both paths already terminate correctly in a logout + toast + redirect. The reason it matters more than anything else in this document is §3: **a `200` is undetectable to us.** We can build any amount of frontend cleverness and it will not help if the server says "here is your empty list" instead of "I don't know who you are".

If you can only do one thing, do this.

**2. Add `Access-Control-Expose-Headers` + a session header** as a belt-and-braces layer.

The ideal shape, on every response:

```
HTTP/1.1 200 OK
X-Session-User: Administrator          # or "Guest"
Access-Control-Expose-Headers: X-Session-User
```

We would read this in one interceptor and route `Guest` straight into the existing expiry handler. It costs no extra requests, catches expiry on the next call the user makes for any reason, and degrades gracefully — if the header is missing we just carry on as today.

**3. If you build a session-check endpoint**, this shape makes it trivially correct for us:

```json
{
  "authenticated": true,
  "user": "doctor@clinic.local",
  "expires_in": 1800,
  "access_version": "a3f9c1"
}
```

`expires_in` would let us warn the user *before* they lose work — the clinical use case is a vet mid-visit with unsaved notes, which is the thing we most want to avoid. `access_version` would let us skip re-fetching the whole access snapshot when nothing changed.

**4. `Access-Control-Max-Age`** on preflights. Unrelated to session handling, but every API call in production currently pays a preflight round trip. Cheap, large win.

**What we do not need:** a websocket or push channel for session state. Polite refusal — it is more infrastructure than this problem justifies, and options 1–2 solve it.

---

## 7. Every request a normal session makes

**On boot (once):**

| Request | Notes |
|---|---|
| `pet_app.api.permissions.get_current_access` | **×2** — one from the app bootstrap, one from the router guard. Uncoordinated; worth knowing when you read logs |
| `frappe.core.doctype.user.user.get_roles` | **only** if `get_current_access` fails for a non-auth reason |
| `frappe.auth.get_logged_user` | only on a `403` (the probe, §4), de-duplicated |
| route-specific data calls | varies by landing page |

**Periodic, always on while any tab is open:**

| Source | Interval | Notes |
|---|---|---|
| Notification poll | **15 s** visible / **45 s** hidden | Global store. The single most frequent request in a normal session |

**Periodic, only while a specific page is open:**

| Page | Interval |
|---|---|
| Healthcare coordinator workspace | 30 s (skipped while tab hidden or a fetch is in flight) |
| WhatsApp monitoring panel | 15 s |
| WhatsApp inbox / live threads | 4 s active thread, 12 s list; 25 s / 45 s when hidden |
| Queue display screen | 2 s |
| Queue doctor screen | 4 s |
| Guardian conversation (while open) | 1.5 s |
| Import/export status (while an import runs) | 2.5 s |

**Baseline for session-lifetime reasoning:** a clinic machine sitting on a normal page makes **~4 requests/minute** (the notification poll). A machine on the queue display makes **~30/minute**. All of these are authenticated requests, so **an idle-timeout policy based on request activity will effectively never expire an open tab** — the notification poll alone keeps every session alive indefinitely. If you intend sessions to expire, it must be on absolute age, not inactivity.

**Also relevant:** in production every one of these is cross-origin, so without `Access-Control-Max-Age` each is **two** HTTP round trips. Roughly double the numbers above at the nginx layer.

### A bug we already fixed that you may still see in logs

Until today, a component leaked a 30-second interval on every visit to the healthcare coordinator page — each visit added a permanent timer that never stopped, even after navigating away. A machine could accumulate 20+ of them over a shift, each firing `get_count` + a `Healthcare Practitioner` lookup forever. One captured tab reached **3347 requests**. **If your access logs show a single client hammering `frappe.client.get_count?doctype=PetCareService` alternating with `Healthcare Practitioner` list calls, that is this bug, and it is fixed — not a session issue.** Deploy the frontend before drawing conclusions from historical logs.

---

## 8. Constraints — what cannot change

- **The 15 s notification poll stays unless the owner decides otherwise.** It is a deliberate product decision (staff must see new notifications promptly), not an accident. Please do not design a fix that requires reducing it. Its side effect on session lifetime (above) is a fact to design around, not a bug to fix.
- **The access snapshot must stay.** It exists because permission checks run *synchronously* during rendering — the sidebar resolves permissions for ~80–100 nav nodes on every render, and route guards must decide before a page mounts. Neither can await a network call. The snapshot is not a cache we added for speed; the architecture requires a locally-readable answer. Any design that assumes the frontend can ask the backend per permission check is not viable.
- **The 8 h snapshot TTL is a judgement call**, chosen to cover one shift while guaranteeing a snapshot cannot survive overnight on a shared machine. If your session lifetime is shorter, tell us and we will lower it to match — **the snapshot TTL should never exceed the session lifetime**, and right now we do not know what that is.
- **The app runs over plain HTTP in some deployments**, so anything depending on a secure context is unavailable.
- **Frontend and API are different origins in production.** Any header-based solution must handle CORS exposure explicitly.
- **No Frappe Desk JS.** We cannot rely on `frappe.*` client globals, Desk realtime, or Desk session handling. Everything must work over plain REST.
- **We will not add a fifth speculative fix.** Four hypotheses were investigated and rejected during this incident; three separate real bugs were found and fixed along the way that turned out not to be the reported symptom. We are now working from confirmed evidence only. If you propose a change, we would like to know what evidence would confirm or refute it before we ship it.

---

## Backend handoff note: standard response hazard

`pet_app.api.response.standardize_response` catches every exception and returns a successful JSON envelope with `ok:false` instead of preserving the HTTP status. That is acceptable only for endpoints whose clients intentionally parse the envelope. It is dangerous for auth and permission failures because the SPA's session-expiry handling depends on a non-2xx status. Do not wrap authentication/permission failures in `200 {ok:false}`.

## Open questions for you

1. **What is the actual session lifetime** on the server (`session_expiry`, and any idle vs absolute distinction)? We are guessing, and our 8 h TTL should be derived from it.
2. **Which auth path does production actually use** — OAuth Bearer, `sid` cookie, or API keys? This determines what "expiry" even means for our users. We have all three implemented and cannot tell from the frontend which is live.
3. **Are there endpoints that answer a Guest with `200`** rather than `403`? Per §3, those are invisible to us and are the likeliest cause of the reported symptom.
4. **Does `pet_app.api.permissions.get_current_access` have `allow_guest`?** We inferred *whitelisted but not guest-allowed* from the fact that it works for real users at 0.13 s. Worth confirming.
5. **Can your proxy layer add and expose a response header**, or is it stripped?
