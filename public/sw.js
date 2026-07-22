// Minimal service worker: makes the app installable (required for iOS "Add to
// Home Screen") and serves the shell offline. Push handling lands in Phase 3.
const CACHE = "mynews-v1";
const SHELL = ["/", "/index.html", "/styles.css", "/app.js", "/config.js", "/manifest.json"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  // Network-first for API and Supabase calls; cache-first for the static shell.
  if (request.method !== "GET" || request.url.includes("/api/") ||
      request.url.includes("supabase.co")) {
    return;
  }
  event.respondWith(
    caches.match(request).then((cached) => cached || fetch(request))
  );
});
