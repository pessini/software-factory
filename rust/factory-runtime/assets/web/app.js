// Planning UI. All text is inserted with textContent; the controller owns all state.
const app = document.getElementById("app");
const connection = document.getElementById("connection");
const STATUS_TEXT = {
  pending: "Starting", running: "Working", waiting_for_input: "Waiting for you",
  recovery_needed: "Recovery required", stopping: "Stopping", planned: "Plan ready",
  ready_for_review: "Ready for review", failed: "Failed", blocked: "Blocked", cancelled: "Cancelled",
};

function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "class") node.className = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const store = {
  get(key) { try { return JSON.parse(localStorage.getItem(key)); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* optional */ } },
  drop(key) { try { localStorage.removeItem(key); } catch { /* optional */ } },
};

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  };
  let response;
  try {
    response = await fetch(path, { credentials: "same-origin", ...options });
  } catch (error) {
    setConnection(false);
    throw new Error("The planning service is not reachable. Your answers are kept as drafts.");
  }
  setConnection(true);
  const value = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(value.error || `Request failed (${response.status})`);
  return value;
}

function setConnection(ok) {
  connection.dataset.state = ok ? "ok" : "offline";
  connection.textContent = ok ? "Connected" : "Disconnected";
}

function pill(status) {
  return h("span", { class: "pill", "data-state": status }, STATUS_TEXT[status] || status);
}

function list(items, render = (item) => item) {
  return items && items.length ? h("ul", { class: "list" }, items.map((item) => h("li", {}, render(item)))) : null;
}

// ---------- Session list ----------
async function renderHome() {
  const data = await api("/api/sessions");
  document.getElementById("workspace").textContent = data.workspace;
  const pendingStart = store.get("pending-start");
  const request = h("textarea", { id: "request", placeholder: "Describe the feature to plan…", rows: 4 });
  if (pendingStart) request.value = pendingStart.request;
  const error = h("p", { class: "notice bad", hidden: true });
  const start = h("button", { class: "primary", type: "submit" }, "Start planning");
  const form = h("form", {
    class: "card stack",
    onsubmit: async (event) => {
      event.preventDefault();
      if (!request.value.trim()) return;
      // Keep one request ID until the service confirms it, so a lost response cannot start twice.
      const saved = store.get("pending-start");
      const pending = saved && saved.request === request.value ? saved : { request: request.value, id: crypto.randomUUID() };
      store.set("pending-start", pending);
      start.disabled = true;
      try {
        const result = await api("/api/sessions", { request: pending.request, request_id: pending.id });
        store.drop("pending-start");
        location.href = `/planning/${result.session_id}`;
      } catch (exc) {
        error.hidden = false; error.textContent = exc.message; start.disabled = false;
      }
    },
  }, h("h2", {}, "New planning session"), request, error, h("div", { class: "actions" }, start,
    h("span", { class: "muted small" }, "The Grill interviewer reads the repository first, then asks only what it cannot find.")));
  const sessions = data.sessions.length
    ? h("div", { class: "card sessions" }, h("h2", {}, "Sessions"), data.sessions.map((item) =>
        h("a", { href: `/planning/${item.session_id}` },
          h("div", { class: "actions" }, pill(item.status), h("span", { class: "muted small" }, `Revision ${item.revision} · ${item.created_at}`)),
          h("div", {}, item.request.slice(0, 180)))))
    : h("p", { class: "muted" }, "No planning sessions yet.");
  app.replaceChildren(h("div", { class: "stack", style: "max-width:860px;margin:0 auto" }, form, sessions));
}

// ---------- Session page ----------
let session = null;
let timer = null;
let prepared = {};

function latestEnvelope(state) {
  const rounds = state.rounds.filter((round) => round.envelope);
  return rounds.length ? rounds[rounds.length - 1].envelope : null;
}

function draftKey(interactionId) { return `draft:${interactionId}`; }

