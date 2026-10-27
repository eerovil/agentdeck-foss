// The Work-first UI's browser state (#2147).
//
// Two jobs, both about keeping what the reader is doing through a live update:
//
// 1. `keyed-rows` (alias `keyed-works`) swaps a list's rows by identity (#2219, #3228). Each
//    row carries a stable `data-key` and a `data-rev` that changes exactly when it does --
//    Work rows use `data-work-id` and `data-work-rev` -- so a push replaces only the rows
//    that changed and leaves the rest, and any focus in them, alone. A `data-same`
//    placeholder keeps the row the page holds; order follows the push.
// 2. `details[data-keep-open]` remembers being opened. The Work page swaps a whole section
//    when it changes, and the Deckhand panel is replaced on every push; either would shut a
//    disclosure the reader had just opened. The open set lives in sessionStorage, keyed by
//    the attribute's value, and is re-applied to whatever a swap brought in.
// 3. The Work page's message box (#2218). Its text box sits outside the section a live update
//    swaps, so typed text is never touched; this only empties it after a send the server
//    accepted, and keeps its placeholder in step with whether the target is a question.
//    A send whose answer never arrived may still have landed, so it is never replayed
//    blindly: the page offers Retry, which resends the same text under the same action ID
//    (`AgentDeckActionTiming.retryForm`), and the chat's queue keeps one copy. What may be
//    kept for that is the composer's own rule (`AgentDeckSendRetry.bufferable`): a
//    temporary secret is never kept, and has to be typed again.
// 4. A Work action press (#2237) that got no answer may still have been made, so nothing here
//    repeats it: the page says so, and the deck itself refuses a second press of the same
//    action until the first is known to have failed.
// 5. The front page's folds and project filter (#2283, #2311) live in front_filters.js, which
//    loads in the head so neither is painted first and applied after (#3108).
// 6. A part swapped whole (`data-skip-unchanged`, Other activity) skips a push that matches
//    what it already shows (#2739). Every stream the front page opens pushes it at once, and
//    Back to the front page opens one, so an unchanged list was replaced under the reader's
//    pointer: a press whose mouse-down landed on the old link and mouse-up on the new one is
//    no click at all. Whether a `<details>` is open, and how an age is worded, are not compared.
(function () {
  'use strict';

  var KEY = 'agentdeck.openDisclosures';

  function stored() {
    try {
      var parsed = JSON.parse(sessionStorage.getItem(KEY) || '[]');
      return Array.isArray(parsed) ? parsed : [];
    } catch (_) { return []; }
  }

  function remember(name, open) {
    var next = stored().filter(function (item) { return item !== name; });
    if (open) next.push(name);
    try { sessionStorage.setItem(KEY, JSON.stringify(next.slice(-64))); } catch (_) {}
  }

  function restore(root) {
    var open = stored();
    var scope = root && root.querySelectorAll ? root : document;
    var found = Array.prototype.slice.call(scope.querySelectorAll('details[data-keep-open]'));
    if (scope.matches && scope.matches('details[data-keep-open]')) found.push(scope);
    found.forEach(function (details) {
      var wanted = open.indexOf(details.getAttribute('data-keep-open')) !== -1;
      if (details.open !== wanted) details.open = wanted;
    });
  }

  document.addEventListener('toggle', function (e) {
    var details = e.target;
    if (!details || !details.matches || !details.matches('details[data-keep-open]')) return;
    remember(details.getAttribute('data-keep-open'), details.open);
  }, true);

  // `toggle` fires a task after the click, so a swap landing in between would read the old
  // value and undo the press. Record the intent synchronously on the click instead.
  document.addEventListener('click', function (e) {
    var summary = e.target && e.target.closest ? e.target.closest('summary') : null;
    if (!summary || (e.target.closest && e.target.closest('a'))) return;
    var details = summary.parentElement;
    if (details && details.matches('details[data-keep-open]')) {
      remember(details.getAttribute('data-keep-open'), !details.open);
    }
  }, true);

  function cleanup(node) {
    [node].concat(Array.prototype.slice.call(node.querySelectorAll('*'))).forEach(function (el) {
      htmx.trigger(el, 'htmx:beforeCleanupElement');
    });
    node.remove();
  }

  // A row a person is using is not swapped under them (#3228): one with a focused text box,
  // text typed into one, or an open menu. A focused button or link alone does not hold it,
  // because a phone keeps focus on the button just tapped.
  function inUse(el) {
    var focused = document.activeElement;
    if (focused && el.contains(focused) && focused.matches &&
        focused.matches('textarea, input:not([type=button]):not([type=submit]):not([type=checkbox]):not([type=radio]), select, [contenteditable]')) {
      return true;
    }
    if (el.querySelector('[aria-expanded="true"]')) return true;
    if (el.querySelector('[data-wm-dragging]')) return true; // a wayfinder map mid-drag (#3627)
    if (Array.prototype.some.call(el.querySelectorAll('input[type=radio], input[type=checkbox]'),
      function (box) { return box.checked !== box.defaultChecked; })) return true;
    return Array.prototype.some.call(el.querySelectorAll('textarea, input[type=text], input:not([type])'),
      function (box) { return box.value !== box.defaultValue; });
  }

  // The stream the target is swapped from, reopened: a new connection sends every row again.
  // After the swap has settled: mid-swap htmx holds the rows the connect URL names.
  // The Works the page lost are not named on that connect (`unclaimed`), even where another
  // part still shows them: the stream's claims are not per part, so a Work that moved part
  // came back as a placeholder in its new part on every connect, and the page reconnected
  // ten times a second (#3511). A reconnect also waits out the last one by a few seconds.
  var RECONNECT_GAP_MS = 5000;
  function reconnect(target, lostIds) {
    var source = target.closest('[sse-connect]');
    if (!source || !window.htmx || !htmx.reconnectSSE) return;
    var unclaimed = source._agentdeckUnclaimed = source._agentdeckUnclaimed || {};
    lostIds.forEach(function (id) { unclaimed[id] = true; });
    if (source._agentdeckReconnectQueued) return;
    source._agentdeckReconnectQueued = true;
    var wait = Math.max(0, (source._agentdeckReconnectAt || 0) + RECONNECT_GAP_MS - Date.now());
    window.setTimeout(function () {
      source._agentdeckReconnectQueued = false;
      source._agentdeckReconnectAt = Date.now();
      if (source.isConnected) htmx.reconnectSSE(source, true);
    }, wait);
  }

  // What the page already shows, sent with each (re)connect of a Works stream (#3228): one
  // short fingerprint per row, `rowSig` here and `work_view.row_sig` on the server. The
  // stream sends a placeholder for every row it names, so a connect does not send the list
  // again; a row the page lost is not named, so it comes whole.
  function rowSig(id, rev) {
    var hash = 0x811c9dc5;
    var text = id + ' ' + rev;
    for (var i = 0; i < text.length; i++) {
      hash ^= text.charCodeAt(i);
      hash = Math.imul(hash, 0x01000193) >>> 0;
    }
    return ('0000000' + hash.toString(16)).slice(-8);
  }

  function connectUrl(elt, url) {
    if (!/[?&]view=works(-|&|$)/.test(url)) return url;
    var seen = {};
    var sigs = [];
    var skip = elt._agentdeckUnclaimed || {};
    elt._agentdeckUnclaimed = null;
    Array.prototype.forEach.call(elt.querySelectorAll('[data-work-id][data-work-rev]'), function (row) {
      var stream = row.closest('[sse-connect]');
      if (stream && stream !== elt) return;  // another column's own stream
      if (skip[row.getAttribute('data-work-id')]) return;  // lost in one part: sent whole
      var sig = rowSig(row.getAttribute('data-work-id'), row.getAttribute('data-work-rev'));
      if (!seen[sig] && sigs.length < 400) { seen[sig] = true; sigs.push(sig); }
    });
    url = url.replace(/([?&])sig=[^&]*&?/, '$1').replace(/[?&]$/, '');
    if (!sigs.length) return url;
    return url + (url.indexOf('?') === -1 ? '?' : '&') + 'sig=' + sigs.join('.');
  }

  // A keyed row's identity, revision and placeholder mark (#3228): `data-key`, `data-rev` and
  // `data-same` on any keyed list, or the Work rows' own `data-work-id`, `data-work-rev` and
  // `data-work-same`.
  function keyOf(el) { return el.getAttribute('data-key') || el.getAttribute('data-work-id'); }
  function revOf(el) { return el.getAttribute('data-rev') || el.getAttribute('data-work-rev'); }
  function isSame(el) { return el.hasAttribute('data-same') || el.hasAttribute('data-work-same'); }

  // `keyed-rows` is the shared keyed swap (#3228); `keyed-works` is the same swap under the
  // name the Work lists have always used.
  var KEYED_STYLES = {'keyed-rows': true, 'keyed-works': true};

  function defineExtension() {
    htmx.sseConnectUrl = connectUrl;
    var keyed = {
      handleSwap: function (swapStyle, target, fragment) {
        if (!KEYED_STYLES[swapStyle]) return false;
        var focused = document.activeElement;
        if (!focused || !target.contains(focused)) focused = null;
        var mounted = new Map();
        Array.prototype.forEach.call(target.children, function (el) {
          var id = keyOf(el);
          if (id) mounted.set(id, el);
        });
        var desired = [];
        var added = [];
        var retained = 0;
        var lost = [];
        Array.prototype.slice.call(fragment.children).forEach(function (el) {
          var id = keyOf(el);
          var rev = revOf(el);
          var current = id ? mounted.get(id) : null;
          // A placeholder (#3228): the stream already sent this row at this revision. A row
          // held for a person is swapped for the one it is owed once they are done with it.
          if (isSame(el)) {
            var owed = current && current._agentdeckOwed;
            if (owed && revOf(owed) === rev && !inUse(current)) {
              current._agentdeckOwed = null;
              desired.push(owed);
              added.push(owed);
              mounted.delete(id);
              return;
            }
            if (current && (revOf(current) === rev || owed)) {
              retained++;
              desired.push(current);
              mounted.delete(id);
              return;
            }
            lost.push(id);  // nothing here to keep: the page lost the row, so ask for it again
            return;
          }
          if (current && revOf(current) === rev) {
            current._agentdeckOwed = null;
            retained++;
            desired.push(current);
            mounted.delete(id);
            return;
          }
          if (current && inUse(current)) {
            current._agentdeckOwed = el;
            retained++;
            desired.push(current);
            mounted.delete(id);
            return;
          }
          desired.push(el);
          added.push(el);
        });
        Array.prototype.slice.call(target.children).forEach(function (el) {
          if (desired.indexOf(el) === -1) cleanup(el);
        });
        var cursor = target.firstElementChild;
        desired.forEach(function (node) {
          if (node === cursor) cursor = cursor.nextElementSibling;
          else target.insertBefore(node, cursor);
        });
        if (focused && focused.isConnected && document.activeElement !== focused) {
          focused.focus({preventScroll: true});
        }
        added.forEach(restore);
        keepFinishing(target);
        target.dispatchEvent(new CustomEvent('agentdeck:works-reconciled', {
          bubbles: true,
          detail: {retained: retained, added: added.length}
        }));
        if (lost.length) reconnect(target, lost);
        return added;
      }
    };
    htmx.defineExtension('keyed-works', keyed);
    htmx.defineExtension('keyed-rows', keyed);
  }

  // A template's content, as markup, with every disclosure shut and every age blank: the
  // page re-words an age each second from its `datetime` (message_age.js), which is what is
  // compared. Both sides are parsed the same way, so whitespace and quoting compare alike.
  function pushedMarkup(fragment) {
    Array.prototype.forEach.call(fragment.querySelectorAll('details[open]'), function (details) {
      details.removeAttribute('open');
    });
    Array.prototype.forEach.call(fragment.querySelectorAll('time[data-activity-age]'),
      function (age) { age.textContent = ''; });
    var holder = document.createElement('div');
    holder.appendChild(fragment);
    return holder.innerHTML.trim();
  }

  document.addEventListener('htmx:sseBeforeMessage', function (e) {
    var target = e.target;
    var data = e.detail && e.detail.data;
    if (!target.hasAttribute || !target.hasAttribute('data-skip-unchanged')) return;
    if (typeof data !== 'string') return;
    var pushed = document.createElement('template');
    pushed.innerHTML = data;
    var shown = document.createElement('template');
    shown.innerHTML = target.innerHTML;
    if (pushedMarkup(pushed.content) === pushedMarkup(shown.content)) e.preventDefault();
  });

  // A whole section a stream replaces (`data-hold-in-use`, the Work page's, #3228) waits
  // while a person is using it, as a keyed row does: the push is kept and swapped in once
  // they leave it, or replaced by a newer push.
  document.addEventListener('htmx:sseBeforeMessage', function (e) {
    var target = e.target;
    if (!target.hasAttribute || !target.hasAttribute('data-hold-in-use')) return;
    if (!inUse(target)) { target._agentdeckOwedHtml = null; return; }
    target._agentdeckOwedHtml = e.detail && e.detail.data;
    e.preventDefault();
  });

  document.addEventListener('focusout', function (e) {
    var target = e.target && e.target.closest ? e.target.closest('[data-hold-in-use]') : null;
    if (!target || target._agentdeckOwedHtml == null) return;
    window.setTimeout(function () {
      if (target._agentdeckOwedHtml == null || inUse(target) || !window.htmx) return;
      var html = target._agentdeckOwedHtml;
      target._agentdeckOwedHtml = null;
      htmx.swap(target, html, {swapStyle: 'innerHTML'}, {contextElement: target});
    }, 0);
  });

  if (window.htmx) defineExtension();
  else document.addEventListener('DOMContentLoaded', defineExtension);

  document.addEventListener('DOMContentLoaded', function () { restore(document); });
  if (document.readyState !== 'loading') restore(document);
  document.addEventListener('htmx:afterSwap', function (e) { restore(e.target); });

  function syncMessagePlaceholder() {
    var form = document.querySelector('form[data-work-message]');
    var box = form && form.querySelector('textarea[name="message"]');
    if (!box) return;
    var answer = form.querySelector('[data-wd-answer]');
    var text = answer ? 'Answer the question\u2026' : 'Message this Work\u2026';
    // A ticked "Also post to issue" keeps its own placeholder over this one (#2416).
    if (window.AgentDeckIssuePost) window.AgentDeckIssuePost.setPlaceholder(form, text);
    else box.placeholder = text;
  }

  ['htmx:afterSettle', 'htmx:sseMessage'].forEach(function (name) {
    document.addEventListener(name, function (e) {
      if (e.target && e.target.id === 'work-sec-message') syncMessagePlaceholder();
    });
  });

  function copyForm(data) {
    var copy = new FormData();
    if (data && data.forEach) data.forEach(function (value, key) { copy.append(key, value); });
    return copy;
  }

  document.addEventListener('htmx:configRequest', function (e) {
    var form = e.detail && e.detail.elt;
    if (!form || !form.matches || !form.matches('form[data-work-message]')) return;
    if (form._workRetrying) return;
    // What this attempt sent, kept for a Retry of the same action — unless the shared rule
    // says it must not be kept, and then nothing is, not even an earlier send's copy.
    form._workSent = bufferable(form) ? copyForm(e.detail.formData) : null;
  });

  function bufferable(form) {
    var rule = window.AgentDeckSendRetry;
    return Boolean(rule && rule.bufferable(form, form._agentdeckActionTiming));
  }

  function uncertain(xhr) {
    // No answer, or the web process was down or failed mid-send: the message may or may not
    // be with the chat. A 4xx is a definite refusal and has its own words.
    return !xhr || xhr.status === 0 || xhr.status >= 500;
  }

  function offerRetry(form) {
    var result = form.querySelector('#work-message-result');
    var record = form._agentdeckActionTiming;
    var timing = window.AgentDeckActionTiming;
    if (!result || !record || !timing) return;
    result.innerHTML = '';
    var note = document.createElement('p');
    note.className = 'wd-receipt is-refused';
    note.setAttribute('data-wd-receipt', 'uncertain');
    note.textContent = 'No answer from the deck; the message may already be with the chat. ';
    if (!form._workSent) {
      // Nothing was kept to replay (a temporary secret): it has to be entered again.
      note.textContent += 'Enter it again to resend.';
      result.appendChild(note);
      return;
    }
    var retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'wd-message-retry';
    retry.textContent = 'Retry';
    retry.addEventListener('click', function () {
      retry.disabled = true;
      form._workRetrying = true;
      try {
        if (!timing.retryForm(form, record, form._workSent)) retry.disabled = false;
      } finally {
        form._workRetrying = false;
      }
    });
    note.appendChild(retry);
    result.appendChild(note);
  }

  document.addEventListener('htmx:afterRequest', function (e) {
    var form = e.detail && e.detail.elt;
    if (!form || !form.matches || !form.matches('form[data-work-action]')) return;
    // The front page and a Work can sit side by side, each with its own slot (#2290).
    var scope = form.closest('.work-main, .session-sidebar, .session-work-actions') || document;
    var result = scope.querySelector('.work-action-result');
    if (!result || !uncertain(e.detail.xhr)) return;
    result.innerHTML = '';
    var note = document.createElement('p');
    note.className = 'wd-receipt is-uncertain';
    note.setAttribute('data-wd-receipt', 'uncertain');
    note.setAttribute('role', 'status');
    note.textContent = 'No answer from the deck; the action may already have been sent, so it was not repeated.';
    result.appendChild(note);
  });

  // Finish on a front-page row (#3287) takes the row away on the press: the deck records it
  // at once, but the push that moves it to Recently finished waits for a whole rebuild. A
  // refusal or no answer puts it back, since the Work may not have finished. A push that lands
  // before that rebuild still lists the Work where it was, so each swap hides it again (#3299).
  // The hold is per list: it ends for a list once a push of that list leaves the Work out, so a
  // late push of one list is still held after another showed it finished. FINISHING_MS ends it
  // everywhere; past that the deck's lists stand.
  var FINISHING_MS = 120000;
  var finishing = {};

  function finishingRow(form) {
    var action = form.querySelector('input[name="action"]');
    if (!action || action.value !== 'finish') return null;
    return form.closest('article.fr[data-work-id]');
  }

  function groupCount(row, by) {
    var group = row.getAttribute('data-front-in');
    var part = row.parentElement;
    var head = group && part && part.querySelector('[data-front-group="' + group + '"]');
    var count = head && head.querySelector('.fr-group-count');
    var value = count ? parseInt(count.textContent, 10) : NaN;
    if (isNaN(value)) return;
    value = Math.max(0, value + by);
    count.textContent = String(value);
    // The unfiltered count front_filters.js puts back when no project is hidden.
    var all = parseInt(count.getAttribute('data-count-all'), 10);
    if (!isNaN(all)) count.setAttribute('data-count-all', String(Math.max(0, all + by)));
    head.hidden = value === 0;
  }

  // The keyed list a row sits in: the swap target, whose children are the rows.
  function listOf(row) { return row.parentElement; }

  function hideFinishing(row) {
    row.hidden = true;
    row.setAttribute('data-work-finishing', '');
    groupCount(row, -1);
  }

  // After a keyed swap of `target`: its group headers are the push's own, counting the Work
  // again, so every row still held back is taken off its fresh count once more. One held past
  // FINISHING_MS is shown again as the push has it, already in its fresh count.
  function keepFinishing(target) {
    var now = Date.now();
    Object.keys(finishing).forEach(function (id) {
      var hold = finishing[id];
      if (hold.until < now) { delete finishing[id]; return; }
      if (hold.lists.indexOf(target) === -1) return;
      if (rowsOf(target, id).length) return;
      hold.lists.splice(hold.lists.indexOf(target), 1);  // this list has let it go
      if (!hold.lists.length) delete finishing[id];
    });
    Array.prototype.forEach.call(target.querySelectorAll('article.fr[data-work-id]'), function (row) {
      var hold = finishing[row.getAttribute('data-work-id')];
      if (hold && hold.lists.indexOf(target) !== -1) {
        hideFinishing(row);
      } else if (row.hasAttribute('data-work-finishing')) {
        row.removeAttribute('data-work-finishing');
        row.hidden = false;
      }
    });
  }

  // Every row of the Work on the page but a finished one: a Recently stopped row lists it too.
  function rowsOf(scope, id) {
    return Array.prototype.filter.call(scope.querySelectorAll('article.fr[data-work-id]'), function (row) {
      return row.getAttribute('data-work-id') === id &&
        row.getAttribute('data-front-section') !== 'finished';
    });
  }

  document.addEventListener('htmx:beforeRequest', function (e) {
    var form = e.detail && e.detail.elt;
    if (!form || !form.matches || !form.matches('form[data-work-action]')) return;
    var row = finishingRow(form);
    if (!row || row.hidden) return;
    var id = row.getAttribute('data-work-id');
    var hold = finishing[id] = {until: Date.now() + FINISHING_MS, lists: []};
    rowsOf(document, id).forEach(function (each) {
      if (hold.lists.indexOf(listOf(each)) === -1) hold.lists.push(listOf(each));
      if (!each.hidden) hideFinishing(each);
    });
    // The deadline holds with no push at all, as when the stream has gone quiet.
    window.setTimeout(function () { if (finishing[id] === hold) release(id); }, FINISHING_MS);
  });

  // Shows every row of the Work held back again, back on its group's count.
  function release(id) {
    delete finishing[id];
    rowsOf(document, id).forEach(function (each) {
      if (!each.hasAttribute('data-work-finishing')) return;
      each.removeAttribute('data-work-finishing');
      each.hidden = false;
      groupCount(each, 1);
    });
  }

  // The pressed row may have been swapped for a newer copy meanwhile, so it goes by the Work.
  document.addEventListener('htmx:afterRequest', function (e) {
    var form = e.detail && e.detail.elt;
    if (!form || !form.matches || !form.matches('form[data-work-action]')) return;
    var row = finishingRow(form);
    if (!row || e.detail.successful) return;
    var id = row.getAttribute('data-work-id');
    if (finishing[id]) release(id);
  });

  // Rename a Work (#2328): the title's ✎ shows the form that sits outside the swapped
  // sections, and a saved or cleared name shuts it again; the new title arrives by the stream.
  function renameForm(el) {
    var scope = el.closest('.work-main') || document;
    return scope.querySelector('form[data-work-rename]');
  }

  document.addEventListener('click', function (e) {
    var open = e.target.closest && e.target.closest('[data-work-rename-open]');
    var cancel = e.target.closest && e.target.closest('[data-work-rename-cancel]');
    var form = open ? renameForm(open) : (cancel ? cancel.closest('form[data-work-rename]') : null);
    if (!form) return;
    e.preventDefault();
    form.hidden = cancel ? true : !form.hidden;
    if (!form.hidden) {
      var input = form.querySelector('input[name="title"]');
      if (input) { input.focus(); input.select(); }
    }
  });

  document.addEventListener('htmx:afterRequest', function (e) {
    var form = e.detail && e.detail.elt;
    if (!form || !form.matches || !form.matches('form[data-work-rename]') || !e.detail.successful) return;
    var submitter = e.detail.requestConfig && e.detail.requestConfig.triggeringEvent &&
      e.detail.requestConfig.triggeringEvent.submitter;
    if (submitter && submitter.value === 'clear') {
      var input = form.querySelector('input[name="title"]');
      if (input) input.value = '';
    }
    form.hidden = true;
  });

  // New chat (#2388). The action ID is minted when the box opens, not per click, so a double
  // press or a press after no answer is the same press, which the deck answers with the chat
  // it already started. Only a definite answer (a start, or a refusal) earns a fresh ID.
  function newChatId() {
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID().replace(/-/g, '');
    return (Date.now().toString(36) + Math.random().toString(36).slice(2) + '00000000').slice(0, 24);
  }

  function newChatForm(el) {
    var scope = el.closest('.work-main') || document;
    return scope.querySelector('form[data-work-new-chat]');
  }

  function freshNewChatId(form) {
    var input = form.querySelector('input[name="client_action_id"]');
    if (input) input.value = newChatId();
  }

  function showNewChat(form, open) {
    form.hidden = !open;
    var opener = (form.closest('.work-main') || document).querySelector('[data-work-new-chat-open]');
    if (opener) opener.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (!open) return;
    var input = form.querySelector('input[name="client_action_id"]');
    if (input && !input.value) freshNewChatId(form);
    var box = form.querySelector('textarea[name="message"]');
    if (box) box.focus();
  }

  document.addEventListener('click', function (e) {
    var open = e.target.closest && e.target.closest('[data-work-new-chat-open]');
    var cancel = e.target.closest && e.target.closest('[data-work-new-chat-cancel]');
    var form = open ? newChatForm(open) : (cancel ? cancel.closest('form[data-work-new-chat]') : null);
    if (!form) return;
    e.preventDefault();
    showNewChat(form, cancel ? false : form.hidden);
  });

  document.addEventListener('change', function (e) {
    var radio = e.target;
    if (!radio.matches || !radio.matches('form[data-work-new-chat] input[name="account_key"]')) return;
    var shown = radio.form.querySelector('[data-work-new-chat-account]');
    var label = radio.closest('label');
    if (shown && label) shown.textContent = label.textContent.trim();
  });

  // The start strip settles in the slot: a real chat clears the first message; a start
  // refused after it was accepted opens the box again with the text still in it.
  document.addEventListener('htmx:afterSettle', function (e) {
    var slot = e.target && e.target.closest && e.target.closest('#work-new-chat-slot');
    var strip = slot && slot.querySelector('[data-starting-state]');
    var form = slot && newChatForm(slot);
    if (!strip || !form) return;
    var state = strip.getAttribute('data-starting-state');
    if (state === 'ready') {
      // Only the message that started this chat: a new one typed meanwhile stays.
      var box = form.querySelector('textarea[name="message"]');
      if (box && form._workNewChatSent !== undefined && box.value === form._workNewChatSent) {
        box.value = '';
      }
      form._workNewChatSent = undefined;
    } else if (state === 'failed') {
      showNewChat(form, true);
    }
  });

  document.addEventListener('htmx:afterRequest', function (e) {
    var form = e.detail && e.detail.elt;
    if (!form || !form.matches || !form.matches('form[data-work-new-chat]')) return;
    var xhr = e.detail.xhr;
    if (uncertain(xhr)) {
      var slot = document.getElementById('work-new-chat-slot');
      if (slot) {
        slot.innerHTML = '';
        var note = document.createElement('p');
        note.className = 'wd-receipt is-uncertain';
        note.setAttribute('data-wd-receipt', 'uncertain');
        note.setAttribute('role', 'status');
        note.textContent = 'No answer from the deck; the chat may have started. Press Start again to ' +
          'find out: it will not start a second one.';
        slot.appendChild(note);
      }
      return;
    }
    if (xhr.getResponseHeader('X-AgentDeck-New-Chat') === 'started') {
      // This press has its chat, starting or started: the next press is a new chat. The box
      // shuts but keeps the text until the strip says a chat really exists (#559).
      var sent = form.querySelector('textarea[name="message"]');
      form._workNewChatSent = sent ? sent.value : undefined;
      freshNewChatId(form);
      showNewChat(form, false);
    } else if (xhr.status >= 400) {
      // Refused: nothing started, so trying again is a new, deliberate press.
      freshNewChatId(form);
    }
    // A 202 without a start is the deck saying the start may have begun: keep the ID.
  });

  document.addEventListener('htmx:afterRequest', function (e) {
    var form = e.detail && e.detail.elt;
    if (!form || !form.matches || !form.matches('form[data-work-message]')) return;
    if (e.detail.successful) {
      var box = form.querySelector('textarea[name="message"]');
      if (box) box.value = '';
      form._workSent = null;
    } else if (uncertain(e.detail.xhr)) {
      offerRetry(form);
    }
  });
})();
