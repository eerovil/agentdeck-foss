// Let a refusal the server put in words reach the person who caused it.
//
// htmx never swaps a 4xx, so a route that answers `{"detail": "..."}` leaves its
// control showing nothing but the status number — Continue chat on a worker
// session whose worktree was cleaned up reported a bare 422 and the reason was
// only visible in the server log (#746). A route that means the refusal to be
// read answers it as a rendered fragment instead; this opts exactly those
// responses — 4xx with an HTML body — into the control's normal swap.
//
// A JSON-bodied error is untouched and still lands on htmx's own error path, so
// this cannot spill a raw payload into the page. `successful` is set from the
// status alone and is not affected, so optimistic feedback still settles as a
// failure and keeps whatever the person typed.
(function () {
  'use strict';

  document.body.addEventListener('htmx:beforeSwap', function (event) {
    var detail = event.detail;
    var xhr = detail && detail.xhr;
    if (!xhr || xhr.status < 400 || xhr.status > 499) return;
    var contentType = (xhr.getResponseHeader('content-type') || '').toLowerCase();
    if (contentType.indexOf('text/html') !== 0) return;
    detail.shouldSwap = true;
    detail.isError = false;
  });
})();
