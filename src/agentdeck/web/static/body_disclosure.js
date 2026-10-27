// Disclosure controls whose open state must survive an SSE swap.
//
// The usage header and its per-account capacity diagnostics both live inside
// #limit-bars, which is replaced wholesale on every usage push. Any state held
// in that fragment (a `.open` class, <details open>) would therefore snap shut
// under the operator every few minutes. So the state lives in localStorage, is
// re-applied to whatever markup the swap brought in, and the handler is
// delegated: a swapped-in control keeps working with no rebinding.
//
// Mark a control with `data-body-toggle="<class>"` and
// `data-toggle-key="<localStorage key>"`. The class lands on <body> unless
// `data-toggle-scope="<selector>"` names an ancestor to carry it instead —
// that is what keeps a control repeated per account independent of its
// siblings while still surviving the swap.
(function () {
  function host(el) {
    var scope = el.dataset.toggleScope;
    return scope ? el.closest(scope) : document.body;
  }

  function restore() {
    document.querySelectorAll('[data-body-toggle]').forEach(function (el) {
      var cls = el.dataset.bodyToggle;
      var key = el.dataset.toggleKey;
      var target = host(el);
      if (!cls || !key || !target) return;
      try {
        if (localStorage.getItem(key) === '1') target.classList.add(cls);
      } catch (_) {}
    });
  }

  restore();
  // Controls can arrive with the first SSE payload rather than the first paint.
  document.body.addEventListener('htmx:afterSwap', restore);

  document.addEventListener('click', function (e) {
    var el = e.target.closest('[data-body-toggle]');
    if (!el) return;
    var cls = el.dataset.bodyToggle;
    var key = el.dataset.toggleKey;
    var target = host(el);
    if (!cls || !target) return;
    var open = target.classList.toggle(cls);
    if (key) {
      try { localStorage.setItem(key, open ? '1' : '0'); } catch (_) {}
    }
  });
})();
