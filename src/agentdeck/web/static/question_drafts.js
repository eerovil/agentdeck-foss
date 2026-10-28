// Keeps what the person picked and typed in a chat's open questions (#3451).
//
// The question box is redrawn whenever a question is added, answered or dismissed, and a
// redraw would otherwise wipe the answers half given to the questions still open. Each
// change is saved per chat in sessionStorage and put back whenever the box is drawn again.
// A secret answer is never saved.
(function () {
  'use strict';

  var PREFIX = 'agentdeck:question-draft:';
  var FIELD = /^(answer|other)__/;

  function storageKey(form) { return PREFIX + form.dataset.questionDrafts; }

  function read(form) {
    try {
      return JSON.parse(sessionStorage.getItem(storageKey(form)) || '{}') || {};
    } catch (_) { return {}; }
  }

  function write(form, saved) {
    try {
      if (Object.keys(saved).length) {
        sessionStorage.setItem(storageKey(form), JSON.stringify(saved));
      } else sessionStorage.removeItem(storageKey(form));
    } catch (_) {}
  }

  function fields(form) {
    return Array.from(form.elements).filter(function (el) {
      return el.name && FIELD.test(el.name) && el.type !== 'password';
    });
  }

  function save(form) {
    var saved = {};
    fields(form).forEach(function (el) {
      if (el.type === 'radio' || el.type === 'checkbox') {
        if (!el.checked) return;
        (saved[el.name] = saved[el.name] || []).push(el.value);
      } else if (el.value) saved[el.name] = el.value;
    });
    write(form, saved);
  }

  function restore(form) {
    var saved = read(form);
    fields(form).forEach(function (el) {
      if (!(el.name in saved)) return;
      var value = saved[el.name];
      if (el.type === 'radio' || el.type === 'checkbox') {
        if (Array.isArray(value) && value.indexOf(el.value) !== -1) el.checked = true;
      } else if (!el.value && typeof value === 'string') el.value = value;
    });
  }

  function restoreWithin(root) {
    if (!root || !root.querySelectorAll) return;
    var forms = root.matches && root.matches('form[data-question-drafts]')
      ? [root] : Array.from(root.querySelectorAll('form[data-question-drafts]'));
    forms.forEach(restore);
  }

  function draftForm(target) {
    return target && target.closest ? target.closest('form[data-question-drafts]') : null;
  }

  document.addEventListener('input', function (event) {
    var form = draftForm(event.target);
    if (form) save(form);
  });
  document.addEventListener('change', function (event) {
    var form = draftForm(event.target);
    if (form) save(form);
  });
  // Enter in a text field presses the form's first submit button, which in an open-questions
  // box is the first question's Dismiss: the answer typed was thrown away (#3749). It sends
  // the answer instead by clicking Submit answer: the form's own checks still run, and htmx
  // takes the decision from the button last clicked, not from requestSubmit's submitter.
  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
    var input = event.target;
    if (!input.matches || !input.matches('input.interaction-other')) return;
    var form = input.form;
    if (!form || !form.matches('form[data-agentdeck-action="interaction"]')) return;
    var submit = form.querySelector('button[name="decision"][value="accept"]');
    if (!submit) return;
    event.preventDefault();
    submit.click();
  });
  // Any redraw of the box: the page's own submit swaps it, the live update pushes it as an
  // SSE message (which fires no htmx:load). Only empty fields are filled, so a repeat is
  // harmless.
  document.addEventListener('htmx:load', function (event) { restoreWithin(event.target); });
  document.addEventListener('htmx:afterSwap', function (event) { restoreWithin(event.target); });
  document.addEventListener('htmx:sseMessage', function () { restoreWithin(document); });
  // A submit that went through: what it answered or dismissed is gone for good, so only
  // the questions the answer leaves in the box keep their saved answers.
  document.addEventListener('htmx:afterRequest', function (event) {
    var form = draftForm(event.detail && event.detail.elt);
    if (!form || !event.detail.successful) return;
    var saved = read(form);
    var key = storageKey(form);
    var box = document.getElementById('pending-interaction');
    var open = {};
    if (box) {
      box.querySelectorAll('[name]').forEach(function (el) { open[el.name] = true; });
    }
    Object.keys(saved).forEach(function (name) { if (!open[name]) delete saved[name]; });
    try {
      if (Object.keys(saved).length) sessionStorage.setItem(key, JSON.stringify(saved));
      else sessionStorage.removeItem(key);
    } catch (_) {}
  });
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { restoreWithin(document); });
  } else restoreWithin(document);
})();
