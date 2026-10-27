(function () {
  'use strict';

  var activeController = null;
  var STATE_KEY = 'agentdeckMobileLayer';

  // Whether the list mounted beneath the chat is the page at the current address. Back can
  // land on a list entry this document cannot show — the Work a chat was opened over,
  // before a reload made the chat its own page — and showing the chats view there put one
  // page's list under another's address (#2281). session_navigation.js loads it instead.
  // The query counts too: a filtered chats view is a different state of the same page.
  // A chat page loaded on its own holds no list at all (#2603), so nothing is here.
  function listIsHere(sidebar) {
    if (sidebar && sidebar.hasAttribute('data-list-empty')) return false;
    var path = (sidebar && sidebar.dataset.listPath) || '/';
    var list = new URL(path, location.href);
    var here = new URL(location.href);
    list.searchParams.sort();
    here.searchParams.sort();
    return list.pathname === here.pathname && list.search === here.search;
  }

  function init() {
    var mobile = window.matchMedia('(max-width: 999px)');
    var body = document.body;
    var layout = document.querySelector('.session-layout');
    var sidebar = document.querySelector('.session-sidebar');
    var detail = document.querySelector('.session-detail');
    var back = document.querySelector('a.back[href="/"]');
    if (!body.classList.contains('session-page') || !layout || !sidebar || !detail || !back ||
        detail.dataset.mobileStackReady) return;
    if (activeController) activeController.abort();
    activeController = new AbortController();
    var signal = activeController.signal;
    detail.dataset.mobileStackReady = 'true';

  var initialized = false;

  function setAccessibleLayer(showList) {
    detail.inert = showList;
    detail.setAttribute('aria-hidden', showList ? 'true' : 'false');
    sidebar.inert = !showList;
    sidebar.setAttribute('aria-hidden', showList ? 'false' : 'true');
    // A Work's own arrow stays on its page when the chat over it closes (#2281).
    back.setAttribute('aria-hidden',
      getComputedStyle(back).visibility === 'hidden' ? 'true' : 'false');
  }

  function showLayer(layer) {
    var showList = layer === 'list';
    body.classList.toggle('mobile-list-open', showList);
    body.classList.remove('mobile-stack-dragging');
    detail.style.removeProperty('--mobile-chat-x');
    setAccessibleLayer(showList);
  }

  function initialize() {
    if (initialized || !mobile.matches) return;
    initialized = true;
    // A direct deep link needs a same-document list entry beneath the chat.
    // The navigation module recognizes this state and does not duplicate it.
    // The entry carries the sidebar's own address — the chats view — not `/`,
    // which is the Works (#2256). Back from the chat is that list: the front page for a
    // chat page, never its Work (#2878).
    if (!(history.state && history.state[STATE_KEY])) {
      var chatPath = location.pathname + location.search + location.hash;
      var listState = Object.assign({}, history.state || {});
      listState[STATE_KEY] = 'list';
      history.replaceState(listState, '', sidebar.dataset.listPath || '/');
      var chatState = Object.assign({}, listState);
      chatState[STATE_KEY] = 'chat';
      history.pushState(chatState, '', chatPath);
    }
    back.removeAttribute('data-agentdeck-action');
    body.classList.add('mobile-session-stack-ready');
    showLayer('chat');
  }

  // The house stands in for the back arrow only while the chat covers the front
  // page itself — the same rule as session_navigation.js (#2266). Over the chats
  // view or a Work, or with the list already showing, it is a plain link to `/`.
  function brandClosesInPlace() {
    return !body.classList.contains('mobile-list-open') &&
      new URL(sidebar.dataset.listPath || '/', location.href).pathname === '/';
  }

  function openList() {
    if (!initialized || !mobile.matches || body.classList.contains('mobile-list-open')) return;
    history.back();
  }

  document.addEventListener('click', function (event) {
    var home = event.target.closest('a.back[href="/"], a.brand[href="/"]');
    if (!mobile.matches || !home) return;
    // With the list already showing, the arrow (a Work's, #2281) and the house are plain
    // links to `/`: there is nothing left to close.
    if (body.classList.contains('mobile-list-open')) return;
    if (home.classList.contains('brand') &&
        (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey ||
         event.altKey || (home.target && home.target !== '_self') ||
         !brandClosesInPlace())) return;
    // Only claim the tap while this module can actually move the layers.
    // Swallowing it before then makes the arrow a dead control; leaving it
    // alone lets the navigation module close the chat instead (#2047).
    if (!initialized) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    openList();
  }, {capture: true, signal: signal});

  window.addEventListener('popstate', function (event) {
    var layer = event.state && event.state[STATE_KEY];
    // A Work's entry is a list too: session_navigation.js loads it when it is not here.
    if (layer && layer !== 'chat' && !listIsHere(sidebar)) return;
    if (layer && mobile.matches) showLayer(layer === 'chat' ? 'chat' : 'list');
  }, {signal: signal});

  // A deliberate swipe starting at the left edge drags the chat layer with the
  // finger. Vertical transcript scrolling wins unless horizontal movement is
  // clearly dominant, so ordinary reading never accidentally changes views.
  var drag = null;
  var EDGE_PX = 28;
  var COMMIT_RATIO = 0.28;
  var DIRECTION_LOCK_PX = 8;
  var VERTICAL_CANCEL_RATIO = 1.5;

  function resetDrag() {
    if (drag) {
      try { detail.releasePointerCapture(drag.id); } catch (_) {}
    }
    drag = null;
    body.classList.remove('mobile-stack-dragging');
    detail.style.removeProperty('--mobile-chat-x');
  }

  detail.addEventListener('pointerdown', function (event) {
    if (!initialized || !mobile.matches || body.classList.contains('mobile-list-open') ||
        !event.isPrimary || event.clientX > EDGE_PX ||
        event.target.closest('input, textarea, select, button, a, [contenteditable]')) return;
    drag = {
      id: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      horizontal: false,
    };
    // Keep receiving the gesture even when a fast move leaves its initial
    // target. This does not block vertical scrolling; touch-action still lets
    // the browser cancel a native pan-y gesture.
    try { detail.setPointerCapture(event.pointerId); } catch (_) {}
  }, {signal: signal});

  detail.addEventListener('pointermove', function (event) {
    if (!drag || event.pointerId !== drag.id) return;
    var dx = Math.max(0, event.clientX - drag.startX);
    var dy = Math.abs(event.clientY - drag.startY);
    if (!drag.horizontal) {
      if (dy > 10 && dy > dx * VERTICAL_CANCEL_RATIO) { resetDrag(); return; }
      if (dx < DIRECTION_LOCK_PX || dx <= dy) return;
      drag.horizontal = true;
      body.classList.add('mobile-stack-dragging');
    }
    event.preventDefault();
    detail.style.setProperty('--mobile-chat-x', Math.min(dx, detail.clientWidth) + 'px');
  }, {passive: false, signal: signal});

  function finishDrag(event) {
    if (!drag || event.pointerId !== drag.id) return;
    var dx = Math.max(0, event.clientX - drag.startX);
    var dy = Math.abs(event.clientY - drag.startY);
    var horizontal = drag.horizontal || (dx >= DIRECTION_LOCK_PX && dx > dy);
    var shouldOpen = horizontal && dx >= detail.clientWidth * COMMIT_RATIO;
    if (shouldOpen) {
      drag = null;
      openList();
      return;
    }
    resetDrag();
  }
  detail.addEventListener('pointerup', finishDrag, {signal: signal});
  detail.addEventListener('pointercancel', resetDrag, {signal: signal});

  // A quick swipe toward earlier messages should not require repeating the
  // gesture through a long transcript. The finger moves down while the
  // transcript moves toward its top. Keep the threshold intentionally strict
  // so ordinary reading, horizontal Back swipes, and slow repositioning retain
  // their native behavior.
  var topSwipe = null;
  var TOP_SWIPE_MIN_DISTANCE_PX = 220;
  var TOP_SWIPE_MIN_VELOCITY_PX_MS = 1.25;
  var TOP_SWIPE_VERTICAL_RATIO = 1.5;

  detail.addEventListener('touchstart', function (event) {
    if (!initialized || !mobile.matches || event.touches.length !== 1 ||
        body.classList.contains('mobile-list-open') ||
        event.target.closest('input, textarea, select, button, a, [contenteditable]')) {
      topSwipe = null;
      return;
    }
    var touch = event.touches[0];
    topSwipe = {
      id: touch.identifier,
      startX: touch.clientX,
      startY: touch.clientY,
      startedAt: event.timeStamp,
    };
  }, {passive: true, signal: signal});

  detail.addEventListener('touchend', function (event) {
    if (!topSwipe) return;
    var touch = Array.from(event.changedTouches).find(function (candidate) {
      return candidate.identifier === topSwipe.id;
    });
    if (!touch) return;

    var dx = Math.abs(touch.clientX - topSwipe.startX);
    var dy = touch.clientY - topSwipe.startY;
    var elapsed = Math.max(1, event.timeStamp - topSwipe.startedAt);
    topSwipe = null;
    if (detail.scrollTop <= detail.clientHeight * 2 ||
        dy < TOP_SWIPE_MIN_DISTANCE_PX ||
        dy < dx * TOP_SWIPE_VERTICAL_RATIO ||
        dy / elapsed < TOP_SWIPE_MIN_VELOCITY_PX_MS) return;

    detail.scrollTo({
      top: 0,
      behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
    });
  }, {passive: true, signal: signal});

  detail.addEventListener('touchcancel', function () {
    topSwipe = null;
  }, {passive: true, signal: signal});

  // Keep the chat's usable height equal to the *visual* viewport so the sticky
  // composer — and its Send/Stop buttons — sits above the on-screen keyboard
  // instead of behind it. `dvh` doesn't shrink for the keyboard on iOS, so a
  // bottom-anchored composer otherwise falls under it (issue #17). One observer,
  // no timeouts, per the mobile-keyboard contract in AGENTS.md.
  var viewport = window.visualViewport;
  if (viewport) {
    var applyHeight = function () {
      if (mobile.matches) {
        document.documentElement.style.setProperty(
          '--app-height', Math.round(viewport.height) + 'px'
        );
      } else {
        document.documentElement.style.removeProperty('--app-height');
      }
    };
    viewport.addEventListener('resize', applyHeight, {signal: signal});
    viewport.addEventListener('scroll', applyHeight, {signal: signal});
    if (mobile.addEventListener) {
      mobile.addEventListener('change', applyHeight, {signal: signal});
    }
    applyHeight();
  }

  initialize();
  if (mobile.addEventListener) {
    mobile.addEventListener('change', initialize, {signal: signal});
  }
  }

  window.AgentDeckMobileSessionStack = {
    init: init,
    listIsHere: listIsHere
  };
  document.addEventListener('agentdeck:session-navigation', init);
  init();
})();
