import { pushApi } from "./api/endpoints";

/** PushManager wants the VAPID public key as a raw Uint8Array, not the
 * base64url string the backend hands back. */
function urlBase64ToUint8Array(base64url: string): Uint8Array {
  const padding = "=".repeat((4 - (base64url.length % 4)) % 4);
  const base64 = (base64url + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return bytes;
}

export function pushSupported(): boolean {
  return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

export async function currentPushSubscription(): Promise<PushSubscription | null> {
  if (!pushSupported()) return null;
  const registration = await navigator.serviceWorker.ready;
  return registration.pushManager.getSubscription();
}

/** Asks the browser for permission (if not already decided) and subscribes
 * this device, telling the backend about it. Throws on denial/failure --
 * callers show the message. */
export async function enablePushNotifications(): Promise<void> {
  if (!pushSupported()) throw new Error("unsupported");
  const permission = await Notification.requestPermission();
  if (permission !== "granted") throw new Error("denied");
  const registration = await navigator.serviceWorker.ready;
  const { key } = await pushApi.vapidPublicKey();
  const subscription = await registration.pushManager.subscribe({
    userVisibleOnly: true,
    // TS's lib.dom types want Uint8Array<ArrayBuffer> specifically; the plain
    // Uint8Array built above is functionally identical at runtime.
    applicationServerKey: urlBase64ToUint8Array(key) as BufferSource,
  });
  await pushApi.subscribe(subscription.toJSON() as PushSubscriptionJSON);
}

export async function disablePushNotifications(): Promise<void> {
  const subscription = await currentPushSubscription();
  if (!subscription) return;
  await subscription.unsubscribe();
  await pushApi.unsubscribe(subscription.endpoint);
}
