(function () {
  'use strict';

  var GAP_PX = 8;
  var activeCleanup = null;

  function init() {
    var transcript = document.querySelector('.transcript');
    var button = document.querySelector('[data-last-user-jump]');
    if (!transcript || !button || button.dataset.lastUserJumpReady) return;
    if (activeCleanup) activeCleanup();
    var controller = new AbortController();
    var signal = controller.signal;
    button.dataset.lastUserJumpReady = 'true';

    var detail = document.querySelector('.session-detail');

    function scrollRoot() {
      if (detail && getComputedStyle(detail).overflowY === 'auto') return detail;
      return window;
    }

    // A queued message that has not reached the transcript yet renders at the
    // very bottom, so jumping to one would move nothing. The last durable user
    // message is the one worth reading down from.
    function lastUserMessage() {
      var rows = transcript.querySelectorAll('.ev.user:not(.pending-message)');
      return rows.length ? rows[rows.length - 1] : null;
    }

    // The chat header is pinned over the top of the transcript, so land the
    // message below it rather than underneath it.
    function headOverlap() {
      var head = document.querySelector('.session-detail-head');
      if (!head || getComputedStyle(head).position !== 'sticky') return 0;
      return head.getBoundingClientRect().height;
    }

    function sync() {
      button.hidden = !lastUserMessage();
    }

    function jump() {
      var target = lastUserMessage();
      if (!target) return;
      // Without this the next transcript update would scroll straight back to
      // the newest message and undo the jump.
      if (window.AgentDeckSessionBottomFollow &&
          AgentDeckSessionBottomFollow.stopFollowing) {
        AgentDeckSessionBottomFollow.stopFollowing();
      }
      // Scroll the pane itself, never scrollIntoView. On mobile the chat sits
      // inside overflow:hidden ancestors and is translated a full viewport
      // aside while the chat list is open, so scrollIntoView scrolls those
      // ancestors sideways and strands the reader (#545).
      var box = target.getBoundingClientRect();
      var root = scrollRoot();
      if (root === window) {
        window.scrollTo({
          top: Math.max(0, window.scrollY + box.top - headOverlap() - GAP_PX),
          behavior: 'smooth',
        });
        return;
      }
      var pane = root.getBoundingClientRect();
      root.scrollTo({
        top: Math.max(0, root.scrollTop + (box.top - pane.top) - headOverlap() - GAP_PX),
        behavior: 'smooth',
      });
    }

    button.addEventListener('click', jump, {signal: signal});
    // A chat can start with no user message at all — a read-only transcript of
    // an agent's own leg — and gain one later over SSE or "load earlier".
    document.body.addEventListener('htmx:afterSwap', sync, {signal: signal});
    document.body.addEventListener('htmx:sseMessage', sync, {signal: signal});
    activeCleanup = function () { controller.abort(); };
    sync();
  }

  window.AgentDeckLastUserJump = { init: init };
  init();
})();
