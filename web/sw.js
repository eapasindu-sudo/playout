// Endora Playout phone app: keeps the app shell so it still opens when the phone is off the
// playout Wi-Fi (it then says so). Live data (/api/...) always comes from the playout PC, never cache.
// Browsers only run this on https:// or localhost; on plain http over Wi-Fi the app works without it.
const CACHE = 'playout-remote-v1';
const SHELL = ['/remote', '/login', '/img/logo-playout.png', '/img/app-icon-192.png', '/manifest.webmanifest'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {}).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.origin !== location.origin || u.pathname.startsWith('/api/') || u.pathname.startsWith('/trigger/')) return;
  if (!SHELL.includes(u.pathname)) return;
  // network first (always the newest page from the PC), cached copy when the PC cannot be reached
  e.respondWith(fetch(e.request).then(r => {
    if (r.ok && !r.redirected) { const copy = r.clone(); caches.open(CACHE).then(c => c.put(u.pathname, copy)); }
    return r;
  }).catch(() => caches.match(u.pathname)));
});
