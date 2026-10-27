// Settle the composer once a start has really become a chat (#1799).
//
// Pressing Start no longer navigates anywhere: the spawn runs in the background,
// the chat arrives in the chat list working, and all that stays behind is the
// status strip in the composer's own result slot. This releases that composer
// when the strip reports `ready` — the saved draft goes, and the textarea it was
// typed into is emptied so the next chat starts from a blank one.
//
// Only on `ready`, and only while the text is still exactly this start's. A
// refused start never reaches `ready`, so its first message stays put for a
// one-press retry (#559) — and text typed after this start began belongs to the
// next chat, not to this one.
(function () {
  'use strict';

  var DRAFT_PREFIX = 'agentdeck:message-draft:';

  function clearComposer(key, text) {
    // The draft key is the form's own hx-post path, which is what makes it the
    // one composer this start was typed into.
    if (key.indexOf(DRAFT_PREFIX) !== 0) return;
    var action = key.slice(DRAFT_PREFIX.length);
    var form = document.querySelector('form[hx-post="' + action + '"]');
    var input = form && form.querySelector('textarea[name="message"]');
    if (input && input.value === text) input.value = '';
  }

  function settle(root) {
    if (!root || !root.querySelector) return;
    var element = root.matches && root.matches('[data-starting-state]')
      ? root
      : root.querySelector('[data-starting-state]');
    if (!element || element.getAttribute('data-starting-state') !== 'ready') return;
    var key = element.getAttribute('data-draft-key');
    if (!key) return;
    var text = element.getAttribute('data-draft-text');
    try {
      if (sessionStorage.getItem(key) === text) sessionStorage.removeItem(key);
    } catch (_) {}
    clearComposer(key, text);
  }

  document.body.addEventListener('htmx:load', function (event) {
    settle((event.detail && event.detail.elt) || event.target);
  });
  settle(document);
})();
