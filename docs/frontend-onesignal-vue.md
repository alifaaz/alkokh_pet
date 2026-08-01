# OneSignal Push Notifications - Vue Frontend Handoff

This setup uses Frappe/Pet App as the backend configuration and sender, while the Vue frontend owns browser registration, permission prompts, and the OneSignal service worker.

## Backend Configuration

In **Pet App Notification Settings** set:

```text
enabled = 1
onesignal_enabled = 1
onesignal_web_enabled = 1
onesignal_mobile_enabled = 1
onesignal_mirror_frappe_notifications = 1
onesignal_app_id = <OneSignal App ID>
onesignal_rest_api_key = <OneSignal REST API Key>
onesignal_service_worker_path = OneSignalSDKWorker.js
onesignal_service_worker_scope = /onesignal/
push_frontend_base_url = https://<vue-domain>
dry_run = 1 while testing, then 0 for live sends
```

Use the **Vue app domain** in OneSignal Web Push settings, not the Frappe API domain, if they are different domains.
Set `push_frontend_base_url` to the Vue app origin so push notification clicks open Vue routes instead of Frappe Desk.

After deploy:

```bash
bench --site <site-name> migrate
bench --site <site-name> clear-cache
bench restart
```

## Frontend Service Worker

For a Vite/Vue app, create:

```text
public/OneSignalSDKWorker.js
```

With this content:

```js
importScripts("https://cdn.onesignal.com/sdks/web/v16/OneSignalSDK.sw.js");
```

Verify in the browser:

```text
https://<vue-domain>/OneSignalSDKWorker.js
```

It must be public, same-origin with the Vue app, served over HTTPS, and return JavaScript content.

## Backend Endpoints

All endpoints require the logged-in user session or API auth.

```text
GET  /api/method/pet_app.api.push.get_config
POST /api/method/pet_app.api.push.register_subscription
POST /api/method/pet_app.api.push.unregister_subscription
GET  /api/method/pet_app.api.push.list_subscriptions
POST /api/method/pet_app.api.push.manual_register_subscription
POST /api/method/pet_app.api.push.send_test_push
GET  /api/method/pet_app.api.push.get_onesignal_status
```

Over HTTP, Frappe wraps method return values in `message`. `get_config` returns:

```json
{
  "message": {
    "ok": true,
    "data": {
      "enabled": true,
      "web_enabled": true,
      "mobile_enabled": true,
      "app_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
      "external_id": "user@example.com",
      "service_worker_path": "OneSignalSDKWorker.js",
      "service_worker_scope": "/onesignal/",
      "frontend_base_url": "https://clinic.example.com"
    }
  }
}
```

The backend sends push to:

```text
OneSignal external_id = Frappe User.name
```

So the Vue app must call `OneSignal.login(config.external_id)` after the user is authenticated.

## Manual Debug APIs

Use these only for testing. A normal user can test/register only themselves. `Administrator` or a System Manager can pass another `user`.

Manual register from values you copied from the browser or OneSignal:

```http
POST /api/method/pet_app.api.push.manual_register_subscription
Content-Type: application/json
```

```json
{
  "user": "Administrator",
  "subscription_id": "4088b8e0-515c-419a-994a-0f3ccef5f091",
  "onesignal_id": "0468819a-56d0-4989-ab53-8d9139fa3fbf",
  "platform": "web",
  "permission": "granted",
  "token": "<push token if available>",
  "opted_in": 1
}
```

Check what OneSignal currently has for a Frappe user:

```http
GET /api/method/pet_app.api.push.get_onesignal_status?user=Administrator
```

Response data contains one selected/top subscription for backward-compatible simple UI fields, plus all known subscriptions for multi-device UI:

```json
{
  "user": "Administrator",
  "external_id": "Administrator",
  "onesignal_id": "0468819a-56d0-4989-ab53-8d9139fa3fbf",
  "subscription_id": "4088b8e0-515c-419a-994a-0f3ccef5f091",
  "enabled": true,
  "token_present": true,
  "sendable_subscription_count": 2,
  "sendable_subscription_ids": [
    "4088b8e0-515c-419a-994a-0f3ccef5f091",
    "54d3a2f0-f0a0-4abf-abb6-89a9617b4cc0"
  ],
  "subscriptions": [
    {
      "id": "4088b8e0-515c-419a-994a-0f3ccef5f091",
      "type": "ChromePush",
      "enabled": true,
      "token_present": true,
      "app_id_matches": true,
      "sendable": true
    }
  ],
  "local_subscriptions": [
    {
      "subscription_id": "4088b8e0-515c-419a-994a-0f3ccef5f091",
      "platform": "web",
      "device_id": "Mozilla/5.0 ...",
      "token_present": true,
      "opted_in": true,
      "disabled": false
    }
  ],
  "local_active_count": 2
}
```

