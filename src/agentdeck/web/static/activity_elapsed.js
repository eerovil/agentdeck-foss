(function () {
  'use strict';

  // The server renders the elapsed seconds a tool has been running at render
  // time; this keeps ticking from there. It re-reads the document every second
  // instead of binding to one node, so a detail pane mounted by client-side
  // session navigation is picked up without any re-initialization.
  function formatElapsed(seconds) {
    if (seconds < 60) return seconds + 's';
    var minutes = Math.floor(seconds / 60);
    return minutes + 'm ' + (seconds % 60) + 's';
  }

  function updateActivityElapsed() {
    var marker = document.querySelector('[data-activity-elapsed]');
    if (!marker) return;
    if (!marker._agentdeckMountedAt) marker._agentdeckMountedAt = Date.now();
    var base = Number(marker.dataset.activityElapsed) || 0;
    var elapsed = base + Math.floor((Date.now() - marker._agentdeckMountedAt) / 1000);
    var output = marker.querySelector('.tool-wait-elapsed');
    if (output) output.textContent = formatElapsed(elapsed);
  }

  updateActivityElapsed();
  window.setInterval(updateActivityElapsed, 1000);
})();
