const CACHE = "cxy-finance-v1"
const SHELL = ["./", "index.html", "manifest.json", "favicon.svg", "icon-192.png", "icon-512.png"]

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}))
  self.skipWaiting()
})

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))))
  self.clients.claim()
})

self.addEventListener("fetch", (e) => {
  const u = new URL(e.request.url)
  if (e.request.method !== "GET") return
  if (u.pathname.endsWith("/api/data") || u.pathname.endsWith("/api/months") || u.pathname.endsWith("/api/add-month") || u.pathname.endsWith("/api/trends") || u.pathname.endsWith("/api/ping") || u.pathname.endsWith("/api/verify-pin")) {
    e.respondWith(networkFirst(e.request))
  } else {
    e.respondWith(cacheFirst(e.request))
  }
})

async function cacheFirst(req) {
  const cached = await caches.match(req)
  if (cached) return cached
  try {
    const resp = await fetch(req)
    if (resp.ok) {
      const clone = resp.clone()
      caches.open(CACHE).then((c) => c.put(req, clone))
    }
    return resp
  } catch {
    return cached || new Response("Offline", { status: 503 })
  }
}

async function networkFirst(req) {
  try {
    const resp = await fetch(req)
    if (resp.ok) {
      const clone = resp.clone()
      caches.open(CACHE).then((c) => c.put(req, clone))
    }
    return resp
  } catch {
    return caches.match(req)
  }
}
