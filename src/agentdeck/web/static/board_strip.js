// Remember whether the operator left the board strip open (#1007).
//
// The strip renders shut every time, because collapsed is the default the
// mobile vertical-space rule asks for. That would also mean reopening it after
// every reload, so the choice is kept in localStorage and reapplied here —
// deliberate user action only, never a server decision.
(function () {
  "use strict";
  var KEY = "agentdeck.boardStrip.open";

  function apply(strip) {
    if (strip.dataset.boardStripBound === "1") return;
    strip.dataset.boardStripBound = "1";
    try {
      if (window.localStorage.getItem(KEY) === "1") strip.open = true;
    } catch (err) {
      // A browser with storage blocked still gets a working strip.
    }
    strip.addEventListener("toggle", function () {
      try {
        window.localStorage.setItem(KEY, strip.open ? "1" : "0");
      } catch (err) {
        /* nothing to remember with */
      }
    });
  }

  function scan() {
    var strip = document.querySelector("[data-board-strip]");
    if (strip) apply(strip);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", scan);
  } else {
    scan();
  }
  // A swap can replace the strip with a fresh, shut copy; rebind it.
  document.body.addEventListener("htmx:afterSwap", scan);
})();
