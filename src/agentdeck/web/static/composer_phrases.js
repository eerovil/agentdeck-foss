// One-tap steering phrases under the chat composer (#734).
//
// The chip's whole point is that the phrase costs one tap, so an empty
// composer sends immediately. A non-empty composer is someone mid-sentence:
// the phrase is appended to what they wrote and nothing is sent, because the
// deck's standing rule is that no background or incidental action may destroy
// unsent text.
//
// Everything is delegated off `document`, so a chat opened in-page through
// session_navigation.js needs no re-init hook.
(function () {
  'use strict';

  // Every composer menu that opens over the transcript shares this dismissal, so two are
  // never open at once and a tap elsewhere closes them (#2416 added "Also post to issue").
  function panelOf(node) {
    return node && node.closest ? node.closest('[data-composer-menu]') : null;
  }

  function closeAll(except) {
    document.querySelectorAll('[data-composer-menu][open]').forEach(function (menu) {
      if (menu !== except) menu.removeAttribute('open');
    });
  }

  function apply(chip) {
    var form = chip.closest('form');
    var input = form && form.querySelector('textarea[name="message"]');
    var text = chip.getAttribute('data-quick-phrase') || '';
    if (!form || !input || !text) return;
    closeAll();

    var draft = input.value;
    if (!draft.trim()) {
      // A one-tap phrase is steering, never something to post to the issue (#2416).
      if (window.AgentDeckIssuePost) window.AgentDeckIssuePost.untick(form);
      input.value = text;
      // Draft persistence and the autocomplete both listen for `input`; a
      // programmatic value assignment fires nothing on its own.
      input.dispatchEvent(new Event('input', { bubbles: true }));
      form.requestSubmit();
      return;
    }
    input.value = draft + (/\s$/.test(draft) ? '' : ' ') + text;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  }

  document.addEventListener('click', function (event) {
    var chip = event.target.closest && event.target.closest('[data-quick-phrase]');
    if (chip) {
      event.preventDefault();
      apply(chip);
      return;
    }
    // A tap anywhere else dismisses the panel, including on the summary of a
    // second menu — <details> would otherwise leave both open at once.
    closeAll(panelOf(event.target));
  });

  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Escape') return;
    if (document.querySelector('[data-composer-menu][open]')) closeAll();
  });
})();
