// Minimal service worker: exists to make the app installable (Add to Home
// Screen) as a real PWA. Deliberately network-passthrough — no caching, so
// there's never a stale app shell to debug; the app requires the network
// anyway (every action is an API call to the same origin).
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {
  // No respondWith → the browser handles the request normally.
});
