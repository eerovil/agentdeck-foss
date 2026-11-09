// Ask before a message wakes a costly chat whose cache has gone (#4169).
//
// The server renders the bar hidden, with `data-ask` when it should ask now. This shows
// it in the composer's place until the person answers. "Wake it" gives the composer
// back as it was, and is remembered in this browser until the chat's cache changes,
// which is when the chat has had a new turn. "New chat" turns the composer into the
// first message of a new chat, the next part of the same one, started on the old
// chat's account through Continue, with the effort picked in the box; the old chat stays
// asleep. It posts to no issue, so that choice hides. Continue takes text only,
// so an attached file stops the start until it is removed, and once the new chat runs the
// box closes, so a second Send cannot start another chat or wake this one.
//
// It asks again when the person comes back to the tab with an empty message box, by one
// read of `/sessions/<key>/cold-chat`. Nothing here runs on a timer. A chat opened in place
// (a phone's front-page row, a chat jump) asks as a loaded page does.
(function () {
  'use strict';

  var WOKEN_PREFIX = 'agentdeck:cold-chat-woken:';
  var DRAFT_PREFIX = 'agentdeck:message-draft:';

  function bar() { return document.querySelector('[data-cold-chat]'); }
  function composer() { return document.querySelector('form.inject-form'); }
  function box(form) { return form && form.querySelector('textarea[name="message"]'); }

  function woken(el) {
    try {
      return localStorage.getItem(WOKEN_PREFIX + el.dataset.sessionKey) === el.dataset.stamp;
    } catch (_) { return false; }
  }

  function show(el, asking) {
    var form = composer();
    if (!form) return;
    el.hidden = !asking;
    form.hidden = asking;
  }

  function ask(el) {
    if (!el.hasAttribute('data-ask') || woken(el)) return;
    var form = composer();
    if (!form || form.hasAttribute('data-cold-new-chat') || form.hasAttribute('data-cold-started')) return;
    show(el, true);
  }

  function useComposer(el, isNew) {
    var form = composer();
    if (!form) return;
    show(el, false);
    var label = form.querySelector('[data-cold-label]');
    var input = box(form);
    if (label) {
      if (!label.dataset.coldText) label.dataset.coldText = label.textContent;
      label.textContent = isNew ? 'This starts a new chat' : label.dataset.coldText;
    }
    if (input) {
      if (!input.dataset.coldPlaceholder) input.dataset.coldPlaceholder = input.placeholder;
      input.placeholder = isNew ? 'What should the new chat do?' : input.dataset.coldPlaceholder;
      input.focus();
    }
    // The new chat starts on the new-chat default, not this chat's effort, so the blank
    // choice says so while the box starts one.
    var keep = form.querySelector('select[name="effort"] option[value=""]');
    if (keep) {
      if (!keep.dataset.coldText) keep.dataset.coldText = keep.textContent;
      keep.textContent = isNew ? 'New-chat default' : keep.dataset.coldText;
    }
    // A new chat has no issue to post to, so the box is unticked before it hides.
    var post = form.querySelector('[data-issue-post-box]');
    if (isNew && post && post.checked) {
      post.checked = false;
      post.dispatchEvent(new Event('change', {bubbles: true}));
      if (input) input.placeholder = 'What should the new chat do?';
    }
    form.toggleAttribute('data-cold-new-chat', isNew);
  }

  document.addEventListener('click', function (event) {
    var el = bar();
    if (!el || !event.target.closest) return;
    if (event.target.closest('[data-cold-wake]')) {
      try { localStorage.setItem(WOKEN_PREFIX + el.dataset.sessionKey, el.dataset.stamp); } catch (_) {}
      useComposer(el, false);
    } else if (event.target.closest('[data-cold-new]')) {
      useComposer(el, true);
    }
  });

  function refuse(form, why) {
    var slot = form.querySelector('#inject-result');
    if (!slot) return;
    slot.className = 'inject-result failed has-detail';
    slot.setAttribute('role', 'alert');
    slot.textContent = '';
    var mark = document.createElement('span');
    mark.className = 'send-error';
    mark.setAttribute('aria-hidden', 'true');
    mark.textContent = '!';
    var words = document.createElement('span');
    words.textContent = why;
    slot.appendChild(mark);
    slot.appendChild(words);
  }

  // Once the new chat has started, this box has done its job: a second Send would
  // start yet another chat, so the box closes down to its start line, which links to it.
  function done(form) {
    form.removeAttribute('data-cold-new-chat');
    form.setAttribute('data-cold-started', '');
    var input = box(form);
    if (input) input.disabled = true;
    form.querySelectorAll('button[type="submit"], button:not([type])').forEach(function (button) {
      button.disabled = true;
    });
  }

  // Capture on the document runs before htmx's own submit listener on the form.
  document.addEventListener('submit', function (event) {
    var form = event.target;
    var el = bar();
    if (!el || form !== composer()) return;
    if (form.hasAttribute('data-cold-started')) {
      // The box is closed: nothing it sends may wake the old chat.
      event.preventDefault();
      event.stopImmediatePropagation();
      return;
    }
    if (!form.hasAttribute('data-cold-new-chat')) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (!window.htmx) return;
    // Continue starts a new chat from text only, so an attached file is never dropped
    // silently: the start waits until the person removes it.
    var files = form.querySelector('input[type="file"][name="images"]');
    if (files && files.files && files.files.length) {
      refuse(form, 'A new chat starts from text only. Remove the attached files (×) and send again.');
      return;
    }
    var input = box(form);
    var text = input ? input.value : '';
    form._coldChatSent = text;
    var values = {
      mode: 'transcript',
      account_key: el.dataset.accountKey,
      message: text,
      result_id: 'inject-result'
    };
    // The chosen effort carries to the new chat; "Keep current" sends nothing.
    var effort = form.querySelector('select[name="effort"]');
    if (effort && effort.value) values.effort = effort.value;
    window.htmx.ajax('POST', '/sessions/' + el.dataset.sessionKey + '/continue', {
      target: '#inject-result',
      swap: 'outerHTML',
      values: values
    });
  }, true);

  // The typed message goes only once the new chat exists, so a refused start keeps it.
  document.body.addEventListener('htmx:load', function (event) {
    var root = (event.detail && event.detail.elt) || event.target;
    var form = composer();
    if (!form || form._coldChatSent === undefined || !root || !root.matches) return;
    if (!root.matches('#inject-result[data-starting-state="ready"]')) return;
    var input = box(form);
    var sent = form._coldChatSent;
    delete form._coldChatSent;
    if (input && input.value === sent) input.value = '';
    try {
      var key = DRAFT_PREFIX + form.getAttribute('hx-post');
      if (sessionStorage.getItem(key) === sent) sessionStorage.removeItem(key);
    } catch (_) {}
    done(form);
  });

  document.addEventListener('visibilitychange', function () {
    var el = bar();
    var form = composer();
    var input = box(form);
    if (document.hidden || !el || !form || form.hasAttribute('data-cold-new-chat')) return;
    if (form.hasAttribute('data-cold-started')) return;
    if (input && input.value) return;
    fetch('/sessions/' + el.dataset.sessionKey + '/cold-chat', {headers: {Accept: 'application/json'}})
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (found) {
        if (!found || bar() !== el || (input && input.value)) return;
        if (!found.ask) {
          el.removeAttribute('data-ask');
          if (!el.hidden) show(el, false);
          return;
        }
        el.setAttribute('data-ask', '');
        el.dataset.stamp = found.stamp;
        var cost = el.querySelector('[data-cold-cost]');
        if (cost) cost.textContent = found.cost + ' %';
        ask(el);
      })
      .catch(function () {});
  });

  function settle(root) {
    var el = root && root.querySelector ? root.querySelector('[data-cold-chat]') : null;
    if (el) ask(el);
  }
  document.body.addEventListener('htmx:load', function (event) {
    settle((event.detail && event.detail.elt) || event.target);
  });
  // A chat opened in place, as a phone opens a front-page row's chat, swaps the page
  // without an htmx:load: its bar asks then too.
  document.addEventListener('agentdeck:session-navigation', function () { settle(document); });
  settle(document);
})();
