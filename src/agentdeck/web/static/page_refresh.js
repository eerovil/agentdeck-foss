// The topbar's reload button (#2240). Installed from the home screen the deck
// runs without any browser controls, so there is no reload button and no
// pull-to-refresh — this is the only way to reload a page there. A message
// draft survives it through the composer's sessionStorage restore.
//
// Since #4591 the button also says when the page is not live, and a reload has a
// time limit. A phone back from sleep can hold a connection that died while it
// slept without knowing; a reload then waited on it for minutes with the button
// spinning and the old page on screen. The service worker answers a page load it
// gets no response to within 15 s with the "coming back up" screen; this timer is
// longer, so it fires only when no worker answered.
(function () {
  'use strict';

  var LABEL = 'Reload page';
  var NOT_LIVE_LABEL = 'Not live — tap to reload';
  var STUCK_LABEL = 'Reload stuck — close and reopen the app';
  var RECONNECTING_LABEL = 'Reconnecting…';
  var RELOADING_LABEL = 'Reloading…';
  // A browser test shortens these through window.AgentDeckResumeTimings.
  var timings = window.AgentDeckResumeTimings || {};
  var RELOAD_LIMIT_MS = timings.reloadLimitMs || 20000;
  var STALL_KEY = 'agentdeck:resume-stall';

  function buttons() {
    return Array.prototype.slice.call(document.querySelectorAll('[data-page-refresh]'));
  }
  function label(button, text) {
    button.setAttribute('aria-label', text);
    button.setAttribute('title', text);
  }

  // The stall record (#4591): what happened after a resume that heard nothing,
  // kept on the phone and sent on the next page that loads normally. One record,
  // overwritten by each stall, so a dead connection can never pile up copies.
  function readStall() {
    try { return JSON.parse(localStorage.getItem(STALL_KEY) || 'null'); } catch (_) { return null; }
  }
  function noteStall(fields) {
    try {
      var record = readStall() || {};
      for (var key in fields) record[key] = fields[key];
      localStorage.setItem(STALL_KEY, JSON.stringify(record));
    } catch (_) {}
  }
  function startStall(fields) {
    try { localStorage.setItem(STALL_KEY, JSON.stringify(fields)); } catch (_) {}
  }
  function sendStall() {
    var record = readStall();
    if (!record) return;
    // Removed before sending: a send that fails is dropped, never retried.
    try { localStorage.removeItem(STALL_KEY); } catch (_) {}
    try {
      fetch('/api/browser-errors', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          kind: 'resume-stall',
          message: JSON.stringify(record),
          page: window.location.pathname + window.location.search,
        }),
        keepalive: true,
      }).catch(function () {});
    } catch (_) {}
  }

  // The notice under the topbar (#4601): from a second after the app comes back
  // until its first live update, it says the page is not live yet. It speaks the
  // reload button's words, so the two never disagree, and is tappable only once
  // a reload can help: before 8 s a reload would wait on the same sleeping
  // network. A reload or a stuck reload changes it only while it is showing.
  var NOTICE_TEXT = {
    reconnecting: RECONNECTING_LABEL,
    'not-live': NOT_LIVE_LABEL,
    reloading: RELOADING_LABEL,
    stuck: STUCK_LABEL,
  };
  var noticeState = 'hidden';
  function setNotice(state) {
    var box = document.getElementById('resume-notice');
    if (!box) return;
    // A later wake does not step an amber notice back to Reconnecting…, and a
    // reload in flight keeps it saying Reloading… like the spinning button.
    if (state === 'reconnecting' && noticeState !== 'hidden') return;
    if (pending && (state === 'reconnecting' || state === 'not-live')) return;
    noticeState = NOTICE_TEXT[state] ? state : 'hidden';
    var button = box.querySelector('[data-resume-notice]');
    box.hidden = noticeState === 'hidden';
    box.classList.toggle('is-not-live', noticeState === 'not-live' || noticeState === 'stuck');
    if (button) {
      button.textContent = NOTICE_TEXT[noticeState] || '';
      button.disabled = !(noticeState === 'not-live' || noticeState === 'stuck');
    }
  }

  var pending = null;
  // A stall stays open until the first live update arrives; after that a later
  // reload or message belongs to no stall and leaves the record alone.
  function openStall() {
    var record = readStall();
    return record && record.first_message_at_ms == null ? record : null;
  }
  function sinceResume() {
    var record = readStall();
    return record && record.resumed_at ? Date.now() - record.resumed_at : null;
  }

  // A reload with a time limit. When the old page is still here after the limit,
  // stop that load and try once more; after a second limit, stop spinning and say
  // the app needs closing. A reload that finishes replaces this document, and its
  // timers with it.
  function timedReload(by) {
    if (pending) return;
    if (openStall()) noteStall({ reload_by: by, reload_at_ms: sinceResume() });
    if (noticeState !== 'hidden') setNotice('reloading');
    buttons().forEach(function (button) {
      button.classList.add('is-refreshing');
      button.disabled = true;
      label(button, LABEL);
    });
    pending = setTimeout(function () {
      try { window.stop(); } catch (_) {}
      if (openStall()) noteStall({ reload_retry_at_ms: sinceResume() });
      window.location.reload();
      pending = setTimeout(function () {
        pending = null;
        try { window.stop(); } catch (_) {}
        if (openStall()) noteStall({ stuck_at_ms: sinceResume() });
        if (noticeState !== 'hidden') setNotice('stuck');
        buttons().forEach(function (button) {
          button.classList.remove('is-refreshing');
          button.disabled = false;
          label(button, STUCK_LABEL);
        });
      }, RELOAD_LIMIT_MS);
    }, RELOAD_LIMIT_MS);
    window.location.reload();
  }

  // Whether this page hears its live updates. Not live turns the button amber.
  function setLive(live) {
    buttons().forEach(function (button) {
      button.classList.toggle('is-not-live', !live);
      if (!button.classList.contains('is-refreshing')) {
        label(button, live ? LABEL : NOT_LIVE_LABEL);
      }
    });
  }

  window.AgentDeckPageRefresh = {
    reload: timedReload,
    setLive: setLive,
    setNotice: setNotice,
    startStall: startStall,
    noteStall: noteStall,
    stallOpen: function () { return !!openStall(); },
  };

  document.addEventListener('click', function (event) {
    var target = event.target instanceof Element ? event.target : null;
    var button = target && target.closest('[data-page-refresh]');
    var notice = target && target.closest('[data-resume-notice]');
    if (!button && !notice) return;
    event.preventDefault();
    timedReload(notice ? 'notice' : 'button');
  });

  // A page that loaded is the end of whatever stalled before it.
  if (document.readyState === 'complete') sendStall();
  else window.addEventListener('load', sendStall);
})();