// Prefer the matching option over the recommendation's explanatory text.
function recommendedAnswer(question) {
  const options = question.options || [];
  const strip = (text) => text.replace(/\s*\(recommended\)\s*$/i, "").trim().toLowerCase();
  const target = question.recommendation.toLowerCase();
  return options.find((option) => /\(recommended\)\s*$/i.test(option))
    || options.find((option) => target.startsWith(strip(option)))
    || question.recommendation;
}

function questionCard(question, interactionId) {
  const key = draftKey(interactionId);
  const drafts = store.get(key) || {};
  const value = prepared[question.id] ?? drafts[question.id] ?? "";
  const field = h("textarea", { "aria-label": `Answer for ${question.question}`, placeholder: "Your answer" });
  field.value = value;
  const draftNote = h("span", { class: "draft", hidden: !value }, "Draft — not submitted");
  const remember = () => {
    prepared[question.id] = field.value;
    store.set(key, { ...(store.get(key) || {}), [question.id]: field.value });
    draftNote.hidden = !field.value;
    optionButtons.forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.value === field.value)));
    renderSubmit();
  };
  field.addEventListener("input", remember);
  const optionButtons = (question.options || []).map((option) =>
    h("button", { type: "button", "data-value": option, "aria-pressed": String(option === value),
      onclick: () => { field.value = option; remember(); } }, JSON.stringify(option)));
  const discuss = h("button", { type: "button", class: "ghost", onclick: () => {
    if (!field.value.startsWith("[discuss]")) field.value = `[discuss] ${field.value}`.trimEnd() + " ";
    field.focus(); remember();
  } }, "Ask about this");
  return h("article", { class: "question" },
    h("header", {}, h("strong", {}, question.question), h("span", { class: "qid" }, question.id)),
    question.context ? h("p", { class: "muted" }, question.context) : null,
    question.recommendation ? h("div", { class: "recommend" },
      h("div", { class: "small muted" }, "Recommendation (a suggestion, not a decision)"),
      h("div", {}, question.recommendation),
      h("button", { type: "button", class: "ghost small", onclick: () => { field.value = recommendedAnswer(question); remember(); } }, "Use recommendation")) : null,
    optionButtons.length ? h("div", { class: "options" }, optionButtons) : null,
    field,
    h("div", { class: "actions" }, discuss, draftNote));
}

let submitBar = null;
function renderSubmit() {
  if (!submitBar || !session || !session.pending) return;
  const questions = session.pending.questions;
  const ready = questions.filter((question) => (prepared[question.id] || "").trim()).length;
  submitBar.button.disabled = ready !== questions.length;
  submitBar.button.textContent = `Submit ${questions.length} answer${questions.length === 1 ? "" : "s"}`;
  submitBar.note.textContent = `${ready} of ${questions.length} prepared. Only submitted answers become decisions.`;
}

function roundView(state) {
  const pending = state.pending;
  const envelope = latestEnvelope(state);
  const error = h("p", { class: "notice bad", hidden: true });
  const button = h("button", { class: "primary", type: "button", onclick: async () => {
    button.disabled = true;
    try {
      const answers = Object.fromEntries(pending.questions.map((question) => [question.id, prepared[question.id]]));
      const result = await api(`/api/sessions/${state.session_id}/answer`, { interaction_id: pending.interaction_id, answers });
      store.drop(draftKey(pending.interaction_id));
      prepared = {};
      update(result);
    } catch (exc) { error.hidden = false; error.textContent = exc.message; button.disabled = false; }
  } });
  const note = h("span", { class: "muted small" });
  submitBar = { button, note };
  const view = h("section", { class: "stack" },
    envelope ? h("div", { class: "card" }, h("h2", {}, "Current understanding"), h("p", {}, envelope.summary),
      envelope.affected && envelope.affected.length ? h("div", { class: "notice" }, h("strong", {}, "Needs review after a changed answer: "), envelope.affected.join("; ")) : null) : null,
    h("h2", {}, `Round ${pending.interaction_id.split(":").pop()}`),
    pending.questions.map((question) => questionCard(question, pending.interaction_id)),
    error, h("div", { class: "actions" }, button, note));
  queueMicrotask(renderSubmit);
  return view;
}

