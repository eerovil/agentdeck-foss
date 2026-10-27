(function () {
  'use strict';

  var records = new Map();
  var pendingNavigationKey = 'agentdeck.pendingNavigation';
  var lastPresenceReportAt = -Infinity;
  var presenceReportIntervalMs = 30000;
  var relevantSseTargets = new Set([
    'session-status',
    'composer-controls',
    'pending-interaction',
    'inject-result',
    'sessions'
  ]);

  function uuid() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
      return window.crypto.randomUUID();
    }
    var bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    return Array.from(bytes, function (value) {
      return value.toString(16).padStart(2, '0');
    }).join('');
  }

  function recordMark(record, phase, relativeTime, epochTime) {
    if (!record || record.marks[phase] !== undefined) return;
    record.marks[phase] = relativeTime;
    record.epochMarks = record.epochMarks || {};
    record.epochMarks[phase] = epochTime;
    try {
      performance.mark('agentdeck:' + record.id + ':' + phase, {startTime: relativeTime});
    } catch (_) {
      performance.mark('agentdeck:' + record.id + ':' + phase);
    }
    document.dispatchEvent(new CustomEvent('agentdeck:action-timing', {
      detail: { actionId: record.id, action: record.action, phase: phase }
    }));
  }

  function mark(record, phase) {
    var now = performance.now();
    recordMark(record, phase, now, performance.timeOrigin + now);
  }

  function pathName(path) {
    try { return new URL(path, location.href).pathname; } catch (_) { return path; }
  }

  function actionPath(form) {
    return form.getAttribute('hx-post') || form.action || '';
  }

  function sessionKey(path) {
    var match = pathName(path).match(/^\/sessions\/([^/]+)(?:\/|$)/);
    return match ? decodeURIComponent(match[1]) : null;
  }

  function isCurrentSession(key) {
    if (!key) return true;
    if (decodeURIComponent(location.pathname).indexOf(key) !== -1) return true;
    var detail = document.querySelector('.session-detail[data-session-key]');
    return Boolean(detail && detail.dataset.sessionKey === key);
  }

  function hiddenId(form, value) {
    var input = form.querySelector('input[name="client_action_id"]');
    if (!input) {
      input = document.createElement('input');
      input.type = 'hidden';
      input.name = 'client_action_id';
      form.appendChild(input);
    }
    input.value = value;
  }

  function prepare(form) {
    if (!form || !form.matches('form[data-agentdeck-action]')) return null;
    if (form._agentdeckAutoRetryRecord) return form._agentdeckAutoRetryRecord;
    var current = form._agentdeckActionTiming;
    if (current && current.marks.response === undefined) return current;
    var path = actionPath(form);
    var record = {
      id: uuid(),
      action: form.dataset.agentdeckAction,
      path: path,
      sessionKey: sessionKey(path),
      marks: {},
      epochMarks: {},
      serverTiming: ''
    };
    form._agentdeckActionTiming = record;
    records.set(record.id, record);
    if (records.size > 100) records.delete(records.keys().next().value);
    hiddenId(form, record.id);
    mark(record, 'interaction');
    return record;
  }

  function retryForm(form, record, payload) {
    if (!form || form._agentdeckActionTiming !== record ||
        record.marks.response === undefined || !payload ||
        !window.htmx || typeof window.htmx.ajax !== 'function') return false;
    // A lost frontend response is an uncertain write. Reuse the original
    // action ID so the runtime can return the first delivery instead of
    // treating this as a second message.
    delete record.marks.response;
    if (record.epochMarks) delete record.epochMarks.response;
    delete record.status;
    delete record.successful;
    record.serverTiming = '';
    form._agentdeckRetrySubmittedMessage = payload.get('message') || '';
    form._agentdeckRetrySubmittedImages = payload.getAll('images');
    form._agentdeckRetryDispatching = true;
    try {
      window.htmx.ajax('POST', actionPath(form), {
        source: form,
        target: form.getAttribute('hx-target') || undefined,
        swap: form.getAttribute('hx-swap') || undefined,
        values: payload
      });
    } finally {
      form._agentdeckRetryDispatching = false;
    }
    return true;
  }

  function prepareNavigation(link) {
    if (!link || !link.matches('a[data-agentdeck-action]')) return null;
    var current = link._agentdeckActionTiming;
    if (current && current.marks.settled === undefined) return current;
    var record = {
      id: uuid(),
      action: link.dataset.agentdeckAction,
      path: link.href,
      sessionKey: link.dataset.sessionKey || sessionKey(link.href),
      marks: {},
      epochMarks: {},
      serverTiming: ''
    };
    link._agentdeckActionTiming = record;
    records.set(record.id, record);
    if (records.size > 100) records.delete(records.keys().next().value);
    mark(record, 'interaction');
    return record;
  }

  function navigationLink(target) {
    if (!target || !target.closest) return null;
    if (target.closest('.session-actions [role="button"]')) return null;
    return target.closest('a[data-agentdeck-action]');
  }

  function isPlainNavigation(event, link) {
    return !event.defaultPrevented && event.button === 0 && !event.metaKey &&
      !event.ctrlKey && !event.shiftKey && !event.altKey &&
      !link.hasAttribute('download') && (!link.target || link.target === '_self') &&
      new URL(link.href, location.href).origin === location.origin;
  }

  function persistNavigation(record) {
    try { sessionStorage.setItem(pendingNavigationKey, JSON.stringify(record)); } catch (_) {}
  }

  function restoreNavigation() {
    var raw;
    try {
      raw = sessionStorage.getItem(pendingNavigationKey);
      sessionStorage.removeItem(pendingNavigationKey);
    } catch (_) {}
    if (!raw) return;
    var record;
    try { record = JSON.parse(raw); } catch (_) { return; }
    if (!record || !record.action ||
        pathName(record.path) !== location.pathname) return;
    record.marks = record.marks || {};
    record.epochMarks = record.epochMarks || {};
    records.set(record.id, record);
    var navigation = performance.getEntriesByType('navigation')[0];
    if (navigation) {
      recordMark(
        record,
        'response',
        navigation.responseEnd,
        performance.timeOrigin + navigation.responseEnd
      );
      record.serverTiming = (navigation.serverTiming || []).map(function (entry) {
        return entry.name + ';dur=' + entry.duration.toFixed(1);
      }).join(', ');
    } else {
      mark(record, 'response');
    }
    record.successful = true;
    window.addEventListener('pageshow', function () {
      requestAnimationFrame(function () { mark(record, 'settled'); });
    }, {once: true});
  }

  function formFromEvent(event) {
    var elt = event.detail && event.detail.elt;
    if (elt && elt.matches && elt.matches('form')) return elt;
    return elt && elt.closest ? elt.closest('form') : null;
  }

  function reportPresence() {
    var now = performance.now();
    if (now - lastPresenceReportAt < presenceReportIntervalMs) return;
    lastPresenceReportAt = now;
    fetch('/api/presence', {
      method: 'POST',
      credentials: 'same-origin',
      keepalive: true
    }).catch(function () {
      // A later input retries after the normal interval; never make activity
      // reporting noisy while the deck is offline.
    });
  }

  document.addEventListener('pointerdown', function (event) {
    reportPresence();
    var button = event.target.closest('button[type="submit"], input[type="submit"]');
    if (button && button.form) prepare(button.form);
    var link = navigationLink(event.target);
    if (link && event.button === 0) prepareNavigation(link);
  }, true);

  document.addEventListener('keydown', function (event) {
    reportPresence();
    if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
    var form = event.target.form;
    if (form) prepare(form);
    var link = navigationLink(event.target);
    if (link) prepareNavigation(link);
  }, true);

  document.addEventListener('submit', function (event) {
    prepare(event.target);
  }, true);

  document.addEventListener('click', function (event) {
    var link = navigationLink(event.target);
    if (!link || !isPlainNavigation(event, link)) return;
    var record = prepareNavigation(link);
    link.classList.add('opening');
    link.setAttribute('aria-busy', 'true');
    mark(record, 'acknowledged');
    mark(record, 'request_start');
    persistNavigation(record);
  });

  window.addEventListener('pageshow', function (event) {
    if (!event.persisted) return;
    document.querySelectorAll('a[data-agentdeck-action].opening').forEach(function (link) {
      link.classList.remove('opening');
      link.removeAttribute('aria-busy');
    });
  });

  document.body.addEventListener('htmx:configRequest', function (event) {
    var form = formFromEvent(event);
    var record = prepare(form);
    if (!record) return;
    event.detail.headers['X-AgentDeck-Action-ID'] = record.id;
    event.detail.headers['X-AgentDeck-Action'] = record.action;
    event.detail.parameters.client_action_id = record.id;
    mark(record, 'request_start');
  });

  document.body.addEventListener('htmx:afterRequest', function (event) {
    var form = formFromEvent(event);
    var record = form && form._agentdeckActionTiming;
    if (!record) return;
    var xhr = event.detail && event.detail.xhr;
    if (xhr && xhr.getResponseHeader) {
      record.serverTiming = xhr.getResponseHeader('Server-Timing') || '';
      record.status = xhr.status;
    }
    record.successful = Boolean(event.detail && event.detail.successful);
    mark(record, 'response');
  });

  document.body.addEventListener('htmx:sseMessage', function (event) {
    var target = event.target;
    if (!target || (!relevantSseTargets.has(target.id) && !target.matches('.transcript'))) {
      return;
    }
    records.forEach(function (record) {
      if (record.marks.request_start === undefined || record.marks.settled !== undefined) return;
      if (!isCurrentSession(record.sessionKey)) return;
      mark(record, 'first_sse_state');
      var composerSettled = target.id === 'composer-controls' &&
        !target.querySelector('.stop-button');
      var interactionSettled = target.id === 'pending-interaction' && !target.textContent.trim();
      if (composerSettled || interactionSettled) {
        mark(record, 'settled');
      }
    });
  });

  window.AgentDeckActionTiming = {
    records: records,
    prepareForm: prepare,
    retryForm: retryForm,
    snapshot: function () {
      return Array.from(records.values()).map(function (record) {
        return JSON.parse(JSON.stringify(record));
      });
    },
    summary: function () {
      var grouped = {};
      records.forEach(function (record) {
        (grouped[record.action] = grouped[record.action] || []).push(record);
      });
      function percentiles(values) {
        if (!values.length) return null;
        values.sort(function (left, right) { return left - right; });
        function at(fraction) {
          return values[Math.min(values.length - 1, Math.ceil(values.length * fraction) - 1)];
        }
        return {p50: at(0.5), p95: at(0.95)};
      }
      function duration(items, start, end) {
        return percentiles(items.filter(function (item) {
          var marks = item.epochMarks || item.marks;
          return marks[start] !== undefined && marks[end] !== undefined;
        }).map(function (item) {
          var marks = item.epochMarks || item.marks;
          return marks[end] - marks[start];
        }));
      }
      var result = {};
      Object.keys(grouped).forEach(function (action) {
        var items = grouped[action];
        result[action] = {
          samples: items.length,
          acknowledgement_ms: duration(items, 'interaction', 'acknowledged'),
          http_ms: duration(items, 'request_start', 'response'),
          sse_ms: duration(items, 'request_start', 'first_sse_state'),
          transcript_ms: duration(items, 'request_start', 'first_transcript'),
          settled_ms: duration(items, 'interaction', 'settled')
        };
      });
      return result;
    },
    prepareNavigation: prepareNavigation,
    navigationHeaders: function (link) {
      var record = prepareNavigation(link);
      if (!record) return {};
      return {
        'X-AgentDeck-Action-ID': record.id,
        'X-AgentDeck-Action': record.action
      };
    },
    finishNavigation: function (link, status, successful, serverTiming) {
      var record = link && link._agentdeckActionTiming;
      if (!record) return;
      try { sessionStorage.removeItem(pendingNavigationKey); } catch (_) {}
      link.classList.remove('opening');
      link.removeAttribute('aria-busy');
      record.status = status;
      record.successful = successful;
      record.serverTiming = serverTiming || '';
      mark(record, 'response');
      requestAnimationFrame(function () { mark(record, 'settled'); });
    },
    mark: function (actionId, phase) { mark(records.get(actionId), phase); }
  };
  restoreNavigation();
})();
