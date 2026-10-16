// Service worker: aplikácia funguje aj offline.
// Stránky: zobrazí uloženú verziu hneď a na pozadí stiahne novú (pri ďalšom otvorení je aktuálna).
// Dáta: najprv sieť, bez signálu posledné uložené. Mapové dlaždice OSM sa neukladajú (podmienky OSM).
const VERSION = "v1";
const SHELL = `shell-${VERSION}`;
const DATA = "data";
const SHELL_FILES = [
  "./", "index.html", "mapa.html", "pwa.js", "manifest.webmanifest",
  "icons/icon.svg", "icons/icon-192.png", "icons/icon-512.png", "icons/apple-touch-icon.png",
];
const CDN_FILES = [
  "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js",
  "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css",
];
const DATA_FILES = ["data/today.json", "data/stats.json", "data/places.json", "data/reviews.json"];

// Súbory, ktoré sa nepodarí stiahnuť (napr. chýbajúci reviews.json), inštaláciu nezhodia.
const addEach = (cache, urls) => Promise.all(urls.map(u => cache.add(u).catch(() => {})));

self.addEventListener("install", event => {
  event.waitUntil((async () => {
    await caches.open(SHELL).then(c => c.addAll(SHELL_FILES));
    await caches.open(SHELL).then(c => addEach(c, CDN_FILES));
    await caches.open(DATA).then(c => addEach(c, DATA_FILES));
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", event => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) if (![SHELL, DATA].includes(key)) await caches.delete(key);
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", event => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.hostname.endsWith("tile.openstreetmap.org")) return;  // dlaždice vždy zo siete

  if (url.origin === location.origin && url.pathname.includes("/data/")) {
    event.respondWith(networkFirst(req));
  } else if (url.origin === location.origin || url.hostname === "cdnjs.cloudflare.com") {
    event.respondWith(staleWhileRevalidate(req, event));
  }
});

async function networkFirst(req) {
  const cache = await caches.open(DATA);
  try {
    const res = await fetch(req);
    if (res.ok) await cache.put(req.url.split("?")[0], res.clone());
    return res;
  } catch {
    return (await cache.match(req, { ignoreSearch: true })) || Response.error();
  }
}

async function staleWhileRevalidate(req, event) {
  const cache = await caches.open(SHELL);
  const cached = await cache.match(req, { ignoreSearch: true });
  const fresh = fetch(req).then(res => {
    if (res.ok) cache.put(req, res.clone());
    return res;
  }).catch(() => cached || Response.error());
  if (cached) { event.waitUntil(fresh); return cached; }
  return fresh;
}
