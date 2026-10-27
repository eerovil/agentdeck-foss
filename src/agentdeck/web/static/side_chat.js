// Side chats (#2485): start new work from one finished reply (#3171) — a side
// chat cut there files it as an issue — and send a side chat's conclusion back
// to the chat it came from.
//
// The server renders a hidden ＋ on every assistant reply and a hidden ↩ beside
// it. Whether a reply ends a finished turn depends on what came after it, and the
// transcript grows a line at a time over SSE, so the decision is made here on the
// rendered page and made again whenever anything is swapped in.
(function () {
  if (window.agentdeckSideChat) return;

  var SEND_BACK_PREFIX = 'Summary from side chat';

  function forkForm() {
    return document.querySelector('[data-fork-form]');
  }

  function forkLegs(form) {
    try {
      return JSON.parse((form && form.dataset.forkLegs) || '[]');
    } catch (_) {
      return [];
    }
  }

  function working() {
    return !!document.querySelector('#session-status .dot.thinking');
  }

  // A reply ends a finished turn when the next message after it is yours, or
  // when nothing follows it and the chat is not working. Tool rows carry no
  // `data-event-seq`, so they never decide; an answer to the agent's question
  // belongs to the open turn, so it does not end it. The server checks the cut
  // again against the transcript itself, so a guess here can only show a button
  // whose press is refused with the reason, never cut in the wrong place.
  function endsTurn(events, index) {
    for (var next = index + 1; next < events.length; next += 1) {
      var event = events[next];
      if (event.classList.contains('assistant')) return false;
      if (event.classList.contains('user')) {
        if (event.querySelector('.ev-answer')) continue;
        return true;
      }
    }
    return !working();
  }

  function refresh() {
    var form = forkForm();
    var legs = forkLegs(form);
    var events = Array.prototype.slice.call(
      document.querySelectorAll('.session-detail [data-event-seq]')
    );
    var sideChat = !!document.querySelector('[data-side-chat-origin]');
    events.forEach(function (event, index) {
      var fork = event.querySelector('[data-fork-toggle]');
      if (fork) {
        fork.hidden = !(
          form &&
          legs.indexOf(fork.dataset.forkSessionKey) !== -1 &&
          endsTurn(events, index)
        );
      }
      var back = event.querySelector('[data-send-back]');
      if (back) back.hidden = !sideChat;
    });
  }

  function forkDialog() {
    return document.querySelector('[data-fork-dialog]');
  }

  function forkError(text) {
    var error = document.querySelector('[data-fork-error]');
    if (!error) return;
    error.textContent = text || '';
    error.hidden = !text;
  }

  function openFork(button) {
    var form = forkForm();
    var dialog = forkDialog();
    if (!form || !dialog) return;
    var path = '/sessions/' + button.dataset.forkSessionKey + '/fork';
    if (form.getAttribute('hx-post') !== path) {
      form.setAttribute('hx-post', path);
      if (window.htmx && htmx.process) htmx.process(form);
    }
    form.querySelector('[data-fork-seq-input]').value = button.dataset.forkSeq;
    var event = button.closest('[data-event-id]');
    var anchor = form.querySelector('[data-fork-anchor]');
    if (anchor && event) anchor.setAttribute('href', '#' + event.id);
    forkError('');
    if (!dialog.open) dialog.showModal();
    var input = form.querySelector('textarea[name="message"]');
    if (input) input.focus({ preventScroll: true });
  }

  // The start's outcome lands in the slot behind the modal: an accepted start
  // closes the modal so the chat shows it, and a refusal is repeated inside the
  // modal, where the person still is, with their words put back — the box
  // empties as the request leaves, and an HTML refusal counts as sent there.
  var sentText = '';
  document.body.addEventListener('htmx:beforeRequest', function (event) {
    var form = forkForm();
    if (!form || event.detail.elt !== form) return;
    var input = form.querySelector('textarea[name="message"]');
    sentText = input ? input.value : '';
  });
  document.body.addEventListener('htmx:afterRequest', function (event) {
    var form = forkForm();
    if (!form || event.detail.elt !== form) return;
    // From the status itself: refusal_swap.js turns an HTML refusal into a normal
    // swap, which htmx then reports as successful.
    var status = event.detail.xhr ? event.detail.xhr.status : 0;
    if (status >= 200 && status < 300) {
      forkError('');
      var dialog = forkDialog();
      if (dialog && dialog.open) dialog.close();
      return;
    }
    var input = form.querySelector('textarea[name="message"]');
    if (input && !input.value && sentText) {
      input.value = sentText;
      input.dispatchEvent(new Event('input', { bubbles: true }));
    }
    var slot = document.getElementById('fork-session-result');
    var text = slot ? slot.textContent.trim() : '';
    if (slot) slot.textContent = '';
    forkError(text || 'The work could not be started. Try again.');
  });

  function messageText(event) {
    var text = event && event.querySelector('.ev-text');
    return text ? text.innerText.trim() : '';
  }

  function sendBack(text) {
    var origin = document.querySelector('[data-side-chat-origin]');
    if (!origin || !origin.dataset.targetUrl || !text) return;
    var status = origin.querySelector('[data-send-back-status]');
    var title = origin.dataset.sideChatTitle || 'this side chat';
    var summary = SEND_BACK_PREFIX + ' “' + title + '” (for information):\n\n' + text;
    // Asked now rather than when this page loaded: the original may have moved on
    // to a new leg since, and only that leg's composer restores the draft.
    fetch(origin.dataset.targetUrl, { headers: { Accept: 'application/json' }, cache: 'no-store' })
      .then(function (response) {
        if (!response.ok) throw new Error('unreachable');
        return response.json();
      })
      .then(function (target) {
        // Text already waiting in that composer is the person's own; keep it and
        // add the summary after it rather than replacing it.
        var waiting = sessionStorage.getItem(target.draft_key) || '';
        var draft = waiting.trim() ? waiting.replace(/\s+$/, '') + '\n\n' + summary : summary;
        sessionStorage.setItem(target.draft_key, draft);
        window.location.assign(target.url);
      })
      .catch(function () {
        if (status) status.textContent = 'The original chat cannot take messages right now.';
      });
  }

  document.addEventListener('click', function (event) {
    var fork = event.target.closest('[data-fork-toggle]');
    if (fork) {
      event.preventDefault();
      openFork(fork);
      return;
    }
    if (event.target.closest('[data-fork-cancel]')) {
      var dialog = forkDialog();
      if (dialog && dialog.open) dialog.close();
      return;
    }
    var back = event.target.closest('[data-send-back]');
    if (back) {
      event.preventDefault();
      sendBack(messageText(back.closest('[data-event-id]')));
      return;
    }
    if (event.target.closest('[data-send-back-latest]')) {
      event.preventDefault();
      var replies = document.querySelectorAll('.session-detail .ev.assistant[data-event-seq]');
      sendBack(messageText(replies[replies.length - 1]));
    }
  });

  document.body.addEventListener('htmx:load', refresh);
  document.body.addEventListener('htmx:afterSettle', refresh);
  // A chat the front page opens in its column is inserted without `htmx:load`.
  document.addEventListener('agentdeck:session-navigation', refresh);
  window.agentdeckSideChat = { refresh: refresh };
  refresh();
})();
