// The phone page's service worker: it only receives push messages and shows them. No fetch handling, no caching —
// the page itself is always fetched live from AgenticCAD. Served at /m/sw.js with scope /m.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));

self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = { title: 'AgenticCAD', body: e.data ? e.data.text() : '' }; }
  const opts = { body: d.body || '', tag: d.tag || 'agenticcad', renotify: true, data: d, requireInteraction: d.kind === 'tool_change' };
  // Tool changed · continue: browsers that show action buttons (Android, desktop) post the one-shot code for this
  // wait; iOS shows the notification without buttons and a tap opens the page, where Confirm is.
  if (d.confirm_url) opts.actions = [{ action: 'confirm', title: 'Tool changed · continue' }];
  e.waitUntil(self.registration.showNotification(d.title || 'AgenticCAD', opts));
});

self.addEventListener('notificationclick', e => {
  const d = e.notification.data || {};
  e.notification.close();
  if (e.action === 'confirm' && d.confirm_url) { e.waitUntil(fetch(d.confirm_url, { method: 'POST' }).catch(() => {})); return; }
  e.waitUntil((async () => {
    const cs = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of cs) { if ('focus' in c) { await c.focus(); return; } }
    if (d.url) await self.clients.openWindow(d.url);
  })());
});
