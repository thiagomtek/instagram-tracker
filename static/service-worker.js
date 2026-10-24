const CACHE_NAME = 'instagram-tracker-shell-v3';
const APP_ROOT = new URL(self.registration.scope).pathname;
const OFFLINE_URL = `${APP_ROOT}offline`;

self.addEventListener('install', (event) => {
    event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll([
        OFFLINE_URL,
        `${APP_ROOT}static/style.css`,
        `${APP_ROOT}static/icon.svg`
    ])));
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys()
            .then((keys) => Promise.all(keys.filter((key) => key.startsWith('instagram-tracker-') && key !== CACHE_NAME).map((key) => caches.delete(key))))
            .then(() => self.clients.claim())
    );
});

// Static files: always try the network (they are versioned by modification time),
// keep the last good copy for offline use.
async function networkFirst(request) {
    const cache = await caches.open(CACHE_NAME);
    try {
        const response = await fetch(request);
        if (response.ok) cache.put(request, response.clone());
        return response;
    } catch (error) {
        const cached = await cache.match(request, {ignoreSearch: true});
        if (cached) return cached;
        throw error;
    }
}

self.addEventListener('fetch', (event) => {
    if (event.request.method !== 'GET') return;
    const requestUrl = new URL(event.request.url);
    if (requestUrl.origin !== self.location.origin) return;
    if (event.request.mode === 'navigate') {
        event.respondWith(fetch(event.request).catch(() => caches.match(OFFLINE_URL)));
        return;
    }
    if (requestUrl.pathname.startsWith(`${APP_ROOT}static/`)) {
        event.respondWith(networkFirst(event.request));
    }
});

self.addEventListener('push', (event) => {
    let payload = {};
    try { payload = event.data ? event.data.json() : {}; } catch (_error) { payload = {}; }
    event.waitUntil(self.registration.showNotification(payload.title || 'Instagram Tracker', {
        body: payload.body || 'Uma nova análise foi concluída.',
        icon: `${APP_ROOT}static/icon-192.png`,
        badge: `${APP_ROOT}static/icon-192.png`,
        tag: payload.tag || 'instagram-tracker',
        data: {url: payload.url || ''}
    }));
});

self.addEventListener('notificationclick', (event) => {
    event.notification.close();
    const relativeTarget = event.notification.data?.url || '';
    const targetUrl = new URL(relativeTarget, self.registration.scope).href;
    event.waitUntil(
        clients.matchAll({type: 'window', includeUncontrolled: true}).then((clientList) => {
            const existing = clientList.find((client) => client.url.startsWith(self.registration.scope));
            if (existing) {
                existing.navigate(targetUrl);
                return existing.focus();
            }
            return clients.openWindow(targetUrl);
        })
    );
});
