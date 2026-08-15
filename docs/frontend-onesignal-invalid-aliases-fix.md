# OneSignal Push — Why It Works "Sometimes", and the Frontend Fix

**Status:** Backend investigated, root cause confirmed against production data.
**Owner of the fix:** Frontend (Vue). The backend send path is working correctly.
**Date:** 2026-08-13

---

## TL;DR

Push is not randomly broken. It fails **per user**, permanently, and the cause is that the
browser never told OneSignal who the user is.

The backend sends to `external_id = <Frappe User.name>`. If the Vue app did not successfully
call `OneSignal.login(external_id)`, OneSignal has no such user and rejects the send with
`invalid_aliases`. The user still sees a granted notification permission and still has a row
in our database, so **everything looks fine on the client while nothing can ever be delivered.**

The current init code in `frontend-onesignal-vue.md` has no error handling and does not wait for
the session, so whether `login()` lands is a race. That is the "sometimes".

---

## The Evidence

Production totals: **2055 sent / 265 failed (~11% failure).**

Credentials, service worker, app ID, and the scheduler are all fine — 2055 successful sends
prove the pipeline works. What matters is *how the failures cluster*:

```
sent=0    failed=41    zainabalijafer@gmail.com    <- never receives anything
sent=0    failed=6     asd@gmail.com               <- never receives anything
sent=89   failed=106   dr.ayman@petapp.com         <- "sometimes"
sent=202  failed=53    abbas@app.com               <- "sometimes"
sent=107  failed=0     vip11n@gmail.com            <- always works
sent=74   failed=0     zayni.reema@gmail.com       <- always works
```

A broken backend fails for everyone. This fails for *specific users*, which points at
per-browser identity state.

### Two different errors, two different meanings

| Error from OneSignal | Count | What it actually means |
|---|---:|---|
| `invalid_aliases` | ~240 | **No OneSignal user has this `external_id`.** `OneSignal.login()` never succeeded. |
| `All included players are not subscribed` | ~25 | Device is known but its push token is dead (permission revoked, browser data cleared, token expired). |

`invalid_aliases` is ~90% of all failures. That is the one to fix.

### The local database lies

These two users have **zero** push subscription rows, yet the backend happily sent them
41 and 26 notifications:

```
mostafaomar9751@gmail.com:  total_subs=0  active=0   (26 failures)
asd@gmail.com:              total_subs=0  active=0   (6 failures)
```

And this user has **7 active local rows** and still gets `invalid_aliases`:

```
dr.ayman@petapp.com:        total_subs=7  active=7   (106 failures)
```

So a healthy-looking row in `Pet App Push Subscription` does **not** mean the user can receive
push. `register_subscription` records what the browser reported; it does not prove the browser
ever logged into OneSignal with the right external ID. Stale rows also pile up because nothing
prunes them when OneSignal rejects a send.

---

## Root Cause

The backend targets the user by alias:

```python
# apps/pet_app/pet_app/notifications/channels/onesignal.py
payload = {
    "app_id": self._app_id(),
    "include_aliases": {"external_id": [user]},   # <- Frappe User.name
    "target_channel": "push",
    ...
}
```

Delivery therefore depends **entirely** on `OneSignal.login(external_id)` having succeeded in
the browser. The documented init flow does this:

```ts
window.OneSignalDeferred.push(async (OneSignal: any) => {
  await OneSignal.init({ ... });                  // can throw
  const supported = await OneSignal.Notifications?.isPushSupported?.();
  if (!supported) return;
  await OneSignal.login(config.external_id);      // can throw, can run pre-session
  ...
});
```

Three defects, all in the frontend:

1. **No `try/catch`.** The callback is an async function handed to the SDK. If `init()` or
   `login()` rejects, the promise is unhandled, the callback dies silently, and no retry
   happens. Nothing is logged and nothing surfaces to the user.
2. **No ordering guarantee against session restore.** `initPushNotifications()` first fetches
   `get_config`, which needs a session. If it is called while the session is still being
   restored, `external_id` can be wrong or the call fails — and the whole callback aborts.
