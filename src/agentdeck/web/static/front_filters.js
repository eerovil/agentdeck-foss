// The front page's folds and project tabs (#2283, #2311, #3226), split from work_list.js (#3108).
//
// Loaded without `defer` in every page's head (base.html), so the generated stylesheets are in place
// before the body is parsed: deferred, the page was painted with every project's rows and
// open groups, and then they disappeared -- the pop on every reload. The page calls
// `AgentDeckFront.apply()` once more right after its last group, so the counts, the empty
// headers and Other activity's fold are corrected before the parser goes on.
//
// 1. The groups fold. Tapping a header (`[data-front-group]`) folds its rows
//    (`[data-front-in]`); the folded keys persist in localStorage under
//    `agentdeck-front-collapsed` as `{key: true}`. Folding is one generated stylesheet rather
//    than a class on each row, so a live push that brings new rows in is folded before it
//    paints, and a retained row is never touched. A tap folds the group its press started
//    on, since a live push redraws the header and a press that ends on the new one is no
//    click (#3432). Other activity is a `<details>` that shares
//    the same store (`data-front-collapse`). Everything is open by default.
// 2. The project tabs (#3226). Each Work row names its project (`data-work-project`); a line
//    of tabs under the counts, pinned under the top bar, has one tab per project on the
//    page, the one with the newest activity first (#3399), each with its new count, and All
//    last. The order holds while the page is open: it is sorted again only on a load, or when
//    a project comes onto the page or leaves it, so a push never moves a tab under a finger. A tap shows only that project, a hold adds or removes it, and a sideways
//    swipe on the list steps to the next or previous tab, round the ends. The hidden
//    projects persist in localStorage under
//    `agentdeck-front-hidden-projects` as `{project: true}`. Like folding it is a generated
//    stylesheet, so a pushed row is hidden before it paints; the group headers, their NEW
//    pills and the counts line are then recounted without the hidden rows.
(function () {
  'use strict';

  var FOLD_KEY = 'agentdeck-front-collapsed';
  var FOLD_NAME = /^[a-z0-9_-]+$/;

  function folded() {
    try {
      var parsed = JSON.parse(localStorage.getItem(FOLD_KEY) || '{}');
      return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
    } catch (_) { return {}; }
  }

  function applyFolds() {
    var fold = folded();
    var rules = [];
    Object.keys(fold).forEach(function (key) {
      if (!fold[key] || !FOLD_NAME.test(key)) return;
      rules.push('[data-front-in="' + key + '"]{display:none !important}');
      rules.push('[data-front-group="' + key + '"] .fr-chev::before{content:"\\25B8"}');
    });
    var style = document.getElementById('front-fold-style');
    if (!style && rules.length) {
      style = document.createElement('style');
      style.id = 'front-fold-style';
      document.head.appendChild(style);
    }
    if (style) style.textContent = rules.join('\n');
    Array.prototype.forEach.call(document.querySelectorAll('[data-front-toggle]'), function (button) {
      button.setAttribute('aria-expanded', fold[button.getAttribute('data-front-toggle')] ? 'false' : 'true');
    });
    Array.prototype.forEach.call(document.querySelectorAll('details[data-front-collapse]'), function (details) {
      var open = !fold[details.getAttribute('data-front-collapse')];
      if (details.open !== open) details.open = open;
    });
  }

  function setFold(key, shut) {
    if (!FOLD_NAME.test(key)) return;
    var fold = folded();
    if (shut) fold[key] = true;
    else delete fold[key];
    try { localStorage.setItem(FOLD_KEY, JSON.stringify(fold)); } catch (_) {}
    applyFolds();
  }

  // A live push redraws every header of its list, and one opening the page lands at once: a
  // press whose header was redrawn before the release gets no click at all (Chrome sends
  // only the pointerup), so the release folds the group the press started on (#3432).
  var pressed = null;
  var released = null;
  var PRESS_PX = 10;
  var RELEASE_MS = 500;

  document.addEventListener('pointerdown', function (e) {
    var head = e.target && e.target.closest ? e.target.closest('[data-front-group]') : null;
    released = null;  // a new press is a new tap, not the late click of the last one
    pressed = head && !e.target.closest('a')
      ? {head: head, key: head.getAttribute('data-front-group'), x: e.clientX, y: e.clientY}
      : null;
  }, true);

  document.addEventListener('pointerup', function (e) {
    var press = pressed;
    if (!press || press.head.isConnected) return;
    pressed = null;
    if (Math.abs(e.clientX - press.x) > PRESS_PX || Math.abs(e.clientY - press.y) > PRESS_PX) return;
    // Only onto the redrawn header itself: a release anywhere else is no tap on it.
    var head = e.target && e.target.closest ? e.target.closest('[data-front-group]') : null;
    if (!head || head.getAttribute('data-front-group') !== press.key || e.target.closest('a')) return;
    released = {key: press.key, at: Date.now()};
    setFold(press.key, !folded()[press.key]);
  }, true);

  document.addEventListener('click', function (e) {
    var head = e.target && e.target.closest ? e.target.closest('[data-front-group]') : null;
    if (!head || e.target.closest('a')) return;
    var key = head.getAttribute('data-front-group');
    // A browser that does click the redrawn header after its release folded it.
    var late = released;
    released = null;
    if (late && late.key === key && Date.now() - late.at < RELEASE_MS) return;
    setFold(key, !folded()[key]);
  });

  document.addEventListener('toggle', function (e) {
    var details = e.target;
    if (!details || !details.matches || !details.matches('details[data-front-collapse]')) return;
    var key = details.getAttribute('data-front-collapse');
    if (Boolean(folded()[key]) !== !details.open) setFold(key, !details.open);
  }, true);

  var PROJECT_KEY = 'agentdeck-front-hidden-projects';

  function hiddenProjects() {
    try {
      var parsed = JSON.parse(localStorage.getItem(PROJECT_KEY) || '{}');
      return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
    } catch (_) { return {}; }
  }

  function quoted(value) {
    return '"' + String(value).replace(/["\\]/g, '\\$&') + '"';
  }

  function projectRows() {
    return Array.prototype.slice.call(document.querySelectorAll('.front-page [data-work-project]'));
  }

  function shown(el, hidden) {
    return !hidden[el.getAttribute('data-work-project')];
  }

  function recountProjects(hidden) {
    var any = Object.keys(hidden).length > 0;
    Array.prototype.forEach.call(document.querySelectorAll('.front-page [data-front-group]'), function (head) {
      var key = head.getAttribute('data-front-group');
      var rows = Array.prototype.filter.call(
        document.querySelectorAll('article[data-front-in=' + quoted(key) + ']:not([data-work-finishing])'),
        function (row) { return shown(row, hidden); });
      var count = head.querySelector('.fr-group-count');
      if (count) {
        if (!count.hasAttribute('data-count-all')) count.setAttribute('data-count-all', count.textContent);
        count.textContent = any ? String(rows.length) : count.getAttribute('data-count-all');
      }
      var pill = head.querySelector('[data-front-new]');
      if (pill) {
        var fresh = any
          ? rows.filter(function (row) { return row.hasAttribute('data-work-unseen'); }).length
          : Number(pill.getAttribute('data-front-new'));
        pill.textContent = fresh + ' new';
        pill.hidden = fresh === 0;
      }
      // A row Finish is taking away (#3299, `work_list.js`) is off the count already, and
      // its group's header goes with the last one.
      var held = document.querySelector('article[data-front-in=' + quoted(key) + '][data-work-finishing]');
      head.hidden = (any || !!held) && rows.length === 0;
    });
    Array.prototype.forEach.call(document.querySelectorAll('[data-front-summary][data-by-project]'), function (item) {
      var by;
      try { by = JSON.parse(item.getAttribute('data-by-project')) || {}; } catch (_) { by = {}; }
      var bold = item.querySelector('b');
      if (!bold) return;
      if (!bold.hasAttribute('data-count-all')) bold.setAttribute('data-count-all', bold.textContent);
      var total = any ? 0 : Number(bold.getAttribute('data-count-all'));
      if (any) Object.keys(by).forEach(function (name) { if (!hidden[name]) total += Number(by[name]) || 0; });
      bold.textContent = String(total);
      var words = item.querySelector('[data-one]');
      if (words) words.textContent = total === 1 ? words.getAttribute('data-one') : words.getAttribute('data-many');
      item.hidden = total === 0;
    });
  }

  // How many new Works each project has, from the counts line's per-project breakdown.
  function newByProject() {
    var item = document.querySelector('.front-page [data-front-summary="new"][data-by-project]');
    var by;
    try { by = item ? JSON.parse(item.getAttribute('data-by-project')) || {} : {}; } catch (_) { by = {}; }
    return by;
  }

  // The order last sorted, kept for one tab line (a load, or a desktop Work page mounting the
  // list, brings a new one) until the projects with rows, or the tabs, change.
  var keptOrder = null;
  var keptFor = null;
  var keptKey = '';

  // The project whose newest row activity (the time its age shows) is the most recent first,
  // then the ones with none by name; All is the last tab.
  function tabOrder(hidden) {
    var names = {};
    var latest = {};
    projectRows().forEach(function (row) {
      var name = row.getAttribute('data-work-project');
      names[name] = true;
      var at = Date.parse(row.getAttribute('data-work-active') || '');
      if (!isNaN(at) && !(latest[name] >= at)) latest[name] = at;
    });
    // A hidden project whose last row left keeps its tab but moves, so the projects with rows
    // are part of the key as well as every tab's.
    var withRows = Object.keys(names).sort();
    Object.keys(hidden).forEach(function (name) { names[name] = true; });
    var key = JSON.stringify([withRows, Object.keys(names).sort()]);
    var bar = document.getElementById('front-tabs');
    if (!keptOrder || keptFor !== bar || keptKey !== key) {
      keptOrder = Object.keys(names).sort(function (a, b) {
        return (latest[b] || 0) - (latest[a] || 0) || (a < b ? -1 : a > b ? 1 : 0);
      });
      keptFor = bar;
      keptKey = key;
    }
    return {names: keptOrder.slice(), fresh: newByProject()};
  }

  // Keep the tabs under the top bar while the page scrolls; where the list scrolls in a box
  // of its own (a desktop Work page's side column), at the top of that box.
  function stickTabs(bar) {
    var top = 0;
    var box = bar.parentElement;
    while (box && box !== document.body && !/(auto|scroll)/.test(getComputedStyle(box).overflowY)) {
      box = box.parentElement;
    }
    if (!box || box === document.body) {
      var topbar = document.querySelector('.topbar');
      if (topbar && getComputedStyle(topbar).position === 'sticky') top = topbar.offsetHeight;
    }
    bar.style.setProperty('--front-tabs-top', top + 'px');
  }

  function projectTabs(hidden) {
    var bar = document.getElementById('front-tabs');
    if (!bar) return;
    var order = tabOrder(hidden);
    var off = Object.keys(hidden).length;
    // One project is nothing to filter, unless a hidden one has to be brought back.
    bar.hidden = order.names.length < 2 && off === 0;
    if (bar.hidden) return;
    stickTabs(bar);
    var shown = order.names.filter(function (name) { return !hidden[name]; });
    var counts = order.names.map(function (name) { return Number(order.fresh[name]) || 0; });
    var signature = JSON.stringify([order.names, counts, off ? shown : null]);
    if (bar.getAttribute('data-tabs') === signature) return;
    bar.setAttribute('data-tabs', signature);
    var row = bar.querySelector('.front-tabs-row');
    var scroll = row ? row.scrollLeft : 0;
    bar.textContent = '';
    row = document.createElement('div');
    row.className = 'front-tabs-row';
    bar.appendChild(row);
    order.names.concat(['']).forEach(function (name, index) {
      var tab = document.createElement('button');
      tab.type = 'button';
      tab.className = 'front-tab';
      tab.setAttribute('data-project-tab', name);
      tab.textContent = name || 'All';
      var on = name ? off > 0 && !hidden[name] : off === 0;
      tab.classList.toggle('on', on);
      tab.setAttribute('aria-pressed', on ? 'true' : 'false');
      var fresh = name ? counts[index] : 0;
      if (fresh) {
        var bubble = document.createElement('span');
        bubble.className = 'front-tab-new';
        bubble.textContent = String(fresh);
        bubble.setAttribute('aria-label', fresh + ' new');
        tab.appendChild(bubble);
      }
      tab.title = name ? 'Show only ' + name + '; hold to add or remove it' : 'Show every project';
      row.appendChild(tab);
    });
    row.scrollLeft = scroll;
  }

  function applyProjects() {
    var hidden = hiddenProjects();
    var rules = Object.keys(hidden).filter(function (name) { return hidden[name]; }).map(function (name) {
      return '.front-page [data-work-project=' + quoted(name) + ']{display:none !important}';
    });
    var style = document.getElementById('front-project-style');
    if (!style && rules.length) {
      style = document.createElement('style');
      style.id = 'front-project-style';
      document.head.appendChild(style);
    }
    if (style) style.textContent = rules.join('\n');
    // In the head there is no body yet: the stylesheet alone is what keeps the rows unpainted.
    if (!document.body || !document.body.classList.contains('front-page')) return;
    recountProjects(hidden);
    projectTabs(hidden);
  }


  function saveHidden(hidden) {
    try { localStorage.setItem(PROJECT_KEY, JSON.stringify(hidden)); } catch (_) {}
    applyProjects();
    document.dispatchEvent(new CustomEvent('agentdeck:front-projects'));
  }

  // The one project the tabs show alone, else "" — All, or several held together (#3253).
  function onlyProject() {
    var hidden = hiddenProjects();
    if (!Object.keys(hidden).length) return '';
    var shown = tabOrder(hidden).names.filter(function (name) { return !hidden[name]; });
    return shown.length === 1 ? shown[0] : '';
  }

  // A tap shows only that project; tapping the one already shown alone, or All, shows every
  // project again.
  function showOnly(name) {
    var hidden = hiddenProjects();
    var names = tabOrder(hidden).names;
    var next = {};
    var alone = names.every(function (other) { return other === name ? !hidden[other] : hidden[other]; });
    if (name && !alone) names.forEach(function (other) { if (other !== name) next[other] = true; });
    saveHidden(next);
  }

  // A hold adds that project to what is shown, or takes it away; never down to nothing.
  function toggleProject(name) {
    var hidden = hiddenProjects();
    var names = tabOrder(hidden).names;
    if (hidden[name]) {
      delete hidden[name];
    } else {
      hidden[name] = true;
    }
    if (names.every(function (other) { return hidden[other]; })) hidden = {};
    saveHidden(hidden);
  }

  var HOLD_MS = 450;
  var holdTimer = null;
  var held = null;

  document.addEventListener('pointerdown', function (e) {
    var tab = e.target && e.target.closest ? e.target.closest('[data-project-tab]') : null;
    clearTimeout(holdTimer);
    held = null;
    if (!tab || !tab.getAttribute('data-project-tab')) return;
    holdTimer = setTimeout(function () {
      // By name: the hold rebuilds the tabs, so the tap that ends it lands on a new button.
      held = tab.getAttribute('data-project-tab');
      if (navigator.vibrate) navigator.vibrate(15);
      toggleProject(tab.getAttribute('data-project-tab'));
    }, HOLD_MS);
  });
  ['pointerup', 'pointercancel'].forEach(function (type) {
    document.addEventListener(type, function () { clearTimeout(holdTimer); });
  });
  // Scrolling the tab line is not a hold.
  document.addEventListener('scroll', function () { clearTimeout(holdTimer); }, true);
  document.addEventListener('contextmenu', function (e) {
    if (e.target && e.target.closest && e.target.closest('[data-project-tab]')) e.preventDefault();
  });

  document.addEventListener('click', function (e) {
    var tab = e.target && e.target.closest ? e.target.closest('[data-project-tab]') : null;
    if (!tab) return;
    var wasHeld = held !== null && held === tab.getAttribute('data-project-tab');
    held = null;
    if (!wasHeld) showOnly(tab.getAttribute('data-project-tab'));
  });

  // Swiping the list sideways steps to the next or previous tab. A swipe that starts at the
  // screen's edge is left to the browser's own back gesture.
  var EDGE_PX = 24;
  var SWIPE_PX = 70;
  var swipe = null;

  function step(direction) {
    var hidden = hiddenProjects();
    var tabs = tabOrder(hidden).names.concat(['']);
    var shown = tabs.filter(function (name) { return name && !hidden[name]; });
    var all = tabs.length - 1;
    var at = Object.keys(hidden).length === 0 ? all : shown.length === 1 ? tabs.indexOf(shown[0]) : all;
    // All is last, where every swipe starts: it wraps round to the first project.
    showOnly(tabs[(at + direction + tabs.length) % tabs.length]);
    var tab = document.querySelector('#front-tabs .front-tab.on');
    if (tab && tab.scrollIntoView) tab.scrollIntoView({block: 'nearest', inline: 'center'});
  }

  document.addEventListener('touchstart', function (e) {
    swipe = null;
    var bar = document.getElementById('front-tabs');
    if (!bar || bar.hidden || e.touches.length !== 1) return;
    var t = e.target && e.target.closest ? e.target : null;
    if (!t || !t.closest('.front-page main') || t.closest('#front-tabs, input, textarea, select')) return;
    var x = e.touches[0].clientX;
    if (x < EDGE_PX || x > window.innerWidth - EDGE_PX) return;
    swipe = {x: x, y: e.touches[0].clientY};
  }, {passive: true});
  document.addEventListener('touchend', function (e) {
    if (!swipe) return;
    var dx = e.changedTouches[0].clientX - swipe.x;
    var dy = e.changedTouches[0].clientY - swipe.y;
    swipe = null;
    if (Math.abs(dx) >= SWIPE_PX && Math.abs(dy) * 2 < Math.abs(dx)) step(dx < 0 ? 1 : -1);
  }, {passive: true});

  function apply() {
    applyFolds();
    applyProjects();
  }

  // Run where this file loads, in the head: both stylesheets exist before the first row is
  // parsed, so a folded group or a hidden project is never painted first and hidden after.
  apply();
  window.AgentDeckFront = {apply: apply, onlyProject: onlyProject};
  document.addEventListener('DOMContentLoaded', apply);
  document.addEventListener('htmx:afterSwap', apply);
  // A desktop Work or chat page mounts the front page beside itself after loading (#3114).
  document.addEventListener('agentdeck:session-navigation', apply);
})();
