(function () {
  'use strict';

  var STORAGE_KEY = 'agentdeck:poller-board-filters';
  var OPEN_STORAGE_KEY = 'agentdeck:poller-board-open';

  function savedValues(key) {
    try {
      var parsed = JSON.parse(sessionStorage.getItem(key) || '{}');
      return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
    } catch (_error) {
      return {};
    }
  }

  function saveValue(key, instance, value) {
    var saved = savedValues(key);
    saved[instance] = value;
    try {
      sessionStorage.setItem(key, JSON.stringify(saved));
    } catch (_error) {
      // Filtering still works when storage is unavailable.
    }
  }

  function applyFilter(board, requested) {
    var buttons = board.querySelectorAll('[data-board-filter]');
    var status = requested || '';
    var valid = false;
    for (var index = 0; index < buttons.length; index++) {
      if (buttons[index].dataset.boardFilter === status) valid = true;
    }
    if (!valid) status = '';

    var rows = board.querySelectorAll('[data-board-status]');
    var shown = 0;
    for (var rowIndex = 0; rowIndex < rows.length; rowIndex++) {
      var visible = !status || rows[rowIndex].dataset.boardStatus === status;
      rows[rowIndex].hidden = !visible;
      if (visible) shown += 1;
    }
    for (var buttonIndex = 0; buttonIndex < buttons.length; buttonIndex++) {
      buttons[buttonIndex].setAttribute(
        'aria-pressed',
        String(buttons[buttonIndex].dataset.boardFilter === status)
      );
    }
    var result = board.querySelector('.poller-board-result');
    if (result) {
      result.textContent = status
        ? 'Showing ' + shown + ' ' + status + ' issue' + (shown === 1 ? '' : 's')
        : 'Showing all ' + rows.length + ' issues';
    }
    board.dataset.boardFilterActive = status;
    return status;
  }

  function initialize(root) {
    var saved = savedValues(STORAGE_KEY);
    var savedOpen = savedValues(OPEN_STORAGE_KEY);
    var boards = (root || document).querySelectorAll('details.poller-board');
    for (var index = 0; index < boards.length; index++) {
      var card = boards[index].closest('[data-instance]');
      var instance = card ? card.dataset.instance : '';
      applyFilter(boards[index], saved[instance] || '');
      if (Object.prototype.hasOwnProperty.call(savedOpen, instance)) {
        boards[index].open = Boolean(savedOpen[instance]);
      }
    }
  }

  document.addEventListener('click', function (event) {
    var button = event.target.closest('[data-board-filter]');
    if (!button) return;
    var board = button.closest('details.poller-board');
    var card = board && board.closest('[data-instance]');
    if (!board || !card) return;
    saveValue(
      STORAGE_KEY,
      card.dataset.instance,
      applyFilter(board, button.dataset.boardFilter)
    );
  });

  document.addEventListener('toggle', function (event) {
    var board = event.target;
    if (!board.matches || !board.matches('details.poller-board')) return;
    var card = board.closest('[data-instance]');
    if (card) saveValue(OPEN_STORAGE_KEY, card.dataset.instance, board.open);
  }, true);

  document.addEventListener('DOMContentLoaded', function () { initialize(document); });
  document.addEventListener('htmx:load', function (event) { initialize(event.detail.elt); });
}());
