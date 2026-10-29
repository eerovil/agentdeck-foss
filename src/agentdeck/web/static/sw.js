// agentdeck service worker.
//
// This is a *live* monitoring dashboard, so the goal here is installability
// (home-screen / standalone display), NOT offline data. We therefore cache
// only the static shell assets and let everything dynamic hit the network
// untouched. In particular the `/events*` Server-Sent-Events streams must
// never be intercepted — wrapping them in respondWith() buffers the stream
// and kills the live tail. Same for `/partials/*`, which must always reflect
// current server state.
//
// Document navigations are the one exception, and only for failures: they are
// still fetched from the network and never cached, but when the fetch fails or
// the proxy answers a gateway status, we substitute the cached restarting
// screen (issue #606). Nothing about a *successful* navigation changes.

// The cache name carries a content hash of the shell assets, substituted when
// /sw.js is served. Any change to app.css/sse.js/etc. changes this string, so
// the browser installs a fresh worker and re-fetches the assets — no manual
// version bump, no stale CSS after a deploy.
const CACHE = 'agentdeck-static-__CACHE_STAMP__';
// Every script the shell loads (base.html plus the session-list controls and
// /usage), not a hand-picked subset: tests/test_web.py asserts base.html's
// script tags are all here, so an added script that is forgotten fails a test
// instead of silently skipping the precache (#792).
const ASSETS = [
  '/static/app.css',
  '/static/htmx.min.js',
  '/static/error_reporting.js',
  '/static/sse.js',
  '/static/work_list.js',
  '/static/front_filters.js',
  '/static/work_stop_sound.js',
  '/static/action_timing.js',
  '/static/mobile_session_stack.js',
  '/static/session_diff.js',
  '/static/session_bottom_follow.js',
  '/static/session_last_user_jump.js',
  '/static/tool_call_groups.js',
  '/static/chat_notes.js',
  '/static/message_collapse.js',
  '/static/side_chat.js',
  '/static/rollback.js',
  '/static/session_navigation.js',
  '/static/session_panels.js',
  '/static/activity_elapsed.js',
  '/static/message_age.js',
  '/static/interaction_feedback.js',
  '/static/question_drafts.js',
  '/static/refusal_swap.js',
  '/static/starting_chat.js',
  '/static/new_chat_usage_mirror.js',
  '/static/new_work.js',
  '/static/push.js',
  '/static/session_babysit.js',
  '/static/body_disclosure.js',
  '/static/job_queue.js',
  '/static/needs_you_menu.js',
  '/static/account_navigation.js',
  '/static/composer_autocomplete.js',
  '/static/composer_phrases.js',
  '/static/composer_speech.js',
  '/static/voice_mode.js',
  '/static/chat_jump.js',
  '/static/issue_post.js',
  '/static/code_copy.js',
  '/static/live_html.js',
  '/static/page_refresh.js',
  '/static/wayfinder_map.js',
  '/static/model_picker.js',
  '/static/chats_page.js',
  '/static/restarting.html',
  '/static/favicon.svg',
  '/static/apple-touch-icon.png',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/icon-maskable-512.png',
  '/manifest.webmanifest',
];

self.addEventListener('install', (event) => {
  // Cache each shell asset independently. cache.addAll() rejects atomically if
  // any single URL 404s (e.g. a half-finished deploy), which would fail the
  // whole install and pin the browser to the previous worker + stale shell.
  // allSettled lets the new worker install with whatever assets are available;
  // the network-first fetch handler re-fetches any that were missed.
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => Promise.allSettled(ASSETS.map((url) => cache.add(url))))
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

// Web Push (issue #7). A push arrives as a JSON {title, body, url, tag}; show it as a
// notification. Clicking focuses an existing app tab (navigating it to the
// target) or opens a new one.
self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = {}; }
  // A clear push (#3365): the Work was opened or marked read, here or on another device,
  // so close its notification and show nothing. The deck sends one only for a
  // notification it really sent, since a push that shows nothing may make the browser
  // show its own "updated in the background" notice.
  if (Array.isArray(data.clear)) {
    event.waitUntil(closeNotifications(data.clear));
    return;
  }
  const title = data.title || 'agentdeck';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      // If this device already has the app open in the foreground, the user is
      // watching the activity live — don't stack a redundant notification
      // (issue #36). This is per-device: each device's worker only sees its own
      // windows, so a phone in your pocket still buzzes while the desktop tab you
      // are looking at stays quiet. A *visible* client is the sanctioned
      // exception to userVisibleOnly, so the browser won't fall back to its own
      // generic "site updated in the background" notification here.
      const foreground = clientList.some((client) => client.visibilityState === 'visible');
      if (foreground) return undefined;
      return self.registration.showNotification(title, {
        body: data.body || '',
        // Collapse repeats about the same thing (one Work, #3348) onto one notification.
        tag: data.tag || data.url || 'agentdeck',
        data: { url: data.url || '/' },
        icon: '/static/icon-192.png',
        badge: '/static/icon-192.png',
      });
    })
  );
});

