// Field app service worker: makes the app installable ("Add to Home Screen"). It caches nothing: the day, the road router and the map all
// need the network, so every request goes straight through and the crew always sees the live plan.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil((async () => {
  if (self.caches) await Promise.all((await caches.keys()).map((k) => caches.delete(k)));
  await self.clients.claim();
})()));
self.addEventListener("fetch", () => {});
