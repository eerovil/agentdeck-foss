(function () {
  'use strict';

  // Extra desktop panes are independent documents. Their parent owns pane
  // selection, while ordinary links inside the frame keep normal browser
  // navigation instead of trying to build a sidebar that the compact page omits.
  if (document.body.classList.contains('session-pane-page')) return;

  var mobile = window.matchMedia('(max-width: 999px)');
  var requestGeneration = 0;
  var LIST_STATE = 'list';
  var CHAT_STATE = 'chat';
  var WORK_STATE = 'work';
  var STATE_KEY = 'agentdeckMobileLayer';
  // Whether this document arrived on one of its own history entries (a reload, Back into
  // it) rather than fresh. Read before the load below stamps the entry it arrived on.
  var arrivedOnEntry = Boolean(history.state && history.state[STATE_KEY]);

  function setTitle(title) {
    if (window.AgentDeckWindowTitle) AgentDeckWindowTitle.setBase(title);
    else document.title = title;
  }

  function state(layer, path) {
    var next = Object.assign({}, history.state || {});
    next[STATE_KEY] = layer;
    next.agentdeckPath = path;
    return next;
  }

  function selectedKey(path) {
    var match = path.match(/^\/sessions\/([^/?#]+)/);
    if (match) return decodeURIComponent(match[1]);
    if (!/^\/chats\/[^/?#]+/.test(path)) return null;
    var detail = document.querySelector('.session-detail[data-session-key]');
    if (detail) return detail.dataset.sessionKey;
    var card = Array.from(document.querySelectorAll('#sessions a.session')).find(
      function (candidate) {
        return new URL(candidate.href, location.href).pathname === path;
      }
    );
    return card ? card.dataset.sessionKey : null;
  }

  function syncSelectedRows(path, selectedSessionKey) {
    var key = selectedSessionKey || selectedKey(path);
    document.querySelectorAll('#sessions a.session').forEach(function (row) {
      var selected = row.dataset.sessionKey === key;
      row.classList.toggle('selected', selected);
      var link = row.matches('a') ? row : row.querySelector('a[data-session-key]');
      if (!link) return;
      if (selected) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
  }

  // Where closing a chat goes, and over which stream. A chat page's own sidebar holds no
  // list (#2603) and says `/` (session.html); a page a chat was opened over in-page — the
  // Works, a Work, the chats view — is recorded as it is promoted (#2256).
  function listLayer() {
    var sidebar = document.querySelector('.session-layout > .session-sidebar');
    return {
      path: (sidebar && sidebar.dataset.listPath) || '/',
      stream: (sidebar && sidebar.dataset.listStream) || '/events'
    };
  }

  function dispatchNavigation() {
    document.dispatchEvent(new CustomEvent('agentdeck:session-navigation'));
  }

  function switchStream(url, force) {
    if (!url || (!force && document.body.getAttribute('sse-connect') === url)) return;
    if (window.htmx) htmx.trigger(document.body, 'htmx:beforeCleanupElement');
    document.body.setAttribute('sse-connect', url);
    if (window.htmx && htmx.reconnectSSE) htmx.reconnectSSE(document.body);
  }

  // The deck scrolls the document; a chat page's deck is a sidebar that scrolls
  // itself, and `body.session-page` stops the document scrolling at all. So the
  // two conversions below move the reader's place from one scroller to the
  // other. Measuring an anchor the move carries along, rather than copying the
  // number across, absorbs the sticky topbar and the padding each side adds
  // (#2091).
  function anchorFor(root) {
    return (root && root.querySelector('#sessions')) || null;
  }

  function keepPlace(anchor, top, apply) {
    if (!anchor || top == null || !anchor.isConnected) return;
    apply(anchor.getBoundingClientRect().top - top);
  }

  // Where the deck's place has to be put back once the new page is standing.
  // Set while the document is still the scroller and read after `session-page`
  // has taken that scrolling away, because losing it moves the content again.
  var pendingPlace = null;

  function ensureLayout() {
    var main = document.querySelector('main');
    var layout = main.querySelector('.session-layout');
    if (layout) return layout;
    // Opened from the deck: everything in `main` becomes the sidebar's content,
    // and without this the reader is dropped at the top of a list they had
    // scrolled — the further down the Deckhand panel the row was, the bigger
    // the jump.
    var anchor = anchorFor(main);
    var anchorTop = anchor ? anchor.getBoundingClientRect().top : null;
    layout = document.createElement('div');
    layout.className = 'session-layout';
    var sidebar = document.createElement('aside');
    sidebar.className = 'session-sidebar';
    sidebar.setAttribute('aria-label', 'All sessions');
    sidebar.dataset.listPath = location.pathname + location.search;
    sidebar.dataset.listStream = document.body.getAttribute('sse-connect') || '/events';
    while (main.firstChild) sidebar.appendChild(main.firstChild);
    layout.appendChild(sidebar);
    main.appendChild(layout);
    pendingPlace = {anchor: anchor, top: anchorTop, sidebar: sidebar};
    if (!mobile.matches && document.body.classList.contains('front-page')) {
      frontColumns(layout, sidebar);
    }
    return layout;
  }

  // Desktop: the front page, one Work and one chat side by side (#2290). Opened
  // over the front page, the front page stays live as the left column, a Work
  // takes the middle one and a chat the right one; the diff, when open, is a
  // fourth. The body's stream follows the chat, as it always has, so the other
  // two columns each keep a stream of their own: the front page its Works, and
  // the Work its sections. Mobile never builds this.
  function frontColumns(layout, sidebar) {
    layout.classList.add('front-columns');
    document.body.classList.add('front-columns-page');
    layout.appendChild(emptyWorkColumn());
    // The front page's live parts were bound to the body's stream when the page
    // loaded. Their own stream binds them again once it opens, and the body's is
    // restarted so the old binding goes with it instead of swapping each push twice.
    ownStream(sidebar, sidebar.dataset.listStream);
    if (window.htmx && htmx.reconnectSSE) htmx.reconnectSSE(document.body, true);
  }

  function ownStream(column, url) {
    column.setAttribute('sse-connect', url);
    if (window.htmx && htmx.reconnectSSE) htmx.reconnectSSE(column);
  }

  function emptyWorkColumn() {
    var column = document.createElement('section');
    column.className = 'work-column';
    column.setAttribute('aria-label', 'Selected Work');
    var note = document.createElement('p');
    note.className = 'work-column-empty';
    note.textContent = 'Open a Work to see it here.';
    column.appendChild(note);
    return column;
  }

  // `htmx.remove` only detaches the node: it runs none of htmx's cleanup, so a stream
  // the node or anything in it held stays open. A Work's stream sends nothing but
  // comments while the Work is unchanged, so the SSE extension never notices its
  // element is gone, and every Work opened and left kept a stream rebuilding its page
  // on the server until the deck crawled (#2359). Close them first.
  function removeNode(node) {
    if (window.htmx) {
      [node].concat(Array.from(node.querySelectorAll('*'))).forEach(function (element) {
        htmx.trigger(element, 'htmx:beforeCleanupElement');
      });
    }
    node.remove();
  }

  function rowPath(row) {
    var link = row.querySelector('a.fr-body');
    return link ? new URL(link.href, location.href).pathname : null;
  }

  // The open Work's row on the front page, marked the way an open chat's card is.
  function syncOpenWork() {
    var column = document.querySelector('.session-layout > .work-column');
    var work = column && column.querySelector('.work-main[data-work-id]');
    var id = work ? work.dataset.workId : null;
    // A Work still loading (#2422) has no page yet to name it; its row is the one it opens.
    var loading = column && column.dataset.workLoading;
    document.querySelectorAll('.front-columns > .session-sidebar .fr[data-work-id]').forEach(
      function (row) {
        var open = loading ? rowPath(row) === loading : row.dataset.workId === id;
        row.classList.toggle('is-open', open);
        var link = row.querySelector('a.fr-body');
        if (!link) return;
        if (open) link.setAttribute('aria-current', 'page');
        else link.removeAttribute('aria-current');
      }
    );
  }

  // Scripts a fetched page names in its content (the new-chat model picker) are
  // inert once parsed; load any this document does not already have.
  function loadScripts(root) {
    root.querySelectorAll('script[src]').forEach(function (script) {
      var src = script.getAttribute('src');
      var known = Array.from(document.querySelectorAll('script[src]')).some(function (other) {
        return other !== script && other.getAttribute('src') === src;
      });
      if (known) return;
      var fresh = document.createElement('script');
      fresh.src = src;
      document.head.appendChild(fresh);
    });
  }

  function isWorkPath(path) {
    return /^\/works\/[^?#]+$/.test(path) && !/\/(action|message|visibility)$/.test(path);
  }

  // The chat column empties, and the body's stream goes back to the front page's.
  function dropChat() {
    var detail = document.querySelector('.session-layout > .session-detail');
    if (detail) removeNode(detail);
    dropDiff();
    document.body.classList.remove('session-stack-page');
  }

  // The chat a Work opens beside itself on a desktop (#2345), named by the Work page.
  function workChat(column) {
    var work = column && column.querySelector('.work-main[data-open-chat]');
    if (!work || mobile.matches) return null;
    var url = new URL(work.dataset.openChat, location.href);
    if (url.origin !== location.origin) return null;
    return {key: work.dataset.openChatKey, path: url.pathname};
  }

  function shownChatKey() {
    var detail = document.querySelector('.session-layout > .session-detail');
    return detail ? detail.dataset.sessionKey : null;
  }

  // The Work a click is still loading (#2422). Every load takes a new generation and drops
  // the one before, so a second click on the same row threw away the answer about to land,
  // and clicking faster than the Work loads never opened it. A repeat joins the load instead.
  var workLoad = null;

  function openWork(path, updateHistory) {
    if (workLoad && workLoad.path === path && workLoad.generation === requestGeneration) {
      return workLoad.done;
    }
    var load = {path: path, generation: requestGeneration + 1};
    load.done = loadWork(path, updateHistory).finally(function () {
      if (workLoad === load) workLoad = null;
    });
    workLoad = load;
    return load.done;
  }

  // The Work column a click fills at once, before the Work has arrived (#2422): the row
  // is marked open and the address moves, so the press is answered however slow the Work.
  function loadingWorkColumn(path, row) {
    var column = document.createElement('section');
    column.className = 'work-column';
    column.setAttribute('aria-label', 'Selected Work');
    column.setAttribute('aria-busy', 'true');
    column.dataset.workLoading = path;
    var note = document.createElement('p');
    note.className = 'work-column-loading';
    note.setAttribute('role', 'status');
    var spinner = document.createElement('span');
    spinner.className = 'send-spinner';
    spinner.setAttribute('aria-hidden', 'true');
    var title = row && row.querySelector('.fr-title');
    var label = document.createElement('span');
    label.textContent = 'Opening ' + (title ? title.textContent.trim() : 'the Work') + '…';
    note.append(spinner, label);
    column.appendChild(note);
    return column;
  }

  function showWorkLoading(path, updateHistory) {
    var layout = ensureLayout();
    if (!layout.classList.contains('front-columns')) return false;
    var row = Array.from(layout.querySelectorAll('.session-sidebar .fr[data-work-id]'))
      .find(function (candidate) { return rowPath(candidate) === path; });
    var column = layout.querySelector(':scope > .work-column');
    var fresh = loadingWorkColumn(path, row);
    layout.insertBefore(fresh, column);
    if (column) removeNode(column);
    dropChat();
    document.body.classList.add('session-page');
    document.body.classList.remove('session-list-page', 'mobile-list-open');
    settlePlace();
    syncOpenWork();
    if (updateHistory) history.pushState(state(WORK_STATE, path), '', path);
    return true;
  }

  async function loadWork(path, updateHistory) {
    var generation = ++requestGeneration;
    var loading = null;
    var pushed = false;
    var from = location.pathname;
    var parent = null;
    try {
      var layout = document.querySelector('.session-layout.front-columns');
      var column = layout && layout.querySelector(':scope > .work-column');
      if (!column || column.dataset.workPath !== path) {
        var response = fetch(path, {headers: {'Accept': 'text/html'}});
        if (showWorkLoading(path, updateHistory)) {
          loading = document.querySelector('.front-columns > .work-column[data-work-loading]');
          pushed = updateHistory;
          updateHistory = false;
        }
        response = await response;
        if (!response.ok) throw new Error('work request failed: ' + response.status);
        var parsed = new DOMParser().parseFromString(await response.text(), 'text/html');
        var incoming = parsed.querySelector('main.work-main');
        // A Work page has no stream of its own since #2864; one that still names one keeps it.
        var stream = parsed.body.getAttribute('sse-connect');
        if (!incoming) throw new Error('work page missing from response');
        if (generation !== requestGeneration) return settleLoading(loading);
        // Opened from anywhere but its parent, a sub-issue's Work has the parent's beneath it.
        parent = parentPath(incoming);
        if (parent === from || parent === path) parent = null;
        if (parent && pushed) {
          history.replaceState(state(WORK_STATE, parent), '', parent);
          history.pushState(state(WORK_STATE, path), '', path);
        }
        layout = ensureLayout();
        if (!layout.classList.contains('front-columns')) throw new Error('no front columns');
        var fresh = document.createElement('section');
        fresh.className = 'work-column';
        fresh.setAttribute('aria-label', 'Selected Work');
        fresh.dataset.workPath = path;
        fresh.dataset.workTitle = parsed.title || 'agentdeck';
        if (stream) fresh.setAttribute('sse-connect', stream);
        fresh.appendChild(incoming);
        column = layout.querySelector(':scope > .work-column');
        layout.insertBefore(fresh, column);
        if (column) removeNode(column);
        column = fresh;
        if (window.htmx) htmx.process(fresh);
      } else if (generation !== requestGeneration) {
        return;
      }
      var title = column.dataset.workTitle;
      // Back to a Work whose own chat is still open beside it leaves that chat as it is.
      var chat = workChat(column);
      var keep = Boolean(chat && chat.key && shownChatKey() === chat.key);
      if (!keep) dropChat();
      document.body.classList.add('session-page');
      document.body.classList.remove('session-list-page', 'mobile-list-open');
      settlePlace();
      if (title) setTitle(title);
      syncSelectedRows(path, keep ? chat.key : undefined);
      syncOpenWork();
      if (updateHistory && parent) history.pushState(state(WORK_STATE, parent), '', parent);
      if (updateHistory) history.pushState(state(WORK_STATE, path), '', path);
      if (!keep) switchStream(listLayer().stream);
      dispatchNavigation();
      if (chat && !keep) openSession(null, chat.path, false, {beside: true});
    } catch (_) {
      if (generation !== requestGeneration) return settleLoading(loading);
      // The loading column already put the Work's address on the history.
      if (pushed) location.replace(path);
      else location.assign(path);
    }
  }

  // A load a later navigation overtook leaves no spinner behind: a chat opened meanwhile
  // shows beside an empty Work column, and another Work's load has already replaced it.
  function settleLoading(loading) {
    if (!loading || !loading.isConnected) return;
    loading.parentNode.replaceChild(emptyWorkColumn(), loading);
    syncOpenWork();
  }

  // A Work page loaded on its own (a reload, a shared link) gets the front page
  // beside it, and a Back underneath it that returns to the front page in place.
  async function mountFrontBesideWork() {
    var main = document.querySelector('main');
    var work = main && main.querySelector(':scope > .work-main');
    if (!work) return;
    var path = location.pathname;
    var generation = ++requestGeneration;
    try {
      var response = await fetch('/', {headers: {'Accept': 'text/html'}});
      if (!response.ok) throw new Error('front page request failed: ' + response.status);
      var parsed = new DOMParser().parseFromString(await response.text(), 'text/html');
      var front = parsed.querySelector('main');
      var frontStream = parsed.body.getAttribute('sse-connect') || '/events?view=works';
      if (!front || generation !== requestGeneration || !work.isConnected) return;
      var layout = document.createElement('div');
      layout.className = 'session-layout front-columns';
      var sidebar = document.createElement('aside');
      sidebar.className = 'session-sidebar';
      sidebar.setAttribute('aria-label', 'Works');
      sidebar.dataset.listPath = '/';
      sidebar.dataset.listStream = frontStream;
      sidebar.setAttribute('sse-connect', frontStream);
      while (front.firstChild) sidebar.appendChild(front.firstChild);
      var column = document.createElement('section');
      column.className = 'work-column';
      column.setAttribute('aria-label', 'Selected Work');
      column.dataset.workPath = path;
      column.dataset.workTitle = document.title;
      var workStream = document.body.getAttribute('sse-connect');
      column.appendChild(work);
      layout.appendChild(sidebar);
      layout.appendChild(column);
      main.appendChild(layout);
      document.body.classList.remove('work-page');
      document.body.classList.add('session-page', 'front-page', 'front-columns-page');
      if (window.htmx) htmx.process(sidebar);
      // The Work's parts are bound to the body's stream; its own binds them again.
      if (workStream) ownStream(column, workStream);
      switchStream(frontStream);
      loadScripts(sidebar);
      syncOpenWork();
      if (!(history.state && history.state[STATE_KEY] === WORK_STATE)) {
        var parent = parentPath(work);
        if (parent && cameFrom(parent)) {
          // Straight from its parent's Work, that page's own entry is already beneath it.
          history.replaceState(state(WORK_STATE, path), '', location.href);
        } else if (parent && cameFrom('/')) {
          // From the front page, its own entry is beneath: the parent's takes this one.
          history.replaceState(state(WORK_STATE, parent), '', parent);
          history.pushState(state(WORK_STATE, path), '', path);
        } else {
          history.replaceState(state(LIST_STATE, '/'), '', '/');
          if (parent) history.pushState(state(WORK_STATE, parent), '', parent);
          history.pushState(state(WORK_STATE, path), '', path);
        }
      }
      dispatchNavigation();
      var chat = workChat(column);
      if (chat) openSession(null, chat.path, false, {beside: true});
    } catch (_) {
      // The Work page alone is still the whole Work.
    }
  }

  // A chat page loaded on its own (a reload, a notification, a shared link) is laid out
  // the way opening it from the front page lays it out (#2346): the front page on the
  // left, the Work that owns the chat in the middle, and the chat on the right, with Back
  // stepping chat → Work → front page. The page's own chats sidebar gives way to the front
  // page; if either read fails the chat page stays exactly as it loaded.
  async function mountFrontBesideChat() {
    var layout = document.querySelector('main > .session-layout');
    var sidebar = layout && layout.querySelector(':scope > .session-sidebar');
    var detail = layout && layout.querySelector(':scope > .session-detail');
    if (!sidebar || !detail || layout.classList.contains('front-columns')) return;
    var workPath = sidebar.dataset.workPath || '';
    var chatPath = location.pathname + location.search + location.hash;
    var generation = ++requestGeneration;
    async function page(path) {
      var response = await fetch(path, {headers: {'Accept': 'text/html'}});
      if (!response.ok) throw new Error('request failed: ' + response.status);
      return new DOMParser().parseFromString(await response.text(), 'text/html');
    }
    try {
      var reads = await Promise.all([page('/'), workPath ? page(workPath) : null]);
      var front = reads[0].querySelector('main');
      var frontStream = reads[0].body.getAttribute('sse-connect') || '/events?view=works';
      var work = reads[1] && reads[1].querySelector('main.work-main');
      var workStream = reads[1] && reads[1].body.getAttribute('sse-connect');
      if (!front || (workPath && !work)) throw new Error('page missing');
      if (generation !== requestGeneration || !detail.isConnected) return;
      // The chats list goes, with the live bindings it held on the chat's stream.
      if (window.htmx) htmx.trigger(sidebar, 'htmx:beforeCleanupElement');
      while (sidebar.firstChild) sidebar.removeChild(sidebar.firstChild);
      while (front.firstChild) sidebar.appendChild(front.firstChild);
      sidebar.setAttribute('aria-label', 'Works');
      sidebar.dataset.listPath = '/';
      sidebar.dataset.listStream = frontStream;
      delete sidebar.dataset.listEmpty;
      delete sidebar.dataset.workPath;
      sidebar.setAttribute('sse-connect', frontStream);
      var column;
      if (work) {
        column = document.createElement('section');
        column.className = 'work-column';
        column.setAttribute('aria-label', 'Selected Work');
        column.dataset.workPath = workPath;
        column.dataset.workTitle = reads[1].title || 'agentdeck';
        if (workStream) column.setAttribute('sse-connect', workStream);
        column.appendChild(work);
      } else {
        column = emptyWorkColumn();
      }
      layout.insertBefore(column, sidebar.nextSibling);
      layout.classList.add('front-columns');
      document.body.classList.add('front-page', 'front-columns-page');
      if (window.htmx) {
        htmx.process(sidebar);
        htmx.process(column);
      }
      loadScripts(sidebar);
      loadScripts(column);
      syncOpenWork();
      if (!arrivedOnEntry) {
        history.replaceState(state(LIST_STATE, '/'), '', '/');
        if (work) history.pushState(state(WORK_STATE, workPath), '', workPath);
        history.pushState(state(CHAT_STATE, chatPath), '', chatPath);
      }
      dispatchNavigation();
    } catch (_) {
      // The chat page alone is still the whole chat.
    }
  }

  function settlePlace() {
    var place = pendingPlace;
    pendingPlace = null;
    if (!place) return;
    keepPlace(place.anchor, place.top, function (drift) {
      place.sidebar.scrollTop += drift;
    });
  }

  // The diff pane and its topbar control sit outside the chat pane, so a chat
  // opened in-page has to bring them along. Without this the pane is not on the
  // page at all for every chat reached by tapping a card — the usual way — and
  // neither the edge gesture nor the control has anything to open, which reads
  // as a chat that simply has no diff (#1219).
  function adoptDiff(parsed, layout) {
    if (!layout.querySelector('.session-diff')) {
      var pane = parsed.querySelector('.session-diff');
      if (pane) layout.appendChild(pane);
    }
    var row = document.querySelector('.topbar-row');
    if (!row || row.querySelector('[data-session-diff-open]')) return;
    var toggle = parsed.querySelector('[data-session-diff-open]');
    if (!toggle) return;
    var brand = row.querySelector('.brand');
    if (brand) row.insertBefore(toggle, brand); else row.appendChild(toggle);
  }

  function dropDiff() {
    // The pane itself goes with the layout; the control is in the topbar, which
    // survives, and a control left behind would open a chat that is gone.
    var toggle = document.querySelector('[data-session-diff-open]');
    if (toggle) toggle.remove();
    document.body.classList.remove('diff-open', 'diff-dragging');
  }

  // Both ways of closing a chat assume the deck is already on the page: the
  // mobile one slides the chat aside to reveal the list mounted beneath it, and
  // the desktop one unwraps that list back into `main`. A chat document served
  // for in-page navigation carries no sidebar at all, and on one of those the
  // close reveals nothing — an empty `main` under a live topbar, with the arrow
  // hidden and a second press doing the same thing again (#2047). A chat page loaded
  // on its own has only the empty slot the desktop columns fill (#2603). When the list
  // is not there to reveal, load the deck for real instead.
  function listMounted() {
    return Boolean(
      document.querySelector('.session-layout > .session-sidebar:not([data-list-empty])')
    );
  }

  function closeSession(updateHistory) {
    requestGeneration += 1;
    if (document.body.classList.contains('session-page') && !listMounted()) {
      location.assign('/');
      return;
    }
    var list = listLayer();
    var layout = document.querySelector('.session-layout');
    var sidebar = layout && layout.querySelector('.session-sidebar');
    var detail = layout && layout.querySelector('.session-detail');
    if (mobile.matches && detail) {
      document.body.classList.add('mobile-list-open');
      setTitle('agentdeck');
      syncSelectedRows(list.path);
      if (updateHistory) history.pushState(state(LIST_STATE, list.path), '', list.path);
      // The list it reveals is live again: a phone's front-page row opens a chat over the
      // front page (#2512), and on the chat's stream its Works would stand still. Going
      // forward to the chat reloads it and its stream.
      switchStream(list.stream);
      dispatchNavigation();
      return;
    }
    if (detail) removeNode(detail);
    dropDiff();
    var columns = Boolean(layout && layout.classList.contains('front-columns'));
    if (layout && sidebar) {
      // The other half of the same move: the sidebar's content goes back to a
      // document that scrolls, so the place it was read at has to go with it —
      // otherwise the deck comes back at the top however far down it was.
      var anchor = anchorFor(sidebar);
      var anchorTop = anchor ? anchor.getBoundingClientRect().top : null;
      var main = layout.parentElement;
      var work = layout.querySelector(':scope > .work-column');
      if (work) removeNode(work);
      while (sidebar.firstChild) main.insertBefore(sidebar.firstChild, layout);
      // The front page's own stream closes with its column; the body's is
      // restarted below so the front page is bound to it again.
      if (columns && window.htmx) htmx.trigger(sidebar, 'htmx:beforeCleanupElement');
      layout.remove();
    }
    document.body.classList.remove(
      'session-page', 'session-stack-page', 'mobile-list-open',
      'mobile-session-stack-ready', 'mobile-stack-dragging', 'front-columns-page'
    );
    document.body.classList.add('session-list-page');
    // Once `session-page` is off the document scrolls again, so this is the
    // first moment the deck's place can be put back.
    keepPlace(anchor, anchorTop, function (drift) {
      window.scrollBy(0, drift);
    });
    setTitle('agentdeck');
    switchStream(list.stream, columns);
    syncSelectedRows(list.path);
    if (updateHistory) history.pushState(state(LIST_STATE, list.path), '', list.path);
    dispatchNavigation();
  }

  // The house is the front page, not the list beneath the chat (#2266). It closes
  // the chat in place only while the chat covers the front page itself; over the
  // chats view or a Work, or once a phone already shows the list, it is a plain
  // link to `/`, and swallowing it there left a tap that did nothing.
  function brandClosesInPlace() {
    if (document.body.classList.contains('mobile-list-open')) return false;
    return new URL(listLayer().path, location.href).pathname === '/';
  }

  function isPlainClick(event, link) {
    return event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey &&
      !event.altKey && (!link.target || link.target === '_self');
  }

  // On a phone, Back from a chat or a Work is the front page (#2878, replacing #2854's
  // chat → Work → front page). A chat opened over a Work takes the Work's entry, so Back
  // goes to what lies beneath the Work, which is the front page (see the end of this file).
  function chatReplacesWork() {
    return mobile.matches && isWorkPath(new URL(listLayer().path, location.href).pathname);
  }

  // Whether the entry this page was loaded over is the front page's own. A tab opened on
  // its own has nothing beneath it, whatever its referrer says.
  function cameFrom(path) {
    if (history.length < 2 || !document.referrer) return false;
    var from = new URL(document.referrer, location.href);
    return from.origin === location.origin && from.pathname === path;
  }

  // A sub-issue's Work names its parent's (#3691): Back from it is the parent's Work,
  // whatever led here, and then the front page beneath that.
  function parentPath(work) {
    var href = work && work.dataset.parentWork;
    return href ? new URL(href, location.href).pathname : null;
  }

  // `beside`: the chat a Work opened next to itself (#2345). The address and the title stay
  // the Work's, and a chat that fails to load leaves the Work showing instead of loading it.
  async function openSession(link, path, updateHistory, options) {
    var beside = Boolean(options && options.beside);
    var generation = ++requestGeneration;
    try {
      var headers = {'Accept': 'text/html', 'X-AgentDeck-Session-Navigation': 'detail'};
      if (window.AgentDeckActionTiming && link) {
        Object.assign(headers, AgentDeckActionTiming.navigationHeaders(link));
      }
      var response = await fetch(path, {
        headers: headers
      });
      if (!response.ok) throw new Error('session request failed: ' + response.status);
      var parsed = new DOMParser().parseFromString(await response.text(), 'text/html');
      var incoming = parsed.querySelector('.session-detail');
      if (!incoming) throw new Error('session detail missing from response');
      if (generation !== requestGeneration) return;

      var layout = ensureLayout();
      var current = layout.querySelector('.session-detail');
      if (current) removeNode(current);
      adoptDiff(parsed, layout);
      layout.appendChild(incoming);
      document.body.classList.add('session-page', 'session-stack-page');
      document.body.classList.remove('session-list-page', 'mobile-list-open');
      settlePlace();
      if (!beside) setTitle(parsed.title || 'agentdeck');
      syncSelectedRows(path.split('?')[0], incoming.dataset.sessionKey);
      if (updateHistory) {
        if (chatReplacesWork()) history.replaceState(state(CHAT_STATE, path), '', path);
        else history.pushState(state(CHAT_STATE, path), '', path);
      }
      if (window.htmx) htmx.process(incoming);
      switchStream(parsed.body.getAttribute('sse-connect'));
      if (window.AgentDeckSessionBottomFollow) AgentDeckSessionBottomFollow.init();
      if (window.AgentDeckLastUserJump) AgentDeckLastUserJump.init();
      if (window.AgentDeckMessagePins) AgentDeckMessagePins.init();
      dispatchNavigation();
      if (window.AgentDeckActionTiming) {
        AgentDeckActionTiming.finishNavigation(
          link, response.status, true, response.headers.get('Server-Timing') || ''
        );
      }
    } catch (_) {
      if (generation !== requestGeneration) return;
      if (window.AgentDeckActionTiming) AgentDeckActionTiming.finishNavigation(link, 0, false);
      if (!beside) location.assign(path);
    }
  }

  // A phone opens a front-page row's chat rather than its Work (#2512): almost every visit
  // is to the conversation. The chat opens over the front page the way an All chats card
  // does, and Back closes it to the front page (#2878). A row with no chat yet still opens
  // its Work.
  function rowChat(event) {
    if (!mobile.matches) return null;
    var link = event.target.closest('.fr a.fr-body[data-chat-href]');
    if (!link || !isPlainClick(event, link) || !link.closest('main')) return null;
    var url = new URL(link.dataset.chatHref, location.href);
    if (url.origin !== location.origin || !/^\/(sessions|chats)\/[^/?#]+$/.test(url.pathname)) {
      return null;
    }
    // A chat several Works share names the row's Work in its address (#2567).
    return {link: link, path: url.pathname + url.search};
  }

  document.addEventListener('click', function (event) {
    var chat = rowChat(event);
    if (chat) {
      event.preventDefault();
      openSession(chat.link, chat.path, true);
      return;
    }
    var workLink = event.target.closest('a[href^="/works/"]');
    if (workLink && !mobile.matches && isPlainClick(event, workLink) &&
        (workLink.closest('.front-columns > .session-sidebar') ||
         (document.body.classList.contains('front-page') &&
          !document.querySelector('.session-layout') && workLink.closest('main')))) {
      var workUrl = new URL(workLink.href, location.href);
      if (workUrl.origin === location.origin && !workUrl.search && isWorkPath(workUrl.pathname)) {
        event.preventDefault();
        openWork(workUrl.pathname, true);
        return;
      }
    }
    var card = event.target.closest(
      'a[data-session-key][href^="/sessions/"], a[data-session-key][href^="/chats/"]'
    );
    if (card && !event.target.closest('.session-actions [role="button"]') &&
        isPlainClick(event, card)) {
      event.preventDefault();
      var cardUrl = new URL(card.href, location.href);
      openSession(card, cardUrl.pathname + cardUrl.search, true);
      return;
    }
    var close = event.target.closest(
      'body.session-page a.back[href="/"], body.session-page a.brand[href="/"], ' +
      '.session-sidebar-head a[href="/"]'
    );
    if (close && isPlainClick(event, close)) {
      if (close.matches('a.brand') && !brandClosesInPlace()) return;
      // A phone already showing the list has no chat to close: the arrow left on a Work's
      // page is its own link to `/` (#2281).
      if (document.body.classList.contains('mobile-list-open')) return;
      event.preventDefault();
      closeSession(true);
    }
  });

  // A Work link that loads a page (a phone, a Work opened from elsewhere) restarts that load
  // on every click, so the same repeat press never lets it finish either (#2422). Listening
  // on the window runs after every document handler, so a click one of them took over in
  // place never counts. A stopped load leaves no event, hence the time limit.
  var pageLoad = null;
  window.addEventListener('click', function (event) {
    var link = event.target.closest && event.target.closest('a[href^="/works/"]');
    if (event.defaultPrevented || !link || !isPlainClick(event, link)) return;
    var url = new URL(link.href, location.href);
    if (url.origin !== location.origin || !isWorkPath(url.pathname)) return;
    if (url.pathname === location.pathname) return;  // a place on this page: nothing loads
    var now = Date.now();
    if (pageLoad && pageLoad.href === url.href && now - pageLoad.at < 20000) {
      event.preventDefault();
      return;
    }
    pageLoad = {href: url.href, at: now};
    // The page can take seconds to come: the row says it is on its way.
    var row = link.closest('.fr');
    if (row) row.classList.add('fr-opening');
    link.setAttribute('aria-busy', 'true');
  });
  window.addEventListener('pageshow', function () {
    pageLoad = null;
    document.querySelectorAll('.fr-opening').forEach(function (row) {
      row.classList.remove('fr-opening');
    });
    document.querySelectorAll('a.fr-body[aria-busy]').forEach(function (link) {
      link.removeAttribute('aria-busy');
    });
  });

  // Closing in place reveals the list this document already holds. When that list is not
  // the page at the address Back landed on — the Work a chat was opened over, once a reload
  // has made the chat a page of its own, or an old `/` entry — revealing it put one page
  // under another's address (#2281), so load the address for real. Reload keeps the entry
  // and the forward history; `replace` of the same URL would only scroll for a `#fragment`.
  // A Work page on a phone holds no front page at all, so Back to the front page's entry
  // beneath it (#2878) loads that too.
  function closeOrLoad() {
    var stack = window.AgentDeckMobileSessionStack;
    var sidebar = document.querySelector('.session-layout > .session-sidebar');
    if ((!sidebar && document.body.classList.contains('work-page')) ||
        (stack && !stack.listIsHere(sidebar))) location.reload();
    else closeSession(false);
  }

  window.addEventListener('popstate', function (event) {
    var path = location.pathname;
    if (/^\/(sessions|chats)\//.test(path)) openSession(null, path + location.search, false);
    else if (path === '/') closeOrLoad();
    // Back from a chat to the Work beside it, or between Works (#2290). A phone has no
    // Work column: it loads the Work page at that entry (#2854).
    else if (event.state && event.state[STATE_KEY] === WORK_STATE && isWorkPath(path)) {
      if (mobile.matches) closeOrLoad();
      else openWork(path, false);
    }
    // The list layer's own entry at its own address (#2256). Only while a chat is
    // actually open over it: a list page's own history, an in-page anchor say, lands
    // on the same kind of entry and must not be "closed" into anything.
    else if (event.state && event.state[STATE_KEY] === LIST_STATE &&
             document.querySelector('.session-layout > .session-detail')) {
      closeOrLoad();
    }
  });

  // The Deckhand rows and the open Work's row are swapped by their own streams;
  // re-mark the current ones from the open detail after either changes.
  function resyncSelection() {
    syncSelectedRows(location.pathname);
    syncOpenWork();
  }

  document.body.addEventListener('htmx:afterSwap', resyncSelection);
  // The SSE extension wraps its direct DOM swap in its own lifecycle rather than
  // emitting htmx:afterSwap.
  document.body.addEventListener('htmx:sseMessage', resyncSelection);

  // A direct mobile deep link still needs a same-document list entry beneath
  // the chat so Back closes it to the front page without loading the dashboard (#2878).
  // mobile_session_stack.js usually stamps these first.
  if (mobile.matches && selectedKey(location.pathname) &&
      !(history.state && history.state[STATE_KEY])) {
    var chatPath = location.pathname + location.search + location.hash;
    var listPath = listLayer().path;
    history.replaceState(state(LIST_STATE, listPath), '', listPath);
    history.pushState(state(CHAT_STATE, chatPath), '', chatPath);
  } else if (mobile.matches && document.body.classList.contains('work-page') &&
             isWorkPath(location.pathname) && !(history.state && history.state[STATE_KEY]) &&
             !cameFrom(parentPath(document.querySelector('main.work-main')) || '/')) {
    // A Work page a phone loads fresh puts the front page beneath itself, so Back is the
    // front page whatever led here: a chat's header, another Work, a link (#2878). A
    // sub-issue's Work puts its parent's between the two (#3691). Straight from the page
    // Back would reach, that page's own entry is already beneath it.
    var ownPath = location.pathname;
    var ownWork = ownPath + location.search + location.hash;
    var ownParent = parentPath(document.querySelector('main.work-main'));
    if (ownParent && cameFrom('/')) {
      // From the front page, its own entry is already beneath: the parent's takes this one.
      history.replaceState(state(WORK_STATE, ownParent), '', ownParent);
    } else {
      history.replaceState(state(LIST_STATE, '/'), '', '/');
      if (ownParent) history.pushState(state(WORK_STATE, ownParent), '', ownParent);
    }
    history.pushState(state(WORK_STATE, ownPath), '', ownWork);
  } else if (!(history.state && history.state[STATE_KEY])) {
    history.replaceState(state(selectedKey(location.pathname) ? CHAT_STATE : LIST_STATE,
      location.pathname), '', location.href);
  }
  syncSelectedRows(location.pathname);

  // After htmx has bound the page it loaded, which it does on DOMContentLoaded: the
  // columns rebind what it bound. A deferred script runs before that event, while the
  // document already reads `interactive`, so the state alone cannot say it has passed.
  var mountColumns = null;
  if (!mobile.matches && document.body.classList.contains('work-page')) {
    mountColumns = mountFrontBesideWork;
  } else if (!mobile.matches && document.body.classList.contains('session-page')) {
    mountColumns = mountFrontBesideChat;
  }
  if (mountColumns) {
    var mounted = false;
    var mountOnce = function () {
      if (mounted) return;
      mounted = true;
      mountColumns();
    };
    if (document.readyState === 'complete') mountOnce();
    else {
      document.addEventListener('DOMContentLoaded', mountOnce);
      window.addEventListener('load', mountOnce);
    }
  }
})();