3. **No verification after login.** Nothing ever asserts that
   `OneSignal.User.externalId === config.external_id`. A browser can be subscribed under a
   *previous* user (shared device, account switch, re-login) and stay that way indefinitely.

Because these are timing- and state-dependent, the same user succeeds on one page load and
fails on the next. That is exactly the reported symptom.

> Note: `OneSignal.login()` resolving is necessary but not sufficient — it can resolve before
> the identity is fully linked server-side. The verification step below is what actually
> closes the gap.

---

## The Fix

Replace `initPushNotifications` in `src/services/pushNotifications.ts` with the version below.
Changes marked `// FIX:`.

```ts
export async function initPushNotifications() {
  // FIX 1: never let this throw into the caller and kill app boot.
  try {
    const config = await frappeGet<PushConfig>("pet_app.api.push.get_config");
    if (!config.enabled || !config.web_enabled || !config.app_id) return;
    if (!config.external_id) {
      console.warn("[push] no external_id from backend - session not ready, aborting");
      return;
    }

    window.OneSignalDeferred = window.OneSignalDeferred || [];
    window.OneSignalDeferred.push(async (OneSignal: any) => {
      // FIX 2: guard the entire deferred callback. Without this, any throw is
      // swallowed by the SDK and push silently never works for this user.
      try {
        await OneSignal.init({
          appId: config.app_id,
          serviceWorkerPath: config.service_worker_path,
          serviceWorkerParam: { scope: config.service_worker_scope },
        });

        const supported = await OneSignal.Notifications?.isPushSupported?.();
        if (!supported) {
          console.info("[push] push not supported on this browser");
          return;
        }

        // FIX 3: verify identity instead of assuming login() worked.
        // Handles account switching on a shared device and partial logins.
        const currentExternalId = OneSignal.User?.externalId;
        if (currentExternalId && currentExternalId !== config.external_id) {
          console.warn("[push] external_id mismatch, logging out stale identity", {
            was: currentExternalId,
            expected: config.external_id,
          });
          await OneSignal.logout?.();
        }
        if (OneSignal.User?.externalId !== config.external_id) {
          await OneSignal.login(config.external_id);
        }

        // FIX 4: confirm the alias actually linked. If this check fails the
        // backend WILL get invalid_aliases, so surface it loudly.
        if (OneSignal.User?.externalId !== config.external_id) {
          console.error("[push] login did not link external_id - backend sends will fail", {
            got: OneSignal.User?.externalId,
            expected: config.external_id,
          });
          return;
        }

        if (!OneSignal.User?.PushSubscription?.optedIn) {
          await OneSignal.User?.PushSubscription?.optIn?.();
        }

        if (await waitForSubscribedPush(OneSignal)) {
          await registerBackendSubscription(OneSignal);
        }

        OneSignal.User?.PushSubscription?.addEventListener?.("change", async () => {
          // FIX 5: this listener also needs a guard - a throw here kills all
          // future change events for the session.
          try {
            await registerBackendSubscription(OneSignal);
          } catch (err) {
            console.error("[push] failed to sync subscription change", err);
          }
        });
      } catch (err) {
        console.error("[push] OneSignal init/login failed", err);
      }
    });

    await loadOneSignalSdk();
  } catch (err) {
    console.error("[push] initPushNotifications failed", err);
  }
}
```

### Call it only after the session is real

This is as important as the code above. `get_config` returns `external_id` from the logged-in
session — calling it too early yields the wrong identity or fails outright.

```ts
// main.ts / auth store
await restoreSession();
if (authStore.isAuthenticated) {
  await initPushNotifications();   // never before this point
}
```

### Re-run it on every login

A user logging in as someone else on the same browser keeps the previous `external_id` until
`login()` is called again. Call `initPushNotifications()` in your login success handler too,
not just at app boot. The identity check in FIX 3 will clear the stale one.

