// The card's babysit button (issue #824): take a mute lease on one chat,
// which takes a mute lease on it, and press again to hand it back.
//
// Every card is server-rendered and re-pushed over SSE, so a fragment produced BEFORE the
// server granted the lease can land after the press and flip the button back.
// So the press records an intent, the intent is re-projected onto the DOM after
// every swap, and it is dropped as soon as the server's own render agrees with
// it — or the request fails, which restores exactly what the server last said.
(function () {
  'use strict';

  var SETTLE_MS = 8000;    // a server that never agrees must not pin the view
  var intents = new Map(); // session key -> {state: 'on'|'off', settled, settledAt}
  var inflight = new Set();
  var projected = false;

  function forEach(selector, fn) {
    Array.prototype.forEach.call(document.querySelectorAll(selector), fn);
  }

  // The control carries no key of its own — one `data-session-key` per card is
  // what everything else on the page locates by — so it is reached through the
  // card that owns it.
  function controls(key) {
    return 'a.session[data-session-key="' + key + '"] [data-babysit-control]';
  }

  function serverAgrees(key, state) {
    var seen = [];
    forEach(controls(key), function (control) {
      if (control.dataset.bsState) seen.push(control.dataset.bsState);
    });
    return seen.length > 0 && seen.every(function (value) { return value === state; });
  }

  function reset() {
    forEach('[data-babysit-control]', function (control) {
      control.dataset.bsView = control.dataset.bsState || 'off';
    });
  }

  function settle() {
    var now = Date.now();
    intents.forEach(function (intent, key) {
      if (!intent.settled) return;
      if (serverAgrees(key, intent.state) || now - intent.settledAt > SETTLE_MS) {
        intents.delete(key);
      }
    });
  }

  function apply() {
    intents.forEach(function (intent, key) {
      forEach(controls(key), function (control) {
        control.dataset.bsView = intent.state;
      });
    });
  }

  function project() {
    if (!intents.size && !projected) return;
    reset();
    settle();
    apply();
    projected = intents.size > 0;
  }

  function press(key, state) {
    var intent = {state: state, settled: false, settledAt: 0};
    intents.set(key, intent);
    project();
    return function finish(successful) {
      // Only the request that recorded an intent may settle or revert it: a
      // press made while an older one is in flight owns the view from then on.
      if (intents.get(key) === intent) {
        if (successful) {
          intent.settled = true;
          intent.settledAt = Date.now();
        } else {
          intents.delete(key);
        }
      }
      project();
    };
  }

  function submit(button) {
    var stopping = button.getAttribute('data-babysit-action') === 'stop';
    var card = button.closest('[data-session-key]');
    var key = card && card.getAttribute('data-session-key');
    // One press at a time per chat: the second would record an intent the first
    // answer could then settle on its behalf.
    if (!key || inflight.has(key)) return;
    inflight.add(key);
    var finish = press(key, stopping ? 'off' : 'on');
    var url = '/api/sessions/' + encodeURIComponent(key) + '/mute';
    var options = {
      method: stopping ? 'DELETE' : 'PUT',
      headers: {
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest'
      }
    };
    // The boss pressing the button is an instruction, not the deck's own guess:
    // a longer lease, and no hard cap. The deck records which it was.
    if (!stopping) options.body = JSON.stringify({source: 'boss'});
    fetch(url, options).then(function (response) {
      inflight.delete(key);
      finish(response.ok);
    }).catch(function () {
      inflight.delete(key);
      finish(false);
    });
  }

  function buttonFor(target) {
    return target.closest ? target.closest('[data-session-babysit]') : null;
  }

  document.addEventListener('click', function (event) {
    var button = buttonFor(event.target);
    if (!button) return;
    // The control lives inside the card's own `<a href>`, so the press must not
    // also open the chat.
    event.preventDefault();
    event.stopPropagation();
    submit(button);
  });

  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    var button = buttonFor(event.target);
    if (!button) return;
    event.preventDefault();
    event.stopPropagation();
    submit(button);
  });

  document.body.addEventListener('htmx:afterSwap', project);

  window.AgentDeckBabysit = {intents: intents, press: press, project: project};
})();