function planView(state) {
  const snap = state.snapshot;
  const plan = snap.plan;
  const confirm = h("input", { type: "checkbox", id: "approve" });
  const error = h("p", { class: "notice bad", hidden: true });
  const startButton = h("button", { class: "primary", type: "button", disabled: true, onclick: async () => {
    startButton.disabled = true;
    try { update(await api(`/api/sessions/${state.session_id}/approve`, { plan_hash: state.plan_hash })); }
    catch (exc) { error.hidden = false; error.textContent = exc.message; startButton.disabled = false; }
  } }, "Start implementation");
  confirm.addEventListener("change", () => { startButton.disabled = !confirm.checked; });
  const handedOff = Boolean(state.handoff);
  const feedback = h("textarea", { placeholder: "What should change? Name the decision to revisit." });
  const reviseButton = h("button", { type: "button", onclick: async () => {
    if (!feedback.value.trim()) return;
    reviseButton.disabled = true;
    try { update(await api(`/api/sessions/${state.session_id}/revise`, { feedback: feedback.value })); }
    catch (exc) { error.hidden = false; error.textContent = exc.message; reviseButton.disabled = false; }
  } }, "Create new revision");
  return h("section", { class: "stack" },
    h("div", { class: "card stack" },
      h("h2", {}, plan.goal),
      h("p", {}, snap.summary),
      h("h3", {}, "Scope"), list(plan.scope),
      h("h3", {}, "Requirements"), list(plan.requirements),
      h("h3", {}, "Completion checks"), list(plan.completion_checks, (check) => [h("span", { class: "mono" }, check.id), ` ${check.description}`]),
      h("h3", {}, "Decisions"), list(snap.decisions, (item) => [h("strong", {}, item.title), `: ${item.decision} — ${item.rationale}`, item.adr ? h("span", { class: "muted small" }, " (ADR)") : null]),
      plan.assumptions.length ? [h("h3", {}, "Assumptions"), list(plan.assumptions)] : null,
      plan.exclusions.length ? [h("h3", {}, "Exclusions"), list(plan.exclusions)] : null,
      plan.deferred.length ? [h("h3", {}, "Deferred"), list(plan.deferred)] : null,
      h("h3", {}, "Document versions"),
      h("p", { class: "mono" }, `HEAD ${snap.documents.head.slice(0, 12)} · ${Object.keys(snap.documents.files).length} glossary/ADR files read`),
      h("p", { class: "mono muted" }, `Plan revision ${state.revision} · sha256 ${state.plan_hash}`)),
    handedOff ? implementationView(state) : h("div", { class: "card stack" },
      h("h2", {}, "Approve and start"),
      h("p", { class: "muted" }, "Approval starts one implementation run with this exact plan. Later edits create a new revision and do not change it. Existing checks, reviews, and delivery permissions still apply."),
      h("label", { class: "check-row", for: "approve" }, confirm, h("span", {}, `I approve plan revision ${state.revision} (${state.plan_hash.slice(0, 12)}) for implementation.`)),
      h("div", { class: "actions" }, startButton)),
    h("div", { class: "card stack" }, h("h2", {}, "Revise the plan"), feedback, h("div", { class: "actions" }, reviseButton)),
    error);
}

