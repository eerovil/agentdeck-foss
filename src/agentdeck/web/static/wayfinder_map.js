// A wayfinder map you can move around (#3627).
//
// The server lays the map out and draws it as SVG (`partials/wayfinder_map.html`); this makes
// its frame a map, with no library: drag the background to pan, pinch (or Ctrl+wheel, which is
// also a trackpad pinch) to zoom, drag a box to move it, and tap a box to open its ticket's
// Work. A box a person moves is saved on the deck when the drag ends (`data-wm-save`, one
// layout per map, so every device draws it there); the view they left is kept in this
// browser's localStorage per map. Reset puts back the server's layout and the fitted view.
//
// A drag holds the section's live updates (`data-wm-dragging`, read by work_list.js's
// `inUse`) and hands the held one back when it ends, so a box never jumps out of a finger.
(function () {
  'use strict';

  var STORE = 'agentdeck:wayfinder-map:';
  var MOVE = 6; // CSS pixels a press moves before it is a drag, not a tap
  var MIN_K = 0.3;
  var MAX_K = 2.5;
  var TALL = 0.7; // the frame is at most this share of the window's height
  var LOW = 180; // and at least this many pixels

  function clamp(value, low, high) { return Math.max(low, Math.min(high, value)); }

  function load(key) {
    try { return JSON.parse(localStorage.getItem(STORE + key) || '{}') || {}; } catch (_) { return {}; }
  }

  function store(map) {
    var moved = Object.keys(map.pos).length > 0;
    try {
      if (!map.view) localStorage.removeItem(STORE + map.key);
      else localStorage.setItem(STORE + map.key, JSON.stringify({view: map.view}));
    } catch (_) {}
    map.reset.hidden = !moved && !map.view;
  }

  // Saves to the deck: one ordered list per map of what a person meant, kept outside any
  // one drawing of the map, since a live update redraws the section and what is not saved
  // yet must outlive that.
  //
  // Each move or Reset is an intent with a number that only goes up. A newer move of a box
  // replaces the older intent for that box, and a Reset replaces every intent before it, so
  // nothing a newer one superseded is ever sent again, by Retry or otherwise. Requests go
  // one at a time per map, in order. Each move names one box, so another device's moves are
  // never overwritten. An intent leaves the list once it landed, unless it came after a
  // Reset that has not: Retry then replays the Reset and everything after it, in order, so
  // the deck ends where the person left it.
  var saves = {};

  function saving(key) {
    if (!saves[key]) saves[key] = {chain: Promise.resolve(), seq: 0, intents: []};
    return saves[key];
  }

  function intend(state, body) {
    var intent = {seq: ++state.seq, body: body, landed: false, failed: false};
    state.intents = body.reset ? [] : state.intents.filter(function (item) {
      return item.body.reset || item.body.box !== body.box;
    });
    state.intents.push(intent);
    return intent;
  }

  function settle(state) {
    var reset = -1;
    state.intents.forEach(function (item, at) {
      if (reset < 0 && item.body.reset && !item.landed) reset = at;
    });
    state.intents = state.intents.filter(function (item, at) {
      return !item.landed || (reset >= 0 && at > reset);
    });
  }

  function unsaved(state) {
    return state.intents.some(function (item) { return item.failed && !item.landed; });
  }

  function shown(map) {
    if (map.retry) map.retry.hidden = !unsaved(saving(map.key));
  }

  function post(map, intent) {
    var url = map.root.getAttribute('data-wm-save');
    if (!url || !window.fetch) return;
    var key = map.key;
    var state = saving(key);
    intent.failed = false;
    state.chain = state.chain.then(function () {
      return fetch(url, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(intent.body),
      }).then(function (answer) {
        if (!answer.ok) throw new Error('the deck answered ' + answer.status);
        intent.landed = true;
      });
    }).catch(function () {
      intent.failed = true;
    }).then(function () {
      settle(state);
      document.querySelectorAll('[data-wayfinder-map]').forEach(function (root) {
        if (root._wayfinderMap && root._wayfinderMap.key === key) shown(root._wayfinderMap);
      });
    });
  }

  function send(map, body) {
    post(map, intend(saving(map.key), body));
  }

  function retry(map) {
    var state = saving(map.key);
    state.intents.slice().forEach(function (intent) {
      intent.landed = false;
      post(map, intent);
    });
    shown(map);
  }



  // The same curve as `wayfinder_map._curve`: blocker's bottom middle to blocked's top middle.
  function curve(a, b) {
    var x1 = a.x + a.w / 2, y1 = a.y + a.h, x2 = b.x + b.w / 2, y2 = b.y - 3;
    var bend = Math.max(12, (y2 - y1) / 2);
    return 'M' + x1.toFixed(1) + ',' + y1.toFixed(1) +
      ' C' + x1.toFixed(1) + ',' + (y1 + bend).toFixed(1) +
      ' ' + x2.toFixed(1) + ',' + (y2 - bend).toFixed(1) +
      ' ' + x2.toFixed(1) + ',' + y2.toFixed(1);
  }

  function place(box) { box.el.setAttribute('transform', 'translate(' + box.x + ' ' + box.y + ')'); }

  function redraw(map, key) {
    map.arrows.forEach(function (arrow) {
      if (key && arrow.from !== key && arrow.to !== key) return;
      var a = map.boxes[arrow.from], b = map.boxes[arrow.to];
      if (a && b) arrow.el.setAttribute('d', curve(a, b));
    });
  }

  function show(map) {
    var v = map.view || map.fit;
    map.world.setAttribute('transform', 'translate(' + v.x + ' ' + v.y + ') scale(' + v.k + ')');
  }

  function fitted(map) {
    var wide = map.frame.clientWidth || map.width;
    var k = clamp(Math.min(1, wide / map.width), 0.75, 1);
    return {x: Math.max(0, (wide - map.width * k) / 2), y: 0, k: k};
  }

  function zoom(map, k, cx, cy) {
    var v = map.view || map.fit;
    k = clamp(k, MIN_K, MAX_K);
    // Keep the point under the fingers (or the cursor) where it is.
    var wx = (cx - v.x) / v.k, wy = (cy - v.y) / v.k;
    map.view = {x: cx - wx * k, y: cy - wy * k, k: k};
    show(map);
  }

  function local(map, e) {
    var rect = map.frame.getBoundingClientRect();
    return {x: e.clientX - rect.left, y: e.clientY - rect.top};
  }

  function begin(map) {
    map.root.setAttribute('data-wm-dragging', '');
  }

  function finish(map) {
    if (!map.root.hasAttribute('data-wm-dragging')) return;
    map.root.removeAttribute('data-wm-dragging');
    store(map);
    // Every box this gesture moved, whatever it became after: a second finger turns a drag
    // into a pinch, and the box it already moved must still be saved.
    Object.keys(map.moving).forEach(function (key) {
      var box = map.boxes[key];
      if (box) send(map, {box: key, at: [box.x, box.y]});
    });
    map.moving = {};
    // A live update held while the drag ran is swapped in now (work_list.js).
    map.root.dispatchEvent(new FocusEvent('focusout', {bubbles: true}));
  }

  function init(root) {
    if (root._wayfinderMap) return;
    var frame = root.querySelector('[data-wm-frame]');
    var svg = frame && frame.querySelector('svg');
    var world = svg && svg.querySelector('[data-wm-world]');
    if (!world) return;
    var map = {
      root: root, frame: frame, svg: svg, world: world,
      key: root.getAttribute('data-wm-key') || '',
      width: parseFloat(svg.getAttribute('data-wm-width')) || 360,
      height: parseFloat(svg.getAttribute('data-wm-height')) || 200,
      reset: frame.querySelector('[data-wm-reset]'),
      boxes: {}, home: {}, arrows: [], pos: {}, view: null, fit: null,
      retry: frame.querySelector('[data-wm-retry]'),
      pointers: {}, gesture: null, swallowAt: -1, moving: {},
    };
    root._wayfinderMap = map;
    Array.prototype.forEach.call(world.querySelectorAll('[data-wm-box]'), function (el) {
      var at = /translate\(([-\d.]+)[ ,]+([-\d.]+)\)/.exec(el.getAttribute('transform') || '');
      var key = el.getAttribute('data-wm-box');
      var box = {
        el: el, key: key,
        x: at ? parseFloat(at[1]) : 0, y: at ? parseFloat(at[2]) : 0,
        w: parseFloat(el.getAttribute('data-wm-w')) || 0,
        h: parseFloat(el.getAttribute('data-wm-h')) || 0,
      };
      map.boxes[key] = box;
      var home = (el.getAttribute('data-wm-home') || '').split(' ');
      if (home.length === 2 && isFinite(parseFloat(home[0])) && isFinite(parseFloat(home[1]))) {
        // The deck draws it where a person moved it; the server's own place is Reset's.
        map.home[key] = {x: parseFloat(home[0]), y: parseFloat(home[1])};
        map.pos[key] = {x: box.x, y: box.y};
      } else {
        map.home[key] = {x: box.x, y: box.y};
      }
    });
    Array.prototype.forEach.call(world.querySelectorAll('[data-wm-from]'), function (el) {
      map.arrows.push({el: el, from: el.getAttribute('data-wm-from'), to: el.getAttribute('data-wm-to')});
    });
    var saved = load(map.key);
    // A move the deck has not taken yet is drawn where it was left, not where the deck has it.
    saving(map.key).intents.forEach(function (intent) {
      var body = intent.body;
      if (body.reset) {
        Object.keys(map.boxes).forEach(function (key) {
          var box = map.boxes[key];
          box.x = map.home[key].x; box.y = map.home[key].y;
          place(box);
        });
        map.pos = {};
        return;
      }
      var box = map.boxes[body.box];
      if (!box) return;
      box.x = body.at[0]; box.y = body.at[1];
      map.pos[box.key] = {x: box.x, y: box.y};
      place(box);
    });
    shown(map);
    redraw(map);
    root.classList.add('wm-live');
    svg.removeAttribute('width');
    svg.removeAttribute('height');
    map.fit = fitted(map);
    var v = saved.view;
    if (v && isFinite(v.x) && isFinite(v.y) && isFinite(v.k)) {
      map.view = {x: v.x, y: v.y, k: clamp(v.k, MIN_K, MAX_K)};
    }
    frame.style.height = clamp(map.height * map.fit.k + 2, LOW, window.innerHeight * TALL) + 'px';
    show(map);
    store(map);

    frame.addEventListener('pointerdown', function (e) {
      if (e.target.closest('[data-wm-reset], [data-wm-retry]')) return;
      map.pointers[e.pointerId] = local(map, e);
      var ids = Object.keys(map.pointers);
      if (ids.length === 2) {
        var a = map.pointers[ids[0]], b = map.pointers[ids[1]];
        map.gesture = {
          kind: 'pinch', dist: Math.hypot(a.x - b.x, a.y - b.y) || 1,
          k: (map.view || map.fit).k, moved: true,
        };
        begin(map);
        return;
      }
      if (ids.length > 2) return;
      var hit = e.target.closest('[data-wm-box]');
      var v = map.view || map.fit;
      map.gesture = {
        kind: hit ? 'box' : 'pan', id: e.pointerId, start: map.pointers[e.pointerId],
        box: hit ? map.boxes[hit.getAttribute('data-wm-box')] : null,
        from: hit ? {x: map.boxes[hit.getAttribute('data-wm-box')].x,
                     y: map.boxes[hit.getAttribute('data-wm-box')].y} : {x: v.x, y: v.y},
        moved: false,
      };
    });

    frame.addEventListener('pointermove', function (e) {
      if (!(e.pointerId in map.pointers)) return;
      map.pointers[e.pointerId] = local(map, e);
      var g = map.gesture;
      if (!g) return;
      if (g.kind === 'pinch') {
        var ids = Object.keys(map.pointers);
        if (ids.length < 2) return;
        var a = map.pointers[ids[0]], b = map.pointers[ids[1]];
        zoom(map, g.k * Math.hypot(a.x - b.x, a.y - b.y) / g.dist, (a.x + b.x) / 2, (a.y + b.y) / 2);
        return;
      }
      var at = map.pointers[g.id];
      var dx = at.x - g.start.x, dy = at.y - g.start.y;
      if (!g.moved) {
        if (Math.hypot(dx, dy) < MOVE) return;
        g.moved = true;
        // Only now: a tap that never moved must still reach the box's link as a click.
        try { frame.setPointerCapture(e.pointerId); } catch (_) {}
        begin(map);
        if (g.box) map.world.appendChild(g.box.el); // the box in hand is drawn on top
      }
      if (g.kind === 'pan') {
        var k = (map.view || map.fit).k;
        map.view = {x: g.from.x + dx, y: g.from.y + dy, k: k};
        show(map);
      } else if (g.box) {
        var scale = (map.view || map.fit).k;
        g.box.x = Math.round(g.from.x + dx / scale);
        g.box.y = Math.round(g.from.y + dy / scale);
        map.pos[g.box.key] = {x: g.box.x, y: g.box.y};
        map.moving[g.box.key] = true;
        place(g.box);
        redraw(map, g.box.key);
      }
      e.preventDefault();
    });

    function lift(e) {
      if (!(e.pointerId in map.pointers)) return;
      delete map.pointers[e.pointerId];
      var g = map.gesture;
      // Only a mouse or pen drag that ends with a lift is followed by a click to swallow. A
      // cancelled gesture, or a finger's drag, has none, and must not eat the next real tap.
      var clicks = e.type === 'pointerup' && e.pointerType !== 'touch';
      map.swallowAt = g && g.moved && clicks ? e.timeStamp : -1;
      var left = Object.keys(map.pointers);
      if (left.length === 1 && g && g.kind === 'pinch') {
        // One finger stays after a pinch: it pans from here.
        var v = map.view || map.fit;
        map.gesture = {kind: 'pan', id: left[0], start: map.pointers[left[0]],
                       from: {x: v.x, y: v.y}, moved: true};
        return;
      }
      if (!left.length) {
        map.gesture = null;
        finish(map);
      }
    }
    frame.addEventListener('pointerup', lift);
    frame.addEventListener('pointercancel', lift);

    // A drag that ends over a box is not a tap on it.
    frame.addEventListener('click', function (e) {
      var at = map.swallowAt;
      map.swallowAt = -1;
      // Only ever a box's own click: Reset and Retry are never a drag's leftover.
      if (!e.target.closest('[data-wm-box]')) return;
      if (at < 0 || e.timeStamp - at > 1000) return;
      e.preventDefault();
      e.stopPropagation();
    }, true);

    frame.addEventListener('wheel', function (e) {
      if (!e.ctrlKey && !e.metaKey) return; // a plain wheel scrolls the page past the map
      e.preventDefault();
      var at = local(map, e);
      zoom(map, (map.view || map.fit).k * Math.exp(-e.deltaY / 300), at.x, at.y);
      window.clearTimeout(map.wheelSave);
      map.wheelSave = window.setTimeout(function () { store(map); }, 300);
    }, {passive: false});

    map.reset.addEventListener('click', function () {
      Object.keys(map.boxes).forEach(function (key) {
        var box = map.boxes[key];
        box.x = map.home[key].x; box.y = map.home[key].y;
        place(box);
      });
      var had = Object.keys(map.pos).length > 0 || saving(map.key).intents.length > 0;
      map.pos = {};
      map.view = null;
      redraw(map);
      show(map);
      store(map);
      if (had) send(map, {reset: true});
    });

    if (map.retry) map.retry.addEventListener('click', function () { retry(map); });
  }

  function scan() {
    Array.prototype.forEach.call(document.querySelectorAll('[data-wayfinder-map]'), init);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', scan);
  else scan();
  ['htmx:afterSettle', 'htmx:sseMessage'].forEach(function (name) {
    document.addEventListener(name, scan);
  });
})();

// A wayfinder ticket's chat page (#3696): once its question form is answered and gone, the
// map strip above it says where to go next — the map's next waiting question, or the map.
// The answer swaps the form out, so its own events never reach the document: a send is noted
// when it starts, and the strip is shown once the page settles with no form left.
(function () {
  'use strict';

  var sent = false;

  function isAnswer(elt) {
    return !!(elt && elt.matches && elt.matches('form[data-agentdeck-action="interaction"]'));
  }

  document.addEventListener('htmx:beforeRequest', function (event) {
    if (isAnswer(event.detail && event.detail.elt)) sent = true;
  });
  document.addEventListener('htmx:afterSettle', function () {
    if (!sent || document.querySelector('#pending-interaction form')) return;
    sent = false;
    var next = document.querySelector('[data-wm-strip] [data-wm-next]');
    if (next) next.hidden = false;
  });
})();