function closeNotifications(tags) {
  return Promise.all(tags.filter((tag) => typeof tag === 'string' && tag).map((tag) =>
    self.registration.getNotifications({ tag })
      .then((shown) => shown.forEach((notification) => notification.close()))
      .catch(() => {})
  ));
}

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil(openApp(event.notification.data && event.notification.data.url));
});

// Tapping a notification must reliably surface the page it is about — the Work's
// chat (#3348), the front page for a summary — freshly loaded, never a no-op
// (issue #35). The SW never caches navigations, so a real navigate() always pulls
// fresh server state. A target on another origin, or none, goes home.
//
// Reliability matters more than elegance here, because the client we find may be
// a frozen, *uncontrolled* window (mobile PWA resumed from background) whose
// navigate() rejects — the old `navigate().catch().then(focus())` chain then
// left the app focused on its stale view, or did nothing at all. So: focus the
// existing window first (this brings the installed PWA forward even when
// navigate() is unavailable), then move it to the target via navigate(); if
// navigate() is missing or rejects, ask the page itself to go there via
// postMessage; and if there's no app window to reuse, open a fresh one.
async function openApp(url) {
  let target = new URL('/', self.location.origin);
  try {
    const wanted = new URL(url || '/', self.location.origin);
    if (wanted.origin === self.location.origin) target = wanted;
  } catch (e) { /* a malformed url goes home */ }
  const clientList = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
  for (const client of clientList) {
    if (!client.url || new URL(client.url).origin !== self.location.origin) continue;
    try { await client.focus(); } catch (e) { /* still try to route it below */ }
    if (client.navigate) {
      try { await client.navigate(target.href); return; } catch (e) { /* fall through */ }
    }
    // navigate() unavailable or rejected: let the page navigate itself.
    client.postMessage({ type: 'agentdeck:open', path: target.pathname + target.search + target.hash });
    return;
  }
  await self.clients.openWindow(target.href);
}

// The statuses a reverse proxy invents when it cannot reach its upstream. The
// deck sits behind `tailscale serve`, which answers 502 for the seconds a deploy
// takes to hand agentdeck.service over to a new process; other proxies use 503
// or 504 for the same "upstream is not there" condition. AgentDeck itself never
// answers a document navigation with any of them — its own 503s are on POST/API
// routes — so seeing one here means the app is down, not that a page failed.
const GATEWAY_DOWN = [502, 503, 504];

const RESTARTING_SCREEN = '/static/restarting.html';

// The restarting screen, or the original failure if it was never cached (a
// half-finished install). Degrading back to the proxy's page is the same thing
// the user sees today; inventing a second copy of the screen here would just be
// a second thing to keep in sync.
function restartingScreen(fallback) {
  return caches.open(CACHE)
    .then((cache) => cache.match(RESTARTING_SCREEN))
    .then((hit) => hit || fallback || Response.error())
    .catch(() => fallback || Response.error());
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // A navigation while the deck is restarting otherwise renders the proxy's raw
  // "502 Bad Gateway". Serve the restarting screen instead — it polls /healthz
  // and reloads itself into the page that was asked for. The request is passed
  // through untouched (redirect mode stays `manual`, so a 307 from
  // routes_files.py comes back as an opaqueredirect the browser follows), and
  // no navigation response is ever cached.
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req)
        .then((res) => (res && GATEWAY_DOWN.includes(res.status) ? restartingScreen(res) : res))
        .catch(() => restartingScreen(null))
    );
    return;
  }

  // Only manage the static shell + the manifest. Everything else —
  // /partials/* fragments and the /events* SSE streams — is left to the
  // network so the dashboard always shows live state.
  const managed = url.pathname.startsWith('/static/') || url.pathname === '/manifest.webmanifest';
  if (!managed) return;

  // Network-first: this is an always-online dashboard, so fetch the live asset
  // (a changed app.css/sse.js shows up immediately, no stale copy), update the
  // cache, and fall back to the cache only when the network is unreachable.
  event.respondWith(
    caches.open(CACHE).then((cache) =>
      fetch(req)
        .then((res) => {
          if (res && res.ok) cache.put(req, res.clone());
          return res;
        })
        .catch(() => cache.match(req))
    )
  );
});
