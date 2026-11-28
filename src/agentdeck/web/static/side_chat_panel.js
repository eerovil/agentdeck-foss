// The message box's side chat (#4313). The box's Main chat / Side chat switch picks where
// a message goes. On Side chat the chat's one side chat takes the chat's place on the
// page, the box sends there through `POST /sessions/{key}/side-chat`, and the box keeps a
// draft of its own. The deck's own send never sees a side message: this file's submit
// listener runs first, on the window, and stops it, so no bubble lands in the main chat.
(function () {
  if (window.agentdeckSidePanel) return;

  var MODE_PREFIX = 'agentdeck:side-mode:';
  var DRAFT_PREFIX = 'agentdeck:message-draft:';
  var SIDE_DRAFT = '/side-chat';

  function view() { return document.querySelector('[data-side-view]'); }
  function detailOf(side) { return (side && side.closest('.session-detail')) || document.body; }
  function composer(side) {
    return detailOf(side).querySelector('form.inject-form') ||
      document.querySelector('form.inject-form');
  }
  function box(form) { return form && form.querySelector('textarea[name="message"]'); }
  function sideOn(side) { return !!side && detailOf(side).classList.contains('side-mode'); }
  function mainKey(side) { return side.dataset.mainKey; }

  function read(key) { try { return sessionStorage.getItem(key) || ''; } catch (_) { return ''; } }
  function write(key, value) {
    try {
      if (value) sessionStorage.setItem(key, value);
      else sessionStorage.removeItem(key);
    } catch (_) {}
  }
  function mainDraftKey(form) { return DRAFT_PREFIX + form.getAttribute('hx-post'); }
  function sideDraftKey(side) { return DRAFT_PREFIX + side.dataset.sidePost + SIDE_DRAFT; }

  // The box starts one line high and grows with its text, up to the CSS max-height. Where
  // the browser has `field-sizing: content` the CSS does it all; elsewhere grow does, and
  // must run whenever the text or the width changes, not only on typing (#4330).
  var SIZES_ITSELF = !!(window.CSS && CSS.supports && CSS.supports('field-sizing', 'content'));
  var TEXT = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value');

  function grow(input) {
    if (SIZES_ITSELF) {
      input.style.height = '';
      return;
    }
    input.style.height = 'auto';
    input.style.height = input.scrollHeight + 'px';
  }

  // Code that sets the text (a send that empties the box, a quick phrase, dictation, a
  // restored draft) fires no `input` event, so the box's own text setter grows it.
  function followText(input) {
    if (SIZES_ITSELF || input._growsOnSet) return;
    input._growsOnSet = true;
    Object.defineProperty(input, 'value', {
      configurable: true,
      get: function () { return TEXT.get.call(this); },
      set: function (text) {
        TEXT.set.call(this, text);
        grow(this);
      },
    });
  }

  function growShown() {
    var input = box(composer(view()));
    if (input) grow(input);
  }
  if (!SIZES_ITSELF) {
    window.addEventListener('resize', growShown);
    if (window.visualViewport) window.visualViewport.addEventListener('resize', growShown);
  }

  function setBox(input, text) {
    input.value = text;
    grow(input);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  }

  // One rule for both modes' drafts: each mode keeps its own unsent text and its own
  // attached files, and the box and picker show only the shown mode's. base.html stores
  // the text under the key `data-draft-key` names; the files wait here, per mode, while
  // the other mode is shown (a page reload drops them, as it drops any picked file).
  function picker(form) { return form && form.querySelector('input[type="file"][name="images"]'); }
  function heldFiles(side) {
    if (!side._files) side._files = { main: [], side: [] };
    return side._files;
  }

  function setMode(side, mode, opts) {
    var form = composer(side);
    var input = box(form);
    var detail = detailOf(side);
    var toSide = mode === 'side';
    if (toSide !== sideOn(side) && input) {
      if (toSide) input.dataset.draftKey = side.dataset.sidePost + SIDE_DRAFT;
      else delete input.dataset.draftKey;
      setBox(input, read(toSide ? sideDraftKey(side) : mainDraftKey(form)));
      var held = heldFiles(side);
      held[toSide ? 'main' : 'side'] = fileList(picker(form));
      putFiles(picker(form), held[mode]);
      held[mode] = [];
    }
    if (input) {
      // The box says where its message goes.
      if (!input.dataset.mainPlaceholder) input.dataset.mainPlaceholder = input.placeholder;
      input.placeholder = toSide ? 'Ask the side chat…' : input.dataset.mainPlaceholder;
    }
    detail.classList.toggle('side-mode', toSide);
    if (form) form.classList.toggle('side-mode', toSide);
    side.hidden = !toSide;
    detail.querySelectorAll('[data-side-switch] [data-side-mode]').forEach(function (tab) {
      tab.setAttribute('aria-selected', String(tab.dataset.sideMode === mode));
    });
    if (toSide) {
      var dot = detail.querySelector('[data-side-dot]');
      if (dot) dot.hidden = true;
    }
    write(MODE_PREFIX + mainKey(side), toSide ? 'side' : '');
    if (opts && opts.focus && input) input.focus({ preventScroll: true });
    refresh(side);
    // Voice mode follows the switch (#4349).
    document.dispatchEvent(new CustomEvent('agentdeck:side-mode', { detail: { mode: toSide ? 'side' : 'main' } }));
  }

  function body(side) { return side.querySelector('[data-side-view-body]'); }

  // The foot offers send-back once the side chat has replied; ⤢ once it has a page.
  function refresh(side) {
    side = side || view();
    if (!side) return;
    var current = body(side);
    var open = side.querySelector('[data-side-open]');
    var url = current && current.dataset.sideUrl;
    if (open) {
      open.hidden = !url;
      if (url) open.setAttribute('href', url);
    }
    var foot = side.querySelector('[data-side-foot]');
    if (foot) foot.hidden = !latestReply(side);
  }

  function latestReply(side) {
    var replies = side.querySelectorAll('[data-side-view-log] .ev.assistant[data-event-seq]');
    return replies[replies.length - 1] || null;
  }

  function showError(side, text) {
    var error = side.querySelector('[data-side-error]');
    if (!error) return;
    error.textContent = text || '';
    error.hidden = !text;
  }

  // Through htmx, so the old body's side stream is closed as it leaves: a plain
  // replace would leave one open stream per send until the browser runs out of
  // connections to the deck.
  function swapBody(side, html) {
    var current = body(side);
    if (!current) return;
    if (window.htmx && typeof htmx.swap === 'function') {
      htmx.swap(current, html.trim(), { swapStyle: 'outerHTML', swapDelay: 0, settleDelay: 0 });
    } else {
      var holder = document.createElement('div');
      holder.innerHTML = html.trim();
      if (holder.firstElementChild) current.replaceWith(holder.firstElementChild);
    }
    refresh(side);
  }

  function pendingCard(side, text, id) {
    var log = side.querySelector('[data-side-view-log]');
    var empty = side.querySelector('[data-side-view-empty]');
    if (empty) empty.hidden = true;
    if (!log) {
      log = document.createElement('div');
      log.className = 'side-view-log';
      log.setAttribute('data-side-view-log', '');
      body(side).appendChild(log);
    }
    var card = document.createElement('div');
    card.className = 'ev user side-view-pending';
    card.dataset.clientActionId = id;
    card.dataset.afterEventId = newestEventId(side);
    var inner = document.createElement('div');
    inner.className = 'ev-text';
    inner.textContent = text;
    card.appendChild(inner);
    log.appendChild(card);
    card.scrollIntoView({ block: 'end' });
    return card;
  }

  // A line's place in the whole side chat: its chat-wide id, "17" on the first leg or
  // "2.17" on a later one (chat_transcript.chat_event_id). A leg's own numbers restart, so
  // only the leg and the number together name one line and say which came later.
  function eventPlace(id) {
    var parts = String(id || '').split('.');
    if (parts.length === 1) parts.unshift('0');
    var leg = Number(parts[0]);
    var seq = Number(parts[1]);
    if (parts.length !== 2 || !isFinite(leg) || !isFinite(seq)) return null;
    return [leg, seq];
  }
  function later(a, b) {
    if (!a) return false;
    if (!b) return true;
    return a[0] !== b[0] ? a[0] > b[0] : a[1] > b[1];
  }

  // The newest line the side view holds: a pending card counts only a line after it.
  function newestEventId(side) {
    var newest = '';
    side.querySelectorAll('[data-side-view-log] [data-event-id]').forEach(function (line) {
      var id = line.dataset.eventId;
      if (later(eventPlace(id), eventPlace(newest))) newest = id;
    });
    return newest;
  }

  // Each pending card goes only with its own delivery: the user line that names its
  // action id, or, when the transcript names none, a line with its words that came after
  // the card was sent. One line answers one card, so two equal sends need two lines, and
  // an older line with the same words answers none. Lines are told apart and ordered by
  // their chat-wide id, never by a leg's own number.
  function reconcile(side, root) {
    var claimed = side._claimed || (side._claimed = new Set());
    var lines = Array.from((root || side).querySelectorAll(
      '.ev.user[data-event-id]:not(.side-view-pending)'));
    var cards = Array.from(side.querySelectorAll('.side-view-pending[data-client-action-id]'));
    function take(card, test) {
      var line = lines.find(function (candidate) {
        return !claimed.has(candidate.dataset.eventId) && test(candidate);
      });
      if (!line) return false;
      claimed.add(line.dataset.eventId);
      card.remove();
      return true;
    }
    cards = cards.filter(function (card) {
      var id = card.dataset.clientActionId;
      return !take(card, function (line) {
        return (line.dataset.observedActionId || '').split(/\s+/).indexOf(id) !== -1;
      });
    });
    cards.forEach(function (card) {
      var text = (card.textContent || '').trim();
      var after = eventPlace(card.dataset.afterEventId);
      take(card, function (line) {
        var words = line.querySelector('.ev-text');
        if (line.dataset.observedActionId || !later(eventPlace(line.dataset.eventId), after) ||
            !words) return false;
        var shown = words.textContent.trim();
        // A secret's line is the notice with its name (`secret_notice`), never its value.
        if (card.dataset.secretName) {
          return shown.indexOf('temporary secret named ' + card.dataset.secretName + ' is') !== -1;
        }
        // A message voice mode dressed (#4349) shows as its words and the 🎧 mark
        // (hide_voice_line), not the lines it was sent with.
        if (card.dataset.voiced) shown = shown.replace(/\s*\u{1F3A7}$/u, '');
        return shown === text;
      });
    });
  }

  function actionId() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
      return window.crypto.randomUUID();
    }
    var bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    return Array.from(bytes, function (value) {
      return value.toString(16).padStart(2, '0');
    }).join('');
  }

  function fileList(input) { return input && input.files ? Array.from(input.files) : []; }
  function fileNames(list) {
    return list.map(function (file) { return file.name + ':' + file.size; }).join('|');
  }
  function putFiles(input, list) {
    if (!input || typeof DataTransfer !== 'function') return;
    if (!list.length && !fileList(input).length) return;
    var holder = new DataTransfer();
    list.forEach(function (file) { holder.items.add(file); });
    input.files = holder.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }

  // A send whose outcome is unknown (no answer, or the server could not say) keeps its
  // action id: sending the same words again reuses it, so the deck delivers it once.
  function sendId(side, text, files) {
    var held = side._uncertainSend;
    var sig = fileNames(files);
    if (held && held.text === text && held.files === sig) return held.id;
    return actionId();
  }

  function joined(first, second) {
    if (!first.trim()) return second;
    if (!second.trim()) return first;
    return first.replace(/\s+$/, '') + '\n\n' + second;
  }

  // A refused or uncertain send goes back into the side draft by the same rule, ahead of
  // whatever was typed or attached there since, so neither is lost: in the box and
  // picker when Side is shown, else in what Side keeps while Main is.
  function giveBack(side, text, files) {
    var form = composer(side);
    if (sideOn(side)) {
      var input = box(form);
      // Words before a /secret typed since would send its value as ordinary text (#4610),
      // so the failed words are shown with the error instead.
      if (input && text.trim() && window.AgentDeckSendRetry.isSecretCommand(input.value)) {
        var error = side.querySelector('[data-side-error]');
        showError(side, (error ? error.textContent : '') + ' Not sent: ' + text);
      } else if (input) setBox(input, joined(text, input.value));
      putFiles(picker(form), files.concat(fileList(picker(form))));
      return;
    }
    write(sideDraftKey(side), joined(text, read(sideDraftKey(side))));
    var held = heldFiles(side);
    held.side = files.concat(held.side);
  }

  // One send at a time, as the chat's own box (#2843).
  function send(side, form) {
    var input = box(form);
    var picker = form.querySelector('input[type="file"][name="images"]');
    var text = input ? input.value : '';
    var files = fileList(picker);
    if (!text.trim() && !files.length) return;
    if (side.dataset.sending) return;
    side.dataset.sending = '1';
    var id = sendId(side, text, files);
    var data = new FormData(form);
    data.set('client_action_id', id);
    // Voice mode's lines ride on a side message too (#4349); the draft given back on a
    // refusal is the text as written, without them.
    var voice = window.agentdeckVoiceMode;
    var dressed = voice && typeof voice.dress === 'function' && text.trim()
      ? voice.dress(text, id) : text;
    data.set('message', dressed);
    showError(side, '');
    // As in the main box: a secret's text is never shown, kept or given back once sent,
    // so one that failed is typed again (#4610).
    var label = window.AgentDeckSendRetry.secretLabel(text);
    var card = pendingCard(side, label || text, id);
    if (label) card.dataset.secretName = window.AgentDeckSendRetry.secretName(text) || '';
    if (dressed !== text) card.dataset.voiced = '1';
    if (input) setBox(input, '');
    write(sideDraftKey(side), '');
    if (picker) {
      picker.value = '';
      picker.dispatchEvent(new Event('change', { bubbles: true }));
    }
    var uncertain = true;
    fetch(side.dataset.sidePost, {
      method: 'POST',
      body: data,
      credentials: 'same-origin',
      headers: { 'X-AgentDeck-Action-ID': id, 'HX-Request': 'true' }
    }).then(function (response) {
      return response.text().then(function (payload) {
        // A 4xx is a known refusal; a 5xx may have written, like a lost answer.
        uncertain = response.status >= 500;
        if (response.ok) {
          if (!label) delete side._uncertainSend;
          swapBody(side, payload);
          // Accepted but maybe still queued: the card stays until its line lands.
          var log = side.querySelector('[data-side-view-log]');
          var live = body(side) && body(side).dataset.sideState === 'live';
          if (live && log) {
            var empty = side.querySelector('[data-side-view-empty]');
            if (empty) empty.hidden = true;
            log.appendChild(card);
            reconcile(side);
          }
          return;
        }
        var reason = '';
        try { reason = JSON.parse(payload).detail; } catch (_) {}
        throw new Error(typeof reason === 'string' && reason ? reason : 'The side chat did not take the message.');
      });
    }).catch(function (error) {
      if (label) {
        card.remove();
        var reason = error.message || 'The side chat could not be reached';
        showError(side, uncertain
          ? reason.replace(/[.\s]+$/, '') + '. Re-enter the secret to try again.' : reason);
        giveBack(side, '', files);
        return;
      }
      if (uncertain) side._uncertainSend = { id: id, text: text, files: fileNames(files) };
      else delete side._uncertainSend;
      card.remove();
      showError(side, error.message || 'The side chat could not be reached. Try again.');
      giveBack(side, text, files);
    }).finally(function () {
      delete side.dataset.sending;
    });
  }

  // On the window, in the capture phase: ahead of the deck's own listeners on the
  // document, so a side message never becomes a main-chat send or bubble.
  window.addEventListener('submit', function (event) {
    var form = event.target;
    if (!form.matches || !form.matches('form.inject-form')) return;
    var side = view();
    if (!side || !sideOn(side) || composer(side) !== form) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    send(side, form);
  }, true);

  // The side chat's latest reply goes into the chat's box as an unsent draft, after any
  // text already waiting there, with the same opening a side chat's own page gives it.
  function sendBackSummary(side) {
    var reply = latestReply(side);
    var textNode = reply && reply.querySelector('.ev-text');
    var text = textNode ? textNode.innerText.trim() : '';
    if (!text) return;
    var form = composer(side);
    var summary = window.agentdeckSideChat && window.agentdeckSideChat.summary
      ? window.agentdeckSideChat.summary('Side chat', text) : text;
    var key = mainDraftKey(form);
    var waiting = read(key);
    write(key, waiting.trim() ? waiting.replace(/\s+$/, '') + '\n\n' + summary : summary);
    setMode(side, 'main', { focus: true });
  }

  document.addEventListener('click', function (event) {
    var side = view();
    if (!side) return;
    var tab = event.target.closest('[data-side-switch] [data-side-mode]');
    if (tab) {
      event.preventDefault();
      setMode(side, tab.dataset.sideMode, { focus: false });
      return;
    }
    if (event.target.closest('[data-side-back]')) {
      event.preventDefault();
      setMode(side, 'main');
      return;
    }
    if (event.target.closest('[data-side-send-back]')) {
      event.preventDefault();
      sendBackSummary(side);
    }
  });

  // A side reply that lands while the chat is shown lights the switch's dot.
  document.body.addEventListener('htmx:sseMessage', function (event) {
    var side = view();
    if (!side || !side.contains(event.target)) return;
    // Only what this message brought counts, not what the log already held.
    var brought = document.createElement('template');
    brought.innerHTML = (event.detail && event.detail.data) || '';
    if (brought.content.querySelector('[data-event-seq]')) {
      reconcile(side, brought.content);
      var empty = side.querySelector('[data-side-view-empty]');
      if (empty) empty.hidden = true;
    }
    if (!sideOn(side) && brought.content.querySelector('.ev.assistant')) {
      var dot = detailOf(side).querySelector('[data-side-dot]');
      if (dot) dot.hidden = false;
    }
    refresh(side);
  });
  document.body.addEventListener('htmx:afterSettle', function (event) {
    var side = view();
    if (side && side.contains(event.target)) refresh(side);
  });

  document.addEventListener('input', function (event) {
    if (event.target.matches('form.inject-form textarea[name="message"]')) grow(event.target);
  });

  // The mode is kept per chat for this tab: coming back to the chat reopens the side.
  function init() {
    var side = view();
    // Every chat's box fits its text, with a side chat or without one.
    var input = box(composer(side));
    if (input) {
      followText(input);
      grow(input);
    }
    if (!side) return;
    if (read(MODE_PREFIX + mainKey(side)) === 'side') setMode(side, 'side');
    else refresh(side);
  }

  window.agentdeckSidePanel = { init: init, setMode: function (mode) {
    var side = view();
    if (side) setMode(side, mode);
  }, mode: function () {
    var side = view();
    return sideOn(side) ? 'side' : 'main';
  } };
  document.addEventListener('agentdeck:session-navigation', init);
  init();
})();
