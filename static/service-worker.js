const CACHE_NAME = 'instagram-tracker-shell-v1';
const APP_ROOT = new URL(self.registration.scope).pathname;
const OFFLINE_URL = `${APP_ROOT}offline`;

self.addEventListener('install', (event) => {
    event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll([
        OFFLINE_URL,
        `${APP_ROOT}static/style.css`,
        `${APP_ROOT}static/app.js`,
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

self.addEventListener('fetch', (event) => {
    if (event.request.method !== 'GET') return;
    const requestUrl = new URL(event.request.url);
    if (requestUrl.origin !== self.location.origin) return;
    if (event.request.mode === 'navigate') {
        event.respondWith(fetch(event.request).catch(() => caches.match(OFFLINE_URL)));
        return;
    }
    if (requestUrl.pathname.includes('/static/')) {
        event.respondWith(caches.match(event.request).then((cached) => cached || fetch(event.request)));
    }
});

self.addEventListener('push', (event) => {
    let payload = {};
    try { payload = event.data ? event.data.json() : {}; } catch (_error) { payload = {}; }
    event.waitUntil(self.registration.showNotification(payload.title || 'Instagram Tracker', {
        body: payload.body || 'Uma nova análise foi concluída.',
        icon: `${APP_ROOT}static/icon.svg`,
        badge: `${APP_ROOT}static/icon.svg`,
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
