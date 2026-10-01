// Present for two reasons: (1) PWA installability -- Chrome requires a
// registered SW with a fetch handler to offer "Install app" instead of a
// plain bookmark shortcut; (2) Web Push -- a push can arrive and show a
// notification even while no tab is open, which is the whole point (an
// agent-chat turn finishing while the phone is locked). No caching.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});

self.addEventListener("push", (event) => {
  let data = { title: "ComfyUI Orchestrator", body: "" };
  try {
    data = { ...data, ...event.data.json() };
  } catch {
    // no payload / not JSON -- show the generic title above
  }
  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: "/icon-192.png",
      badge: "/icon-192.png",
      data: { url: data.url || "/" },
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = event.notification.data?.url || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if ("focus" in client) return client.focus();
      }
      return self.clients.openWindow(url);
    }),
  );
});