### Log out of OneSignal on app logout

Otherwise the next user on that device inherits the previous subscription:

```ts
async function onLogout(OneSignal: any) {
  await disablePushNotifications(OneSignal);   // already exists in the service
}
```

---

## How to Verify

Run this in DevTools on the Vue app **after logging in**:

```js
window.OneSignalDeferred = window.OneSignalDeferred || [];
OneSignalDeferred.push(async function (OneSignal) {
  console.log({
    externalId: OneSignal.User.externalId,
    onesignalId: OneSignal.User.onesignalId,
    subscriptionId: OneSignal.User.PushSubscription.id,
    token: OneSignal.User.PushSubscription.token,
    optedIn: OneSignal.User.PushSubscription.optedIn,
    permission: OneSignal.Notifications.permission,
  });
});
```

Pass condition — **`externalId` must exactly equal** the `external_id` from
`GET /api/method/pet_app.api.push.get_config`, and `token` must be non-empty.

If `externalId` is `null` or a different user, this browser cannot receive push. That is the bug.

Backend cross-check for any user (Administrator / System Manager can pass `?user=`):

```http
GET /api/method/pet_app.api.push.get_onesignal_status?user=someone@example.com
```

Pass condition: `sendable_subscription_count > 0`.
If it is `0`, the backend has nothing to send to regardless of what the local device list shows.

Then send a real test:

```http
POST /api/method/pet_app.api.push.send_test_push
{ "user": "someone@example.com", "title": "test", "body": "test" }
```

| Result | Meaning |
|---|---|
| Delivered | Fixed. |
| `invalid_aliases` | `login()` still not linking — recheck ordering vs session restore. |
| `invalid_player_ids` / `not subscribed` | Identity is fine; the device token is dead. User must re-subscribe. |

---

## Notes and Caveats

- **iOS/iPadOS web push:** the user must add the app to the home screen and subscribe *from
  the home-screen app*. Safari/Chrome tabs on iPhone can create a OneSignal user record with
  **no sendable token** — which shows up as a subscribed-looking client that never receives
  anything. Worth an explicit in-app hint for iOS users.
- **Service worker must be same-origin** with the Vue app, public, HTTPS, and return JS:
  `https://<vue-domain>/OneSignalSDKWorker.js`. It cannot be hosted on the Frappe origin.
- **`push_frontend_base_url`** is set to `https://clinic.kokh-vet.com`. OneSignal's Web Push
  config must use that same Vue origin, not the Frappe API domain.
- The ~25 `not subscribed` failures are **not** fixed by this change. Those are genuinely dead
  tokens; the backend will be updated to disable those rows instead of retrying them.

---

## What the Backend Will Change (FYI — no frontend action needed)

These are queued on the backend side and are listed so both sides stay in sync:

1. **Skip queueing when the user has no sendable subscription** — return
   `PUSH_SUBSCRIPTION_NOT_SENDABLE` instead of queueing a send that cannot succeed.
2. **Treat `invalid_aliases` and `not subscribed` as terminal** — currently every such failure
   is retried 3× at 5-minute intervals (222 rows sat at `retry_count=3`), which is pure waste
   since neither error can recover on retry. They will be marked `Cancelled` and the stale
   local subscription rows set to `disabled=1`.
3. **Fix a naming-series deadlock** — mirrored pushes send to both the user and Administrator
   simultaneously and occasionally collide on `tabSeries` (`QueryDeadlockError`). This one is
   genuinely random and drops a real notification when it hits. Rare (1 occurrence) but real.

There are also 3 push rows stuck in `Queued` since 2026-08-12 that were never drained. Cause not
yet confirmed — likely one bad row aborting a scheduler batch, since `process_due_notifications`
has no per-row exception handling. Being investigated separately; it is not related to the
`invalid_aliases` issue above.

---

## Priority

Fix the frontend init first. It accounts for ~90% of all push failures, and no backend change
can compensate for a browser that never registered its identity with OneSignal.
