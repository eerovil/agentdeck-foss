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
//    the same store (`data-front-collapse`). Everything is open by default, except a
//    `<details>` marked `data-front-shut` (Other decks, #3834): shut until opened, which the
//    store keeps as `{key: false}`.
// 2. The project tabs (#3226). Each Work row names its project (`data-work-project`); a line
//    of tabs under the counts, pinned under the top bar, has one tab per project on the
//    page, the one with the newest activity first (#3399), each with its new count, and All
//    last. The order holds while the page is open: it is sorted again only on a load, or when
//    a project comes onto the page or leaves it, so a push never moves a tab under a finger. A tap shows only that project, a hold adds or removes it, and a sideways
//    swipe on the list steps to the next or previous tab, round the ends, except that on a
//    phone a swipe to the right on a Work's row opens its Work page (#4625). The hidden
//    projects persist in localStorage under
//    `agentdeck-front-hidden-projects` as `{project: true}`. Like folding it is a generated
//    stylesheet, so a pushed row is hidden before it paints; the group headers, their NEW
//    pills and the counts line are then recounted without the hidden rows, and the All Works
//    links (`data-all-works`) open the list on the one project shown alone (#3873).
// 3. The search (#4624): the icon before the tabs opens a field over them. A row shows when
//    every word is in its title, project or repo#number; folds open while it searches, and
//    it stays within the projects the tabs show. A pushed row is filtered in the swap that
//    brings it, before it paints. Finished Works that left the page come from
//    `/works/finished`, asked by the field itself.
// 4. The deck chat can switch the tabs on every open page (#3814, `view_commands.py`): its
//    command arrives as a `view` event in the top bar's hidden slot.
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

  // Whether a group is shut: its stored fold, else its default.
  function isShut(fold, key) {
    if (Object.prototype.hasOwnProperty.call(fold, key)) return Boolean(fold[key]);
    var details = document.querySelector('details[data-front-collapse="' + key + '"]');
    return Boolean(details && details.hasAttribute('data-front-shut'));
  }

  function applyFolds() {
    var fold = folded();
    var rules = [];
    var finding = query();
    Object.keys(fold).forEach(function (key) {
      if (finding) return;  // a search looks inside folded groups
      if (!fold[key] || !FOLD_NAME.test(key)) return;
      rules.push('[data-front-in="' + key + '"]{display:none !important}');
      rules.push('[data-front-group="' + key + '"] .fr-chev::before{content:"\\25B8"}');
      // Finish all (#4623) never takes rows that are folded away.
      rules.push('[data-front-group="' + key + '"] [data-finish-all]{display:none !important}');
    });
    var style = document.getElementById('front-fold-style');
    if (!style && rules.length) {
      style = document.createElement('style');
      style.id = 'front-fold-style';
      document.head.appendChild(style);
    }
    if (style) style.textContent = rules.join('\n');
    Array.prototype.forEach.call(document.querySelectorAll('[data-front-toggle]'), function (button) {
      button.setAttribute('aria-expanded', !finding && fold[button.getAttribute('data-front-toggle')] ? 'false' : 'true');
    });
    Array.prototype.forEach.call(document.querySelectorAll('details[data-front-collapse]'), function (details) {
      var open = !isShut(fold, details.getAttribute('data-front-collapse'));
      if (details.open !== open) details.open = open;
    });
  }

  function setFold(key, shut) {
    if (!FOLD_NAME.test(key)) return;
    var fold = folded();
    var details = document.querySelector('details[data-front-collapse="' + key + '"]');
    if (details && details.hasAttribute('data-front-shut')) fold[key] = Boolean(shut);
    else if (shut) fold[key] = true;
    else delete fold[key];
    try { localStorage.setItem(FOLD_KEY, JSON.stringify(fold)); } catch (_) {}
    applyFolds();
    showFinishAll();
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
    pressed = head && !e.target.closest('a, [data-finish-all]')
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
    if (!head || head.getAttribute('data-front-group') !== press.key || e.target.closest('a, [data-finish-all]')) return;
    released = {key: press.key, at: Date.now()};
    if (!query()) setFold(press.key, !folded()[press.key]);  // a search holds every group open
  }, true);

  document.addEventListener('click', function (e) {
    var head = e.target && e.target.closest ? e.target.closest('[data-front-group]') : null;
    if (!head || e.target.closest('a, [data-finish-all]')) return;
    var key = head.getAttribute('data-front-group');
    // A browser that does click the redrawn header after its release folded it.
    var late = released;
    released = null;
    if (late && late.key === key && Date.now() - late.at < RELEASE_MS) return;
    if (!query()) setFold(key, !folded()[key]);
  });

  document.addEventListener('toggle', function (e) {
    var details = e.target;
    if (!details || !details.matches || !details.matches('details[data-front-collapse]')) return;
    var key = details.getAttribute('data-front-collapse');
    if (isShut(folded(), key) !== !details.open) setFold(key, !details.open);
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

  // Show older's rows (#4289) are hidden by project like the rest, but name no tab: an old
  // project with nothing current must not get one by being scrolled past.
  function projectRows() {
    return Array.prototype.slice.call(
      document.querySelectorAll('.front-page [data-work-project]:not([data-front-in="older"])'));
  }

  function shown(el, hidden) {
    return !hidden[el.getAttribute('data-work-project')] && found(el);
  }

  function query() {
    var box = document.getElementById('front-find-input');
    return box && !box.form.hidden ? box.value.trim().toLowerCase() : '';
  }

  // The same rule as the server's finished search (`routes_works._matches`).
  function found(row) {
    var words = query();
    if (!words) return true;
    var title = row.querySelector('.fr-title');
    var text = [title ? title.textContent : '', row.getAttribute('data-work-id'),
      row.getAttribute('data-work-project') || ''].join(' ').toLowerCase();
    return words.split(/\s+/).every(function (word) { return text.indexOf(word) !== -1; });
  }

  function applySearch(hidden) {
    var bar = document.getElementById('front-bar');
    var note = document.getElementById('front-find-note');
    var style = document.getElementById('front-find-style');
    var finding = query();
    if (!style && finding) {
      style = document.createElement('style');
      style.id = 'front-find-style';
      document.head.appendChild(style);
    }
    if (bar) bar.classList.toggle('finding', !!finding);
    var rules = [];
    var here = 0;
    var elsewhere = 0;
    var counted = {};  // a starred Work is a row in Starred and in its own group (#4745)
    if (finding) {
      Array.prototype.forEach.call(document.querySelectorAll(
        '.front-page .front-sec:not(.front-stopped) article[data-work-id]:not([data-front-in="older"]):not([data-work-finishing])'
      ), function (row) {
        var id = row.getAttribute('data-work-id');
        if (!found(row)) {
          rules.push('.front-sec article[data-work-id=' + quoted(id) +
            ']:not([data-front-in="older"]){display:none !important}');
          return;
        }
        if (counted[id]) return;
        counted[id] = true;
        if (hidden[row.getAttribute('data-work-project')]) elsewhere += 1;
        else here += 1;
      });
    }
    if (style) style.textContent = rules.join('\n');
    if (!note) return;
    // Redrawn only when it changed: it is read out, and its button may hold the focus.
    var said = finding ? here + (here === 1 ? ' current Work' : ' current Works') : '';
    var more = !finding || !Object.keys(hidden).length ? ''
      : elsewhere ? elsewhere + ' more in other projects' : 'Search every project';
    if (note.getAttribute('data-said') === said + '|' + more) return;
    note.setAttribute('data-said', said + '|' + more);
    note.hidden = !said;
    note.textContent = said + (more ? ' · ' : '');
    if (!more) return;
    var all = document.createElement('button');
    all.type = 'button';
    all.setAttribute('data-find-all', '');
    all.textContent = more;
    note.appendChild(all);
  }

  // Show older's button knows no query: it is away until the search's answer brings its own.
  // The rows it loaded go at the first letter, not when the answer comes.
  var searching = false;
  function holdOlder() {
    var rows = document.getElementById('front-older-rows');
    var more = document.getElementById('front-older-more');
    if (query() && more) more.hidden = true;
    if (query() && !searching && rows) rows.textContent = '';
    searching = !!query();
  }

  var narrowed = null;  // the tabs' choice before "more in other projects", put back on close

  function openSearch() {
    var form = document.getElementById('front-find');
    var button = document.querySelector('[data-find-open]');
    if (!form || !button) return;
    var box = document.getElementById('front-find-input');
    box.value = '';  // a reload may have put back what was typed
    form.hidden = false;
    button.setAttribute('aria-expanded', 'true');
    box.focus();
  }

  function closeSearch() {
    var form = document.getElementById('front-find');
    var button = document.querySelector('[data-find-open]');
    var box = document.getElementById('front-find-input');
    if (!form || !button || !box) return;
    var searched = query();
    box.value = '';
    form.hidden = true;
    searching = false;
    button.setAttribute('aria-expanded', 'false');
    button.focus();
    apply();
    // Emptied, the finished rows it brought go too; a closed empty field leaves Show older's.
    if (searched) resetOlder();
    var back = narrowed;
    narrowed = null;
    if (back && !Object.keys(hiddenProjects()).length) saveHidden(back);
  }

  document.addEventListener('input', function (e) {
    if (e.target && e.target.id === 'front-find-input') { apply(); holdOlder(); }
  });
  document.addEventListener('keydown', function (e) {
    if (!e.target || e.target.id !== 'front-find-input') return;
    // Enter only puts the phone's keyboard away: the list is filtered already.
    if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); e.target.blur(); }
  });
  // Escape closes the search from anywhere on the list, and from the body, where focus lands
  // when a push or the note takes the focused control away. Anything else it would close (a
  // menu, a dialog, the diff) goes first, so this looks before those handlers run.
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape' || e.isComposing) return;
    var form = document.getElementById('front-find');
    var t = e.target;
    if (!form || form.hidden || form.closest('[inert]') || !t || !t.closest) return;
    if (t !== document.body && !form.closest('.session-sidebar, main').contains(t)) return;
    if (t.id !== 'front-find-input' && t.closest('input, textarea, select')) return;
    if (document.querySelector('dialog[open], .needs-you-menu[open], [data-composer-menu][open], .diff-open')) return;
    e.preventDefault();
    closeSearch();
  }, true);
  document.addEventListener('click', function (e) {
    var t = e.target && e.target.closest ? e.target : null;
    if (!t) return;
    if (t.closest('[data-find-open]')) openSearch();
    else if (t.closest('[data-find-close]')) closeSearch();
    else if (t.closest('[data-find-all]')) { narrowed = hiddenProjects(); saveHidden({}); }
  });

  // The rows Finish all takes (#4623): on the shown projects, matching the search, not folded away, and not being
  // finished already. Its button shows while there are two.
  function finishable() {
    var hidden = hiddenProjects();
    if (!query() && isShut(folded(), 'issue_closed')) return [];  // a search unfolds it
    return Array.prototype.filter.call(
      document.querySelectorAll('article[data-front-in="issue_closed"][data-finish-id]:not([data-work-finishing])'),
      function (row) { return shown(row, hidden); });
  }

  function showFinishAll() {
    var few = finishable().length < 2;
    Array.prototype.forEach.call(document.querySelectorAll('[data-finish-all]'), function (button) {
      button.hidden = few;
    });
  }

  function recountProjects(hidden) {
    showFinishAll();
    var any = Object.keys(hidden).length > 0 || !!query();
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
      // Recently finished stays even when empty (#4289): Show older is under it.
      head.hidden = !head.hasAttribute('data-front-keep') && (any || !!held) && rows.length === 0;
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
  function stickTabs() {
    var bar = document.getElementById('front-bar');
    if (!bar) return;
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
    stickTabs();
    var order = tabOrder(hidden);
    var off = Object.keys(hidden).length;
    // One project is nothing to filter, unless a hidden one has to be brought back.
    bar.hidden = order.names.length < 2 && off === 0;
    if (bar.hidden) return;
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
    applySearch(hidden);
    recountProjects(hidden);
    projectTabs(hidden);
    allWorksLinks();
  }

  // The All Works links open the list on the project the tabs show alone (#3873); All, or
  // several projects held together, open it whole. The count-bearing "more active" and
  // "quiet" links count every project, so they stay whole.
  function allWorksLinks() {
    if (!document.body || !document.body.classList.contains('front-page')) return;
    var project = onlyProject();
    var href = project ? '/works?project=' + encodeURIComponent(project) : '/works';
    Array.prototype.forEach.call(document.querySelectorAll('.front-page a[data-all-works]'), function (link) {
      if (link.getAttribute('href') !== href) link.setAttribute('href', href);
    });
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

  // On a phone a row's tap opens its chat, so swiping the row to the right opens its Work page
  // (#4625): the row follows the finger and uncovers a "Work" label, which lights up once
  // letting go will open it. A swipe to the left on a row still steps the tabs.
  var LOCK_PX = 10;
  var ROW_PX = 90;
  var phone = window.matchMedia('(max-width: 999px)');

  function swipeRow(target) {
    var row = phone.matches ? target.closest('main .fr') : null;
    return row && row.querySelector('a.fr-body[href^="/works/"]') ? row : null;
  }

  function dropSwipe() {
    var row = swipe && swipe.row;
    swipe = null;
    if (!row) return;
    row.classList.remove('fr-swiping', 'fr-swipe-ready');
    row.style.removeProperty('--swipe-x');
    if (window.AgentDeckRows) window.AgentDeckRows.settle(row);
  }

  document.addEventListener('touchstart', function (e) {
    dropSwipe();
    if (e.touches.length !== 1) return;
    var t = e.target && e.target.closest ? e.target : null;
    if (!t || !t.closest('.front-page main') || t.closest('#front-bar, input, textarea, select')) return;
    var x = e.touches[0].clientX;
    if (x < EDGE_PX || x > window.innerWidth - EDGE_PX) return;
    var bar = document.getElementById('front-tabs');
    var row = swipeRow(t);
    var tabs = Boolean(bar && !bar.hidden && !query());
    if (row || tabs) swipe = {x: x, y: e.touches[0].clientY, row: row, tabs: tabs, way: ''};
  }, {passive: true});

  document.addEventListener('touchmove', function (e) {
    if (!swipe || !swipe.row) return;
    if (e.touches.length !== 1) { dropSwipe(); return; }
    var dx = e.touches[0].clientX - swipe.x;
    var dy = Math.abs(e.touches[0].clientY - swipe.y);
    if (!swipe.way) {
      if (Math.max(Math.abs(dx), dy) < LOCK_PX) return;
      swipe.way = dx > dy ? 'row' : 'other';
    }
    if (swipe.way !== 'row') return;
    swipe.row.classList.add('fr-swiping');
    swipe.row.classList.toggle('fr-swipe-ready', dx >= SWIPE_PX && dy * 2 < dx);
    swipe.row.style.setProperty('--swipe-x', Math.max(0, Math.min(dx, ROW_PX)) + 'px');
  }, {passive: true});

  document.addEventListener('touchend', function (e) {
    if (!swipe) return;
    var done = swipe;
    var dx = e.changedTouches[0].clientX - done.x;
    var dy = e.changedTouches[0].clientY - done.y;
    var link = done.row && done.row.querySelector('a.fr-body');
    dropSwipe();
    var far = Math.abs(dx) >= SWIPE_PX;
    var flat = Math.abs(dy) * 2 < Math.abs(dx);
    if (done.row && (done.way === 'row' || dx > 0)) {
      // A fast swipe can be sampled as only a start and an end, so one that never chose a
      // way is judged by where it ended. One that started off the row's way opens nothing.
      var nav = window.AgentDeckSessionNavigation;
      if (done.way !== 'other' && dx > 0 && far && flat && nav) {
        if (navigator.vibrate) navigator.vibrate(15);
        nav.openRowWork(link);
      }
      return;
    }
    if (done.tabs && far && flat) step(dx < 0 ? 1 : -1);
  }, {passive: true});
  document.addEventListener('touchcancel', dropSwipe, {passive: true});

  // The deck chat's view change (#3814): every open page shows one project, or All, as a tap
  // on its tab would, but setting rather than toggling, so the same command twice is the same
  // view. `projects` is every project the deck knows, so a page whose list is not mounted
  // still hides the others.
  var lastCommand = '';

  document.addEventListener('htmx:sseMessage', function (e) {
    var slot = e.target;
    if (!slot || slot.id !== 'view-command-slot') return;
    var el = slot.querySelector('[data-view-command]');
    var command;
    try { command = el ? JSON.parse(el.getAttribute('data-view-command')) : null; } catch (_) { command = null; }
    if (!command || !command.id || command.id === lastCommand) return;
    lastCommand = command.id;
    var name = String(command.project || '');
    var next = {};
    if (name) {
      tabOrder(hiddenProjects()).names.concat(command.projects || []).forEach(function (other) {
        if (other !== name) next[other] = true;
      });
    }
    saveHidden(next);
    var tab = document.querySelector('#front-tabs .front-tab.on');
    if (tab && tab.scrollIntoView) tab.scrollIntoView({block: 'nearest', inline: 'center'});
  });

  function apply() {
    applyFolds();
    applyProjects();
  }

  // Show older (#4289) and the search (#4624) ask for the one project the tabs show alone, so a page
  // is ten rows of that project; with several held together the rows of the others are
  // hidden like any row. A tab change empties what was loaded and starts the button over.
  document.addEventListener('htmx:configRequest', function (e) {
    var el = e.detail && e.detail.elt;
    if (!el || !el.closest || !el.closest('[data-front-older]')) return;
    var project = onlyProject();
    if (project) e.detail.parameters.project = project;
    else delete e.detail.parameters.project;
  });

  function resetOlder() {
    var rows = document.getElementById('front-older-rows');
    var more = document.getElementById('front-older-more');
    if (!rows || !more) return;
    rows.textContent = '';
    more.hidden = false;
    more.setAttribute('hx-get', '/works/finished');
    more.setAttribute('hx-target', '#front-older-rows');
    more.setAttribute('hx-swap', 'beforeend');
    if (window.htmx) window.htmx.process(more);
    // Supersedes a search still waiting or on its way, and asks again in the new tab.
    var box = document.getElementById('front-find-input');
    if (window.htmx && box) {
      window.htmx.trigger('#front-bar', 'htmx:abort');
      holdOlder();
      if (query()) window.htmx.trigger(box, 'search');
    }
  }
  document.addEventListener('agentdeck:front-projects', resetOlder);

  // Run where this file loads, in the head: both stylesheets exist before the first row is
  // parsed, so a folded group or a hidden project is never painted first and hidden after.
  apply();
  window.AgentDeckFront = {
    apply: apply, onlyProject: onlyProject, finishable: finishable, showFinishAll: showFinishAll
  };
  // A live push redraws the links with a plain `/works`, so each one is set again as it is
  // reached for, by press, keyboard or a long-press's open in new tab, as well as after a swap.
  ['pointerdown', 'focusin', 'click'].forEach(function (type) {
    document.addEventListener(type, function (e) {
      if (e.target && e.target.closest && e.target.closest('a[data-all-works]')) allWorksLinks();
    }, true);
  });
  document.addEventListener('htmx:sseMessage', allWorksLinks);
  document.addEventListener('DOMContentLoaded', apply);
  document.addEventListener('htmx:afterSwap', apply);
  // A desktop Work or chat page mounts the front page beside itself after loading (#3114).
  document.addEventListener('agentdeck:session-navigation', apply);
})();
