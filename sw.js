var CACHE_NAME = 'cyxnb-zoho-v3';
var ASSETS = [
  '/',
  '/index.html',
  '/manifest.json',
  '/favicon.svg',
  '/icon-192.png',
  '/icon-512.png',
];

self.addEventListener('install', function(event) {
  event.waitUntil(
    caches.open(CACHE_NAME).then(function(cache) {
      return Promise.all(ASSETS.map(function(url) {
        return fetch(url).then(function(r) {
          if (r.ok) cache.put(url, r);
        }).catch(function(e) {
          console.warn('SW cache fail:', url, e.message);
        });
      }));
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', function(event) {
  event.waitUntil(
    caches.keys().then(function(keys) {
      return Promise.all(
        keys.filter(function(k) { return k !== CACHE_NAME; }).map(function(k) {
          return caches.delete(k);
        })
      );
    })
    .then(function() {
      return caches.open(CACHE_NAME).then(function(cache) {
        return Promise.all(ASSETS.map(function(url) {
          return fetch(url + '?t=' + Date.now()).then(function(r) {
            if (r.ok) cache.put(url, r);
          }).catch(function(){});
        }));
      });
    })
    .then(function() {
      return self.clients.claim();
    })
  );
});

self.addEventListener('fetch', function(event) {
  var u = new URL(event.request.url);
  if (u.pathname.startsWith('/api/') || u.pathname === '/ping') return;
  if (u.pathname === '/' || u.pathname.endsWith('.html')) {
    event.respondWith(
      fetch(event.request).then(function(response) {
        if (response && response.status === 200) {
          var responseClone = response.clone();
          caches.open(CACHE_NAME).then(function(cache) {
            cache.put(event.request, responseClone);
          });
        }
        return response;
      }).catch(function() {
        return caches.match(event.request);
      })
    );
    return;
  }
  event.respondWith(
    caches.match(event.request).then(function(cached) {
      var networkFetch = fetch(event.request).then(function(response) {
        if (response && response.status === 200) {
          var responseClone = response.clone();
          caches.open(CACHE_NAME).then(function(cache) {
            cache.put(event.request, responseClone);
          });
        }
        return response;
      }).catch(function() {
        return cached;
      });
      return cached || networkFetch;
    })
  );
});