Expected sendable subscription:

```text
sendable_subscription_count > 0
```

If the OneSignal user or web subscription does not exist, this endpoint still returns `ok = true` with:

```text
onesignal_id = null
subscription_id = null
enabled = false
token_present = false
sendable_subscription_count = 0
```

List backend-audited local device registrations:

```http
GET /api/method/pet_app.api.push.list_subscriptions?include_disabled=1
```

Use `local_subscriptions[]` / `list_subscriptions` for the Vue device list. Do not treat the top-level `subscription_id` as the only device; it is just the selected/default row for old debug UI.

Send a normal backend test through `external_id = Frappe User.name`. This targets all sendable OneSignal push subscriptions for that Frappe user:

```http
POST /api/method/pet_app.api.push.send_test_push
Content-Type: application/json
```

```json
{
  "user": "Administrator",
  "title": "Pet App test push",
  "body": "Testing push notifications now."
}
```

When `push_frontend_base_url` is configured, test push clicks open that Vue app origin. They do not open Frappe Desk.

Send a direct diagnostic test to one subscription ID:

```json
{
  "user": "Administrator",
  "subscription_id": "4088b8e0-515c-419a-994a-0f3ccef5f091",
  "title": "Pet App direct test",
  "body": "Testing this exact subscription."
}
```

Passing `subscription_id` intentionally sends to only that one exact device. Omit `subscription_id` when testing all devices for the user.

If direct test fails with `invalid_player_ids`, the subscription ID is not sendable in the configured OneSignal app. If normal test fails with `invalid_aliases`, the OneSignal user is not linked to that Frappe external ID.

Stable backend error codes used by these debug APIs:

```text
PUSH_NOT_CONFIGURED
PUSH_USER_FORBIDDEN
PUSH_USER_NOT_FOUND
PUSH_SUBSCRIPTION_NOT_FOUND
PUSH_SUBSCRIPTION_NOT_SENDABLE
PUSH_PROVIDER_REJECTED
```

`manual_register_subscription` validates the subscription against OneSignal before saving it. It only saves `disabled = 0` when the subscription belongs to the configured OneSignal app, is linked to the requested Frappe user, is enabled, and has a non-empty push token.

## Vue Integration Example

Create `src/services/pushNotifications.ts`:

