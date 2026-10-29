// Live HTML in an agent's reply (#3547). An `html-live` code block in an assistant
// message is shown as a sandboxed frame inside the message instead of as code, so an
// agent can hand the reader an audio player, a small table or a widget. The frame has
// scripts but no same-origin access: it cannot read the deck page, its drafts or its
// storage. Its document inherits the page's base URL, so `/var/tmp/a.wav` loads that
// host file through the deck's file route.
//
// `mdToHtml` in base.html builds the frame from `srcdoc(...)`; this file sizes each
// frame to its content, from the height the frame's own prelude script reports.
(function () {
  'use strict';

  var MAX_HEIGHT = 4000;

  // The sandbox keeps the frame off the deck page, but not off the deck's routes: none
  // of them ask for a login, so a script that could send requests could pause a poller
  // or stop a chat. This policy comes before the agent's HTML, so nothing it adds can
  // loosen it: no fetch, beacon or form; images and media still load from the deck
  // (the same rules as the file preview's `_PREVIEW_CSP` in routes_files.py).
  var DECK = window.location.origin;
  var POLICY = [
    "default-src 'none'",
    "script-src 'unsafe-inline'",
    "style-src 'unsafe-inline'",
    'img-src ' + DECK + ' data: blob:',
    'font-src ' + DECK + ' data:',
    'media-src ' + DECK + ' data: blob:',
    "connect-src 'none'",
    "object-src 'none'",
    "frame-src 'none'",
    "form-action 'none'",
    "base-uri 'none'"
  ].join('; ');

  // Prepended to the agent's HTML. The color scheme follows the device, as the deck's
  // does, so the frame's text matches the message around it. `flow-root` keeps a first
  // or last child's margin inside the measured height.
  var PRELUDE =
    '<!doctype html><meta charset="utf-8">' +
    '<meta http-equiv="Content-Security-Policy" content="' + POLICY + '">' +
    '<meta name="color-scheme" content="light dark">' +
    '<style>html{margin:0}body{margin:0;display:flow-root;' +
    'font:14px/1.45 system-ui,-apple-system,sans-serif}</style>' +
    '<script>(function(){var last=-1;function post(){' +
    'var h=Math.ceil(document.documentElement.getBoundingClientRect().height);' +
    'if(h!==last){last=h;parent.postMessage({agentdeckLiveHeight:h},"*");}}' +
    'if(window.ResizeObserver)new ResizeObserver(post).observe(document.documentElement);' +
    'addEventListener("load",post);document.addEventListener("DOMContentLoaded",post);' +
    '})();<\/script>';

  function escapeAttr(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // The frame for one block. `escapedHtml` is the block's source already escaped
  // for an attribute (mdToHtml escapes the whole message first), so it goes into
  // `srcdoc` as it is and the browser decodes it back to the agent's HTML.
  function frame(escapedHtml) {
    return '<iframe class="md-live" sandbox="allow-scripts" ' +
      'title="HTML from the agent" srcdoc="' + escapeAttr(PRELUDE) + escapedHtml +
      '"></iframe>';
  }

  // A ```mermaid block in a markdown file drawn as a diagram (#3822): the same sandbox
  // and sizing as `frame`, plus the deck's own Mermaid copy and diagram_frame.js, which
  // draws, styles and makes it clickable. The source is escaped once more, so it reaches
  // the frame as text, and the box notes go in as JSON.
  var MERMAID_SRC = DECK + '/static/vendor/mermaid.min.js';
  var MERMAID_PRELUDE = PRELUDE.replace("script-src 'unsafe-inline'",
    "script-src 'unsafe-inline' " + DECK + '/static/vendor/ ' + DECK + '/static/diagram_frame.js');
  var MERMAID_RUN =
    '<script src="' + MERMAID_SRC + '"><\/script>' +
    '<script src="' + DECK + '/static/diagram_frame.js"><\/script>';

  // `fill` draws the diagram as a canvas filling the frame, with pan and zoom, for
  // the full-screen view; otherwise it is laid out in the page at its own height.
  // `reserved` names the colours the file's legend gives a meaning, which groups skip.
  function mermaidFrame(escapedSource, notes, fill, reserved) {
    var json = JSON.stringify(notes || {}).replace(/</g, '\\u003c');
    var colours = JSON.stringify(reserved || []).replace(/</g, '\\u003c');
    return '<iframe class="md-live md-diagram' + (fill ? ' md-diagram-fill' : '') +
      '" sandbox="allow-scripts" ' +
      'title="Diagram" srcdoc="' + escapeAttr(MERMAID_PRELUDE) +
      escapeAttr('<script>window.agentdeckBoxNotes=' + json + ';window.agentdeckFill=' +
        (fill ? 'true' : 'false') + ';window.agentdeckReservedColours=' + colours +
        ';<\/script>') +
      escapeAttr('<pre class="mermaid">') + escapeAttr(escapedSource) +
      escapeAttr('</pre>' + MERMAID_RUN) + '"></iframe>';
  }

  window.addEventListener('message', function (event) {
    var data = event.data;
    if (!data || typeof data.agentdeckLiveHeight !== 'number') return;
    var frames = document.querySelectorAll('iframe.md-live');
    for (var i = 0; i < frames.length; i++) {
      if (frames[i].contentWindow !== event.source) continue;
      var height = Math.max(0, Math.min(MAX_HEIGHT, data.agentdeckLiveHeight));
      frames[i].style.height = height + 'px';
      return;
    }
  });

  window.AgentdeckLiveHtml = { frame: frame, mermaid: mermaidFrame, PRELUDE: PRELUDE };
})();
