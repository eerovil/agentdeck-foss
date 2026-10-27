// "Also post to issue" (#2416): one message, ticked on purpose, posted to the Work's issue.
//
// The chip is the state: it lights up while ticked, and it sits outside every swapped
// fragment, so a live update never resets it. The tick is used up by the send that carried
// it — cleared after an accepted send even when a new draft typed meanwhile is kept — and
// kept after a refusal, like the text, so pressing Send again still posts.
//
// Everything is delegated off `document`, so a chat opened in-page needs no re-init hook.
(function () {
  'use strict';

  var DISMISSED = 'agentdeck:issue-post-dismissed:';

  function boxOf(form) {
    return form && form.querySelector ? form.querySelector('[data-issue-post-box]') : null;
  }

  function sync(form) {
    var box = boxOf(form);
    if (!box) return;
    var menu = box.closest('[data-issue-post]');
    if (menu) menu.classList.toggle('is-on', box.checked);
    var input = form.querySelector('textarea[name="message"]');
    if (!input) return;
    if (box.checked) {
      if (input.dataset.issuePostBase === undefined) input.dataset.issuePostBase = input.placeholder;
      input.placeholder = box.getAttribute('data-issue-post-placeholder') || input.placeholder;
    } else if (input.dataset.issuePostBase !== undefined) {
      input.placeholder = input.dataset.issuePostBase;
      delete input.dataset.issuePostBase;
    }
  }

  // The Work page renames its box when its target changes; while ticked that name waits
  // underneath the "also posts" one instead of replacing it.
  function setPlaceholder(form, text) {
    var input = form && form.querySelector('textarea[name="message"]');
    if (!input) return;
    if (input.dataset.issuePostBase !== undefined) input.dataset.issuePostBase = text;
    else input.placeholder = text;
  }

  function untick(form) {
    var box = boxOf(form);
    if (!box || !box.checked) return;
    box.checked = false;
    sync(form);
  }

  function hideDismissed(root) {
    var scope = root && root.querySelectorAll ? root : document;
    var notices = scope.matches && scope.matches('#issue-post-notice')
      ? [scope] : scope.querySelectorAll('#issue-post-notice');
    Array.prototype.forEach.call(notices, function (notice) {
      var id = notice.getAttribute('data-issue-post-action');
      try {
        if (id && sessionStorage.getItem(DISMISSED + id)) notice.hidden = true;
      } catch (err) { /* storage unavailable: keep it shown */ }
    });
  }

  document.addEventListener('change', function (event) {
    var box = event.target;
    if (!box || !box.matches || !box.matches('[data-issue-post-box]')) return;
    sync(box.form);
    // The chip shows the choice, so the panel gets out of the way of the transcript.
    var menu = box.closest('[data-issue-post]');
    if (menu) menu.removeAttribute('open');
  });

  document.addEventListener('click', function (event) {
    var button = event.target.closest && event.target.closest('[data-issue-post-dismiss]');
    if (!button) return;
    var notice = button.closest('#issue-post-notice');
    if (!notice) return;
    var id = notice.getAttribute('data-issue-post-action');
    try {
      if (id) sessionStorage.setItem(DISMISSED + id, '1');
    } catch (err) { /* storage unavailable: hide for this page only */ }
    notice.hidden = true;
  });

  // Remember whether this request carried the tick, so the answer clears exactly that one.
  document.addEventListener('htmx:beforeRequest', function (event) {
    var form = event.detail && event.detail.elt;
    var box = boxOf(form);
    if (box) form._issuePostSent = box.checked;
  });

  document.addEventListener('htmx:afterRequest', function (event) {
    var form = event.detail && event.detail.elt;
    if (!boxOf(form) || !form._issuePostSent) return;
    form._issuePostSent = false;
    if (event.detail.successful) untick(form);
  });

  // A post that landed, or went to the Poller, needs no answer: it says so, then steps aside.
  // Anything that went wrong stays until it is dismissed.
  var SETTLED_MS = 6000;

  function stepAsideIfSettled(root) {
    var notice = root && root.id === 'issue-post-notice' ? root
      : root && root.querySelector ? root.querySelector('#issue-post-notice') : null;
    if (!notice || notice.hidden) return;
    if (!notice.classList.contains('is-done') && !notice.classList.contains('is-relayed')) return;
    var id = notice.getAttribute('data-issue-post-action');
    setTimeout(function () {
      var current = document.getElementById('issue-post-notice');
      if (current && current.getAttribute('data-issue-post-action') === id) current.hidden = true;
    }, SETTLED_MS);
  }

  document.addEventListener('htmx:afterSwap', function (event) { hideDismissed(event.target); });
  document.addEventListener('htmx:oobAfterSwap', function (event) {
    hideDismissed(event.target);
    stepAsideIfSettled(event.target);
  });
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { hideDismissed(document); });
  } else {
    hideDismissed(document);
  }

  window.AgentDeckIssuePost = { sync: sync, setPlaceholder: setPlaceholder, untick: untick };
})();
