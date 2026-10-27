// PWA push opt-in (issue #7). Drives the header bell: fetch the VAPID public
// key, subscribe/unsubscribe via PushManager, and mirror state on the button.
// The service worker (sw.js) shows the notifications; this only manages the
// subscription. No-op unless the browser supports push and the server has push
// enabled with a key.
(function () {
  'use strict';

  var bell = null;

  function supported() {
    return 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
  }

  // base64url (VAPID application server key) -> Uint8Array for PushManager.
  function urlB64ToUint8Array(b64) {
    var pad = '='.repeat((4 - (b64.length % 4)) % 4);
    var base64 = (b64 + pad).replace(/-/g, '+').replace(/_/g, '/');
    var raw = atob(base64);
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  var LABELS = {
    on: 'Notifications on — tap to turn off',
    off: 'Enable notifications',
    denied: 'Notifications blocked in browser settings',
    busy: 'Working…',
  };

  function setState(state) {
    if (!bell) return;
    bell.dataset.state = state;
    bell.setAttribute('aria-label', LABELS[state] || 'Notifications');
    bell.setAttribute('title', LABELS[state] || 'Notifications');
    bell.setAttribute('aria-pressed', state === 'on' ? 'true' : 'false');
    // 'busy' means "we do not know yet" on the first pass, so it must not
    // unhide the bell: on a 320px viewport the extra 24px overflows the topbar
    // for as long as /push/public-key takes to answer. Leave the visibility
    // where it was and let the settled state decide (#1368). A click makes it
    // busy too, and the bell is already visible by then.
    if (state !== 'busy') bell.hidden = state === 'unsupported';
    bell.disabled = state === 'busy';
  }

  function getSubscription(reg) {
    return reg.pushManager.getSubscription().catch(function () { return null; });
  }

  function refresh() {
    if (!supported()) { setState('unsupported'); return; }
    fetch('/push/public-key').then(function (r) { return r.json(); }).then(function (info) {
      if (!info || !info.enabled || !info.key) { setState('unsupported'); return; }
      bell._key = info.key;
      if (Notification.permission === 'denied') { setState('denied'); return; }
      navigator.serviceWorker.ready.then(getSubscription).then(function (sub) {
        setState(sub ? 'on' : 'off');
      });
    }).catch(function () { setState('unsupported'); });
  }

  function enable() {
    setState('busy');
    Notification.requestPermission().then(function (perm) {
      if (perm !== 'granted') { setState(perm === 'denied' ? 'denied' : 'off'); return; }
      navigator.serviceWorker.ready.then(function (reg) {
        return reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlB64ToUint8Array(bell._key),
        });
      }).then(function (sub) {
        return fetch('/push/subscribe', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(sub),
        }).then(function (res) {
          if (res && res.ok) { setState('on'); return; }
          // Server didn't store it — roll back the browser subscription so a
          // later refresh can't read "on" while the server knows nothing.
          return sub.unsubscribe().catch(function () {}).then(function () { setState('off'); });
        });
      }).catch(function () { setState('off'); });
    }).catch(function () { setState('off'); });
  }

  function disable() {
    setState('busy');
    navigator.serviceWorker.ready.then(getSubscription).then(function (sub) {
      if (!sub) { setState('off'); return; }
      fetch('/push/unsubscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ endpoint: sub.endpoint }),
      }).catch(function () {}).then(function () {
        return sub.unsubscribe().catch(function () {});
      }).then(function () { setState('off'); });
    });
  }

  function onClick() {
    var state = bell.dataset.state;
    if (state === 'off') enable();
    else if (state === 'on') disable();
    // 'denied' / 'busy' / 'unsupported': nothing actionable here.
  }

  function init() {
    bell = document.querySelector('.notif-bell');
    if (!bell) return;
    setState('busy');
    bell.addEventListener('click', onClick);
    refresh();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
