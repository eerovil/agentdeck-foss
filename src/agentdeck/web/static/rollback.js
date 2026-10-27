// Roll a chat back to before one of your messages (#3474).
//
// The server renders a hidden ↶ on each of your messages and, when this chat can
// be rolled back, a confirm dialog naming the leg that can. The button shows only
// on that leg's messages after its first one, and only while the agent is not
// working; the server checks all of it again. A rollback puts the removed
// message back into the message box and reloads the chat, which no longer shows
// it or anything after it.
(function () {
  if (window.agentdeckRollback) return;

  var DRAFT_PREFIX = 'agentdeck:message-draft:';
  var chosen = null;

  function dialog() {
    return document.querySelector('[data-rollback-dialog]');
  }

  function working() {
    return !!document.querySelector('#session-status .dot.thinking');
  }

  function refresh() {
    var box = dialog();
    var leg = box ? box.dataset.rollbackLeg : '';
    // The chat's first message has nothing before it to go back to.
    var floor = box ? Number(box.dataset.rollbackFloor || 0) : 0;
    var busy = working();
    document.querySelectorAll('[data-rollback-toggle]').forEach(function (button) {
      button.hidden = !(
        leg &&
        button.dataset.rollbackSessionKey === leg &&
        Number(button.dataset.rollbackSeq) > floor &&
        !busy
      );
    });
  }

  function showError(text) {
    var error = document.querySelector('[data-rollback-error]');
    if (!error) return;
    error.textContent = text || '';
    error.hidden = !text;
  }

  function open(button) {
    var box = dialog();
    if (!box) return;
    chosen = button;
    var event = button.closest('[data-event-id]');
    var anchor = box.querySelector('[data-rollback-anchor]');
    if (anchor && event) anchor.setAttribute('href', '#' + event.id);
    showError('');
    if (!box.open) box.showModal();
  }

  // The removed message goes where the composer keeps unsent text, after any text
  // already waiting there, so the reload brings it back in the box.
  function keepAsDraft(text) {
    var input = document.querySelector('form[data-clear-on-send].inject-form textarea[name="message"]');
    if (!input || !text) return;
    var key = DRAFT_PREFIX + (input.form.getAttribute('hx-post') || location.pathname);
    var waiting = input.value.trim() ? input.value.replace(/\s+$/, '') + '\n\n' : '';
    try {
      sessionStorage.setItem(key, waiting + text);
    } catch (_) {}
  }

  function submit(form) {
    if (!chosen || form.dataset.sending) return;
    var body = new FormData();
    body.append('seq', chosen.dataset.rollbackSeq);
    form.dataset.sending = '1';
    showError('');
    fetch('/sessions/' + chosen.dataset.rollbackSessionKey + '/rollback', {
      method: 'POST',
      body: body,
      headers: { Accept: 'application/json' },
    })
      .then(function (response) {
        return response.json().then(function (answer) {
          if (!response.ok) throw new Error(answer.detail || 'The chat could not be rolled back.');
          return answer;
        }, function () {
          throw new Error('The chat could not be rolled back. Try again.');
        });
      })
      .then(function (answer) {
        keepAsDraft(answer.text || '');
        location.reload();
      })
      .catch(function (error) {
        delete form.dataset.sending;
        showError(error.message);
      });
  }

  document.addEventListener('click', function (event) {
    var button = event.target.closest('[data-rollback-toggle]');
    if (button) {
      event.preventDefault();
      open(button);
      return;
    }
    if (event.target.closest('[data-rollback-cancel]')) {
      var box = dialog();
      if (box && box.open) box.close();
    }
  });

  document.addEventListener('submit', function (event) {
    var form = event.target.closest && event.target.closest('[data-rollback-form]');
    if (!form) return;
    event.preventDefault();
    submit(form);
  });

  document.body.addEventListener('htmx:load', refresh);
  document.body.addEventListener('htmx:afterSettle', refresh);
  // A chat the front page opens in its column is inserted without `htmx:load`.
  document.addEventListener('agentdeck:session-navigation', refresh);
  window.agentdeckRollback = { refresh: refresh };
  refresh();
})();
