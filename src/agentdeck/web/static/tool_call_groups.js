(function () {
  'use strict';

  // A busy turn writes the same tool over and over, and each call used to cost a
  // full card with its own repeated "TOOL BASH" head. Adjacent calls sharing one
  // `data-tool-key` are merged into a single card here: the head is printed once
  // with a ×N count, and every call keeps its own peek, timestamp and expander.
  //
  // This runs on the DOM rather than in the renderer because a run is a property
  // of what is actually next to what: transcript rows arrive one SSE fragment at
  // a time, optimistic messages sit between them until they reconcile, and
  // "load earlier" prepends history above. Re-deriving from real adjacency keeps
  // all of those correct without any cross-fragment server bookkeeping.
  var activeCleanup = null;

  // The agent's in-between lines ("Find the code.") fold the same way (#3200):
  // within one turn only the reply that ends it reads as a message, and across
  // turns the deck woke up only the latest one does. An assistant
  // reply followed by more of the turn (a tool call or another reply) before the
  // next user message or deck notice is folded to one muted line and joins the
  // card of the tool calls after it. A tap opens it again.
  function keyOf(node) {
    if (!node.classList || !node.classList.contains('tool-call')) return null;
    return node.dataset ? (node.dataset.toolKey || null) : null;
  }

  function foldable(node) {
    var list = node.classList;
    if (!list || !list.contains('ev') || !list.contains('assistant')) return false;
    if (list.contains('sub') || list.contains('failed')) return false;
    if (node.querySelector('.ev-question')) return false;
    return true;
  }

  // Does the turn go on after nodes[i]? Sub-agent rows are part of the turn
  // around them, and so is a note the agent never saw (chat_notes.js, #3227);
  // anything else that is not the agent's own work ends it.
  function turnContinues(nodes, i) {
    for (var j = i + 1; j < nodes.length; j++) {
      var list = nodes[j].classList;
      if (!list || !list.contains('ev') || list.contains('sub') || list.contains('chat-note')) continue;
      return list.contains('tool') || list.contains('assistant');
    }
    return false;
  }

  function isEv(node) {
    var list = node.classList;
    return !!list && list.contains('ev') && !list.contains('sub') && !list.contains('chat-note');
  }

  // A turn the deck woke up (a wake-on, a verdict, a reminder) rather than a
  // person: the nearest message above the reply is a deck notice.
  function deckWoken(nodes, i) {
    for (var j = i - 1; j >= 0; j--) {
      if (!isEv(nodes[j])) continue;
      if (nodes[j].classList.contains('deck-notice')) return true;
      if (nodes[j].classList.contains('user')) return false;
    }
    return false;
  }

  // Has the agent replied again since, before the person's next message?
  function repliedAgain(nodes, i) {
    for (var j = i + 1; j < nodes.length; j++) {
      if (!isEv(nodes[j])) continue;
      if (nodes[j].classList.contains('user')) return false;
      if (nodes[j].classList.contains('assistant')) return true;
    }
    return false;
  }

  // While the agent is still working (the chat ends in its tool calls, not a
  // reply), its latest line in that turn reads in full, so a person can see what
  // it is doing. It folds once the turn moves on.
  function liveLine(nodes) {
    for (var i = nodes.length - 1; i >= 0; i--) {
      var list = nodes[i].classList;
      if (!isEv(nodes[i]) || list.contains('leg-switch')) continue;
      if (!list.contains('tool')) return -1;
      for (var j = i - 1; j >= 0; j--) {
        if (!isEv(nodes[j])) continue;
        if (foldable(nodes[j])) return j;
        if (!nodes[j].classList.contains('tool')) return -1;
      }
      return -1;
    }
    return -1;
  }

  function regroup(transcript) {
    var nodes = Array.prototype.slice.call(transcript.children).filter(function (node) {
      return !node.classList.contains('tool-block-summary');
    });
    // Between two of the person's own messages only the agent's latest reply
    // reads in full: an earlier reply that ended a turn the deck woke up folds
    // too. A reply to the person's own message never does.
    var live = liveLine(nodes);
    var folded = nodes.map(function (node, i) {
      if (!foldable(node) || i === live) return false;
      return turnContinues(nodes, i) || (deckWoken(nodes, i) && repliedAgain(nodes, i));
    });
    var keys = nodes.map(function (node, i) { return folded[i] ? null : keyOf(node); });
    // Folded lines are see-through when deciding what is next to what.
    function solid(i, step) {
      while (i >= 0 && i < nodes.length && folded[i]) i += step;
      return i;
    }
    function keyAt(i) { return i >= 0 && i < nodes.length ? keys[i] : null; }
    // A folded line joins the card of the tool calls after it.
    function attaches(i) { return folded[i] && keyAt(solid(i + 1, 1)) !== null; }
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      var key = keys[i];
      var noteAbove = i > 0 && attaches(i - 1);
      node.classList.toggle('ev-folded', folded[i]);
      if (!folded[i]) node.classList.remove('ev-unfolded');
      if (folded[i]) {
        var joins = attaches(i);
        var inside = joins && (noteAbove || keyAt(solid(i - 1, -1)) === keyAt(solid(i + 1, 1)));
        node.classList.toggle('tool-run-item', inside);
        node.classList.toggle('tool-run-lead', joins && !inside);
        node.classList.toggle('tool-run-end', false);
        node.classList.toggle('tool-run-joined', false);
        continue;
      }
      var before = keyAt(solid(i - 1, -1));
      var after = keyAt(solid(i + 1, 1));
      var follows = key !== null && key === before;
      var leads = key !== null && !follows && key === after;
      node.classList.toggle('tool-run-item', follows);
      node.classList.toggle('tool-run-lead', leads);
      // Only the closing row keeps the card's bottom corners.
      node.classList.toggle('tool-run-end', follows && key !== after);
      // First call of its tool, under a folded line in the same card: it keeps
      // its own head but closes the gap above it.
      node.classList.toggle('tool-run-joined', key !== null && !follows && noteAbove);
      var counter = node.querySelector('.tool-run-count');
      if (!counter) continue;
      var count = '';
      if (leads) {
        var run = 1;
        for (var j = solid(i + 1, 1); keyAt(j) === key; j = solid(j + 1, 1)) run++;
        count = '×' + run;
      }
      if (counter.textContent !== count) counter.textContent = count;
    }
    summarize(transcript, nodes, function (i) { return folded[i] || keys[i] !== null; });
  }

  // A turn's steps (its tool calls and folded lines, next to each other) close
  // behind one line, "▸ 9 steps · Bash ×7 · Read ×2", so a finished turn reads
  // as what was asked and what was answered. The steps at the very end of the
  // chat stay open while they are still the live edge; a tap opens or closes any
  // of them, and that choice is kept on the line.
  function summarize(transcript, nodes, isStep) {
    var kept = [];
    var i = 0;
    while (i < nodes.length) {
      if (!isStep(i)) {
        nodes[i].classList.remove('tool-block-hidden', 'tool-quiet');
        i++;
        continue;
      }
      var start = i;
      while (i < nodes.length && isStep(i)) i++;
      var rows = nodes.slice(start, i);
      var quiet = quietTurn(nodes, i);
      // One step is already one line: a lone folded reply shows its own first
      // words and opens on a tap, so a "1 step" line would hide them for nothing.
      if (rows.length < 2) {
        rows[0].classList.remove('tool-block-hidden');
        rows[0].classList.toggle('tool-quiet', quiet);
        continue;
      }
      rows.forEach(function (row) { row.classList.remove('tool-quiet'); });
      var line = rows[0].previousElementSibling;
      if (!line || !line.classList.contains('tool-block-summary')) {
        line = document.createElement('div');
        line.className = 'tool-block-summary';
        line.setAttribute('role', 'button');
        line.setAttribute('tabindex', '0');
        transcript.insertBefore(line, rows[0]);
      }
      kept.push(line);
      var tail = !nodes.slice(i).some(function (node) {
        return node.classList.contains('ev') && !node.classList.contains('leg-switch');
      });
      var open = line.dataset.chosen ? line.dataset.chosen === 'open' : tail;
      describe(line, rows, open);
      line.classList.toggle('tool-quiet', quiet);
      rows.forEach(function (row) { row.classList.toggle('tool-block-hidden', !open); });
    }
    Array.prototype.slice.call(transcript.querySelectorAll(':scope > .tool-block-summary'))
      .forEach(function (line) { if (kept.indexOf(line) < 0) line.remove(); });
  }

  // Steps the turn ended on with no reply to read (the agent only used tools,
  // or its reply folded): a person's message or a deck notice comes next. They
  // read as a quiet grey line like the deck's, not an orange card. The live
  // edge is not one of them: the turn may still reply.
  function quietTurn(nodes, after) {
    for (var j = after; j < nodes.length; j++) {
      if (!isEv(nodes[j]) || nodes[j].classList.contains('leg-switch')) continue;
      return !nodes[j].classList.contains('assistant');
    }
    return false;
  }

  function describe(line, rows, open) {
    var names = [];
    var counts = {};
    var notes = 0;
    rows.forEach(function (row) {
      if (row.classList.contains('ev-folded')) { notes++; return; }
      var name = row.querySelector('.tool-name');
      var label = name ? name.textContent.trim() : 'tool';
      if (!(label in counts)) { counts[label] = 0; names.push(label); }
      counts[label]++;
    });
    var parts = names.map(function (name) {
      return counts[name] > 1 ? name + ' ×' + counts[name] : name;
    });
    if (notes) parts.push(notes + (notes === 1 ? ' note' : ' notes'));
    var times = rows.map(function (row) { return row.querySelector('.ev-time'); })
      .filter(Boolean);
    var html = '<span class="tool-block-count">' + rows.length + ' steps</span>' +
      '<span class="tool-block-names"></span>' +
      (times.length ? '<span class="tool-block-time"></span>' : '');
    if (line.dataset.shape !== html) {
      line.innerHTML = html;
      line.dataset.shape = html;
    }
    line.querySelector('.tool-block-names').textContent = parts.join(' · ');
    if (times.length) {
      line.querySelector('.tool-block-time').textContent = times[times.length - 1].textContent;
    }
    line.classList.toggle('open', open);
    line.setAttribute('aria-expanded', open ? 'true' : 'false');
  }

  // A folded line opens on a tap anywhere on it, and closes again from its head.
  function onClick(event) {
    var line = event.target.closest && event.target.closest('.tool-block-summary');
    if (line) {
      line.dataset.chosen = line.classList.contains('open') ? 'closed' : 'open';
      regroup(event.currentTarget);
      return;
    }
    var row = event.target.closest && event.target.closest('.ev-folded');
    if (!row) return;
    if (row.classList.contains('ev-unfolded')) {
      if (!event.target.closest('.ev-head') || event.target.closest('button, a')) return;
      row.classList.remove('ev-unfolded');
      return;
    }
    if (window.getSelection && String(window.getSelection())) return;
    row.classList.add('ev-unfolded');
  }

  function onKey(event) {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    if (!event.target.classList || !event.target.classList.contains('tool-block-summary')) return;
    event.preventDefault();
    onClick(event);
  }

  function init() {
    var transcript = document.querySelector('.transcript[data-session-key]');
    if (activeCleanup) activeCleanup();
    activeCleanup = null;
    if (!transcript) return;
    // childList only. Regroup writes classes and text, and adds or removes only
    // its own summary lines, so the one extra callback it can cause changes
    // nothing. The callback runs before paint, so a merged row never flashes as
    // a separate card.
    var observer = new MutationObserver(function () { regroup(transcript); });
    observer.observe(transcript, { childList: true });
    transcript.addEventListener('click', onClick);
    transcript.addEventListener('keydown', onKey);
    activeCleanup = function () {
      observer.disconnect();
      transcript.removeEventListener('click', onClick);
      transcript.removeEventListener('keydown', onKey);
    };
    regroup(transcript);
  }

  window.AgentDeckToolCallGroups = { init: init, regroup: regroup };
  document.addEventListener('agentdeck:session-navigation', init);
  init();
})();
