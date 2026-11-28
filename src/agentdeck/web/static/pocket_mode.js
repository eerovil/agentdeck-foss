// Pocket mode (#4792): voice mode goes on while the phone sits in a pocket.
//
// The 🌙 beside 🎧, shown only while voice mode is on, lays a black screen over the
// whole page. Nothing under it can be pressed: every tap, swipe, pinch and long press
// lands on the black screen and does nothing, so the page cannot scroll, zoom or
// pull to refresh. Voice mode itself is untouched: it listens, reads and sends as
// before, and its wake lock keeps the screen on (black, so it costs little).
//
// The way out is a deliberate two-step one a pocket will not make: a touch shows a
// dim slider for a few seconds, and only a single finger that starts on its knob and
// drags it all the way to the right unlocks. A second finger, or letting go early,
// drops the knob back. Back (the phone's Back gesture) is swallowed while pocket mode
// is on, as it would leave the chat and so end voice mode. A deploy's reload waits
// until pocket mode ends (voice_mode.js `AgentDeckVoiceHoldsReload`). Voice mode ending
// by itself leaves the black screen up; its slider then says voice is off.
(function () {
  'use strict';

  var HINT_MS = 5000;
  var UNLOCK_SHARE = 0.85;
  var STATE_KEY = 'agentdeckPocket';

  var screen = null;
  var knob = null;
  var track = null;
  var hint = null;
  var hideTimer = null;
  var drag = null;
  var unwinding = false;
  var fullscreen = false;

  function active() { return !!screen; }

  function build() {
    var el = document.createElement('div');
    el.className = 'pocket-screen';
    el.setAttribute('data-pocket-screen', '');
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-modal', 'true');
    el.setAttribute('aria-label', 'Pocket mode: slide to unlock');
    el.innerHTML =
      '<div class="pocket-hint" data-pocket-hint hidden>' +
        '<p data-pocket-voice></p>' +
        '<div class="pocket-track" data-pocket-track>' +
          '<span class="pocket-track-label">Slide to unlock</span>' +
          '<span class="pocket-knob" data-pocket-knob>➜</span>' +
        '</div>' +
      '</div>';
    return el;
  }

  // Voice mode can end by itself under the black screen (a blocked microphone, the
  // speech service gone). The screen stays, as the page lit in a pocket is what it is
  // there to stop, but the slider says voice is off.
  function voiceLine() {
    var on = document.documentElement.classList.contains('voice-on');
    return on ? 'Pocket mode · voice still on' : 'Pocket mode · voice is off';
  }

  function showHint() {
    if (!hint) return;
    hint.querySelector('[data-pocket-voice]').textContent = voiceLine();
    hint.hidden = false;
    clearTimeout(hideTimer);
    hideTimer = setTimeout(function () {
      if (!drag && hint) hint.hidden = true;
    }, HINT_MS);
  }

  function setKnob(x) {
    if (knob) knob.style.transform = 'translateX(' + x + 'px)';
  }

  function room() {
    return Math.max(1, track.clientWidth - knob.offsetWidth - 2 * knob.offsetLeft);
  }

  function dropKnob() {
    drag = null;
    setKnob(0);
    showHint();
  }

  // Everything on the black screen is swallowed here; only the knob's own drag counts.
  function onDown(event) {
    event.preventDefault();
    event.stopPropagation();
    // A second finger, or a first touch while the slider is hidden, never unlocks.
    if (drag) { dropKnob(); return; }
    var hidden = hint.hidden;
    showHint();
    if (hidden || !event.isPrimary || !knob.contains(event.target)) return;
    drag = {id: event.pointerId, x: event.clientX};
    try { screen.setPointerCapture(event.pointerId); } catch (_) {}
  }

  function onMove(event) {
    event.preventDefault();
    event.stopPropagation();
    if (!drag || event.pointerId !== drag.id) return;
    setKnob(Math.min(room(), Math.max(0, event.clientX - drag.x)));
  }

  function onUp(event) {
    event.preventDefault();
    event.stopPropagation();
    if (!drag || event.pointerId !== drag.id) return;
    var moved = event.type === 'pointerup' ? event.clientX - drag.x : 0;
    if (moved >= room() * UNLOCK_SHARE) leave();
    else dropKnob();
  }

  function swallow(event) {
    event.preventDefault();
    event.stopPropagation();
  }

  // A copy of the chat's own entry, so the other modules that read it
  // (mobile_session_stack.js) still see a chat and do not set their entries up again.
  function pushEntry() {
    var state = Object.assign({}, history.state || {});
    state[STATE_KEY] = true;
    try { history.pushState(state, '', location.href); } catch (_) {}
  }

  function enter() {
    if (active()) return;
    screen = build();
    hint = screen.querySelector('[data-pocket-hint]');
    track = screen.querySelector('[data-pocket-track]');
    knob = screen.querySelector('[data-pocket-knob]');
    screen.addEventListener('pointerdown', onDown);
    screen.addEventListener('pointermove', onMove);
    screen.addEventListener('pointerup', onUp);
    screen.addEventListener('pointercancel', onUp);
    ['click', 'dblclick', 'contextmenu', 'wheel', 'touchstart', 'touchmove', 'touchend']
      .forEach(function (type) {
        screen.addEventListener(type, swallow, {passive: false});
      });
    // The keyboard goes down: nothing is typed in a pocket.
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
    document.body.appendChild(screen);
    document.documentElement.classList.add('pocket-on');
    // Back would leave the chat and end voice mode: an entry of its own takes it.
    pushEntry();
    // The phone's bars go too where it lets a page do that; Back leaves this first.
    var root = document.documentElement;
    if (root.requestFullscreen && !document.fullscreenElement) {
      root.requestFullscreen({navigationUI: 'hide'}).then(function () {
        // Unlocked before the browser answered: the chat must not stay fullscreen.
        // A pocket screen up again by then takes it as its own.
        if (active()) fullscreen = true;
        else if (document.fullscreenElement && document.exitFullscreen) {
          document.exitFullscreen().catch(function () {});
        }
      }).catch(function () {});
    }
  }

  function leave() {
    if (!active()) return;
    clearTimeout(hideTimer);
    screen.remove();
    screen = knob = track = hint = drag = null;
    document.documentElement.classList.remove('pocket-on');
    if (fullscreen && document.fullscreenElement && document.exitFullscreen) {
      document.exitFullscreen().catch(function () {});
    }
    fullscreen = false;
    if (history.state && history.state[STATE_KEY]) {
      unwinding = true;
      history.back();
    }
  }

  // Captured on the window, so it runs before the chat's own Back handling
  // (session_navigation.js would open the chat again in place).
  window.addEventListener('popstate', function (event) {
    if (unwinding) {
      unwinding = false;
      event.stopImmediatePropagation();
      return;
    }
    if (!active()) return;
    event.stopImmediatePropagation();
    pushEntry();
    showHint();
  }, true);

  document.addEventListener('click', function (event) {
    var button = event.target.closest && event.target.closest('[data-pocket-button]');
    if (!button) return;
    event.preventDefault();
    enter();
  });

  window.AgentDeckPocketMode = {enter: enter, leave: leave, active: active};
})();
