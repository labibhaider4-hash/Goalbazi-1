const CACHE_NAME = "goalbazi-pwa-v5";

// Files that make the installed PWA open quickly and still show a useful page offline.
const APP_SHELL = [
  "/",
  "/login",
  "/register",
  "/styles.css",
  "/nav.js",
  "/assets/goalbazi-logo.svg",
  "/manifest.webmanifest"
];

self.addEventListener("install", event => {
  // Pre-cache the app shell and move the new worker into the waiting phase quickly.
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(APP_SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", event => {
  // Remove old cache versions so users do not stay stuck on stale screens.
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("message", event => {
  // nav.js sends this when the user taps the "Update" banner.
  if (event.data && event.data.type === "SKIP_WAITING") {
    self.skipWaiting();
  }
});

self.addEventListener("fetch", event => {
  // Network-first for pages, cache fallback for slower/offline mobile connections.
  const request = event.request;
  const url = new URL(request.url);

  if (request.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/api/")) {
    return;
  }

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then(response => {
          const copy = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(request, copy));
          return response;
        })
        .catch(() => caches.match(request).then(cached => cached || caches.match("/")))
    );
    return;
  }

  event.respondWith(
    caches.match(request).then(cached => {
      if (cached) return cached;
      return fetch(request).then(response => {
        const copy = response.clone();
        caches.open(CACHE_NAME).then(cache => cache.put(request, copy));
        return response;
      });
    })
  );
});

self.addEventListener("push", event => {
  // Shows phone/browser notifications for direct messages and future alerts.
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = { title: "Goalbazi", body: "You have a new notification." };
  }

  event.waitUntil(
    self.registration.showNotification(data.title || "Goalbazi", {
      body: data.body || "You have a new notification.",
      icon: data.icon || "/assets/goalbazi-logo.svg",
      badge: data.badge || "/assets/goalbazi-logo.svg",
      data: { url: data.url || "/dashboard" },
    })
  );
});

self.addEventListener("notificationclick", event => {
  // Opens or focuses Goalbazi when the user taps a notification.
  event.notification.close();
  const targetUrl = event.notification.data?.url || "/dashboard";
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then(clientList => {
      for (const client of clientList) {
        if ("focus" in client) {
          client.navigate(targetUrl);
          return client.focus();
        }
      }
      return clients.openWindow(targetUrl);
    })
  );
});
