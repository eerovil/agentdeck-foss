// New-session settings pickers: keep model and effort selects in sync with the
// chosen account provider. Options for other providers are removed (not just
// hidden — mobile Safari ignores `hidden` on <option>), and empty fields vanish.
//
// The account control is a <select> on the chat page's continuation form and a
// radio group on the new-chat form (#1912), so everything below reads it through
// `chosen()` — the selected <option> or the checked radio, whichever is there.
// Both fire `change` on the container, so one listener covers each.
(function () {
  function initPicker(root) {
    if (root.dataset.modelPickerReady) return;
    var account = root.querySelector('[data-model-picker-account]');
    if (!account) return;
    root.dataset.modelPickerReady = 'true';

    function chosen() {
      if (account.tagName === 'SELECT') return account.options[account.selectedIndex] || null;
      return account.querySelector('input[type="radio"]:checked');
    }

    function selectedProvider() {
      var opt = chosen();
      return opt ? opt.getAttribute('data-provider') : '';
    }

    function setting(selectSelector, fieldSelector, defaultAttribute) {
      var select = root.querySelector(selectSelector);
      var field = root.querySelector(fieldSelector);
      if (!select || !field) return null;
      var all = Array.prototype.map.call(
        select.querySelectorAll('option[data-provider]'),
        function (opt) {
          return {
            value: opt.value,
            label: opt.textContent,
            provider: opt.getAttribute('data-provider'),
          };
        }
      );
      return function rebuild() {
        var provider = selectedProvider();
        var previous = select.value;
        select.textContent = '';
        var def = document.createElement('option');
        def.value = '';
        var accountOption = chosen();
        var defaultValue = accountOption
          ? accountOption.getAttribute(defaultAttribute)
          : '';
        def.textContent = 'Default (' + (defaultValue || 'account') + ')';
        select.appendChild(def);
        var keepPrevious = false;
        all.forEach(function (entry) {
          if (entry.provider !== provider) return;
          var opt = document.createElement('option');
          opt.value = entry.value;
          opt.textContent = entry.label;
          opt.setAttribute('data-provider', entry.provider);
          select.appendChild(opt);
          if (entry.value === previous) keepPrevious = true;
        });
        select.value = keepPrevious ? previous : '';
        field.hidden = select.options.length <= 1;
      };
    }

    var rebuilders = [
      setting(
        '[data-model-picker-select]',
        '[data-model-picker-field]',
        'data-default-model'
      ),
      setting(
        '[data-effort-picker-select]',
        '[data-effort-picker-field]',
        'data-default-effort'
      ),
    ].filter(Boolean);

    // The collapsed "Edit advanced" line reads out what will actually be used, so
    // model and effort stay legible without opening the disclosure (#1912). A
    // hidden field contributes nothing: the provider does not offer that choice.
    var summary = root.querySelector('[data-advanced-summary]');

    function currentText(selectSelector, fieldSelector, defaultAttribute) {
      var select = root.querySelector(selectSelector);
      var field = root.querySelector(fieldSelector);
      if (!select || !field || field.hidden) return '';
      if (select.value) {
        var opt = select.options[select.selectedIndex];
        return opt ? opt.textContent : '';
      }
      var accountOption = chosen();
      return accountOption ? accountOption.getAttribute(defaultAttribute) || '' : '';
    }

    // The server's own sentinel for "this account records no explicit default"
    // (choice_label in account_settings.py). Printed raw it reads as nonsense —
    // "account · account" — so the line says what it means instead (#1912).
    var ACCOUNT_DEFAULT = 'account';

    function updateSummary() {
      if (!summary) return;
      var parts = [
        currentText(
          '[data-model-picker-select]',
          '[data-model-picker-field]',
          'data-default-model'
        ),
        currentText(
          '[data-effort-picker-select]',
          '[data-effort-picker-field]',
          'data-default-effort'
        ),
      ].filter(function (part) { return part; });
      var allDefault = parts.length && parts.every(function (part) {
        return part === ACCOUNT_DEFAULT;
      });
      summary.textContent = allDefault
        ? 'Account defaults'
        : parts
            .map(function (part) {
              return part === ACCOUNT_DEFAULT ? 'account default' : part;
            })
            .join(' · ');
    }

    function rebuild() {
      rebuilders.forEach(function (fn) { fn(); });
      updateSummary();
    }

    if (!rebuilders.length) return;
    account.addEventListener('change', rebuild);
    // Changing model or effort by hand moves the read-only line too, so a closed
    // disclosure never contradicts the selects inside it.
    root.addEventListener('change', function (event) {
      if (
        event.target.matches('[data-model-picker-select], [data-effort-picker-select]')
      ) {
        updateSummary();
      }
    });
    rebuild();
  }

  function init() {
    document.querySelectorAll('[data-model-picker]').forEach(initPicker);
  }

  window.AgentDeckModelPicker = { init: init };
  document.addEventListener('agentdeck:session-navigation', init);

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