```ts
type PushConfig = {
  enabled: boolean;
  web_enabled: boolean;
  app_id: string | null;
  external_id: string;
  service_worker_path: string;
  service_worker_scope: string;
};

declare global {
  interface Window {
    OneSignalDeferred?: Array<(OneSignal: any) => Promise<void> | void>;
  }
}

const API_BASE = import.meta.env.VITE_FRAPPE_API_BASE || "";

function unwrapFrappeResponse(payload: any) {
  return payload.message || payload;
}

async function frappeGet<T>(method: string): Promise<T> {
  const response = await fetch(`${API_BASE}/api/method/${method}`, {
    credentials: "include",
  });
  const result = unwrapFrappeResponse(await response.json());
  if (!result.ok) throw new Error(result.errors?.[0]?.message || "Request failed");
  return result.data;
}

async function frappePost<T>(method: string, body: Record<string, unknown>): Promise<T> {
  const response = await fetch(`${API_BASE}/api/method/${method}`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const result = unwrapFrappeResponse(await response.json());
  if (!result.ok) throw new Error(result.errors?.[0]?.message || "Request failed");
  return result.data;
}

function loadOneSignalSdk(): Promise<void> {
  return new Promise((resolve, reject) => {
    if (document.querySelector("script[data-onesignal-sdk]")) {
      resolve();
      return;
    }

    const script = document.createElement("script");
    script.src = "https://cdn.onesignal.com/sdks/web/v16/OneSignalSDK.page.js";
    script.defer = true;
    script.dataset.onesignalSdk = "1";
    script.onload = () => resolve();
    script.onerror = () => reject(new Error("Failed to load OneSignal SDK"));
    document.head.appendChild(script);
  });
}

async function registerBackendSubscription(OneSignal: any) {
  const subscription = OneSignal.User?.PushSubscription;
  const subscriptionId = subscription?.id;
  const token = subscription?.token;
  const optedIn = Boolean(subscription?.optedIn);
  if (!subscriptionId || !token || !optedIn) return;

  await frappePost("pet_app.api.push.register_subscription", {
    subscription_id: subscriptionId,
    onesignal_id: OneSignal.User?.onesignalId || null,
    platform: "web",
    device_id: navigator.userAgent,
    permission: OneSignal.Notifications?.permission,
    token,
    opted_in: optedIn,
  });
}

function waitForSubscribedPush(OneSignal: any, timeoutMs = 10000): Promise<boolean> {
  const subscription = OneSignal.User?.PushSubscription;
  if (subscription?.id && subscription?.token && subscription?.optedIn) {
    return Promise.resolve(true);
  }

  return new Promise((resolve) => {
    const timeout = window.setTimeout(() => {
      subscription?.removeEventListener?.("change", listener);
      resolve(false);
    }, timeoutMs);

    function listener(event: any) {
      const current = event?.current || OneSignal.User?.PushSubscription;
      if (current?.id && current?.token && current?.optedIn) {
        window.clearTimeout(timeout);
        subscription?.removeEventListener?.("change", listener);
        resolve(true);
      }
    }

    subscription?.addEventListener?.("change", listener);
  });
}

export async function initPushNotifications() {
  const config = await frappeGet<PushConfig>("pet_app.api.push.get_config");
  if (!config.enabled || !config.web_enabled || !config.app_id) return;

  window.OneSignalDeferred = window.OneSignalDeferred || [];
  window.OneSignalDeferred.push(async (OneSignal: any) => {
    await OneSignal.init({
      appId: config.app_id,
      serviceWorkerPath: config.service_worker_path,
      serviceWorkerParam: {
        scope: config.service_worker_scope,
      },
    });

    const supported = await OneSignal.Notifications?.isPushSupported?.();
    if (!supported) return;

    await OneSignal.login(config.external_id);

    if (!OneSignal.User?.PushSubscription?.optedIn) {
      await OneSignal.User?.PushSubscription?.optIn?.();
    }

    if (await waitForSubscribedPush(OneSignal)) {
      await registerBackendSubscription(OneSignal);
    }

    OneSignal.User?.PushSubscription?.addEventListener?.("change", async () => {
      await registerBackendSubscription(OneSignal);
    });
  });

  await loadOneSignalSdk();
}

export async function disablePushNotifications(OneSignal: any) {
  const subscriptionId = OneSignal.User?.PushSubscription?.id || OneSignal.User?.PushSubscription?.token;
  if (subscriptionId) {
    await frappePost("pet_app.api.push.unregister_subscription", {
      subscription_id: subscriptionId,
    });
  }
  await OneSignal.logout?.();
}
```

Call it after login/session restore, for example in `main.ts` or your auth store:

```ts
import { initPushNotifications } from "@/services/pushNotifications";

await restoreSession();
await initPushNotifications();
```

## CORS/Auth Notes

If Vue and Frappe are on different origins:

- `VITE_FRAPPE_API_BASE` must point to the Frappe backend origin.
- The frontend must send session cookies with `credentials: "include"` or use token auth.
- Frappe must allow the Vue origin through CORS.
- The OneSignal service worker still must be hosted on the Vue origin, not the Frappe origin.

## Test Flow

1. Set `dry_run = 1`.
2. Login in the Vue app.
3. Call `initPushNotifications()`.
4. Allow browser notifications.
5. Confirm a `Pet App Push Subscription` row is created with `opted_in = 1`, `disabled = 0`, and a non-empty push token.
6. Set `dry_run = 0`.
7. Create a Frappe `Notification Log` for that same user.
8. Confirm a `Pet App Notification Queue` row with `channel = Push` is created and sent.

For iOS/iPadOS web push, the user must add the Vue app to the home screen, open it from the home screen, then subscribe from there. Browser-only Safari/Chrome on iPhone can create a OneSignal user record but may not create a sendable push token.

## Browser Debug Check

Run this in Vue browser DevTools after subscribing:

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

Expected:

```text
externalId = the same value returned by pet_app.api.push.get_config.data.external_id
subscriptionId = non-empty
token = non-empty
optedIn = true
permission = true or "granted"
```

Also confirm the Network tab has a successful request to:

```text
POST /api/method/pet_app.api.push.register_subscription
```

If OneSignal returns `invalid_aliases`, the browser is subscribed but is not logged into OneSignal with the external ID that backend is targeting.

## References

- OneSignal service worker: https://documentation.onesignal.com/docs/en/onesignal-service-worker
- OneSignal Web SDK reference: https://documentation.onesignal.com/docs/en/web-sdk-reference
- OneSignal create message API: https://documentation.onesignal.com/reference/create-message
