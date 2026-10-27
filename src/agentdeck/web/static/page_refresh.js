// The topbar's reload button (#2240). Installed from the home screen the deck
// runs without any browser controls, so there is no reload button and no
// pull-to-refresh — this is the only way to reload a page there. A message
// draft survives it through the composer's sessionStorage restore.
(function () {
  'use strict';

  document.addEventListener('click', function (event) {
    var button = event.target instanceof Element &&
      event.target.closest('[data-page-refresh]');
    if (!button) return;
    event.preventDefault();
    button.classList.add('is-refreshing');
    button.disabled = true;
    window.location.reload();
  });
})();
