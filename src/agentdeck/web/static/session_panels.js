(function () {
  'use strict';

  var STORAGE_KEY = 'agentdeckExtraSessionPanes';
  // Three chat columns need more room than the ordinary two-pane desktop view.
  var desktop = window.matchMedia('(min-width: 1280px)');

  function validPath(value) {
    return typeof value === 'string' && /^\/(?:sessions|chats)\/[^/?#]+$/.test(value);
  }

  function readPanes() {
    try {
      var parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');
      if (!Array.isArray(parsed)) return [];
      return parsed.filter(function (item, index, all) {
        return item && validPath(item.path) && typeof item.title === 'string' &&
          all.findIndex(function (candidate) { return candidate.path === item.path; }) === index;
      }).slice(0, 2);
    } catch (_) {
      return [];
    }
  }

  function writePanes(panes) {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(panes)); } catch (_) {}
  }

  function currentDetail() {
    return document.querySelector('.session-layout > .session-detail');
  }

  function framePath(path) {
    return path + (path.indexOf('?') === -1 ? '?' : '&') + 'pane=extra';
  }

  function removeMountedPanes() {
    document.querySelectorAll('.session-extra-pane').forEach(function (pane) { pane.remove(); });
    document.body.classList.remove('session-extra-active');
  }

  function updateAddButton(panes) {
    var button = document.querySelector('[data-session-extra-add]');
    var detail = currentDetail();
    if (!button || !detail) return;
    var path = detail.dataset.sessionPath || location.pathname;
    var alreadyOpen = panes.some(function (pane) { return pane.path === path; });
    button.disabled = alreadyOpen || panes.length >= 2;
    button.title = alreadyOpen ? 'This chat is already in an extra pane' :
      panes.length >= 2 ? 'Both extra panes are in use' : 'Move this chat to an extra pane';
  }

  function makePane(item, slot) {
    var pane = document.createElement('section');
    pane.className = 'session-extra-pane';
    pane.dataset.sessionExtraSlot = String(slot);
    pane.dataset.sessionExtraPath = item ? item.path : '';
    pane.setAttribute('aria-label', item ? 'Extra chat: ' + item.title : 'Empty extra chat pane');

    var head = document.createElement('div');
    head.className = 'session-extra-head';
    var title = document.createElement('span');
    title.className = 'session-extra-title';
    title.textContent = item ? item.title : 'Extra pane ' + (slot + 1);
    head.appendChild(title);
    if (item) {
      var close = document.createElement('button');
      close.type = 'button';
      close.className = 'session-extra-close';
      close.dataset.sessionExtraClose = item.path;
      close.setAttribute('aria-label', 'Close extra chat: ' + item.title);
      close.title = 'Close extra pane';
      close.textContent = '×';
      head.appendChild(close);
    }
    pane.appendChild(head);

    if (item) {
      var frame = document.createElement('iframe');
      frame.className = 'session-extra-frame';
      frame.src = framePath(item.path);
      frame.title = item.title;
      frame.dataset.sessionPath = item.path;
      pane.appendChild(frame);
    } else {
      var empty = document.createElement('p');
      empty.className = 'session-extra-empty';
      empty.textContent = 'Open another chat, then move it here.';
      pane.appendChild(empty);
    }
    return pane;
  }

  function render() {
    if (document.body.classList.contains('session-pane-page')) return;
    var panes = readPanes();
    var layout = document.querySelector('.session-layout');
    var detail = currentDetail();
    if (!desktop.matches || !layout || !detail) {
      removeMountedPanes();
      updateAddButton(panes);
      return;
    }
    if (panes.length) {
      document.body.classList.add('session-extra-active');
      var mounted = Array.from(layout.querySelectorAll('.session-extra-pane'));
      var kept = [];
      for (var slot = 0; slot < 2; slot += 1) {
        var item = panes[slot];
        var path = item ? item.path : '';
        var pane = mounted.find(function (candidate) {
          return candidate.dataset.sessionExtraPath === path && kept.indexOf(candidate) === -1;
        });
        if (!pane) {
          pane = makePane(item, slot);
          layout.appendChild(pane);
        }
        pane.dataset.sessionExtraSlot = String(slot);
        kept.push(pane);
      }
      mounted.forEach(function (pane) {
        if (kept.indexOf(pane) === -1) pane.remove();
      });
    } else {
      removeMountedPanes();
    }
    updateAddButton(panes);
  }

  document.addEventListener('click', function (event) {
    var add = event.target.closest('[data-session-extra-add]');
    if (add) {
      var detail = currentDetail();
      var panes = readPanes();
      if (!detail || panes.length >= 2) return;
      var path = detail.dataset.sessionPath || location.pathname;
      if (!validPath(path) || panes.some(function (pane) { return pane.path === path; })) return;
      panes.push({path: path, title: detail.dataset.sessionTitle || document.title || 'Chat'});
      writePanes(panes);
      render();
      return;
    }
    var close = event.target.closest('[data-session-extra-close]');
    if (!close) return;
    writePanes(readPanes().filter(function (pane) {
      return pane.path !== close.dataset.sessionExtraClose;
    }));
    render();
  });

  document.addEventListener('agentdeck:session-navigation', render);
  window.addEventListener('storage', function (event) {
    if (event.key === STORAGE_KEY) render();
  });
  if (desktop.addEventListener) desktop.addEventListener('change', render);
  else desktop.addListener(render);
  render();
})();
