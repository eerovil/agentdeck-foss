// The topbar job-queue pill (#1301).
//
// Two things have to survive the SSE swap that replaces this fragment. The
// deploy pill next to it can ignore both, because its state changes maybe twice
// a day; this one is replaced every time a job starts or finishes, which is
// exactly when someone has the panel open and is watching it.
//
//  1. The panel's open state. It lives in localStorage and is re-applied to
//     whatever markup the swap brought in, the same trick body_disclosure.js
//     plays for the usage header.
//  2. The elapsed clocks. Between swaps the server pushes nothing, so a job
//     running quietly for ten minutes would sit at the age it had when it
//     started. Each row carries the server's elapsed seconds and this ticks on
//     from there using the local clock only as a delta — so a clock skew
//     between phone and host cannot make a job look older or younger than the
//     server said it was.
(function () {
  'use strict';

  var OPEN_KEY = 'agentdeck.jobQueueOpen';

  function stored() {
    try { return localStorage.getItem(OPEN_KEY) === '1'; } catch (_) { return false; }
  }

  function remember(open) {
    try { localStorage.setItem(OPEN_KEY, open ? '1' : '0'); } catch (_) {}
  }

  function restore() {
    var el = document.querySelector('.job-queue');
    if (el) el.open = stored();
  }

  function formatElapsed(seconds) {
    if (seconds < 60) return seconds + 's';
    if (seconds < 3600) return Math.floor(seconds / 60) + 'm ' + (seconds % 60) + 's';
    return Math.floor(seconds / 3600) + 'h ' + Math.floor((seconds % 3600) / 60) + 'm';
  }

  function tick() {
    var now = Date.now();
    document.querySelectorAll('.job-queue-age').forEach(function (el) {
      if (!el._agentdeckMountedAt) el._agentdeckMountedAt = now;
      var base = Number(el.dataset.jobElapsed) || 0;
      el.textContent = formatElapsed(base + Math.floor((now - el._agentdeckMountedAt) / 1000));
    });
  }

  // A click on the summary is what changes the state; `toggle` catches the
  // keyboard path too. Captured on the document so the handler keeps working
  // after the element it belongs to has been replaced.
  document.addEventListener('toggle', function (e) {
    var el = e.target;
    if (el && el.classList && el.classList.contains('job-queue')) remember(el.open);
  }, true);

  // `toggle` is not enough on its own: the browser fires it a task *after* the
  // click that opened the panel, so between the two the stored value is still
  // the one from before the click. Write the intent synchronously here as well.
  // The default action has not flipped `open` yet when a capturing click
  // handler runs, which is why this stores the opposite of what it reads.
  document.addEventListener('click', function (e) {
    var el = e.target;
    var summary = el && el.closest ? el.closest('.job-queue > summary') : null;
    if (summary) remember(!summary.parentElement.open);
  }, true);

  // Only a swap that brought in new pill markup has anything to re-apply. The
  // handler used to run for every swap on the page, so the usage meters or the
  // assistant region — pushed the moment the stream connects, and nothing to do
  // with the queue — re-applied the stored state over a panel someone had just
  // opened. Landing in the gap before `toggle`, that read the value from before
  // the click and shut the panel for good (#1456), which is what failed in the
  // pod lane and did not look like a race from the test's side.
  function touchesTheQueue(target) {
    var slot = document.getElementById('job-queue-slot');
    if (!slot || !target || !target.contains) return true;
    return target === slot || slot.contains(target) || target.contains(slot);
  }

  function afterSwap(e) {
    if (!touchesTheQueue(e && e.target)) return;
    // The queue drained and the pill is gone. Forget that it was open, so the
    // next job hours from now raises a quiet pill rather than throwing an
    // unrequested panel over the page.
    if (!document.querySelector('.job-queue')) {
      remember(false);
      return;
    }
    restore();
    tick();
  }

  restore();
  tick();
  document.body.addEventListener('htmx:afterSwap', afterSwap);
  window.setInterval(tick, 1000);
})();
