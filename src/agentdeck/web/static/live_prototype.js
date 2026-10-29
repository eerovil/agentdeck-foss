// The live prototype page (#3569). The record comes with the page and again after
// every change the agent records (`/events/live/<key>` says when); each new revision
// reloads the prototype's frame. Suggestions, "Go back" and "Write the spec" are sent to
// the chat through its own send route, so they queue like any typed message: the deck
// does not batch suggestions sent during a change; the agent folds the waiting ones into
// its next change, as the procedure tells it (the owner's decision on #3569).
// #3571 adds what the agent is doing (the stream's `activity` event), dictation into the
// suggestion box (composer_speech.js), and use cases: the agent and the person both write
// them, and Start walks this screen through one step at a time over the prototype.
(function () {
  "use strict";
  const root = document.getElementById("live");
  if (!root) return;
  const key = root.dataset.sessionKey;
  const path = encodeURIComponent(key).replace(/%3A/g, ":");
  const frame = document.getElementById("live-frame");
  const empty = document.getElementById("live-empty");
  const versionsBox = document.getElementById("live-versions");
  const stepsList = document.getElementById("live-steps");
  const status = document.getElementById("live-status");
  const form = document.getElementById("live-send");
  const activity = document.getElementById("live-activity");
  const caseList = document.getElementById("live-case-list");
  const caseForm = document.getElementById("live-case-form");
  const caseAdd = document.getElementById("live-case-add");
  const walk = document.getElementById("live-walk");
  // #3831: the panel is a column on a wide screen and a sheet over the prototype on a phone,
  // where it starts closed; its button counts the changes.
  const sideCount = document.getElementById("live-side-count");
  const sideToggle = document.getElementById("live-side-toggle");
  const narrow = window.matchMedia("(max-width: 719px)");
  // The use case being edited (its id), or null when the form adds one.
  let editing = null;
  // This screen's walkthrough: { id, index }, or null. Not shared: each screen walks its own.
  let walking = null;
  let record = JSON.parse(document.getElementById("live-initial").textContent);
  let shownRevision = null;
  // The last pick's save; "Write the spec" waits for it, so a click on B followed at
  // once by the spec request still names B.
  let picking = Promise.resolve();
  // A record fetched while a pick is being saved may hold the old pick: it is dropped.
  let picks = 0;
  let savedPicks = 0;
  // The person's pick, kept by the deck so a reload and the spec request both see it.
  let version = record.picked || null; // a version's name, or null for the main link
  // A diagram has its own slot beside the prototype (#3827); this screen shows one of the
  // two, the diagram when `?view=diagram` asked for it or there is no prototype.
  let showDiagram = new URLSearchParams(window.location.search).get("view") === "diagram";

  function currentUrl() {
    if (diagramShown()) return record.diagram;
    const picked = (record.versions || []).find((item) => item.name === version);
    if (!picked) version = null;
    return picked ? picked.url : record.url;
  }

  // A diagram box picked by a tap in the frame (#3822): the suggestion box says which,
  // and a suggestion sent while one is picked is about that box.
  const boxRow = document.getElementById("live-box");
  const boxName = document.getElementById("live-box-name");
  const PLACEHOLDER = form.elements.message.placeholder;
  let pickedBox = null;

  function diagramShown() {
    return Boolean(record.diagram && (showDiagram || !record.url));
  }

  // On a phone the diagram or prototype gets the whole screen (#3827, #3831): the
  // suggestion bar is always under it, so a tapped box only has to close the sheet.
  function compose(open) {
    if (open && narrow.matches) setSide(false);
  }

  // A switch between the prototype and the diagram closes a phone's sheet.
  let fittedFor = null; // the view the panel was last fitted to

  function fitPanel() {
    fittedFor = diagramShown();
    if (narrow.matches) setSide(false);
  }

  function pickBox(text) {
    pickedBox = text ? text.slice(0, 200) : null;
    compose(Boolean(pickedBox));
    boxRow.hidden = !pickedBox;
    boxName.textContent = pickedBox ? "\u201c" + pickedBox + "\u201d" : "";
    form.elements.message.placeholder = pickedBox
      ? "What should change in \u201c" + pickedBox + "\u201d?"
      : PLACEHOLDER;
  }

  window.addEventListener("message", (event) => {
    // Only the deck's own diagram view, shown in this page's frame, may pick a box.
    if (event.source !== frame.contentWindow || event.origin !== window.location.origin) return;
    const box = event.data && event.data.agentdeckDiagramBox;
    if (box === null || typeof box === "string") pickBox(box);
  });
  document.getElementById("live-box-clear").addEventListener("click", () => pickBox(null));
  frame.addEventListener("load", () => pickBox(null));

  function loadFrame() {
    const url = currentUrl();
    frame.hidden = !url;
    empty.hidden = Boolean(url);
    // Assigning src navigates again even when it is unchanged, which is the reload.
    if (url) frame.src = url;
  }

  function viewButton(label, diagram) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.dataset.liveView = diagram ? "diagram" : "prototype";
    button.setAttribute("aria-pressed", String(showDiagram === diagram));
    button.addEventListener("click", () => {
      showDiagram = diagram;
      fitPanel();
      renderVersions();
      loadFrame();
    });
    return button;
  }

  function renderVersions() {
    versionsBox.replaceChildren();
    if (record.url && record.diagram) {
      versionsBox.append(viewButton("Prototype", false), viewButton("Diagram", true));
    }
    if (diagramShown()) return;
    for (const item of record.versions || []) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = item.name;
      button.setAttribute("aria-pressed", String(item.name === version));
      button.addEventListener("click", () => {
        version = item.name;
        renderVersions();
        loadFrame();
        picking = pick(item.name);
      });
      versionsBox.append(button);
    }
  }

  function renderSteps() {
    const steps = record.steps || [];
    sideCount.hidden = !steps.length;
    sideCount.textContent = steps.length ? String(steps[steps.length - 1].number) : "";
    stepsList.replaceChildren();
    for (const step of steps.slice().reverse()) {
      const item = document.createElement("li");
      const number = document.createElement("span");
      number.className = "n";
      number.textContent = step.number;
      const text = document.createElement("span");
      text.className = "text";
      text.textContent = step.summary;
      if (step === steps[steps.length - 1]) {
        const now = document.createElement("span");
        now.className = "now";
        now.textContent = "Now";
        text.append(now);
      }
      item.append(number, text);
      if (step !== steps[steps.length - 1]) {
        const back = document.createElement("button");
        back.type = "button";
        back.textContent = "Go back";
        back.addEventListener("click", () =>
          send("Go back to step " + step.number + " (" + step.summary + ").")
        );
        item.append(back);
      }
      stepsList.append(item);
    }
  }

  async function pick(name) {
    const mine = ++picks;
    try {
      const response = await fetch("/api/sessions/" + path + "/live-prototype/picked", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ version: name }),
      });
      if (!response.ok) status.textContent = "The pick was not saved (" + response.status + ").";
      else if (mine === picks) record = await response.json();
    } catch (_error) {
      status.textContent = "The pick was not saved: the deck did not answer.";
    } finally {
      if (mine === picks) savedPicks = mine;
    }
  }

  async function writeUseCase(method, suffix, body) {
    try {
      const response = await fetch("/api/sessions/" + path + "/live-prototype/use-cases" + suffix, {
        method,
        headers: { "Content-Type": "application/json" },
        body: body ? JSON.stringify(body) : undefined,
      });
      if (!response.ok) {
        status.textContent = "The use case was not saved (" + response.status + ").";
        return false;
      }
      record = await response.json();
      render();
      return true;
    } catch (_error) {
      status.textContent = "The use case was not saved: the deck did not answer.";
      return false;
    }
  }

  function useCaseVersion(useCase) {
    return (record.versions || []).some((item) => item.name === useCase.version)
      ? useCase.version
      : null;
  }

  function renderUseCases() {
    caseList.replaceChildren();
    for (const useCase of record.use_cases || []) {
      const item = document.createElement("li");
      const title = document.createElement("div");
      title.textContent = useCase.title;
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent =
        useCase.steps.length + (useCase.steps.length === 1 ? " step" : " steps") +
        (useCaseVersion(useCase) ? " · version " + useCase.version : "");
      const actions = document.createElement("div");
      actions.className = "actions";
      for (const [label, action] of [
        ["Start", () => startWalk(useCase)],
        ["Edit", () => openCaseForm(useCase)],
        ["Delete", () => {
          if (confirm("Delete the use case “" + useCase.title + "”?")) {
            writeUseCase("DELETE", "/" + encodeURIComponent(useCase.id));
          }
        }],
      ]) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = label;
        button.addEventListener("click", action);
        actions.append(button);
      }
      item.append(title, meta, actions);
      caseList.append(item);
    }
  }

  function openCaseForm(useCase) {
    editing = useCase ? useCase.id : null;
    const select = caseForm.elements.version;
    select.replaceChildren(new Option("Main link", ""));
    for (const item of record.versions || []) select.append(new Option("Version " + item.name, item.name));
    caseForm.elements.title.value = useCase ? useCase.title : "";
    select.value = useCase ? useCaseVersion(useCase) || "" : version || "";
    caseForm.elements.url.value = useCase ? useCase.url || "" : "";
    caseForm.elements.steps.value = useCase ? useCase.steps.join("\n") : "";
    caseForm.hidden = false;
    caseAdd.hidden = true;
    caseForm.elements.title.focus();
  }

  function closeCaseForm() {
    editing = null;
    caseForm.hidden = true;
    caseAdd.hidden = false;
  }

  function startWalk(useCase) {
    // A use case walks the prototype, never the diagram.
    if (record.url && showDiagram) {
      showDiagram = false;
      fitPanel();
      renderVersions();
    }
    const name = useCaseVersion(useCase);
    if (name !== version) {
      version = name;
      renderVersions();
      picking = pick(name);
    }
    walking = { id: useCase.id, index: 0 };
    // On a phone the sheet covers the prototype, so it closes for the walkthrough.
    if (narrow.matches) setSide(false);
    if (useCase.url) frame.src = useCase.url;
    else loadFrame();
    frame.hidden = false;
    empty.hidden = true;
    renderWalk();
  }

  function renderWalk() {
    const useCase = walking && (record.use_cases || []).find((item) => item.id === walking.id);
    // A use case deleted mid-walk ends its walkthrough.
    if (!useCase) walking = null;
    walk.hidden = !walking;
    if (!walking) return;
    const steps = useCase.steps;
    walking.index = Math.min(walking.index, Math.max(steps.length - 1, 0));
    document.getElementById("live-walk-title").textContent =
      useCase.title + (steps.length ? " · step " + (walking.index + 1) + " of " + steps.length : "");
    document.getElementById("live-walk-step").textContent = steps[walking.index] || "No steps yet.";
    document.getElementById("live-walk-back").disabled = walking.index === 0;
    document.getElementById("live-walk-next").disabled = walking.index >= steps.length - 1;
  }

  function showActivity(text) {
    activity.hidden = !text;
    activity.textContent = text || "";
    activity.title = text || "";
  }

  function render() {
    // Another page (or the agent replacing versions) may have moved the pick.
    if ((record.picked || null) !== version) {
      version = record.picked || null;
      shownRevision = null;
    }
    // A diagram the agent adds, or a prototype link it adds beside one, changes the view.
    if (fittedFor !== diagramShown()) fitPanel();
    renderVersions();
    renderSteps();
    renderUseCases();
    renderWalk();
    if (record.revision !== shownRevision) {
      shownRevision = record.revision;
      loadFrame();
    }
  }

  async function refresh() {
    const before = picks;
    let fetched;
    try {
      const response = await fetch("/api/sessions/" + path + "/live-prototype");
      if (!response.ok) return;
      fetched = await response.json();
    } catch (_error) {
      return;
    }
    // The pick's save publishes once more, so a dropped record is fetched again.
    if (before !== picks || savedPicks !== picks) return;
    record = fetched;
    render();
  }

  async function send(text) {
    status.textContent = "Sending…";
    const body = new FormData();
    body.append("message", text);
    try {
      const response = await fetch("/sessions/" + path + "/inject", {
        method: "POST",
        body,
        // randomUUID exists only on https; the deck is also opened over plain http.
        headers: {
          "X-Agentdeck-Action-Id": crypto.randomUUID
            ? crypto.randomUUID()
            : Date.now().toString(36) + Math.random().toString(36).slice(2),
        },
      });
      status.textContent = response.ok ? "Sent to the agent." : "Not sent (" + response.status + ").";
      return response.ok;
    } catch (_error) {
      status.textContent = "Not sent: the deck did not answer.";
      return false;
    }
  }

  // One send at a time, as in the chat composer (#2843): a second Enter while the first
  // is in flight would send the same suggestion again under a fresh action ID.
  let sending = false;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const box = form.elements.message;
    const text = box.value.trim();
    if (!text || sending) return;
    const about = pickedBox ? "About the box \u201c" + pickedBox + "\u201d in the diagram: " : "";
    sending = true;
    try {
      // The text stays in the box until the send is confirmed.
      if (await send(about + text)) {
        box.value = "";
        grow();
      }
    } finally {
      sending = false;
    }
  });
  document.getElementById("live-spec").addEventListener("click", async () => {
    await picking;
    // The deck words it, naming the picked version and its link.
    const text = record.spec_request;
    if (text && confirm(text)) send(text);
  });
  document.getElementById("live-reload").addEventListener("click", loadFrame);
  function setSide(open) {
    root.classList.toggle("side-open", open);
    sideToggle.setAttribute("aria-expanded", String(open));
  }
  // A phone starts with the sheet closed, a wide screen with the panel open, and turning
  // a device across the breakpoint sets the panel for its new width.
  setSide(!narrow.matches);
  narrow.addEventListener("change", () => setSide(!narrow.matches));
  sideToggle.addEventListener("click", () => setSide(!root.classList.contains("side-open")));
  document.getElementById("live-side-close").addEventListener("click", () => setSide(false));
  const message = form.elements.message;
  // The box grows with its text, up to the height its style allows.
  function grow() {
    message.style.height = "auto";
    message.style.height = message.scrollHeight + "px";
  }
  message.addEventListener("input", grow);
  // Enter sends and Shift+Enter adds a line, as in the chat composer.
  message.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
    event.preventDefault();
    form.requestSubmit();
  });
  // The send button takes no focus, so a phone's keyboard stays up (as in the chat, #17).
  document
    .getElementById("live-send-button")
    .addEventListener("mousedown", (event) => event.preventDefault());
  // The phone's sheet ends above the suggestion bar, whatever its height.
  new ResizeObserver(() => root.style.setProperty("--send-h", form.offsetHeight + "px")).observe(form);

  caseAdd.addEventListener("click", () => openCaseForm(null));
  document.getElementById("live-case-cancel").addEventListener("click", closeCaseForm);
  caseForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const fields = caseForm.elements;
    const body = {
      title: fields.title.value,
      version: fields.version.value || null,
      url: fields.url.value.trim() || null,
      steps: fields.steps.value.split("\n").map((line) => line.trim()).filter(Boolean),
    };
    const suffix = editing ? "/" + encodeURIComponent(editing) : "";
    // The form stays open with its text until the save is confirmed.
    if (await writeUseCase(editing ? "PUT" : "POST", suffix, body)) closeCaseForm();
  });
  document.getElementById("live-walk-back").addEventListener("click", () => {
    walking.index = Math.max(0, walking.index - 1);
    renderWalk();
  });
  document.getElementById("live-walk-next").addEventListener("click", () => {
    walking.index += 1;
    renderWalk();
  });
  document.getElementById("live-walk-end").addEventListener("click", () => {
    walking = null;
    renderWalk();
  });
  for (const tab of document.querySelectorAll("[data-live-tab]")) {
    tab.addEventListener("click", () => {
      for (const other of document.querySelectorAll("[data-live-tab]")) {
        other.setAttribute("aria-pressed", String(other === tab));
        document.getElementById(other.dataset.liveTab).hidden = other !== tab;
      }
    });
  }

  render();
  // The first message on each (re)connection fetches too, so nothing recorded while
  // the stream was down is missed.
  const events = new EventSource("/events/live/" + path);
  events.onmessage = refresh;
  events.addEventListener("activity", (event) => showActivity(JSON.parse(event.data)));
})();
