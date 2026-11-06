// 설치 가능한 앱(PWA)이 되려면 서비스 워커가 필요합니다. 캐시는 하지 않고 항상 네트워크를 사용합니다.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => e.respondWith(fetch(e.request)));
