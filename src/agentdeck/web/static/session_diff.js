(function () {
  'use strict';

  // The diff pane is the layer to the right of the chat. On a phone it is
  // reached the way the session list is reached in the other direction — drag
  // it in with a finger — and on a desktop by the topbar control. Both
  // routes go through the same open/close pair, and both leave a history entry
  // so the system Back gesture closes the pane instead of leaving the chat.

  var activeController = null;
  var bound = null;
  var STATE_KEY = 'agentdeckDiffOpen';
  var NO_CHECKOUT = '<p class="diff-empty">This chat has no checkout on this host, ' +
    'so there is nothing to diff.</p>';

  function init() {
    var body = document.body;
    var layout = document.querySelector('.session-layout');
    var pane = document.getElementById('session-diff');
    if (!body.classList.contains('session-page') || !layout || !pane) return;
    // Session navigation replaces the chat pane and leaves this one standing, so
    // every init re-reads which chat it now belongs to; only a genuinely new
    // pane, layout or topbar control is worth rebinding listeners for. Opening a
    // Work drops the control and the next chat brings a fresh one, so a pane that
    // stayed put can still have a control nothing listens to (#2359).
    var toggle = document.querySelector('[data-session-diff-open]');
    if (bound && bound.pane === pane && bound.layout === layout && bound.toggle === toggle) {
      bound.retarget();
      return;
    }
    if (activeController) activeController.abort();
    activeController = new AbortController();
    var signal = activeController.signal;

    var mobile = window.matchMedia('(max-width: 999px)');
    var content = pane.querySelector('[data-session-diff-body]');
    var spinner = pane.querySelector('[data-session-diff-loading]');
    var placeholder = content ? content.innerHTML : '';
    var loading = spinner ? spinner.innerHTML : placeholder;
    var url = null;
    var targeted = false;
    var loaded = false;
    var pushed = false;

    function isOpen() {
      return body.classList.contains('diff-open');
    }

    function load() {
      if (!content || !url || !window.htmx) return;
      loaded = true;
      // Reading a checkout is never instant, so the pane says what it is doing
      // rather than sitting on the line that invited the open (#1133).
      content.innerHTML = loading;
      // htmx owns the request; this only says when to make it. A pane nobody
      // opens never reads a checkout.
      htmx.ajax('GET', url, {target: content, swap: 'innerHTML'});
    }

    function retarget() {
      // The chat pane is the only element that knows which chat is on screen.
      var detail = layout.querySelector('.session-detail');
      var next = (detail && detail.dataset.diffUrl) || null;
      // The first pass always runs: a chat with no checkout starts with the
      // same null target it would settle on, and its pane still has to say so.
      if (targeted && next === url) return;
      targeted = true;
      url = next;
      loaded = false;
      // A chat with no checkout on this host still opens the pane, which says so.
      // Hiding the control instead left the gesture silently dead with nothing on
      // screen to explain it (#1219).
      if (content) content.innerHTML = url ? placeholder : NO_CHECKOUT;
      if (toggle) toggle.hidden = false;
      // A pane left open belongs to the chat that has just been replaced. Drop
      // it without touching history: the entry behind us is the chat we came
      // from, and unwinding it would navigate straight back there.
      pushed = false;
      apply(false);
    }

    function apply(open) {
      body.classList.toggle('diff-open', open);
      body.classList.remove('diff-dragging');
      pane.style.removeProperty('--diff-pull');
      pane.inert = !open;
      pane.setAttribute('aria-hidden', open ? 'false' : 'true');
      if (toggle) toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      if (open && !loaded) load();
      if (open) pane.focus({preventScroll: true});
    }

    function open() {
      if (isOpen()) return;
      var state = Object.assign({}, history.state || {});
      state[STATE_KEY] = true;
      history.pushState(state, '', location.href);
      pushed = true;
      apply(true);
    }

    function close() {
      if (!isOpen()) return;
      if (pushed) {
        // Unwind our own entry so the Back button and the pane agree.
        pushed = false;
        history.back();
        return;
      }
      apply(false);
    }

    window.addEventListener('popstate', function (event) {
      var open = !!(event.state && event.state[STATE_KEY]);
      pushed = open && pushed;
      apply(open);
    }, {signal: signal});

    if (toggle) {
      toggle.addEventListener('click', function () {
        if (isOpen()) close(); else open();
      }, {signal: signal});
    }

    pane.addEventListener('click', function (event) {
      if (event.target.closest('[data-session-diff-close]')) close();
      if (event.target.closest('[data-session-diff-refresh]')) load();
    }, {signal: signal});

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && isOpen()) close();
    }, {signal: signal});

    // Going home while the diff is open should close the diff first, not leave
    // the chat behind it. Bound on window so this runs before the mobile
    // stack's own capture handler whichever script loaded first.
    window.addEventListener('click', function (event) {
      if (!isOpen()) return;
      var home = event.target.closest('a.back[href="/"], a.brand[href="/"]');
      if (!home) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      close();
    }, {capture: true, signal: signal});

    // A deliberate horizontal drag pulls the pane in with the finger, and the
    // same drag back out pushes it away. Vertical movement wins unless
    // horizontal movement is clearly dominant, so reading never opens the pane.
    // The gesture is bound to the layout — the one element that survives a
    // session navigation — rather than to either pane inside it.
    //
    // A finger may start the drag anywhere on the layer it is dragging, not only
    // in the strip along the screen edge. That strip is where both iOS and
    // Android put their own back/forward edge gestures, so the phone usually
    // swallows a touch that starts there and the pane never hears about it
    // (#1219). A mouse or pen still starts at the edge: pointer devices select
    // text across the transcript, and they have the topbar control anyway.
    // Starting away from the edge asks for more horizontal commitment before the
    // drag takes over, because that is where a vertical reading swipe lives.
    var EDGE_PX = 28;
    var COMMIT_RATIO = 0.28;
    var DIRECTION_LOCK_PX = 8;
    var INNER_DIRECTION_LOCK_PX = 24;
    var VERTICAL_CANCEL_RATIO = 1.5;
    var INNER_HORIZONTAL_RATIO = 1.2;
    var drag = null;
    // A drag that takes over swallows the tap it would otherwise have been, so
    // it can start on a link or a button: a transcript is mostly links, and a
    // gesture that has to avoid them is a gesture that does not work. Text
    // fields and the like still keep the drag off, because a horizontal drag
    // already means something inside them.
    var suppressClick = false;

    function isHorizontal(gesture, dx, dy) {
      return gesture.fromEdge
        ? dx >= DIRECTION_LOCK_PX && dx > dy
        : dx >= INNER_DIRECTION_LOCK_PX && dx > dy * INNER_HORIZONTAL_RATIO;
    }

    function width() {
      return layout.getBoundingClientRect().width || window.innerWidth;
    }

    function resetDrag() {
      if (drag) {
        try { layout.releasePointerCapture(drag.id); } catch (_) {}
      }
      drag = null;
      body.classList.remove('diff-dragging');
      pane.style.removeProperty('--diff-pull');
      if (!isOpen()) {
        pane.inert = true;
        pane.setAttribute('aria-hidden', 'true');
      }
    }

    layout.addEventListener('click', function (event) {
      if (!suppressClick) return;
      suppressClick = false;
      event.preventDefault();
      event.stopImmediatePropagation();
    }, {capture: true, signal: signal});

    layout.addEventListener('pointerdown', function (event) {
      suppressClick = false;
      if (!mobile.matches || !event.isPrimary ||
          body.classList.contains('mobile-list-open') ||
          event.target.closest('input, textarea, select, [contenteditable]')) return;
      var finger = event.pointerType === 'touch';
      if (!finger && event.target.closest('button, a')) return;
      // Measured against the layout's own box: the stack is centred inside
      // `main`, so its edges are not always the screen's edges.
      var box = layout.getBoundingClientRect();
      var closing = isOpen();
      var fromEdge = closing
        ? event.clientX <= box.left + EDGE_PX
        : event.clientX >= box.right - EDGE_PX;
      if (!fromEdge && !finger) return;
      drag = {
        id: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        closing: closing,
        fromEdge: fromEdge,
        horizontal: false,
      };
      try { layout.setPointerCapture(event.pointerId); } catch (_) {}
    }, {signal: signal});

    layout.addEventListener('pointermove', function (event) {
      if (!drag || event.pointerId !== drag.id) return;
      var dx = drag.closing
        ? Math.max(0, event.clientX - drag.startX)
        : Math.max(0, drag.startX - event.clientX);
      var dy = Math.abs(event.clientY - drag.startY);
      if (!drag.horizontal) {
        if (dy > 10 && dy > dx * VERTICAL_CANCEL_RATIO) { resetDrag(); return; }
        if (!isHorizontal(drag, dx, dy)) return;
        drag.horizontal = true;
        suppressClick = true;
        body.classList.add('diff-dragging');
        pane.inert = false;
        pane.setAttribute('aria-hidden', 'false');
        // The body is fetched as soon as the gesture commits to a direction, so
        // the pane is not empty by the time the finger arrives.
        if (!drag.closing && !loaded) load();
      }
      event.preventDefault();
      var pulled = drag.closing
        ? Math.max(0, width() - dx)
        : Math.min(dx, width());
      pane.style.setProperty('--diff-pull', pulled + 'px');
    }, {passive: false, signal: signal});

    layout.addEventListener('pointerup', function (event) {
      if (!drag || event.pointerId !== drag.id) return;
      var dx = drag.closing
        ? Math.max(0, event.clientX - drag.startX)
        : Math.max(0, drag.startX - event.clientX);
      var dy = Math.abs(event.clientY - drag.startY);
      var horizontal = drag.horizontal || isHorizontal(drag, dx, dy);
      var commit = horizontal && dx >= width() * COMMIT_RATIO;
      var closing = drag.closing;
      resetDrag();
      if (!commit) return;
      if (closing) close(); else open();
    }, {signal: signal});

    layout.addEventListener('pointercancel', resetDrag, {signal: signal});

    bound = {pane: pane, layout: layout, toggle: toggle, retarget: retarget};
    retarget();
    apply(!!(history.state && history.state[STATE_KEY]));
  }

  window.AgentDeckSessionDiff = { init: init };
  document.addEventListener('agentdeck:session-navigation', init);
  init();
})();
