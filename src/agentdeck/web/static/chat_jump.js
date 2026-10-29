// The deck chat's jump to another chat (#3816, `chat_jump.py`).
//
// Only a page showing the deck chat receives one, as a `chat_jump` event in the chat's
// hidden slot, and opens that chat in place as a click on its card would, so voice mode
// carries on there (#3548). With voice mode on, it waits until the deck chat's reply has
// been read (`AgentDeckVoiceJump` in voice_mode.js). The way back, said in that chat,
// is voice mode's own: the fast agent spots it and calls `AgentDeckChatJump.go`.
(function () {
  'use strict';

  var last = '';

  // Only this deck's own pages: a path, never another origin.
  function go(path) {
    if (typeof path !== 'string' || path.charAt(0) !== '/' || path.charAt(1) === '/') return;
    var nav = window.AgentDeckSessionNavigation;
    if (nav && nav.open) nav.open(path);
    else location.assign(path);
  }

  document.addEventListener('htmx:sseMessage', function (e) {
    var slot = e.target;
    if (!slot || slot.id !== 'chat-jump-slot') return;
    var el = slot.querySelector('[data-chat-jump]');
    var jump;
    try { jump = el ? JSON.parse(el.getAttribute('data-chat-jump')) : null; } catch (_) { jump = null; }
    if (!jump || !jump.id || jump.id === last) return;
    last = jump.id;
    var voice = window.AgentDeckVoiceJump;
    if (voice && voice(jump, function () { go(jump.path); })) return;
    go(jump.path);
  });

  window.AgentDeckChatJump = { go: go };
})();
