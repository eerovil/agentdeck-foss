// The top bar's list of new Needs you Works (#3239).
//
// The list is pushed over the event stream whenever what it shows changes, and the
// push replaces the whole <details>. Someone reading the open list at that moment
// must not see it snap shut, so this keeps whether it was open and puts that back
// after a swap of its own slot. Unlike the job queue's panel, nothing is stored:
// a list left open is not reopened by the next page load.
//
// It also closes the way a menu does: Escape, or a tap anywhere outside it.
//
// A Work whose chat or page is on screen is not new to the person looking at it
// (#3289): when the list names one, this marks it read at once, the same way the
// row's own check mark does. A tab in the background waits until it is shown again.
//
// A Work on screen or marked read here also closes this device's phone notification
// for it (#3365); other devices are cleared by the deck's own clear push.
(function () {
  'use strict';

  var open = false;

  function menu() {
    return document.querySelector('#needs-you-slot .needs-you-menu');
  }

  // `toggle` fires a task after the click that caused it, so a swap landing between
  // the two would read the state from before the click. The capturing click writes
  // the intent at once; the default action has not flipped `open` yet, hence `!`.
  document.addEventListener('click', function (e) {
    var el = e.target;
    var summary = el && el.closest ? el.closest('.needs-you-menu > summary') : null;
    if (summary) {
      open = !summary.parentElement.open;
      return;
    }
    var current = menu();
    if (current && current.open && !current.contains(el)) {
      current.open = false;
      open = false;
    }
  }, true);

  document.addEventListener('toggle', function (e) {
    var el = e.target;
    if (el && el.classList && el.classList.contains('needs-you-menu')) open = el.open;
  }, true);

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    var current = menu();
    if (!current || !current.open) return;
    current.open = false;
    open = false;
    var summary = current.querySelector('summary');
    if (summary) summary.focus();
  });

  var marking = false;
  var closedOnScreen = {};

  // The Works on screen: a chat pane names its Work, and so does a Work's page or column.
  function onScreen() {
    var ids = {};
    var chats = document.querySelectorAll('.session-detail[data-chat-work-id]');
    for (var i = 0; i < chats.length; i++) ids[chats[i].getAttribute('data-chat-work-id')] = true;
    var works = document.querySelectorAll('.work-main[data-work-id]');
    for (var j = 0; j < works.length; j++) ids[works[j].getAttribute('data-work-id')] = true;
    return ids;
  }

  // Close this device's notifications with these tags, if the worker shows any.
  function closeNotifications(tags) {
    var sw = navigator.serviceWorker;
    if (!tags.length || !sw || !sw.getRegistration) return;
    sw.getRegistration().then(function (registration) {
      if (!registration || !registration.getNotifications) return;
      tags.forEach(function (tag) {
        registration.getNotifications({ tag: tag }).then(function (shown) {
          shown.forEach(function (notification) { notification.close(); });
        }).catch(function () {});
      });
    }).catch(function () {});
  }

  function closeWorks(ids) {
    closeNotifications(ids.map(function (id) { return 'work:' + id; }));
  }

  // A row's check mark, or "Mark all read", which also closes the summary notification.
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form || !form.matches || !form.matches('form[hx-post="/works/seen"]')) return;
    var inputs = form.querySelectorAll('input[name="work_id"]');
    var ids = [];
    for (var i = 0; i < inputs.length; i++) ids.push(inputs[i].value);
    closeWorks(ids);
    if (form.closest('.needs-you-head')) closeNotifications(['needs-you']);
  }, true);

  function markOnScreenRead() {
    if (marking || document.visibilityState !== 'visible' || !window.htmx) return;
    var shown = onScreen();
    // Once per Work while this tab stays shown: every swap lands here. A visible deck
    // gets no new notification (sw.js), so one can only appear while the tab is hidden.
    closeWorks(Object.keys(shown).filter(function (id) {
      if (closedOnScreen[id]) return false;
      closedOnScreen[id] = true;
      return true;
    }));
    var current = menu();
    if (!current) return;
    var ids = [];
    // "Mark all read" carries every new Work, also the ones past the rows shown.
    var listed = current.querySelectorAll('.needs-you-head input[name="work_id"]');
    for (var i = 0; i < listed.length; i++) {
      if (shown[listed[i].value]) ids.push(listed[i].value);
    }
    if (!ids.length) return;
    marking = true;
    window.htmx.ajax('POST', '/works/seen', {
      target: '#needs-you-slot', swap: 'innerHTML', values: { work_id: ids }
    }).finally(function () { marking = false; });
  }

  // Only a swap that brought in this list has anything to put back: re-applying on
  // every swap on the page could close a list someone opened a moment ago (#1456).
  // Any swap may have brought in a new list, or a chat pane naming another Work.
  document.addEventListener('htmx:afterSwap', function (e) {
    markOnScreenRead();
    var slot = document.getElementById('needs-you-slot');
    var target = e && e.target;
    if (!slot || !target || !(target === slot || slot.contains(target))) return;
    var current = menu();
    if (current) current.open = open;
  });

  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState !== 'visible') closedOnScreen = {};
    markOnScreenRead();
  });
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', markOnScreenRead);
  } else {
    markOnScreenRead();
  }
})();
