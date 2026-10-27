// Account detail pages can be opened from the dashboard, a selected chat, or
// the account index. Return to that same-origin source when one exists; a direct
// link falls back to the account index through the anchor's normal href.
(function () {
  document.addEventListener('click', function (event) {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey ||
        event.ctrlKey || event.shiftKey || event.altKey) return;
    var link = event.target.closest('[data-account-back]');
    if (!link) return;
    var source;
    try {
      source = new URL(document.referrer);
    } catch (_) {
      return;
    }
    if (window.history.length <= 1 || source.origin !== window.location.origin ||
        source.pathname + source.search === window.location.pathname + window.location.search) {
      return;
    }
    event.preventDefault();
    window.history.back();
  });
})();
