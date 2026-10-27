// The chats page's cards and filters (#2603). The list is rendered once per page load and
// filtered by the page's own address, so nothing here keeps state across updates: it
// lays out what the server sent and turns a press into a plain form submit.
(function () {
  'use strict';

  // The filters last applied on this device (#3367). The address stays where filters
  // live: any address with a query records what it applies (nothing, for Clear's
  // `?q=`), and a bare /chats reopens with the recorded ones.
  var FILTER_KEY = 'agentdeck:chats:filters';
  var FILTER_NAMES = ['q', 'closed', 'account', 'project', 'source'];

  function appliedFilters(search) {
    var params = new URLSearchParams(search);
    var kept = new URLSearchParams();
    FILTER_NAMES.forEach(function (name) {
      params.getAll(name).forEach(function (value) {
        if (value) kept.append(name, value);
      });
    });
    return kept.toString();
  }

  // Only while "Guess for me" is on (#3367): switched off, nothing is recorded or reapplied.
  var remembering = !!document.querySelector('form.chat-filters[data-remember-filters]');

  function rememberFilters(search) {
    if (!remembering) return;
    try { localStorage.setItem(FILTER_KEY, appliedFilters(search)); } catch (_) {}
  }

  try {
    if (!remembering) {
      // Nothing to reapply.
    } else if (location.search) {
      rememberFilters(location.search);
    } else {
      var saved = localStorage.getItem(FILTER_KEY) || '';
      if (saved) location.replace(location.pathname + '?' + saved);
    }
  } catch (_) {}

  // Recorded as they are applied too, so leaving before the next page has run this
  // script still keeps them.
  document.addEventListener('submit', function (event) {
    if (event.target.matches && event.target.matches('form.chat-filters')) {
      rememberFilters(new URLSearchParams(new FormData(event.target)).toString());
    }
  });
  document.addEventListener('click', function (event) {
    if (event.target instanceof Element && event.target.closest('.chat-filters .filter-clear')) {
      rememberFilters('');
    }
  });

  // The latest reply is a short window kept scrolled to its newest line; expanded, the
  // card shows the latest prompt and reply in full, rendered as Markdown.
  function toBottom(box, inner) {
    var overflow = inner.scrollHeight - box.clientHeight;
    inner.style.transform = overflow > 6 ? 'translateY(' + (-overflow) + 'px)' : '';
  }

  function line(role, text, markdown, sessionKey) {
    var row = document.createElement('span');
    row.className = 'latest-line' + (role === 'agent' ? ' agent' : '');
    var arrow = document.createElement('span');
    arrow.className = 'latest-arrow';
    arrow.textContent = role === 'agent' ? '⟳' : '↳';
    var body = document.createElement('span');
    body.className = 'lt-text';
    if (markdown && typeof mdToHtml === 'function') {
      body.innerHTML = mdToHtml(text, sessionKey);
      body.setAttribute('data-md', '');
    } else {
      body.textContent = text;
    }
    row.appendChild(arrow);
    row.appendChild(body);
    return row;
  }

  function layout(card) {
    var box = card.querySelector('[data-marquee]');
    var inner = box && box.querySelector('.latest-scroll');
    if (!inner) return;
    var prompt = (box.dataset.prompt || '').trim();
    var text = (box.dataset.text || '').trim();
    var expanded = card.classList.contains('expanded');
    var sessionKey = card.getAttribute('data-session-key');
    inner.textContent = '';
    inner.style.transform = '';
    if (expanded) {
      var lines = [];
      if (prompt) lines.push(['user', prompt]);
      if (text) lines.push(['agent', text]);
      // Whoever spoke last reads last.
      if (box.dataset.latestRole === 'user') lines.reverse();
      lines.forEach(function (entry) {
        inner.appendChild(line(entry[0], entry[1], true, sessionKey));
      });
      box.scrollTop = box.scrollHeight;
      return;
    }
    // Collapsed, the prompt already has its own pinned row above the reply.
    if (text) inner.appendChild(line('agent', text, false, sessionKey));
    box.scrollTop = 0;
    toBottom(box, inner);
  }

  function layoutAll() {
    document.querySelectorAll('#sessions a.session').forEach(layout);
  }

  // Sub-agent rows render collapsed (issue #5); the toggle opens its own family.
  function toggleSubagents(toggle) {
    var key = toggle.getAttribute('data-key');
    var rows = key && document.querySelector(
      '.subagent-children[data-subagents-of="' + key + '"]'
    );
    if (!rows) return;
    var open = rows.classList.toggle('collapsed') === false;
    var label = (open ? 'hide ' : 'show ') + 'sub-agents';
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    toggle.setAttribute('aria-label', label);
    toggle.setAttribute('title', label);
    var caret = toggle.querySelector('.subagent-caret');
    if (caret) caret.textContent = open ? '▾' : '▸';
  }

  // Each of these sits inside the card's link, so it must stop the card navigating.
  document.addEventListener('click', function (event) {
    var target = event.target instanceof Element ? event.target : null;
    if (!target) return;
    var expand = target.closest('#sessions .expand-btn');
    var subagents = target.closest('#sessions .subagent-toggle');
    var external = target.closest('#sessions .cc-btn, #sessions .gh-btn');
    if (!expand && !subagents && !external) return;
    event.preventDefault();
    event.stopPropagation();
    if (expand) {
      var card = expand.closest('a.session');
      if (card) {
        card.classList.toggle('expanded');
        layout(card);
      }
    } else if (subagents) {
      toggleSubagents(subagents);
    } else {
      var url = external.getAttribute('data-href');
      if (url) window.open(url, '_blank', 'noopener');
    }
  });

  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    var toggle = event.target instanceof Element &&
      event.target.closest('#sessions .subagent-toggle');
    if (!toggle) return;
    event.preventDefault();
    event.stopPropagation();
    toggleSubagents(toggle);
  });

  // A ticked filter box applies at once; typed text waits for Enter or the Filter button.
  document.addEventListener('change', function (event) {
    var box = event.target instanceof Element && event.target.closest('.chat-filters .filter-box');
    if (!box) return;
    var label = box.closest('.toggle-btn');
    if (label) label.classList.toggle('on', box.checked);
    var form = box.form;
    if (form && form.requestSubmit) form.requestSubmit();
    else if (form) form.submit();
  });

  document.addEventListener('click', function (event) {
    var button = event.target instanceof Element && event.target.closest('#more-filters');
    if (!button) return;
    var panel = document.getElementById('filter-panel');
    if (!panel) return;
    panel.hidden = !panel.hidden;
    button.setAttribute('aria-expanded', panel.hidden ? 'false' : 'true');
  });

  if (document.readyState === 'complete') layoutAll();
  else window.addEventListener('load', function () { requestAnimationFrame(layoutAll); });
})();
