// A short chime when a Work stops (#2742).
//
// "Stopped" is the front page's own Recently stopped list (#2283): a live push that brings
// in a new, unseen row there plays two soft notes. Each row's stop is `data-work-id` plus
// `data-work-stopped` (when it stopped), so a Work that stops, resumes and stops again
// chimes again, while a reconnect or a repeated push of the same rows does not.
//
// - Rows already on the page when it loads never chime; only a push can.
// - One chime per push, however many Works stopped in it.
// - A Work in a project hidden with the Projects filter (#2311) stays quiet.
// - Open tabs share one chime. One tab holds a Web Lock (`agentdeck-stop-sound`) and only it
//   plays; a click or key press in another tab takes the lock over, so the tab you used last
//   is the one that sounds. Every tab receives the same push at the same moment, and
//   localStorage reaches other tabs too late to settle that, so it is only the fallback where
//   Web Locks are missing: a stop is claimed in `agentdeck-stop-chimes` (`{stop: ms}`, pruned
//   after a day) and the first tab to see the claim free plays it.
// - On by default, per device: `agentdeck-stop-sound` = "off" turns it off, from the
//   switch on /settings, which plays the chime once as a preview when ticked.
// - Browsers allow sound only after a click or key press on the page, so the audio
//   context is made and resumed on the first one; until then a chime is skipped, not queued.
(function () {
  'use strict';

  var SETTING_KEY = 'agentdeck-stop-sound';
  var CLAIM_KEY = 'agentdeck-stop-chimes';
  var PROJECT_KEY = 'agentdeck-front-hidden-projects';
  var CLAIM_TTL_MS = 24 * 60 * 60 * 1000;
  var LOCK_NAME = 'agentdeck-stop-sound';
  // Only a front page takes part: a click on a chat or on /settings must not take the chime
  // away from the front page that can play it.
  var front = !!document.getElementById('works-stopped');
  var locks = front && navigator.locks && navigator.locks.request ? navigator.locks : null;
  var leader = false;
  var waiting = null;  // the AbortController of this tab's place in line, if it has one
  var Context = window.AudioContext || window.webkitAudioContext;
  var audio = null;
  var known = new Set();

  function enabled() {
    try { return localStorage.getItem(SETTING_KEY) !== 'off'; } catch (e) { return true; }
  }

  function setEnabled(on) {
    try { localStorage.setItem(SETTING_KEY, on ? 'on' : 'off'); } catch (e) { /* private mode */ }
  }

  function context() {
    if (!audio && Context) {
      try { audio = new Context(); } catch (e) { audio = null; }
    }
    return audio;
  }

  // Hold the lock until another tab steals it, then wait in line for it again. A steal gives
  // up this tab's place in line first, so a tab never queues twice.
  function lead(steal) {
    if (!locks) return;
    if (steal) {
      if (waiting) waiting.abort();
      waiting = null;
    } else if (waiting || leader) {
      return;
    }
    var line = steal ? null : new AbortController();
    if (line) waiting = line;
    locks.request(LOCK_NAME, line ? {signal: line.signal} : {steal: true}, function () {
      if (waiting === line) waiting = null;
      leader = true;
      return new Promise(function () {});
    }).catch(function () {
      if (line && line.signal.aborted) return;  // gave up its place for a steal
      leader = false;  // another tab took it over
      lead(false);
    });
  }

  function unlock() {
    if (locks && !leader) lead(true);
    var ctx = context();
    if (ctx && ctx.state === 'suspended' && ctx.resume) ctx.resume().catch(function () {});
  }

  function note(ctx, frequency, start, length) {
    var osc = ctx.createOscillator();
    var gain = ctx.createGain();
    osc.type = 'sine';
    osc.frequency.value = frequency;
    gain.gain.setValueAtTime(0.0001, start);
    gain.gain.exponentialRampToValueAtTime(0.15, start + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, start + length);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start(start);
    osc.stop(start + length + 0.02);
  }

  function play() {
    var ctx = context();
    if (!ctx || ctx.state !== 'running') return false;
    var now = ctx.currentTime;
    note(ctx, 659.25, now, 0.18);        // E5
    note(ctx, 880.0, now + 0.16, 0.26);  // A5
    return true;
  }

  function hiddenProjects() {
    try {
      var parsed = JSON.parse(localStorage.getItem(PROJECT_KEY) || '{}');
      return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
    } catch (e) { return {}; }
  }

  // True when this tab is the first to claim every one of `stops`' chimes.
  function claim(stops) {
    var now = Date.now();
    var claims;
    try {
      claims = JSON.parse(localStorage.getItem(CLAIM_KEY) || '{}');
      if (!claims || typeof claims !== 'object' || Array.isArray(claims)) claims = {};
    } catch (e) {
      return true;
    }
    var fresh = stops.filter(function (stop) { return !claims[stop]; });
    if (!fresh.length) return false;
    Object.keys(claims).forEach(function (stop) {
      if (!(now - claims[stop] < CLAIM_TTL_MS)) delete claims[stop];
    });
    fresh.forEach(function (stop) { claims[stop] = now; });
    try { localStorage.setItem(CLAIM_KEY, JSON.stringify(claims)); } catch (e) { /* private mode */ }
    return true;
  }

  function stopOf(row) {
    return row.getAttribute('data-work-id') + '|' + (row.getAttribute('data-work-stopped') || '');
  }

  function rows() {
    var list = document.getElementById('works-stopped');
    return list ? Array.prototype.slice.call(list.querySelectorAll('[data-work-id]')) : [];
  }

  function reconcile() {
    var hidden = hiddenProjects();
    var fresh = [];
    rows().forEach(function (row) {
      var stop = stopOf(row);
      if (known.has(stop)) return;
      known.add(stop);
      if (!row.hasAttribute('data-work-unseen')) return;
      if (hidden[row.getAttribute('data-work-project')]) return;
      fresh.push(stop);
    });
    if (!fresh.length || !enabled()) return;
    if (locks ? leader : claim(fresh)) play();
  }

  // The script is deferred, so the page is parsed and no push has been swapped in yet.
  rows().forEach(function (row) { known.add(stopOf(row)); });

  document.addEventListener('agentdeck:works-reconciled', function (event) {
    if (event.target && event.target.id === 'works-stopped') reconcile();
  });
  lead(false);
  // A page restored from the back-forward cache may have lost its place in line.
  window.addEventListener('pageshow', function (event) {
    if (!event.persisted || leader) return;
    if (waiting) waiting.abort();
    waiting = null;
    lead(false);
  });
  ['pointerdown', 'keydown'].forEach(function (name) {
    document.addEventListener(name, unlock, true);
  });

  function syncSetting(root) {
    (root || document).querySelectorAll('[data-stop-sound-setting]').forEach(function (box) {
      box.checked = enabled();
    });
  }
  document.addEventListener('change', function (event) {
    var box = event.target.closest && event.target.closest('[data-stop-sound-setting]');
    if (!box) return;
    setEnabled(box.checked);
    if (box.checked) {
      unlock();
      var ctx = context();
      if (ctx && ctx.state !== 'running' && ctx.resume) ctx.resume().then(play, function () {});
      else play();
    }
  });
  syncSetting(document);

  // `leading()` says whether this tab holds the lock. Taking it over after a click is
  // asynchronous, so a push in that short gap chimes in no tab; tests wait on this first.
  window.AgentDeckStopSound = {
    play: play,
    enabled: enabled,
    leading: function () { return leader; }
  };
})();