function implementationView(state) {
  const impl = state.implementation;
  const reportButton = impl && impl.final_report_path ? h("button", { type: "button", onclick: async () => {
    const result = await api(`/api/sessions/${state.session_id}/report`);
    document.getElementById("report-text").textContent = result.report;
    document.getElementById("report-dialog").showModal();
  } }, "Open final report") : null;
  const waiting = impl && impl.interactions.find((item) => item.state === "waiting");
  return h("div", { class: "card stack" },
    h("h2", {}, "Implementation"),
    impl ? h("div", { class: "actions" }, pill(impl.status), h("span", { class: "mono" }, impl.run_id), impl.current_step ? h("span", { class: "muted small" }, `step: ${impl.current_step}`) : null) : null,
    impl && impl.error ? h("p", { class: "notice bad" }, impl.error) : null,
    waiting ? h("p", { class: "notice" }, `The implementation Planner is asking a question (${waiting.interaction_id}). Answer it from BB, Codex, or the CLI.`) : null,
    h("div", { class: "actions" }, reportButton, impl && impl.pull_request ? h("a", { href: impl.pull_request, target: "_blank", rel: "noreferrer" }, "Pull request") : null),
    h("p", { class: "muted small" }, `Started from plan ${state.handoff.plan_hash.slice(0, 12)}; repeating approval returns this same run.`));
}

function workingView(state) {
  const recent = state.events.slice(-6).reverse();
  return h("div", { class: "card stack" },
    h("h2", {}, state.status === "recovery_needed" ? "Recovery required" : "The interviewer is working"),
    state.status === "recovery_needed"
      ? h("p", {}, "A saved operation needs reconciliation before continuing.", h("button", { type: "button", onclick: async () => update(await api(`/api/sessions/${state.session_id}/resume`, {})) }, "Resume"))
      : h("p", { class: "muted" }, "Your answers are saved. You can close this page; the controller continues independently."),
    recent.length ? h("ul", { class: "clean small muted" }, recent.map((event) => {
      const line = String(event.message || "").split("\n")[0];
      return h("li", {}, `${event.node}: ${line.length > 160 ? `${line.slice(0, 157)}…` : line}`);
    })) : null);
}

function failedView(state) {
  const feedback = h("textarea", { placeholder: "Adjust the request or answer the blocker, then create a new revision." });
  return h("div", { class: "card stack" },
    h("h2", {}, STATUS_TEXT[state.status] || state.status),
    state.error ? h("p", { class: "notice bad" }, state.error) : null,
    feedback,
    h("button", { type: "button", onclick: async () => update(await api(`/api/sessions/${state.session_id}/revise`, { feedback: feedback.value })) }, "Create new revision"));
}

function leftColumn(state) {
  const envelope = latestEnvelope(state);
  const topics = (state.snapshot && state.snapshot.topics) || (envelope && envelope.topics) || [];
  const answered = state.rounds.filter((round) => round.answers);
  return h("aside", { class: "side stack" },
    h("div", { class: "card" }, h("h2", {}, "Decisions"),
      topics.length ? h("ul", { class: "clean" }, topics.map((topic) => h("li", { class: "topic" },
        h("span", { class: "dot", "data-state": topic.state, title: topic.state }),
        h("div", {}, h("div", {}, topic.title),
          h("div", { class: "small muted" }, topic.state === "waiting" && topic.depends_on.length ? `waits on ${topic.depends_on.join(", ")}` : topic.decision || topic.state)))))
        : h("p", { class: "muted small" }, "Topics appear after the first round.")),
    h("div", { class: "card" }, h("h2", {}, "History"),
      answered.length ? answered.map((round) => h("details", {},
        h("summary", {}, `Round ${round.interaction_id.split(":").pop()}`),
        round.questions.map((question) => h("div", { class: "answered" }, h("div", { class: "small muted" }, question.question), h("div", {}, round.answers[question.id])))))
        : h("p", { class: "muted small" }, "No submitted rounds yet."),
      state.history.length > 1 ? [h("h3", {}, "Revisions"), h("ul", { class: "clean small" }, state.history.map((item) =>
        h("li", {}, `${item.revision}: ${STATUS_TEXT[item.status] || item.status}${item.feedback ? ` — ${item.feedback}` : ""}${item.handoff ? " (implemented)" : ""}`)))] : null));
}

