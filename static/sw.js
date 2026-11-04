// Kaffeekasse – Service Worker: macht die Seite installierbar ("Zum Home-Bildschirm").
// Es wird bewusst nichts zwischengespeichert: Preise, Bestände und Guthaben kommen immer frisch vom Server.
const OFFLINE = `<!doctype html><html lang="de"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Keine Verbindung</title>
<body style="margin:0;min-height:100vh;display:grid;place-items:center;background:#101319;color:#E9EDF4;font:16px system-ui,sans-serif;text-align:center;padding:24px">
<div><div style="font-size:44px">☕</div><h1 style="font-size:20px">Keine Verbindung zur Kaffeekasse</h1>
<p style="color:#8C97A8">Bitte WLAN prüfen und neu laden.</p>
<button onclick="location.reload()" style="font:inherit;padding:12px 22px;border-radius:10px;border:0;background:#3FBF7F;color:#06210F;font-weight:600">Neu laden</button></div>`;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (e) => {
  if (e.request.mode !== "navigate") return;
  e.respondWith(fetch(e.request).catch(() => new Response(OFFLINE, { headers: { "Content-Type": "text/html; charset=utf-8" } })));
});
