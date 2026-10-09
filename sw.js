// Inkling 오프라인용 서비스워커: 앱 파일을 기기에 담아 두고, 인터넷이 되면 조용히 새 버전으로 바꾼다
const CACHE = 'inkling-v1.4';
const FILES = [
  './', './index.html', './manifest.webmanifest',
  './icons/icon-192.png', './icons/icon-512.png', './icons/maskable-192.png', './icons/maskable-512.png', './icons/apple-touch-icon.png',
  './fonts/fraunces-latin-500-normal.woff2',
  './fonts/fraunces-latin-600-normal.woff2',
  './fonts/outfit-latin-300-normal.woff2',
  './fonts/outfit-latin-400-normal.woff2',
  './fonts/outfit-latin-500-normal.woff2',
  './fonts/outfit-latin-600-normal.woff2',
];
self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(FILES)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET' || new URL(req.url).origin !== location.origin) return;
  e.respondWith(caches.open(CACHE).then(c => c.match(req, { ignoreSearch: true }).then(hit => {
    const net = fetch(req).then(res => { if (res && res.ok) c.put(req, res.clone()); return res; }).catch(() => hit);
    return hit || net;
  })));
});
