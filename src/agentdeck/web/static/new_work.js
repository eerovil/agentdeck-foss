// The New work form (#2415): which repository it starts on, whether it can attach files, and
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
    var evidence = option.getAttribute('data-evidence') || '';
    var attach = form.querySelector('[data-new-work-attach]');
    if (attach) {
      attach.hidden = !evidence;
      var name = attach.querySelector('[data-new-work-evidence]');
      if (name) name.textContent = evidence;
      var files = attach.querySelector('input[type="file"][name="images"]');
      // A project with nowhere to keep files must not send the ones picked for another.
      if (!evidence && files && files.files && files.files.length) {
        files.value = '';
        files.dispatchEvent(new Event('change', { bubbles: true }));
      }
    }
    var spec = form.querySelector('[data-new-work-spec-only]');
    if (spec) spec.hidden = !option.getAttribute('data-spec-only');
    warm(form);
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
    var current = select.selectedOptions[0];
    if (current && current.getAttribute('data-project') === project) return;
    var options = Array.from(select.options).filter(function (o) {
      return o.getAttribute('data-project') === project;
    });
    if (!options.length) return;
    var last = lastRepository();
    var pick = options.filter(function (o) { return o.value === last; })[0] || options[0];
    select.value = pick.value;
    sync(form);
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
  // else the one last filed to, else its first repository.
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
    var current = select.selectedOptions[0];
    if (current && current.getAttribute('data-project') === project) return current;
    var inProject = Array.prototype.filter.call(select.options, function (o) {
      return o.getAttribute('data-project') === project;
    });
    var last = lastRepository();
    return inProject.filter(function (o) { return o.value === last; })[0] || inProject[0];
  }

  function choose(form, value) {
    var select = form.querySelector('[data-new-work-repository]');
    if (!select || !value || !guessable(form)) return;
    if (!Array.from(select.options).some(function (o) { return o.value === value; })) return;
    if (select.value !== value) {
      select.value = value;
      sync(form);
    }
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