function rightColumn(state) {
  const source = state.snapshot || latestEnvelope(state) || { glossary: [], decisions: [] };
  const sources = (state.snapshot && state.snapshot.plan.sources) || [];
  return h("aside", { class: "side right stack" },
    h("div", { class: "card" }, h("h2", {}, "Glossary — CONTEXT.md"),
      source.glossary && source.glossary.length ? source.glossary.map((item) => h("div", { class: "term" }, h("strong", {}, item.term), h("div", { class: "muted" }, item.definition)))
        : h("p", { class: "muted small" }, "No glossary changes proposed."),
      h("p", { class: "small muted" }, state.snapshot ? "Proposed wording; the implementation run writes it after approval." : "Proposed wording; not saved until a plan is approved.")),
    h("div", { class: "card" }, h("h2", {}, "Decision records"),
      source.decisions && source.decisions.length ? source.decisions.map((item) => h("div", { class: "term" },
        h("strong", {}, item.title), item.adr ? h("span", { class: "muted small" }, " · ADR in docs/adr/") : null,
        h("div", {}, item.decision), h("div", { class: "muted small" }, item.rationale)))
        : h("p", { class: "muted small" }, "No decisions settled yet.")),
    sources.length ? h("div", { class: "card" }, h("h2", {}, "Sources"), list(sources)) : null);
}

function render(state) {
  const center = state.pending ? roundView(state)
    : state.status === "planned" && state.snapshot ? planView(state)
    : ["failed", "blocked", "cancelled"].includes(state.status) ? failedView(state)
    : workingView(state);
  app.replaceChildren(
    h("div", { class: "title-row" },
      h("div", {}, h("h1", {}, state.request ? state.request.split("\n")[0].slice(0, 140) : state.session_id),
        h("div", { class: "muted small" }, `${state.session_id} · revision ${state.revision}`)),
      h("div", { class: "actions" }, pill(state.handoff ? (state.implementation || {}).status || "running" : state.status),
        !["planned", "failed", "blocked", "cancelled"].includes(state.status) ? h("button", { type: "button", class: "ghost", onclick: async () => {
          if (confirm("Stop this planning revision?")) update(await api(`/api/sessions/${state.session_id}/cancel`, {}));
        } }, "Stop") : null)),
    h("div", { class: "layout" }, leftColumn(state), h("div", { class: "stack center" }, center), rightColumn(state)));
}

function signature(state) {
  return JSON.stringify([state.status, state.run_id, state.pending && state.pending.interaction_id, state.plan_hash,
    state.handoff, state.implementation && [state.implementation.status, state.implementation.current_step], state.event_position]);
}

function update(state) {
  const changed = !session || signature(session) !== signature(state);
  const pendingChanged = !session || (session.pending && session.pending.interaction_id) !== (state.pending && state.pending.interaction_id);
  if (pendingChanged) prepared = {};
  session = state;
  // Re-render only on a real change so typing in answer fields is not interrupted.
  if (changed && !(state.pending && !pendingChanged && document.activeElement && document.activeElement.tagName === "TEXTAREA")) render(state);
  schedule();
}

function schedule() {
  clearTimeout(timer);
  const active = session && (["pending", "running", "stopping"].includes(session.status) || (session.implementation && !["ready_for_review", "failed", "blocked", "cancelled"].includes(session.implementation.status)));
  timer = setTimeout(poll, active ? 2000 : 6000);
}

async function poll() {
  try { update(await api(`/api/sessions/${session.session_id}`)); }
  catch { schedule(); }
}

async function main() {
  const match = location.pathname.match(/^\/planning\/([\w-]+)$/);
  try {
    if (!match) return await renderHome();
    const workspace = await api("/api/sessions");
    document.getElementById("workspace").textContent = workspace.workspace;
    update(await api(`/api/sessions/${match[1]}`));
  } catch (exc) {
    app.replaceChildren(h("p", { class: "notice bad" }, exc.message));
  }
}

main();
