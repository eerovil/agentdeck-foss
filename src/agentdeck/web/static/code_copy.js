// Copy button on rendered code blocks (#1475). mdToHtml emits every fenced
// block as .md-code > pre + button, on every surface that renders markdown —
// transcript messages, expanded cards, the local .md preview.
// One delegated listener therefore covers blocks that arrive later over SSE
// without any re-binding after a swap.
(function () {
  'use strict';

  var RESET_MS = 1600;
  var resets = new WeakMap();

  function blockText(button) {
    var code = button.parentElement && button.parentElement.querySelector('pre code');
    return code ? code.textContent : '';
  }

  // The deck is reachable over plain http on a LAN address, where the async
  // clipboard API is missing or rejects. execCommand is deprecated but is the
  // only thing that works there, so it is both the fallback and the last resort
  // after a rejected write.
  function copyByExecCommand(text) {
    var area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.top = '0';
    area.style.left = '-9999px';
    document.body.appendChild(area);
    var selection = window.getSelection();
    var previous = selection && selection.rangeCount ? selection.getRangeAt(0) : null;
    area.select();
    var copied = false;
    try { copied = document.execCommand('copy'); } catch (err) { copied = false; }
    area.remove();
    if (previous && selection) {
      selection.removeAllRanges();
      selection.addRange(previous);
    }
    return copied;
  }

  function copy(text) {
    if (window.isSecureContext && navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).then(
        function () { return true; },
        function () { return copyByExecCommand(text); }
      );
    }
    return Promise.resolve(copyByExecCommand(text));
  }

  function announce(button, copied) {
    button.classList.toggle('copied', copied);
    button.classList.toggle('copy-failed', !copied);
    var label = copied ? 'Copied' : 'Copy failed';
    button.setAttribute('aria-label', label);
    button.title = label;
    clearTimeout(resets.get(button));
    resets.set(button, setTimeout(function () {
      button.classList.remove('copied', 'copy-failed');
      button.setAttribute('aria-label', 'Copy code');
      button.title = 'Copy code';
    }, RESET_MS));
  }

  document.addEventListener('click', function (event) {
    var target = event.target;
    var button = target && target.closest && target.closest('.code-copy');
    if (!button) return;
    // A code block sits inside a message that collapses on click, so the copy
    // must not also fold the message it was copied from.
    event.preventDefault();
    event.stopPropagation();
    var text = blockText(button);
    if (!text) return;
    copy(text).then(function (copied) { announce(button, copied); });
  });
})();
