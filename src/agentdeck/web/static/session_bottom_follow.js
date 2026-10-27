(function () {
  'use strict';

  var NEAR_BOTTOM_PX = 80;
  var activeCleanup = null;
  // Set by the live chat's init, so another control that deliberately scrolls
  // away from the end can hand the follow back before it moves.
  var activeCancelFollow = null;

  function init() {
    var transcript = document.querySelector('.transcript');
    if (!transcript || transcript.dataset.bottomFollowReady) return;
    if (activeCleanup) activeCleanup();
    var controller = new AbortController();
    var signal = controller.signal;
    var resizeObserver = null;
    activeCleanup = function () {
      controller.abort();
      if (resizeObserver) resizeObserver.disconnect();
    };
    transcript.dataset.bottomFollowReady = 'true';

    var detail = document.querySelector('.session-detail');
    var followingSend = false;
    var cancelledSendFollow = false;
    var followingInitial = false;
    var ownScrollTop = null;
    var scrollGeneration = 0;
    var observedQueueIds = new Set();
    var observedActionIds = new Set();

    function scrollRoot() {
      if (detail && getComputedStyle(detail).overflowY === 'auto') return detail;
      return window;
    }

    function atBottom() {
      var root = scrollRoot();
      if (root === window) {
        return window.innerHeight + window.scrollY >=
          document.documentElement.scrollHeight - NEAR_BOTTOM_PX;
      }
      return root.clientHeight + root.scrollTop >= root.scrollHeight - NEAR_BOTTOM_PX;
    }

    function scrollTop() {
      var root = scrollRoot();
      return root === window ? window.scrollY : root.scrollTop;
    }

    function toBottom() {
      var root = scrollRoot();
      if (root === window) window.scrollTo(0, document.documentElement.scrollHeight);
      else root.scrollTo(0, root.scrollHeight);
      ownScrollTop = scrollTop();
    }

    function scheduleToBottom(generation) {
      requestAnimationFrame(function () {
        if (generation === scrollGeneration) toBottom();
      });
    }

    function following() {
      return followingSend || followingInitial;
    }

    activeCancelFollow = cancelFollow;

    function cancelFollow() {
      followingInitial = false;
      ownScrollTop = null;
      scrollGeneration += 1;
      if (followingSend) cancelledSendFollow = true;
      followingSend = false;
    }

    function onScroll() {
      if (atBottom()) {
        // Once the reader deliberately returns to the end, resume following
        // the queued turn and its eventual assistant response.
        if (cancelledSendFollow) {
          followingSend = true;
          cancelledSendFollow = false;
        }
        return;
      }

      // Our own scroll to the end, reported back. The event is delivered a
      // frame later, by which time content added below it (a loading image, an
      // enhanced message) can mean the position we chose is no longer the end.
      // Reading that as the reader leaving would strand them mid-history.
      if (following() && ownScrollTop !== null &&
          Math.abs(scrollTop() - ownScrollTop) <= 1) {
        return;
      }

      // A real position change is the source of truth. This also catches
      // scrollbar dragging and browser/keyboard scrolling that do not produce
      // wheel or touch events.
      scrollGeneration += 1;
      ownScrollTop = null;
      if (followingSend) cancelledSendFollow = true;
      followingSend = false;
      followingInitial = false;
    }

    window.addEventListener('scroll', onScroll, { passive: true, signal: signal });
    if (detail) {
      detail.addEventListener('scroll', onScroll, { passive: true, signal: signal });
    }
    window.addEventListener('wheel', function (event) {
      if (event.deltaY < 0) cancelFollow();
    }, { passive: true, signal: signal });
    window.addEventListener('keydown', function (event) {
      if (['PageUp', 'Home', 'ArrowUp'].includes(event.key) ||
          (event.key === ' ' && event.shiftKey)) {
        cancelFollow();
      }
    }, {signal: signal});

    // Land on the newest message straight away. The transcript is already in the
    // document when this deferred script runs, so waiting for `load` — which
    // waits for every image and other subresource — left the reader staring at
    // the top of the chat for seconds before it jumped (issue #851). Scroll now,
    // and stay pinned to the end through the late height changes that follow
    // (markdown enhancement, images, fonts).
    followingInitial = true;
    toBottom();
    scheduleToBottom(scrollGeneration);

    // Nothing announces that a chat has finished settling: a picture can decode,
    // markdown can be enhanced, or a font can swap at any point, and a chat
    // opened in place from the deck has no `load` event of its own at all. So
    // the follow simply lasts until the reader takes over — a scroll away, or
    // putting the caret in the composer, which must not be pushed around by a
    // picture that lands afterwards. Anything else is a timer pretending to be
    // a signal.
    document.body.addEventListener('focusin', function (event) {
      if (event.target.closest('.inject-form')) followingInitial = false;
    }, {signal: signal});

    function reconcilePendingMessages() {
      transcript.querySelectorAll('[data-observed-queue-id], [data-observed-action-id]')
        .forEach(function (durable) {
          var queueId = durable.dataset.observedQueueId;
          var actionId = durable.dataset.observedActionId;
          if (queueId) observedQueueIds.add(queueId);
          if (actionId && !observedActionIds.has(actionId)) {
            observedActionIds.add(actionId);
            if (window.AgentDeckActionTiming) {
              window.AgentDeckActionTiming.mark(actionId, 'first_transcript');
            }
          }
        });

      var actionRows = new Map();
      transcript.querySelectorAll('[data-pending-message]').forEach(function (pending) {
        var pendingQueueId = pending.dataset.queueId;
        var pendingActionId = pending.dataset.clientActionId;
        if ((pendingQueueId && observedQueueIds.has(pendingQueueId)) ||
            (pendingActionId && observedActionIds.has(pendingActionId))) {
          if (pendingActionId && window.AgentDeckActionTiming) {
            window.AgentDeckActionTiming.mark(pendingActionId, 'first_transcript');
          }
          pending.remove();
          return;
        }
        if (pendingActionId && actionRows.has(pendingActionId)) {
          pending.remove();
          return;
        }
        if (pendingActionId) actionRows.set(pendingActionId, pending);
      });
    }

    function beforeTranscriptSwap(event) {
      if (!event.target.matches('.transcript')) return;
      var composerFocused = document.activeElement &&
        document.activeElement.closest('.inject-form');
      event.target._agentdeckFollowSwap = {
        generation: scrollGeneration,
        enabled: followingSend || (atBottom() && !composerFocused),
      };
    }

    function afterTranscriptSwap(event) {
      if (!event.target.matches('.transcript')) return;
      reconcilePendingMessages();
      var swap = event.target._agentdeckFollowSwap;
      delete event.target._agentdeckFollowSwap;
      if (swap && swap.enabled) scheduleToBottom(swap.generation);
    }

    // Reveal by scrolling the chat's own scroll box, never through
    // scrollIntoView. On mobile the chat is a layer inside overflow:hidden
    // ancestors and is translated a full viewport to the right while the session
    // list is open, so scrollIntoView would scroll those ancestors sideways to
    // chase it — sliding the list away and leaving an inert, pointer-events:none
    // chat over the screen with no scrollbar and no gesture back (issue #545).
    // Scrolling the pane instead still queues the question up for whenever the
    // reader returns to it. Mirrors `block: 'nearest'`: align the nearest edge,
    // and prefer the top when the widget is taller than the pane.
    function revealInPane(section) {
      var root = scrollRoot();
      if (root === window) {
        section.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'nearest' });
        return;
      }
      var pane = root.getBoundingClientRect();
      var box = section.getBoundingClientRect();
      var delta = 0;
      if (box.bottom > pane.bottom) delta = box.bottom - pane.bottom;
      if (box.top - delta < pane.top) delta = box.top - pane.top;
      if (!delta) return;
      root.scrollTo({ top: root.scrollTop + delta, behavior: 'smooth' });
    }

    function revealInteraction(event) {
      // The pending-interaction widget arrives over its own `interaction` SSE
      // topic or through a one-shot PWA foreground reconciliation. When a real
      // prompt appears — a question or an approval, both carry the answer form —
      // bring it into view so the reader sees what needs answering, even if they
      // had scrolled up into history. The cleared state is an empty section with
      // no form, and must not scroll.
      if (!event.target || event.target.id !== 'pending-interaction-slot') return;
      var section = event.target.querySelector('#pending-interaction');
      if (!section || !section.querySelector('form[data-agentdeck-action="interaction"]')) {
        return;
      }
      // One rAF so the swapped-in widget has laid out before we measure/scroll.
      requestAnimationFrame(function () { revealInPane(section); });
    }

    document.body.addEventListener('htmx:beforeSwap', beforeTranscriptSwap, {signal: signal});
    document.body.addEventListener('htmx:afterSwap', afterTranscriptSwap, {signal: signal});
    document.body.addEventListener('htmx:afterSwap', revealInteraction, {signal: signal});
    // The SSE extension uses its own lifecycle events instead of the ordinary
    // before/afterSwap pair.
    document.body.addEventListener(
      'htmx:sseBeforeMessage', beforeTranscriptSwap, {signal: signal}
    );
    document.body.addEventListener('htmx:sseMessage', afterTranscriptSwap, {signal: signal});
    document.body.addEventListener('htmx:sseMessage', revealInteraction, {signal: signal});
    document.body.addEventListener('agentdeck:optimistic-send', function () {
      followingSend = true;
      cancelledSendFollow = false;
      scrollGeneration += 1;
      scheduleToBottom(scrollGeneration);
    }, {signal: signal});
    reconcilePendingMessages();

    if (window.visualViewport) {
      window.visualViewport.addEventListener('resize', function () {
        if (following()) scheduleToBottom(scrollGeneration);
      }, {signal: signal});
    }

    // Markdown enhancement and other real DOM changes can alter transcript
    // height after the HTMX/SSE swap itself. Keep following only while the
    // reader has not scrolled away.
    if (window.ResizeObserver) {
      resizeObserver = new ResizeObserver(function () {
        reconcilePendingMessages();
        if (following()) scheduleToBottom(scrollGeneration);
      });
      resizeObserver.observe(transcript);
    }
  }

  function stopFollowing() {
    if (activeCancelFollow) activeCancelFollow();
  }

  window.AgentDeckSessionBottomFollow = { init: init, stopFollowing: stopFollowing };
  init();
})();
