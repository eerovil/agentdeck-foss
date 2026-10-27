(function () {
  'use strict';

  var initialized = new WeakSet();

  function activeToken(input) {
    var end = input.selectionStart;
    if (end === null || end !== input.selectionEnd) return null;
    var before = input.value.slice(0, end);
    var start = end;
    while (start > 0 && !/\s/.test(before[start - 1])) start -= 1;
    var query = before.slice(start);
    if (!query || (query[0] !== '/' && query[0] !== '$')) return null;
    // Native slash commands and Claude skills are recognized only at message start.
    if (query[0] === '/' && before.slice(0, start).trim()) return null;
    return { start: start, end: end, query: query.toLowerCase() };
  }

  function matchesQuery(item, query) {
    var value = item.value.toLowerCase();
    if (value.indexOf(query) === 0) return true;
    // Slash is also a discovery alias for Codex skills. Keep item.value intact
    // so choosing `/rate` inserts Codex's native `$rate-my-pr` invocation.
    return query[0] === '/' && item.kind === 'skill' && value[0] === '$'
      && value.slice(1).indexOf(query.slice(1)) === 0;
  }

  function setup(root) {
    (root || document).querySelectorAll('[data-composer-autocomplete]').forEach(function (box) {
      if (initialized.has(box)) return;
      initialized.add(box);
      var input = box.querySelector('textarea[name="message"]');
      var panel = box.querySelector('[data-composer-suggestions]');
      var data = box.querySelector('script[type="application/json"]');
      if (!input || !panel || !data) return;
      var suggestions;
      try { suggestions = JSON.parse(data.textContent); } catch (_) { suggestions = []; }
      if (!suggestions.length) return;

      var matches = [];
      var selected = 0;
      var token = null;

      function close() {
        matches = [];
        token = null;
        panel.hidden = true;
        panel.textContent = '';
        input.setAttribute('aria-expanded', 'false');
        input.removeAttribute('aria-activedescendant');
      }

      function choose(index) {
        var item = matches[index];
        if (!item || !token) return;
        var suffix = input.value.slice(token.end);
        var separator = suffix && /^\s/.test(suffix) ? '' : ' ';
        input.setRangeText(item.value + separator, token.start, token.end, 'end');
        input.dispatchEvent(new Event('input', { bubbles: true }));
        close();
        input.focus();
      }

      function paint() {
        token = activeToken(input);
        matches = token ? suggestions.filter(function (item) {
          return matchesQuery(item, token.query);
        }).slice(0, 8) : [];
        if (!matches.length) { close(); return; }
        selected = Math.min(selected, matches.length - 1);
        panel.textContent = '';
        matches.forEach(function (item, index) {
          var option = document.createElement('button');
          option.type = 'button';
          option.className = 'composer-suggestion';
          option.id = panel.id + '-option-' + index;
          option.setAttribute('role', 'option');
          option.setAttribute('aria-selected', index === selected ? 'true' : 'false');
          option.innerHTML = '<span class="composer-suggestion-value"></span>'
            + '<span class="composer-suggestion-kind"></span>'
            + '<span class="composer-suggestion-description"></span>';
          option.querySelector('.composer-suggestion-value').textContent = item.label;
          option.querySelector('.composer-suggestion-kind').textContent = item.kind;
          option.querySelector('.composer-suggestion-description').textContent = item.description;
          option.addEventListener('mousedown', function (event) { event.preventDefault(); });
          option.addEventListener('click', function () { choose(index); });
          panel.appendChild(option);
        });
        panel.hidden = false;
        input.setAttribute('aria-expanded', 'true');
        input.setAttribute('aria-activedescendant', panel.id + '-option-' + selected);
      }

      input.addEventListener('input', function () { selected = 0; paint(); });
      input.addEventListener('click', paint);
      input.addEventListener('blur', function () { window.setTimeout(close, 100); });
      input.addEventListener('keydown', function (event) {
        if (panel.hidden) return;
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
          event.preventDefault();
          event.stopImmediatePropagation();
          var direction = event.key === 'ArrowDown' ? 1 : -1;
          selected = (selected + direction + matches.length) % matches.length;
          paint();
        } else if (event.key === 'Enter' || event.key === 'Tab') {
          event.preventDefault();
          event.stopImmediatePropagation();
          choose(selected);
        } else if (event.key === 'Escape') {
          event.preventDefault();
          event.stopImmediatePropagation();
          close();
        }
      }, true);
    });
  }

  window.addEventListener('DOMContentLoaded', function () { setup(document); });
  document.addEventListener('agentdeck:session-navigation', function () { setup(document); });
  document.body.addEventListener('htmx:load', function (event) {
    setup((event.detail && event.detail.elt) || event.target);
  });
})();
