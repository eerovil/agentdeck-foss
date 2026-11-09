// The New work form (#2415): which repository it starts on, whether it offers Spec only, and
// clearing it once a filing is known to have landed. Everything else — the draft kept across
// reloads, file chips, paste-a-screenshot, the press's action id — is the deck's own composer
// code, which applies because the form has the composer's shape.
(function () {
  'use strict';

  var LAST_KEY = 'agentdeck:new-work:last-repository';

  function lastRepository() {
    try { return localStorage.getItem(LAST_KEY) || ''; } catch (_) { return ''; }
  }

  function remember(value) {
    try { localStorage.setItem(LAST_KEY, value); } catch (_) {}
  }

  function sync(form) {
    var select = form.querySelector('[data-new-work-repository]');
    var option = select && select.selectedOptions[0];
    if (!option) return;
    var spec = form.querySelector('[data-new-work-spec-only]');
    if (spec) spec.hidden = !option.getAttribute('data-spec-only');
    syncAccounts(form, option.value.toLowerCase());
    warm(form);
  }

  // Edit advanced (#4181): only the accounts this repository's Work may run on are offered;
  // a pick the new repository cannot take goes back to Auto. Where a Poller starts the Work
  // nothing reads a pick, so the box is hidden and every pick in it cleared and not sent.
  function syncAccounts(form, repository) {
    var box = form.querySelector('[data-new-work-advanced]');
    var select = form.querySelector('[data-new-work-account]');
    if (!box || !select) return;
    var served = (box.getAttribute('data-repositories') || '').split(' ');
    var offered = served.indexOf(repository) !== -1;
    box.hidden = !offered;
    if (!offered) box.open = false;
    Array.prototype.forEach.call(box.querySelectorAll('select'), function (each) {
      if (!offered) each.value = '';
      each.disabled = !offered;
    });
    keepOnly(select, function (option) {
      var serves = option.getAttribute('data-repositories');
      return serves === null || serves.split(' ').indexOf(repository) !== -1;
    });
    syncProvider(form);
  }

  // The model and effort lists are the chosen account's provider's own; Auto is the deck's
  // choice, a Claude account. A pick the other provider does not offer goes back to Default.
  function syncProvider(form) {
    var box = form.querySelector('[data-new-work-advanced]');
    var account = form.querySelector('[data-new-work-account]');
    if (!box || !account) return;
    var option = account.value ? account.selectedOptions[0] : null;
    var provider = (option && option.getAttribute('data-provider'))
      || box.getAttribute('data-auto-provider') || '';
    ['[data-new-work-model]', '[data-new-work-effort]'].forEach(function (name) {
      var select = form.querySelector(name);
      if (!select) return;
      keepOnly(select, function (each) {
        var own = each.getAttribute('data-provider');
        return own === null || own === provider;
      });
    });
    summarize(form);
  }

  // Leaves only the options ``wanted`` keeps. They are taken out, not hidden: mobile Safari
  // shows a hidden option anyway. The first call keeps every option, to rebuild from later.
  function keepOnly(select, wanted) {
    if (!select._allOptions) select._allOptions = Array.prototype.slice.call(select.options);
    var previous = select.value;
    var kept = select._allOptions.filter(wanted);
    select.textContent = '';
    kept.forEach(function (option) {
      option.hidden = false;
      option.disabled = false;
      select.appendChild(option);
    });
    select.value = kept.some(function (o) { return o.value === previous; }) ? previous : '';
  }

  // The collapsed line names what the Work will run on, so nothing picked is hidden.
  function summarize(form) {
    var line = form.querySelector('[data-new-work-advanced-summary]');
    if (!line) return;
    function picked(name, fallback) {
      var select = form.querySelector(name);
      var option = select && select.value ? select.selectedOptions[0] : null;
      return option ? option.textContent.trim() : fallback;
    }
    var parts = [
      picked('[data-new-work-account]', 'Auto account'),
      picked('[data-new-work-model]', 'default model')
    ];
    var effort = picked('[data-new-work-effort]', '');
    if (effort) parts.push(effort + ' effort');
    line.textContent = parts.join(' · ');
  }

  // While the form is open, the deck keeps a Work's agent booted for the chosen repository
  // (#3590), so pressing File starts it at once. Asked again on another repository, and every
  // few minutes while the form stays open; the deck forgets it ten minutes after the last ask.
  var WARM_AGAIN_MS = 5 * 60 * 1000;

  function warm(form) {
    var details = form.closest('details');
    var select = form.querySelector('[data-new-work-repository]');
    if ((details && !details.open) || !select || !select.value) return;
    var now = Date.now();
    if (form._warmed === select.value && now - (form._warmedAt || 0) < WARM_AGAIN_MS) return;
    form._warmed = select.value;
    form._warmedAt = now;
    var body = new FormData();
    body.append('repository', select.value);
    fetch('/works/new/warm', { method: 'POST', body: body, credentials: 'same-origin' })
      .catch(function () {});
  }

  // The default is chosen once, when the control is first opened, and never over a choice the
  // person already made: a chat's own repository, else the one last filed to on this device.
  function applyDefault(form) {
    if (form._newWorkDefaulted) return;
    form._newWorkDefaulted = true;
    var select = form.querySelector('[data-new-work-repository]');
    if (!select) return;
    var wanted = form.getAttribute('data-new-work-default') || lastRepository();
    if (wanted && Array.from(select.options).some(function (o) { return o.value === wanted; })) {
      select.value = wanted;
      form._newWorkChosen = true;
    }
    followFilter(form);
    sync(form);
  }

  // On the front page, a list filtered to one project files there (#3253): checked when the
  // control opens and when the filter changes, keeping a pick already inside that project.
  // A chat's own form has its repository from the chat and ignores the filter.
  function followFilter(form) {
    if (form.hasAttribute('data-new-work-default') || form.closest('#session-new-work')) return;
    var front = window.AgentDeckFront;
    var project = front && front.onlyProject ? front.onlyProject() : '';
    var select = form.querySelector('[data-new-work-repository]');
    if (!project || !select) return;
    var current = kept(select);
    if (current && current.getAttribute('data-project') === project) return;
    var options = Array.from(select.options).filter(function (o) {
      return o.getAttribute('data-project') === project;
    });
    if (!options.length) return;
    var pick = projectPick(options);
    select.value = pick.value;
    form._newWorkChosen = true;
    sync(form);
  }

  // The selected option, unless it is only the browser's automatic first one (#3882): a
  // project that sorts first must still land on its own default.
  function kept(select) {
    var form = select.form;
    return form && (form._newWorkPicked || form._newWorkChosen) ? select.selectedOptions[0] : null;
  }

  // Within one project's options: the one last filed to, else the project's own default
  // (its issue tracker, #3882), else its first.
  function projectPick(options) {
    var last = lastRepository();
    return options.filter(function (o) { return o.value === last; })[0]
      || options.filter(function (o) { return o.hasAttribute('data-project-default'); })[0]
      || options[0];
  }

  function followFilters() {
    var forms = document.querySelectorAll('form[data-new-work]');
    Array.prototype.forEach.call(forms, followFilter);
  }

  function init(root) {
    var forms = root.querySelectorAll ? root.querySelectorAll('form[data-new-work]') : [];
    Array.prototype.forEach.call(forms, function (form) {
      if (form._newWorkReady) return;
      form._newWorkReady = true;
      applyDefault(form);
    });
  }

  // Guess the project from what is typed (#3367), on the front page's form only. A project
  // or repository named in the text, as a whole word, decides it for free; failing that,
  // Haiku is asked once the typing pauses. Neither ever moves a project picked by hand, and
  // a front page filtered to one project already says where the Work goes.
  var GUESS_PAUSE_MS = 1000;
  var GUESS_MIN_CHARS = 30;
  var GUESS_MAX_ASKS = 3;

  function guessable(form) {
    if (form._newWorkPicked || form.closest('#session-new-work')) return false;
    var front = window.AgentDeckFront;
    return !(front && front.onlyProject && front.onlyProject());
  }

  function escaped(name) {
    return name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }

  // The option for the project (or whole `owner/repo`) named first in the text, or null.
  // A bare repository name is not enough: `example-org/issues` would claim every
  // description that says "issues". Within a named project, a pick already there stays,
  // else `projectPick`'s.
  function named(select, text) {
    var lower = text.toLowerCase();
    var best = null;
    var bestAt = Infinity;
    var wholeName = false;
    Array.prototype.forEach.call(select.options, function (option) {
      [option.getAttribute('data-project') || '', option.value].forEach(function (name, index) {
        if (name.length < 3) return;
        var match = new RegExp('(^|[^a-z0-9_-])' + escaped(name.toLowerCase()) + '($|[^a-z0-9_/-])')
          .exec(lower);
        if (match && match.index < bestAt) {
          best = option;
          bestAt = match.index;
          wholeName = index === 1;
        }
      });
    });
    if (!best || wholeName) return best;
    var project = best.getAttribute('data-project');
    var current = kept(select);
    if (current && current.getAttribute('data-project') === project) return current;
    var inProject = Array.prototype.filter.call(select.options, function (o) {
      return o.getAttribute('data-project') === project;
    });
    return projectPick(inProject);
  }

  function choose(form, value) {
    var select = form.querySelector('[data-new-work-repository]');
    if (!select || !value || !guessable(form)) return;
    if (!Array.from(select.options).some(function (o) { return o.value === value; })) return;
    if (select.value !== value) {
      select.value = value;
      sync(form);
    }
    form._newWorkChosen = true;
    var note = form.querySelector('[data-new-work-guessed]');
    if (note) note.hidden = false;
  }

  function guess(form) {
    var select = form.querySelector('[data-new-work-repository]');
    var input = form.querySelector('textarea[name="message"]');
    // Switched off on /settings ("Guess for me"), the picker is never moved for you.
    if (!form.hasAttribute('data-guess-project')) return;
    if (!select || !input || select.options.length < 2 || !guessable(form)) return;
    var text = input.value.trim();
    var option = named(select, text);
    if (option) {
      choose(form, option.value);
      return;
    }
    if (text.length < GUESS_MIN_CHARS) return;
    if (text === form._guessAsked || (form._guessAsks || 0) >= GUESS_MAX_ASKS) return;
    form._guessAsked = text;
    form._guessAsks = (form._guessAsks || 0) + 1;
    var body = new FormData();
    body.append('text', text);
    fetch('/works/new/guess-project', { method: 'POST', body: body, credentials: 'same-origin' })
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (answer) {
        // A late answer is for words since changed; the next pause asks again.
        if (answer && answer.repository && input.value.trim() === text) {
          choose(form, answer.repository);
        }
      })
      .catch(function () {});
  }

  document.addEventListener('input', function (event) {
    var form = event.target.form;
    if (!form || !form.matches('form[data-new-work]') || event.target.name !== 'message') return;
    clearTimeout(form._guessTimer);
    form._guessTimer = setTimeout(function () { guess(form); }, GUESS_PAUSE_MS);
  });

  document.addEventListener('change', function (event) {
    var advanced = '[data-new-work-account], [data-new-work-model], [data-new-work-effort]';
    if (event.target.matches && event.target.matches('[data-new-work-account]')) {
      syncProvider(event.target.form);
    } else if (event.target.matches && event.target.matches(advanced)) {
      summarize(event.target.form);
    }
    if (event.target.matches && event.target.matches('[data-new-work-repository]')) {
      // Only a person's pick fires `change`; the guesses above set the value quietly.
      var form = event.target.form;
      form._newWorkPicked = true;
      var note = form.querySelector('[data-new-work-guessed]');
      if (note) note.hidden = true;
      sync(form);
    }
  });

  document.body.addEventListener('htmx:afterRequest', function (event) {
    var form = event.detail && event.detail.elt;
    if (!form || !form.matches || !form.matches('form[data-new-work]')) return;
    var xhr = event.detail.xhr;
    var result = form.parentElement && form.parentElement.querySelector('[data-new-work-result]');
    if (xhr && xhr.getResponseHeader('X-AgentDeck-New-Work') === 'filed') {
      var select = form.querySelector('[data-new-work-repository]');
      if (select) remember(select.value);
      // The next Work starts fresh: its project may be guessed again.
      form._newWorkPicked = false;
      form._guessAsked = '';
      form._guessAsks = 0;
      var guessed = form.querySelector('[data-new-work-guessed]');
      if (guessed) guessed.hidden = true;
      var input = form.querySelector('textarea[name="message"]');
      if (input) {
        input.value = '';
        // The composer's draft keeping listens for input and forgets an empty draft.
        input.dispatchEvent(new Event('input', { bubbles: true }));
      }
      var files = form.querySelector('input[type="file"][name="images"]');
      if (files) {
        files.value = '';
        files.dispatchEvent(new Event('change', { bubbles: true }));
      }
      return;
    }
    // No answer at all — the connection dropped, the phone slept, the deck restarted. The
    // filing may still have landed, so say so and keep the words rather than invite a retry.
    if (result && (!xhr || xhr.status === 0 || xhr.status >= 500)) {
      var chosen = form.querySelector('[data-new-work-repository]');
      result.textContent = '';
      var note = document.createElement('p');
      note.className = 'new-work-receipt is-uncertain';
      note.setAttribute('data-new-work-receipt', 'uncertain');
      note.setAttribute('role', 'status');
      note.textContent = 'No answer from the deck, so this may have been filed — ';
      var link = document.createElement('a');
      link.href = 'https://github.com/' + (chosen ? chosen.value : '') + '/issues';
      link.target = '_blank';
      link.rel = 'noopener';
      link.textContent = 'check the issues before filing again';
      note.appendChild(link);
      result.appendChild(note);
    }
  });

  document.body.addEventListener('htmx:beforeRequest', function (event) {
    var form = event.detail && event.detail.elt;
    if (!form || !form.matches || !form.matches('form[data-new-work]')) return;
    var result = form.parentElement && form.parentElement.querySelector('[data-new-work-result]');
    if (result) result.textContent = 'Filing…';
  });

  init(document);
  document.addEventListener('toggle', function (event) {
    if (!event.target.matches || !event.target.matches('details.new-work')) return;
    init(event.target);
    if (event.target.open) {
      followFilters();
      var form = event.target.querySelector('form[data-new-work]');
      if (form) warm(form);
    }
  }, true);
  document.addEventListener('agentdeck:front-projects', followFilters);
  document.body.addEventListener('htmx:load', function (event) { init(event.target); });
})();
