// Forward uncaught page failures to the deck so somebody can notice them (#1514).
//
// The browser is the one part of AgentDeck whose failures left no trace on the
// host: a script that throws on a phone writes to a console nobody will ever
// open. This posts a bounded description back to the server, which logs it and
// — when the operator configured centralized reporting — ships it onward.
//
// It never sends page content: only the failure's own message, location and
// stack, plus the path of the page it happened on. Deliberately loaded before
// the other shell scripts so a failure inside one of them is still caught.
(function () {
  'use strict';

  var ENDPOINT = '/api/browser-errors';
  // A page that throws inside a render loop must not turn into a log flood; the
  // fifth copy of a failure tells a reader nothing the first one did not.
  var MAX_REPORTS = 5;
  var sent = 0;
  var seen = Object.create(null);

  function post(report) {
    var body = JSON.stringify(report);
    try {
      // keepalive survives the navigation that an error often precedes.
      if (window.fetch) {
        fetch(ENDPOINT, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: body,
          keepalive: true,
        }).catch(function () {});
        return;
      }
      if (navigator.sendBeacon) {
        navigator.sendBeacon(ENDPOINT, new Blob([body], { type: 'application/json' }));
      }
    } catch (err) {
      // Reporting a failure must never itself fail the page.
    }
  }

  function report(kind, message, source, line, column, stack) {
    if (sent >= MAX_REPORTS) return;
    var key = kind + '|' + message + '|' + source + '|' + line;
    if (seen[key]) return;
    seen[key] = true;
    sent += 1;
    post({
      kind: kind,
      message: String(message || ''),
      source: String(source || ''),
      line: typeof line === 'number' ? line : null,
      column: typeof column === 'number' ? column : null,
      stack: String(stack || ''),
      page: window.location.pathname + window.location.search,
    });
  }

  window.addEventListener('error', function (event) {
    // A failed <img>/<script> load also fires 'error', on the element rather
    // than the window; those are not exceptions and are not worth a report.
    if (event.target && event.target !== window) return;
    var error = event.error;
    report(
      'error',
      event.message || (error && error.message) || 'uncaught error',
      event.filename,
      event.lineno,
      event.colno,
      error && error.stack
    );
  });

  // A rejection with no message said only "unhandled rejection" (#3432): its type and a
  // short text of it say what was rejected.
  var DESCRIBE_CHARS = 200;

  function describe(reason) {
    var name = (reason.constructor && reason.constructor.name) || 'Object';
    var text;
    if (typeof Event !== 'undefined' && reason instanceof Event) {
      var tag = reason.target && reason.target.tagName ? reason.target.tagName.toLowerCase() : '';
      text = reason.type + (tag ? ' on ' + tag : '');
    } else {
      try { text = JSON.stringify(reason); } catch (_) { text = ''; }
      if (!text) { try { text = String(reason); } catch (_) { text = ''; } }
    }
    return (name + ': ' + text).slice(0, DESCRIBE_CHARS);
  }

  window.addEventListener('unhandledrejection', function (event) {
    var reason = event.reason;
    var message = 'unhandled rejection';
    var stack = '';
    if (reason && typeof reason === 'object') {
      message = reason.message || describe(reason);
      stack = reason.stack || '';
    } else if (reason !== undefined && reason !== null) {
      message = String(reason);
    }
    report('unhandledrejection', message, '', null, null, stack);
  });
})();
