/* Atropos dashboard service worker — true offline support.
 * Caches the dashboard shell and serves it from cache when the network
 * is unavailable (offline mode). Never caches /api/* responses.
 */
'use strict';

const CACHE = 'atropos-v1.2';
const SHELL = ['./', './index.html'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  // API calls always hit the network (auth + live data).
  if (url.pathname.startsWith('/api/')) return;
  // Only handle same-origin GETs for the shell.
  if (event.request.method !== 'GET' || url.origin !== self.location.origin) return;
  event.respondWith(
    fetch(event.request)
      .then((resp) => {
        if (resp && resp.ok && url.pathname === '/') {
          const copy = resp.clone();
          caches.open(CACHE).then((cache) => cache.put('./', copy));
        }
        return resp;
      })
      .catch(() =>
        caches.match(event.request).then((hit) => hit || caches.match('./'))
      )
  );
});