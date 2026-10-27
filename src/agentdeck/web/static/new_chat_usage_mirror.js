// Keep the New chat account tiles' meters agreeing with the sticky usage header.
//
// Both numbers come from the same `_usage_rows`, but only the header is live:
// `#limit-bars` is swapped on every SSE `usage` push, while the New chat control
// is rendered once with the page and deliberately never swapped — it holds a
// selected account, an unsent first message and the focus that goes with them
// (#1912). Left alone the tiles freeze at whatever was true when the page loaded,
// so a phone left open across a 5h or weekly reset showed 92% in a tile beside
// the header's 9% (#2045).
//
// So mirror rather than re-render: after each header swap, copy each account's
// percentages and their warn/crit colouring across, matched by account key and
// window kind. Only text and two class names change; no element in the control is
// replaced, moved or re-created, so nothing can be swapped out from under a tap.
(function () {
  function escapeAttr(value) {
    if (window.CSS && CSS.escape) return CSS.escape(value);
    // Old WebKit has no CSS.escape. Account keys are provider-qualified and can
    // carry a colon, which a selector would otherwise read as a pseudo-class.
    return value.replace(/["\\]/g, '\\$&');
  }

  function mirror() {
    var header = document.getElementById('limit-bars');
    if (!header) return;
    var tiles = document.querySelectorAll('.account-choice-usage[data-account-key]');
    Array.prototype.forEach.call(tiles, function (tile) {
      var row = header.querySelector(
        '.mini-acct[data-account-key="' + escapeAttr(tile.dataset.accountKey) + '"]'
      );
      // An account the header is not showing (or a header that has not arrived
      // yet) leaves the tile exactly as the server rendered it.
      if (!row) return;
      // Matched on the window's own visible label ("5h", "7d", "mo") rather than
      // on a marker attribute: the header row's markup is read as a literal
      // string by several tests, and a provider's window set is small and
      // stable, so the label is the identity that is already there.
      var windows = {};
      Array.prototype.forEach.call(row.querySelectorAll('.mini-window'), function (source) {
        var kind = source.querySelector('.mini-window-kind');
        if (kind) windows[kind.textContent.trim()] = source.querySelector('.mini-pct');
      });
      var cells = tile.querySelectorAll('.account-choice-window[data-window-kind]');
      Array.prototype.forEach.call(cells, function (cell) {
        var source = windows[cell.dataset.windowKind];
        var target = cell.querySelector('b');
        if (!source || !target) return;
        if (target.textContent !== source.textContent) {
          target.textContent = source.textContent;
        }
        target.classList.toggle('crit', source.classList.contains('crit'));
        target.classList.toggle('warn', source.classList.contains('warn'));
      });
    });
  }

  // The page render already agrees with the header, so this first pass is only
  // for a control mounted by a later fragment swap.
  mirror();
  document.body.addEventListener('htmx:afterSwap', function () {
    mirror();
  });
})();
