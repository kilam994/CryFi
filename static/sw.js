// CryFi service worker — network-first so the app is never stale, with an
// offline fallback to the cached shell. API/WS and non-GET requests are never
// cached (they're dynamic and auth-gated).
const CACHE = "cryfi-v1";
const SHELL = [
  "/", "/login",
  "/static/css/style.css",
  "/static/js/app.js",
  "/static/js/api.js",
  "/static/icons/icon-192.png",
  "/static/favicon.svg",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL).catch(() => {})));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return; // never intercept mutations
  const url = new URL(req.url);
  const cacheable = url.origin === location.origin && !url.pathname.startsWith("/api");

  e.respondWith(
    fetch(req)
      .then((res) => {
        if (cacheable && res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return res;
      })
      .catch(() =>
        caches.match(req).then((m) => m || (req.mode === "navigate" ? caches.match("/") : undefined))
      )
  );
});
