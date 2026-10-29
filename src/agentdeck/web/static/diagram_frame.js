// Runs inside a diagram frame (#3822): draws the ```mermaid block live_html.js put in
// `pre.mermaid`, lays it out with ELK when that loads, styles and colours it, and makes
// each box clickable.
//
// `window.agentdeckBoxNotes` maps a box's text (lower case, no spaces) to its note, taken from
// the file's `- **Box text**: note` lines; the note shows when the box is tapped.
(function () {
  'use strict';

  // The frame's look. Mermaid colours each group and box on its own; these quiet ones
  // win over it, and colour() below adds the group and meaning colours on top.
  var STYLE =
    'pre.mermaid{margin:0;text-align:center}pre.mermaid svg{max-width:100%;height:auto}' +
    '.mm-err{white-space:pre-wrap;font:12px/1.4 ui-monospace,monospace}.mm-err b{color:#c0392b}' +
    'svg.mm-focus g.node:not(.mm-on),svg.mm-focus .edgePaths path:not(.mm-on),' +
    'svg.mm-focus .edgeLabel:not(.mm-on){opacity:.18;transition:opacity .15s}' +
    'svg.mm-focus path.mm-on{stroke-width:2.5px}' +
    '.mm-pop{position:absolute;z-index:2;max-width:280px;padding:8px 10px;border-radius:8px;' +
    'background:Canvas;color:CanvasText;border:1px solid rgba(128,128,128,.4);' +
    'box-shadow:0 4px 14px rgba(0,0,0,.18);font-size:13px;text-align:left}' +
    '.mm-pop p{margin:6px 0 0}' +
    // A box the diagram colours itself (`.mm-own`) keeps its colour.
    'pre.mermaid{--mm-bg:#f6f7f9;--mm-panel:#ffffff;--mm-panel-line:#e3e7ec;--mm-box:#ffffff;' +
    '--mm-box-line:#cfd6de;--mm-ink:#1f2328;--mm-quiet:#6b7480;--mm-line:#8a929c}' +
    '@media (prefers-color-scheme:dark){pre.mermaid{--mm-bg:#15181d;--mm-panel:#1b1f25;' +
    '--mm-panel-line:#2a3038;--mm-box:#242a32;--mm-box-line:#3a424c;--mm-ink:#e6e8eb;' +
    '--mm-quiet:#8d97a2;--mm-line:#7c8691}}' +
    'pre.mermaid g.cluster rect{fill:var(--mm-panel)!important;stroke:var(--mm-panel-line)!important;' +
    'stroke-width:1px!important;filter:drop-shadow(0 1px 2px rgba(0,0,0,.04))}' +
    'pre.mermaid g.cluster.mm-inner rect{fill:transparent!important;stroke-dasharray:4 4;filter:none}' +
    'pre.mermaid :is(g.cluster,.mm-headings) :is(.cluster-label,text,span,p,div){fill:var(--mm-quiet)!important;' +
    'color:var(--mm-quiet)!important;font-size:11px!important;font-weight:600!important;' +
    'letter-spacing:.06em;text-transform:uppercase;text-align:left!important}' +
    'pre.mermaid g.node:not(.mm-own) :is(rect,polygon,circle,path,ellipse){fill:var(--mm-box)!important;' +
    'stroke:var(--mm-box-line)!important;stroke-width:1px!important}' +
    'pre.mermaid g.node{filter:drop-shadow(0 1px 1.5px rgba(0,0,0,.07))}' +
    'pre.mermaid g.node:not(.mm-own) :is(.nodeLabel,text,span,p){color:var(--mm-ink)!important;' +
    'fill:var(--mm-ink)!important}' +
    'pre.mermaid g.node :is(.nodeLabel,span,p){font-size:13px!important;font-weight:500}' +
    'pre.mermaid :is(.flowchart-link,.edgePath path){stroke:var(--mm-line)!important;stroke-width:1.25px!important}' +
    'pre.mermaid marker path{fill:var(--mm-line)!important;stroke:var(--mm-line)!important}' +
    'pre.mermaid .edgeLabel,pre.mermaid .edgeLabel :is(p,span){background:var(--mm-bg)!important;' +
    'color:var(--mm-quiet)!important;font-size:11.5px!important}' +
    'pre.mermaid .edgeLabel rect{fill:var(--mm-bg)!important;opacity:1!important}' +
    'pre.mermaid .labelBkg{background:var(--mm-bg)!important}' +
    'html.mm-fill,html.mm-fill body{height:100%;overflow:hidden;background:transparent}' +
    '.mm-stage{position:fixed;inset:0;overflow:hidden;touch-action:none;cursor:grab}' +
    '.mm-stage.mm-grab{cursor:grabbing}' +
    'html.mm-fill pre.mermaid{position:absolute;left:0;top:0;transform-origin:0 0}' +
    'html.mm-fill .mm-pop{position:fixed;left:auto!important;top:auto!important;right:16px;bottom:16px;' +
    'max-width:min(360px,calc(100vw - 32px));font-size:14px;padding:12px 14px}';
  var style = document.createElement('style');
  style.textContent = STYLE;
  document.head.appendChild(style);

  var VENDOR = document.currentScript.src.replace(/diagram_frame\.js.*$/, 'vendor/');
  var el = document.querySelector('pre.mermaid');
  var src = el.textContent;
  var dark = matchMedia('(prefers-color-scheme: dark)').matches;
  var notes = window.agentdeckBoxNotes || {};

  // One quiet palette in place of Mermaid's lavender defaults: white or slate boxes,
  // grey lines, faint group panels. A diagram's own classDef colours still win.
  var PALETTES = {
    light: { background: '#f6f7f9', primaryColor: '#ffffff', primaryBorderColor: '#c4cad3',
      primaryTextColor: '#1f2328', lineColor: '#7d8590', clusterBkg: '#eef1f5',
      clusterBorder: '#d5dae1', edgeLabelBackground: '#f6f7f9', titleColor: '#57606a',
      tertiaryColor: '#eef1f5' },
    dark: { background: '#15181d', primaryColor: '#232830', primaryBorderColor: '#3d444d',
      primaryTextColor: '#e6e8eb', lineColor: '#8b949e', clusterBkg: '#1b1f25',
      clusterBorder: '#30363d', edgeLabelBackground: '#15181d', titleColor: '#9aa4af',
      tertiaryColor: '#1b1f25' }
  };

  function config(theme, layout) {
    var vars = Object.assign({ fontFamily: 'system-ui, -apple-system, sans-serif', fontSize: '14px' },
      PALETTES[theme === 'dark' ? 'dark' : 'light']);
    return {
      startOnLoad: false,
      securityLevel: 'strict',
      theme: 'base',
      layout: layout,
      htmlLabels: true,
      themeVariables: vars,
      flowchart: { htmlLabels: true, curve: 'rounded', nodeSpacing: 44, rankSpacing: 52,
        padding: 14, subGraphTitleMargin: { top: 14, bottom: 10 } },
      elk: { mergeEdges: false, nodePlacementStrategy: 'BRANDES_KOEPF' }
    };
  }

  function fail(error) {
    var out = document.createElement('pre');
    out.className = 'mm-err';
    var head = document.createElement('b');
    head.textContent = String((error && error.message) || error).split('\n')[0] + '\n\n';
    out.appendChild(head);
    out.appendChild(document.createTextNode(src));
    el.replaceWith(out);
  }

  function boxId(node) {
    return (node.id || '').replace(/^.*?flowchart-/, '').replace(/-\d+$/, '');
  }

  // Tapping a box picks it out with the boxes and arrows joined to it, and opens a
  // card with its text and its note.
  function interact(edges) {
    var svg = el.querySelector('svg');
    var pop = document.createElement('div');
    pop.className = 'mm-pop';
    pop.hidden = true;
    document.body.appendChild(pop);

    function clear() {
      if (!svg.classList.contains('mm-focus')) return;
      svg.classList.remove('mm-focus');
      svg.querySelectorAll('.mm-on').forEach(function (n) { n.classList.remove('mm-on'); });
      pop.hidden = true;
      parent.postMessage({ agentdeckDiagramBox: null }, '*');
    }

    svg.querySelectorAll('g.node').forEach(function (node) {
      node.style.cursor = 'pointer';
      node.addEventListener('click', function (event) {
        event.stopPropagation();
        if (dragged) return;
        var id = boxId(node);
        clear();
        svg.classList.add('mm-focus');
        node.classList.add('mm-on');
        edges.forEach(function (edge) {
          if (edge.start !== id && edge.end !== id) return;
          var other = edge.start === id ? edge.end : edge.start;
          svg.querySelectorAll('g.node').forEach(function (n) {
            if (boxId(n) === other) n.classList.add('mm-on');
          });
          (edge.paths || []).forEach(function (p) { p.classList.add('mm-on'); });
          (edge.labels || []).forEach(function (p) { p.classList.add('mm-on'); });
        });
        // innerText keeps a line break between a box's lines, which textContent drops.
        var shown = node.querySelector('.nodeLabel') || node;
        var label = (shown.innerText || shown.textContent).replace(/\s+/g, ' ').trim();
        pop.textContent = '';
        var title = document.createElement('b');
        title.textContent = label;
        pop.appendChild(title);
        // Compared without spaces: a box drawn on two lines reads as one run of text.
        // The page around says which box is picked, so the live page can send a
        // change request about it.
        parent.postMessage({ agentdeckDiagramBox: label }, '*');
        var text = notes[label.replace(/\s+/g, '').toLowerCase()];
        if (text) {
          var note = document.createElement('p');
          note.textContent = text;
          pop.appendChild(note);
        }
        pop.hidden = false;
        if (window.agentdeckFill) return;
        var r = node.getBoundingClientRect();
        var left = Math.max(4, Math.min(r.left + r.width / 2 - pop.offsetWidth / 2,
          document.documentElement.clientWidth - pop.offsetWidth - 4));
        pop.style.left = left + 'px';
        pop.style.top = (r.bottom + window.scrollY + 6) + 'px';
      });
    });
    document.addEventListener('click', function () { if (!dragged) clear(); });
  }

  // Each drawn arrow, matched to its two boxes from the parsed diagram. Mermaid names
  // an arrow `L_<from>_<to>_<n>` (after the drawing's own prefix); its label carries
  // the same name.
  function edgeList() {
    return mermaid.mermaidAPI.getDiagramFromText(src).then(function (diagram) {
      var db = diagram.db, list = db.getEdges ? db.getEdges() : [];
      return list.map(function (edge) {
        var name = 'L_' + edge.start + '_' + edge.end + '_';
        var match = function (n) {
          var key = n.getAttribute('data-id') || n.id || '';
          return key.indexOf(name) !== -1;
        };
        return {
          start: edge.start,
          end: edge.end,
          paths: [].filter.call(el.querySelectorAll('path'), match),
          labels: [].filter.call(el.querySelectorAll('g.edgeLabel g.label, g.edgeLabel'), function (n) {
            return match(n) || (n.parentNode && n.parentNode.getAttribute && match(n.parentNode));
          })
        };
      });
    }).catch(function () { return []; });
  }

  // Boxes the diagram colours itself (a `classDef` with a fill) keep that colour; the
  // quiet palette in the frame's style applies to the rest.
  function markOwnColours() {
    var own = {};
    src.split('\n').forEach(function (line) {
      var m = line.match(/^\s*classDef\s+(\w+)\s+.*fill:/);
      if (m) own[m[1]] = true;
    });
    el.querySelectorAll('g.node').forEach(function (node) {
      for (var name in own) if (node.classList.contains(name)) node.classList.add('mm-own');
    });
  }

  // Finishing touches Mermaid has no setting for: rounded boxes and panels, each group's
  // title moved to its panel's top-left corner as a small heading, and a group drawn
  // inside another as a dashed outline rather than a second filled panel.
  function polish() {
    el.querySelectorAll('g.node rect').forEach(function (r) {
      r.setAttribute('rx', 8); r.setAttribute('ry', 8);
    });
    var clusters = [].map.call(el.querySelectorAll('g.cluster'), function (g) {
      var r = g.querySelector('rect');
      return { g: g, r: r, b: r && r.getBBox() };
    }).filter(function (c) { return c.r; });
    clusters.forEach(function (c) {
      c.r.setAttribute('rx', 12); c.r.setAttribute('ry', 12);
      if (clusters.some(function (o) { return o !== c && contains(o.b, c.b); })) {
        c.g.classList.add('mm-inner');
      }
      var label = c.g.querySelector('.cluster-label');
      if (!label) return;
      var lb = label.getBBox();
      label.setAttribute('transform', 'translate(' + (c.b.x + 14) + ',' + (c.b.y + 10) + ')');
      if (lb.width > c.b.width - 28) label.setAttribute('transform',
        'translate(' + (c.b.x + 8) + ',' + (c.b.y + 8) + ')');
    });
  }

  function contains(o, b) {
    return o.x <= b.x && o.y <= b.y && o.x + o.width >= b.x + b.width && o.y + o.height >= b.y + b.height;
  }

  // Colour coding, in two layers. Each top-level group gets its own hue, carried by its
  // panel, its heading and the boxes inside it (a group inside another takes its
  // parent's). On top of that, a box the diagram puts in one of the named classes
  // (`class a,b amber`) is filled with that colour, and the file's legend says what
  // each colour means. Both follow light and dark.
  var HUES = { blue: 214, violet: 262, teal: 174, amber: 36, rose: 340, green: 142 };
  var GROUP_HUES = [214, 262, 174, 36, 340, 142];
  function hsl(h, sat, light) { return 'hsl(' + h + ' ' + sat + '% ' + light + '%)'; }
  function paint(node, prop, value) { node.style.setProperty(prop, value, 'important'); }
  function colour() {
    var n = 0;
    // Groups take only the hues that mean nothing here: none a box is given and none
    // the file's legend names (`agentdeckReservedColours`), so a colour means one
    // thing. With all six taken, groups stay neutral grey rather than borrow one.
    var classes = src.split('\n').filter(function (l) {
      return /^\s*class\s/.test(l) || /:::/.test(l);
    }).join(' ');
    var reserved = window.agentdeckReservedColours || [];
    var used = Object.keys(HUES).filter(function (k) {
      return reserved.indexOf(k) !== -1 || new RegExp('\\b' + k + '\\b').test(classes);
    }).map(function (k) { return HUES[k]; });
    var free = GROUP_HUES.filter(function (h) { return used.indexOf(h) === -1; });
    var clusters = [].map.call(el.querySelectorAll('g.cluster'), function (g) {
      var r = g.querySelector('rect');
      return { g: g, r: r, b: r.getBBox() };
    });
    clusters.forEach(function (c) {
      var parent = clusters.filter(function (o) { return o !== c && contains(o.b, c.b); })
        .sort(function (a, b) { return a.b.width * a.b.height - b.b.width * b.b.height; })[0];
      c.parent = parent;
      c.g.mmParent = parent && parent.g;
    });
    clusters.forEach(function (c) {
      var root = c;
      while (root.parent) root = root.parent;
      if (root.hue === undefined) root.hue = free.length ? free[n++ % free.length] : null;
      c.hue = root.hue;
      if (c.hue === null) return;
      var shape = c.r, heading = c.g.querySelectorAll('.cluster-label, .cluster-label *');
      if (c.parent) {
        paint(shape, 'stroke', dark ? hsl(c.hue, 35, 36) : hsl(c.hue, 45, 75));
      } else {
        paint(shape, 'fill', dark ? hsl(c.hue, 32, 15) : hsl(c.hue, 75, 95));
        paint(shape, 'stroke', dark ? hsl(c.hue, 38, 32) : hsl(c.hue, 55, 78));
      }
      heading.forEach(function (h) {
        paint(h, 'color', dark ? hsl(c.hue, 55, 72) : hsl(c.hue, 45, 38));
        paint(h, 'fill', dark ? hsl(c.hue, 55, 72) : hsl(c.hue, 45, 38));
      });
    });
    el.querySelectorAll('g.node').forEach(function (node) {
      var named = Object.keys(HUES).filter(function (k) { return node.classList.contains(k); })[0];
      var shapes = node.querySelectorAll('rect, polygon, circle, path, ellipse');
      var texts = node.querySelectorAll('.nodeLabel, text, span, p');
      if (named) {
        var h = HUES[named];
        node.classList.add('mm-own');
        shapes.forEach(function (sh) {
          paint(sh, 'fill', dark ? hsl(h, 45, 25) : hsl(h, 88, 88));
          paint(sh, 'stroke', dark ? hsl(h, 65, 58) : hsl(h, 70, 46));
          paint(sh, 'stroke-width', '1.5px');
        });
        texts.forEach(function (t) {
          paint(t, 'color', dark ? hsl(h, 80, 88) : hsl(h, 60, 24));
          paint(t, 'fill', dark ? hsl(h, 80, 88) : hsl(h, 60, 24));
        });
        return;
      }
      if (node.classList.contains('mm-own')) return;
      var b = node.getBBox(), m = node.transform.baseVal.consolidate();
      var box = { x: b.x + (m ? m.matrix.e : 0), y: b.y + (m ? m.matrix.f : 0), width: b.width, height: b.height };
      var home = clusters.filter(function (c) { return contains(c.b, box); })
        .sort(function (a, c) { return a.b.width * a.b.height - c.b.width * c.b.height; })[0];
      if (!home || home.hue === null) return;
      shapes.forEach(function (sh) {
        paint(sh, 'stroke', dark ? hsl(home.hue, 35, 42) : hsl(home.hue, 50, 72));
      });
    });
  }

  // Arrows are drawn after the groups, so they run over a group's heading. Each heading
  // moves above them on a small backing in its panel's colour, so a line passes behind
  // the words.
  function liftHeadings() {
    var svgNs = 'http://www.w3.org/2000/svg';
    var layer = document.createElementNS(svgNs, 'g');
    layer.setAttribute('class', 'mm-headings');
    el.querySelectorAll('g.cluster').forEach(function (g) {
      var label = g.querySelector('.cluster-label');
      if (!label) return;
      var fill = getComputedStyle(g.querySelector('rect')).fill;
      for (var up = g; fill === 'none' || /rgba\(.*,\s*0\)$/.test(fill) || fill === 'transparent';) {
        up = up.mmParent;
        fill = up ? getComputedStyle(up.querySelector('rect')).fill : (dark ? '#15181d' : '#f6f7f9');
        if (!up) break;
      }
      var box = label.getBBox(), m = label.transform.baseVal.consolidate();
      var x = box.x + (m ? m.matrix.e : 0), y = box.y + (m ? m.matrix.f : 0);
      var wrap = document.createElementNS(svgNs, 'g');
      var outer = g.transform.baseVal.consolidate();
      if (outer) wrap.setAttribute('transform', 'translate(' + outer.matrix.e + ',' + outer.matrix.f + ')');
      var back = document.createElementNS(svgNs, 'rect');
      back.setAttribute('x', x - 5); back.setAttribute('y', y - 2);
      back.setAttribute('width', box.width + 10); back.setAttribute('height', box.height + 4);
      back.setAttribute('rx', 4);
      back.style.fill = fill;
      wrap.appendChild(back);
      wrap.appendChild(label);
      layer.appendChild(wrap);
    });
    var root = el.querySelector('svg > g') || el.querySelector('svg');
    root.appendChild(layer);
  }

  // The full-screen view: the drawing at its own size on a canvas the frame fills,
  // fitted to the screen at first. Drag to pan, wheel or pinch to zoom, and the page's
  // −, Fit and + buttons ask for the same by message.
  var view = { x: 0, y: 0, k: 1 }, dragged = false;
  function canvas() {
    document.documentElement.classList.add('mm-fill');
    var svg = el.querySelector('svg'), vb = svg.viewBox.baseVal;
    svg.setAttribute('width', vb.width);
    svg.setAttribute('height', vb.height);
    svg.style.maxWidth = 'none';
    var stage = document.createElement('div');
    stage.className = 'mm-stage';
    el.parentNode.insertBefore(stage, el);
    stage.appendChild(el);
    function apply() {
      el.style.transform = 'translate(' + view.x + 'px,' + view.y + 'px) scale(' + view.k + ')';
    }
    // Fit shows the whole drawing. The first view instead starts readable: text at its
    // own size (or the width, if narrower), from the top; drag down for the rest.
    function fit() {
      var top = 72, w = innerWidth, h = innerHeight - top;
      view.k = Math.min(w / vb.width, h / vb.height, 1.5) * 0.94;
      view.x = (w - vb.width * view.k) / 2;
      view.y = top + (h - vb.height * view.k) / 2;
      apply();
    }
    function start() {
      var top = 72, w = innerWidth, h = innerHeight - top;
      if (vb.height * Math.min(1, w / vb.width) <= h) { fit(); return; }
      view.k = Math.min(1, (w / vb.width) * 0.94);
      view.x = (w - vb.width * view.k) / 2;
      view.y = top;
      apply();
    }
    function zoomAt(f, cx, cy) {
      var k = Math.max(0.1, Math.min(6, view.k * f));
      view.x = cx - (cx - view.x) * (k / view.k);
      view.y = cy - (cy - view.y) * (k / view.k);
      view.k = k;
      apply();
    }
    var points = {}, last = null;
    stage.addEventListener('pointerdown', function (e) {
      points[e.pointerId] = { x: e.clientX, y: e.clientY };
      dragged = false;
      last = null;
    });
    addEventListener('pointermove', function (e) {
      var p = points[e.pointerId];
      if (!p) return;
      var ids = Object.keys(points);
      if (ids.length === 1) {
        var dx = e.clientX - p.x, dy = e.clientY - p.y;
        if (Math.abs(dx) + Math.abs(dy) > 3) dragged = true;
        if (dragged) { view.x += dx; view.y += dy; apply(); stage.classList.add('mm-grab'); }
      }
      points[e.pointerId] = { x: e.clientX, y: e.clientY };
      if (ids.length === 2) {
        var a = points[ids[0]], b = points[ids[1]];
        var d = Math.hypot(a.x - b.x, a.y - b.y);
        if (last) zoomAt(d / last, (a.x + b.x) / 2, (a.y + b.y) / 2);
        last = d;
        dragged = true;
      }
    });
    function up(e) { delete points[e.pointerId]; last = null; stage.classList.remove('mm-grab'); }
    addEventListener('pointerup', up);
    addEventListener('pointercancel', up);
    // A trackpad's two-finger scroll (and a mouse wheel) pans; a pinch zooms. Chrome
    // and Firefox report a pinch as a wheel with ctrlKey, and Ctrl/⌘ + wheel zooms too.
    stage.addEventListener('wheel', function (e) {
      e.preventDefault();
      var unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? innerHeight : 1;
      if (e.ctrlKey || e.metaKey) {
        zoomAt(Math.exp(-e.deltaY * unit * 0.01), e.clientX, e.clientY);
      } else {
        view.x -= e.deltaX * unit; view.y -= e.deltaY * unit; apply();
      }
    }, { passive: false });
    // Safari reports a trackpad pinch as gesture events with a running scale.
    var gestureScale = 1;
    stage.addEventListener('gesturestart', function (e) { e.preventDefault(); gestureScale = 1; });
    stage.addEventListener('gesturechange', function (e) {
      e.preventDefault();
      zoomAt(e.scale / gestureScale, e.clientX || innerWidth / 2, e.clientY || innerHeight / 2);
      gestureScale = e.scale;
    });
    stage.addEventListener('gestureend', function (e) { e.preventDefault(); });
    addEventListener('message', function (e) {
      var z = e.data && e.data.agentdeckZoom;
      if (z === 'fit') fit();
      else if (z === 'in') zoomAt(1.25, innerWidth / 2, innerHeight / 2);
      else if (z === 'out') zoomAt(0.8, innerWidth / 2, innerHeight / 2);
    });
    start();
  }

  if (!window.mermaid) { fail('Mermaid did not load'); return; }
  import(VENDOR + 'mermaid-elk/mermaid-layout-elk.esm.min.mjs').then(function (m) {
    mermaid.registerLayoutLoaders(m.default);
    return 'elk';
  }, function () { return 'dagre'; }).then(function (layout) {
    document.body.dataset.layout = layout;
    mermaid.initialize(config(dark ? 'dark' : 'default', layout));
    return mermaid.run({ nodes: [el] })
      .then(markOwnColours)
      .then(polish)
      .then(colour)
      .then(liftHeadings)
      .then(function () { if (window.agentdeckFill) canvas(); })
      .then(edgeList)
      .then(interact);
  }).catch(fail);
})();
