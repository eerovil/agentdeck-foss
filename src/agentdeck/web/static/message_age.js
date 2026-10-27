(function () {
  'use strict';

  function formatAge(seconds) {
    if (seconds < 5) return 'just now';
    if (seconds < 60) return Math.floor(seconds) + 's ago';
    var minutes = Math.floor(seconds / 60);
    if (minutes < 60) return minutes + 'm ago';
    var hours = Math.floor(minutes / 60);
    minutes %= 60;
    if (hours < 24) return hours + 'h ' + minutes + 'm ago';
    var days = Math.floor(hours / 24);
    hours %= 24;
    return days + 'd ' + hours + 'h ago';
  }

  function updateActivityAges() {
    var now = Date.now() / 1000;
    document.querySelectorAll('time[data-activity-age]').forEach(function (marker) {
      var since = Date.parse(marker.getAttribute('datetime')) / 1000;
      if (Number.isFinite(since)) marker.textContent = formatAge(Math.max(0, now - since));
    });
  }

  updateActivityAges();
  window.setInterval(updateActivityAges, 1000);
})();
