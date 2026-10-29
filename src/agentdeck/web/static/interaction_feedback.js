(function () {
  'use strict';

  var SEND_RETRY_DELAYS = [800, 1600, 3200];
  var IMAGE_TYPES = /^(?:image\/png|image\/jpeg|image\/webp|image\/gif)$/;

  function timing() { return window.AgentDeckActionTiming; }

  function cloneFormData(source) {
    var copy = new FormData();
    if (source && source.forEach) source.forEach(function (value, key) {
      copy.append(key, value);
    });
    return copy;
  }

  function statusLabel(row, value) {
    var label = row && row.querySelector('.message-state');
    if (label) label.textContent = value;
  }

  function secretName(message) {
    var match = /^\s*\/secret(?:\s|$)/.test(message) &&
      /^\s*\/secret\s+([A-Za-z_][A-Za-z0-9_]*)/.exec(message);
    return match ? match[1] : null;
  }

  function isSecretCommand(message) {
    return /^\s*\/secret(?:\s|$)/.test(message);
  }

  // Whether a send may be kept in memory for a replay. One rule for every send form — the
  // chat composer and the Work page's message box (#2218) — so no copy of a temporary
  // secret outlives its request anywhere: a secret that failed is typed again.
  function retryBufferable(form, record) {
    if (record && record.secret) return false;
    var input = form && form.querySelector('textarea[name="message"]');
    return !(input && isSecretCommand(input.value));
  }
  window.AgentDeckSendRetry = {bufferable: retryBufferable};

  // The line the server sends in place of an empty box (`_attachment_only_text`, #3790).
  function attachmentOnlyText(files) {
    var images = files.every(function (file) { return IMAGE_TYPES.test(file.type); });
    return 'See the attached ' + (images ? 'image' : 'file') + (files.length > 1 ? 's' : '') + '.';
  }

  function optimisticMessage(form, record) {
    var transcript = document.querySelector('.transcript');
    var input = form.querySelector('textarea[name="message"]');
    var fileInput = form.querySelector('input[type="file"][name="images"]');
    var files = Array.from((fileInput && fileInput.files) || []);
    if (!transcript || !input || (!input.value.trim() && !files.length)) return null;
    var row = document.createElement('div');
    row.className = 'ev user pending-message optimistic-message sending';
    row.dataset.pendingMessage = '';
    row.dataset.clientActionId = record.id;

    var head = document.createElement('div');
    head.className = 'ev-head';
    var role = document.createElement('span');
    role.className = 'ev-role';
    role.textContent = 'user';
    var state = document.createElement('span');
    state.className = 'message-state';
    state.textContent = 'Sending';
    head.appendChild(role);
    head.appendChild(state);

    var text = document.createElement('div');
    text.className = 'ev-text';
    var name = secretName(input.value);
    text.textContent = isSecretCommand(input.value)
      ? (name ? 'Temporary secret ' + name + ' submitted securely' :
        'Temporary secret command submitted securely')
      : (input.value.trim() || attachmentOnlyText(files));
    row.appendChild(head);
    row.appendChild(text);
    if (files.length) {
      var attachments = document.createElement('div');
      attachments.className = 'ev-images';
      attachments.setAttribute('aria-label', fileInput.files.length +
        (fileInput.files.length === 1 ? ' attached file' : ' attached files'));
      Array.from(fileInput.files).forEach(function (file, index) {
        if (IMAGE_TYPES.test(file.type)) {
          var image = document.createElement('img');
          var url = URL.createObjectURL(file);
          image.className = 'ev-image';
          image.src = url;
          image.alt = 'Attached image ' + (index + 1);
          image.addEventListener('load', function () { URL.revokeObjectURL(url); }, {once: true});
          image.addEventListener('error', function () { URL.revokeObjectURL(url); }, {once: true});
          attachments.appendChild(image);
        } else {
          var label = document.createElement('span');
          label.className = 'ev-file';
          label.textContent = file.name || 'attachment';
          attachments.appendChild(label);
        }
      });
      row.appendChild(attachments);
    }
    transcript.appendChild(row);
    form._agentdeckOptimisticMessage = row;
    document.body.dispatchEvent(new CustomEvent('agentdeck:optimistic-send'));
    return row;
  }

  function immediateFeedback(form, record, submitter) {
    if (!record || record.marks.acknowledged !== undefined) return;
    if (record.action === 'send' || record.action === 'steer') {
      optimisticMessage(form, record);
    } else if (record.action === 'stop') {
      var stop = submitter || document.querySelector('.stop-button');
      if (stop) {
        stop._agentdeckOriginalText = stop.textContent;
        // Short, so it fits Stop's fixed spot (#3545); the spinner below says the rest.
        stop.textContent = 'Stop…';
        stop.disabled = true;
        form._agentdeckSubmitter = stop;
      }
    } else if (record.action === 'interaction') {
      var note = document.createElement('div');
      note.className = 'interaction-submitting';
      note.setAttribute('role', 'status');
      note.textContent = 'Submitting…';
      form.appendChild(note);
      // Disabling the submit button drops its name/value from the form htmx is
      // about to serialize, which would lose the chosen decision (accept /
      // decline / cancel for approvals; accept for questions). Carry the clicked
      // submitter's value in a hidden field first so htmx still sends it.
      if (submitter && submitter.name) {
        var carrier = document.createElement('input');
        carrier.type = 'hidden';
        carrier.name = submitter.name;
        carrier.value = submitter.value;
        form.appendChild(carrier);
        form._agentdeckDecisionCarrier = carrier;
      }
      form.querySelectorAll('button[type="submit"]').forEach(function (button) {
        button.disabled = true;
      });
      form._agentdeckSubmittingNote = note;
    } else if (record.action === 'new_session') {
      var result = document.querySelector('#new-session-result');
      if (result) {
        result.className = 'inject-result running optimistic-action-status';
        result.textContent = 'Starting chat…';
      }
    }
    timing().mark(record.id, 'acknowledged');
  }

  // The server refuses a send with a sentence meant for the person who typed it.
  // Showing only "Failed" hides it, and #1171 is what that costs: a refused
  // /secret looked identical to a network failure. Secret refusals are written
  // without the value, so this is safe to render verbatim.
  function failureReason(xhr) {
    if (!xhr || !xhr.responseText) return '';
    var detail = '';
    try {
      var parsed = JSON.parse(xhr.responseText);
      if (parsed && typeof parsed.detail === 'string') detail = parsed.detail;
    } catch (error) {
      detail = '';
    }
    return detail.trim();
  }

  function showFailureReason(row, reason) {
    if (!reason) return;
    var note = row.querySelector('.message-error');
    if (!note) {
      note = document.createElement('div');
      note.className = 'message-error';
      row.appendChild(note);
    }
    note.textContent = reason;
  }

  function restoreFailure(form, record, xhr) {
    if (record.action === 'send' || record.action === 'steer') {
      var row = form._agentdeckOptimisticMessage;
      if (row) {
        row.classList.remove('sending');
        row.classList.add('failed');
        // A secret is never kept in the retry buffer or the saved draft, so
        // "retry" would be an empty promise — the value has to be typed again.
        statusLabel(row, record.secret ? 'Failed · re-enter the secret' : 'Failed · retry');
        showFailureReason(row, failureReason(xhr));
      }
    } else if (record.action === 'stop') {
      var stop = form._agentdeckSubmitter;
      if (stop && stop.isConnected) {
        stop.textContent = stop._agentdeckOriginalText || 'Stop';
        stop.disabled = false;
      }
    } else if (record.action === 'interaction') {
      if (form._agentdeckSubmittingNote) form._agentdeckSubmittingNote.remove();
      if (form._agentdeckDecisionCarrier) {
        form._agentdeckDecisionCarrier.remove();
        form._agentdeckDecisionCarrier = null;
      }
      form.querySelectorAll('button[type="submit"]').forEach(function (button) {
        button.disabled = false;
      });
    } else if (record.action === 'new_session') {
      var result = document.querySelector('#new-session-result');
      if (result) {
        result.className = 'inject-result failed optimistic-action-status';
        result.textContent = 'Failed to start chat. Retry.';
      }
    }
  }

  function transientSendFailure(record, xhr) {
    if (record.action !== 'send' && record.action !== 'steer') return false;
    if (!xhr || typeof xhr.status !== 'number') return false;
    // Tailscale Serve answers 502 only when the web process is unavailable, so
    // the request did not reach AgentDeck and is safe to replay. A connection
    // reset or timeout is uncertain — the write may have landed — and must stay
    // manual under the deck's at-most-once delivery rule.
    return xhr.status === 502;
  }

  function scheduleSendRetry(form, record) {
    var attempt = form._agentdeckSendRetryAttempt || 0;
    if (attempt >= SEND_RETRY_DELAYS.length) return false;
    var row = form._agentdeckOptimisticMessage;
    if (!row || !row.isConnected) return false;
    form._agentdeckSendRetryAttempt = attempt + 1;
    form._agentdeckAutoRetryRecord = record;
    row.classList.add('sending');
    row.classList.remove('failed');
    statusLabel(row, 'Auto-retrying…');
    if (!form._agentdeckRetryDisabledButtons) {
      form._agentdeckRetryDisabledButtons = Array.from(
        form.querySelectorAll('button[type="submit"]')
      ).filter(function (button) { return !button.disabled; });
    }
    form._agentdeckRetryDisabledButtons.forEach(function (button) { button.disabled = true; });
    clearTimeout(form._agentdeckSendRetryTimer);
    form._agentdeckSendRetryTimer = setTimeout(function () {
      form._agentdeckSendRetryTimer = null;
      if (!form.isConnected) return;
      // The uncertain first request may have landed even though its HTTP
      // response did not. SSE reconciliation removes the optimistic row; that
      // is success evidence, so cancel the retry and release the composer.
      if (!row.isConnected) {
        clearSendRetry(form);
        return;
      }
      statusLabel(row, 'Retrying…');
      if (!timing().retryForm(form, record, form._agentdeckSendRetryPayload)) {
        clearSendRetry(form);
        restoreFailure(form, record, null);
      }
    }, SEND_RETRY_DELAYS[attempt]);
    return true;
  }

  function clearSendRetry(form) {
    clearTimeout(form._agentdeckSendRetryTimer);
    form._agentdeckSendRetryTimer = null;
    form._agentdeckSendRetryAttempt = 0;
    (form._agentdeckRetryDisabledButtons || []).forEach(function (button) {
      if (button.isConnected) button.disabled = false;
    });
    form._agentdeckRetryDisabledButtons = null;
    form._agentdeckAutoRetryRecord = null;
    form._agentdeckSendRetryPayload = null;
  }

  function accept(form, record, xhr) {
    if (record.action !== 'send' && record.action !== 'steer') return;
    clearSendRetry(form);
    var row = form._agentdeckOptimisticMessage;
    if (!row) return;
    row.classList.remove('sending');
    row.classList.add('accepted');
    var receipt = xhr && xhr.getResponseHeader &&
      xhr.getResponseHeader('X-AgentDeck-Action-State');
    // A send held while the runtime restarts (#2847) is drawn by the held list
    // below the transcript, which the response has just redrawn.
    if (receipt === 'held') {
      row.remove();
      return;
    }
    var queued = receipt === 'queued' ||
      (!receipt && xhr && /inject-result queued/.test(xhr.responseText || ''));
    statusLabel(row, queued ? 'Queued behind active turn' : 'Accepted');
  }

  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (!form.matches('form[data-agentdeck-action]')) return;
    // A click or Enter during backoff must not bypass the stable retry ID with
    // a second user submission. The scheduled replay uses htmx.ajax directly,
    // so it never passes through this submit event.
    if (form._agentdeckAutoRetryRecord) {
      event.preventDefault();
      return;
    }
    var record = timing().prepareForm(form);
    var input = form.querySelector('textarea[name="message"]');
    record.secret = Boolean(input && isSecretCommand(input.value));
    immediateFeedback(form, record, event.submitter);
  }, true);

  document.body.addEventListener('htmx:configRequest', function (event) {
    var form = event.detail && event.detail.elt;
    if (!form || !form.matches || !form.matches('form[data-agentdeck-action]')) return;
    var record = form._agentdeckActionTiming;
    if (!record || (record.action !== 'send' && record.action !== 'steer')) return;
    // A temporary secret is deliberately not copied into the retry buffer. A
    // failed request must be re-entered instead of leaving the value in a
    // long-lived FormData object.
    if (!retryBufferable(form, record)) return;
    if (!form._agentdeckSendRetryPayload) {
      form._agentdeckSendRetryPayload = cloneFormData(event.detail.formData);
    }
  });

  document.body.addEventListener('htmx:confirm', function (event) {
    var form = event.detail && event.detail.elt;
    if (form && form._agentdeckAutoRetryRecord && !form._agentdeckRetryDispatching) {
      event.preventDefault();
    }
  });

  document.body.addEventListener('htmx:afterRequest', function (event) {
    var form = event.detail && event.detail.elt;
    if (!form || !form.matches || !form.matches('form[data-agentdeck-action]')) return;
    var record = form._agentdeckActionTiming;
    if (!record) return;
    if (event.detail.successful) accept(form, record, event.detail.xhr);
    else if (transientSendFailure(record, event.detail.xhr) &&
             scheduleSendRetry(form, record)) return;
    else {
      clearSendRetry(form);
      restoreFailure(form, record, event.detail.xhr);
    }
  });
})();
