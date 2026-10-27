(function () {
  'use strict';

  var activeCleanup = null;
  var nextBodyId = 1;

  function messageRows(scope) {
    var root = scope || document;
    var rows = Array.prototype.slice.call(
      root.querySelectorAll('.transcript .ev.user, .transcript .ev.system')
    );
    if (root.matches && root.matches('.transcript .ev.user, .transcript .ev.system')) {
      rows.unshift(root);
    }
    return rows;
  }

  function prepare(row) {
    var head = row.querySelector(':scope > .ev-head');
    if (!head) return null;

    var meta = head.querySelector(':scope > .ev-head-meta');
    if (!meta) {
      meta = document.createElement('span');
      meta.className = 'ev-head-meta';
      while (head.children.length > 1) meta.appendChild(head.children[1]);
      head.appendChild(meta);
    }

    var body = row.querySelector(':scope > .ev-message-body');
    if (!body) {
      body = document.createElement('div');
      body.className = 'ev-message-body';
      row.appendChild(body);
    }
    Array.prototype.slice.call(row.children).forEach(function (child) {
      if (child === head || child === body) return;
      // The refusal sentence on a failed send says why the message did not
      // land, and #1802 is what hiding it costs: swept into the clamped body it
      // sat behind "Show more", the row read only "Failed · retry", and the same
      // conclusive 409 was retried five times. Keep it a sibling of the body so
      // it stays visible whether or not the message itself is collapsed.
      if (child.classList.contains('message-error')) return;
      body.appendChild(child);
    });
    // The note may predate the body when a send fails before the first measure.
    // Only when it is not already last: the observer treats a move as a
    // childList change, and an unconditional one would reschedule forever.
    var error = row.querySelector(':scope > .message-error');
    if (error && error !== row.lastElementChild) row.appendChild(error);

    var button = meta.querySelector(':scope > .message-collapse-toggle');
    if (!button) {
      button = document.createElement('button');
      button.type = 'button';
      button.className = 'message-collapse-toggle';
      button.textContent = 'Show more';
      button.setAttribute('aria-expanded', 'false');
      body.id = body.id || 'message-body-' + nextBodyId++;
      button.setAttribute('aria-controls', body.id);
      meta.appendChild(button);
    }
    row.classList.add('message-collapse-ready');
    return body;
  }

  function measure(row, force) {
    var body = prepare(row);
    if (!body) return;
    var expanded = row.classList.contains('message-expanded');
    // Measuring costs the reader their place when the row is open. To read the
    // overflow we have to clamp the message back to three lines, and that
    // layout is real: the page briefly gets shorter, the browser pins the
    // scroll position to the shorter document, and putting the class back does
    // not put the reader back. With an event landing every second while the
    // agent works, that reads as the transcript jumping to the bottom once a
    // second (#1845). An open row is already known to overflow and its text
    // does not change, so only a deliberate resize re-measures it.
    if (expanded && !force) return;
    row.classList.remove('message-expanded');
    var overflows = body.scrollHeight > body.clientHeight + 1;
    row.classList.toggle('message-collapsible', overflows);
    if (overflows && expanded) row.classList.add('message-expanded');
    if (!overflows) {
      var button = row.querySelector('.message-collapse-toggle');
      // The observer watches childList changes, including textContent writes.
      // Do not keep scheduling ourselves after a short message has settled.
      if (button.textContent !== 'Show more') button.textContent = 'Show more';
      if (button.getAttribute('aria-expanded') !== 'false') {
        button.setAttribute('aria-expanded', 'false');
      }
    }
  }

  function scan(scope, force) {
    messageRows(scope).forEach(function (row) { measure(row, force); });
  }

  function init() {
    if (activeCleanup) activeCleanup();
    activeCleanup = null;
    var transcript = document.querySelector('.transcript');
    if (!transcript) return;

    // Whichever box the transcript actually scrolls in — the chat pane on a
    // wide screen and on mobile, the document on a plain page.
    function scrollPane() {
      var node = transcript.parentElement;
      while (node && node !== document.body) {
        var overflowY = getComputedStyle(node).overflowY;
        if (overflowY === 'auto' || overflowY === 'scroll') return node;
        node = node.parentElement;
      }
      return document.scrollingElement || document.documentElement;
    }

    var frame = null;
    var pendingScopes = new Set();
    var pendingForce = false;
    function schedule(scope, force) {
      pendingScopes.add(scope || transcript);
      if (force) pendingForce = true;
      if (frame !== null) return;
      frame = requestAnimationFrame(function () {
        frame = null;
        var force = pendingForce;
        pendingForce = false;
        // A forced scan clamps open rows shut for one layout, which is the
        // move that loses the reader's place. Hold the reading anchor across
        // it so even a genuine re-measure leaves them where they were.
        var pane = force ? scrollPane() : null;
        var anchor = pane ? pane.scrollTop : 0;
        if (pendingScopes.has(transcript)) scan(transcript, force);
        else pendingScopes.forEach(function (scope) { scan(scope, force); });
        pendingScopes.clear();
        if (pane && pane.scrollTop !== anchor) pane.scrollTop = anchor;
      });
    }
    function click(event) {
      var button = event.target.closest('.message-collapse-toggle');
      if (!button || !transcript.contains(button)) return;
      var row = button.closest('.ev');
      var expanded = !row.classList.contains('message-expanded');
      row.classList.toggle('message-expanded', expanded);
      button.textContent = expanded ? 'Show less' : 'Show more';
      button.setAttribute('aria-expanded', String(expanded));
    }
    // Only a change in width can change where a message wraps, so only a
    // change in width is worth re-measuring an open row for. A height-only
    // resize — a mobile keyboard opening, the URL bar collapsing — arrives
    // constantly and must leave the reader exactly where they are.
    var measuredWidth = transcript.clientWidth;
    function resize() {
      var width = transcript.clientWidth;
      if (width === measuredWidth) return;
      measuredWidth = width;
      schedule(transcript, true);
    }
    function changed(records) {
      records.forEach(function (record) {
        var row = record.target.closest && record.target.closest('.ev.user, .ev.system');
        if (row) {
          schedule(row);
          return;
        }
        // A new event appended to the transcript says nothing about the
        // messages already above it. Measure only what arrived.
        var added = record.type === 'childList' ? record.addedNodes : null;
        if (!added || !added.length) {
          schedule(transcript);
          return;
        }
        Array.prototype.forEach.call(added, function (node) {
          schedule(node.nodeType === 1 ? node : transcript);
        });
      });
    }

    transcript.addEventListener('click', click);
    window.addEventListener('resize', resize);
    var observer = new MutationObserver(changed);
    observer.observe(transcript, {childList: true, subtree: true});
    activeCleanup = function () {
      if (frame !== null) cancelAnimationFrame(frame);
      pendingScopes.clear();
      observer.disconnect();
      transcript.removeEventListener('click', click);
      window.removeEventListener('resize', resize);
    };
    schedule(transcript);
  }

  window.AgentDeckMessageCollapse = {init: init, scan: scan};
  document.addEventListener('agentdeck:session-navigation', init);
  init();
})();
