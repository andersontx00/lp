// Lightweight PWA shell. Never cache personalized sessions, prices or API responses.
const VERSION='radar-shell-9';
self.addEventListener('install',e=>{self.skipWaiting()});
self.addEventListener('activate',e=>{e.waitUntil(self.clients.claim())});
self.addEventListener('fetch',e=>{ /* Network-only for live pricing, login and favorites. */ });
