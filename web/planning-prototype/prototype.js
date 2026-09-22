/* THROWAWAY — issue 10 UI experiment. In-memory fixtures, no controller calls.
 * Three layouts on one route: A Workbench, B Focus, C Document first.
 * The behavior below explains the proposed interface. It is not a durable workflow. */
const app = document.querySelector('#app');
const variants = { A: 'Workbench', B: 'Focus', C: 'Document first' };
const icons = {
  layers: '<path d="m3 7 9-4 9 4-9 4-9-4Zm0 5 9 4 9-4M3 17l9 4 9-4"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  circle: '<circle cx="12" cy="12" r="8"/>',
  clock: '<circle cx="12" cy="12" r="8"/><path d="M12 7v5l3 2"/>',
  file: '<path d="M14 3H5v18h14V8l-5-5Zm0 0v5h5M8 12h8M8 16h6"/>',
  arrow: '<path d="M5 12h14m-5-5 5 5-5 5"/>',
  left: '<path d="m14 6-6 6 6 6"/>',
  right: '<path d="m10 6 6 6-6 6"/>',
  chat: '<path d="M4 4h16v12H9l-5 4V4Z"/>',
  lock: '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
};
const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name]}</svg>`;
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const button = (action, text, extra = '') => `<button data-action="${action}" ${extra}>${text}</button>`;
const badge = (text, kind = '') => `<span class="badge ${kind}">${text}</span>`;

function initialState() {
  return {
    session: 'planning-010-demo', externalDecision: null, client: 'Codex',
    connection: 'connected', worker: 'waiting for you', round: 1, planRevision: 3,
    questionRevision: 1, answers: {}, drafts: {}, discussions: {}, discussionDrafts: {},
    docRevision: 1, documentsAcceptedFor: null, approval: false, stale: [],
    request: null, run: null, handoffs: {}, simulateLostResponse: false,
    history: [{ title: 'Scope agreed', detail: 'One person, one local service. Browser, BB, and Codex share the same session.', revision: 2, time: 'Earlier · sample answer' }],
    view: 'questions', documentTab: 'sources', focusIndex: 0, reviewStep: 0, openDiscussions: [],
    notice: '', event: 0,
  };
}
let state = initialState();
let variant = new URLSearchParams(location.search).get('variant') || 'A';
if (!variants[variant]) variant = 'A';

function questions(round = state.round) {
  if (round === 1) return [
    { id: 'documents', topic: 'Plan contents', title: 'Which documents belong in the approved plan?',
      description: 'The implementation run needs the exact wording you reviewed.',
      recommendation: 'Include the glossary and decision records. They keep the terms and the reasons for each choice together.',
      source: 'Issue 10 · Handoff contents', options: [
        ['glossary_adr', 'Glossary + decision records', 'Include CONTEXT.md and any accepted records in docs/adr/.'],
        ['glossary', 'Glossary only for this feature', 'Use this when no significant trade-off needs a separate record.'],
      ] },
    { id: 'review', topic: 'Changed decisions', title: 'How should we review the effects of a changed answer?',
      description: 'Later decisions can depend on an earlier answer. Those decisions need another review.',
      recommendation: 'Use a grouped review list. It makes the remaining work visible before implementation can start.',
      source: 'Issue 10 · Interview and documents', options: [
        ['grouped', 'Group affected decisions in one review', 'Show each affected answer and plan section together.'],
        ['inline', 'Review each affected decision in place', 'Mark each question and document section where it appears.'],
      ] },
  ];
  const includesAdr = state.answers.documents?.value === 'glossary_adr';
  return [
    includesAdr ? {
      id: 'record', topic: 'Decision records', title: 'Which trade-off needs a decision record?',
      description: 'You chose to include decision records. Pick the choice that future work must understand.',
      recommendation: 'Record who owns the saved session. This affects all three entry points.',
      source: 'Issue 10 · Architecture and responsibility', options: [
        ['ownership', 'Shared controller owns the session', 'Explain why the browser, BB, and Codex use the same saved record.'],
        ['snapshot', 'Implementation uses an approved snapshot', 'Explain why later plan edits cannot change an active run.'],
      ],
    } : {
      id: 'record', topic: 'Glossary', title: 'Which term needs a clear definition first?',
      description: 'You chose glossary-only documents. Keep the handoff terms clear without adding a decision record.',
      recommendation: 'Define the approved plan. Its revision is the link between planning and implementation.',
      source: 'Issue 10 · Readiness and approval', options: [
        ['plan', 'Approved plan', 'The exact scope, decisions, and checks the user authorizes.'],
        ['session', 'Planning session', 'The shared record of questions, answers, documents, and progress.'],
      ],
    },
    { id: 'check', topic: 'Completion checks', title: 'Which recovery case should we show first?',
      description: 'All required checks stay in the plan. This choice sets the first demonstration.',
      recommendation: 'Repeat an implementation request. The result must point to the same run.',
      source: 'Issue 10 · Exactly one implementation request', options: [
        ['duplicate', 'Repeat the same implementation request', 'Show the saved run ID when the same request arrives again.'],
        ['lost', 'Recover after a response is lost', 'Find the existing run before sending another start request.'],
      ] },
  ];
}
function allQuestions() { return [...questions(1), ...questions(2)]; }
function recordEvent(title, detail) {
  state.event += 1;
  state.history.push({ title, detail, revision: state.planRevision, time: `Demo event ${state.event}` });
}
const available = () => state.connection === 'connected' && state.worker !== 'working';
const isReady = () => ['documents', 'review', 'record', 'check'].every(id => state.answers[id]) && state.stale.length === 0 && Object.keys(state.drafts).length === 0 && state.documentsAcceptedFor === state.planRevision;
const selection = q => q.options.find(option => option[0] === (state.drafts[q.id] ?? (state.stale.includes(q.id) ? null : state.answers[q.id]?.value)));
const answerLabel = (q, value) => q.options.find(o => o[0] === value)?.[1] || value;
const draftCount = () => questions().filter(q => state.drafts[q.id]).length;
const pendingQuestions = () => questions().filter(q => !state.answers[q.id] || state.stale.includes(q.id));
function announce(text) { document.querySelector('#announcement').textContent = text; }
function showNotice(text) { state.notice = text; announce(text); }
function stateView() { return { variant, ...state }; }

function header() {
  const status = state.connection === 'connected' ? state.worker : state.connection;
  return `<div class="prototype-banner"><strong>THROWAWAY PROTOTYPE · ISSUE 10</strong><span>Sample data. Changes stay in this tab. Refresh resets the demo.</span></div>
    <header class="topbar"><div class="brand"><span class="brand-mark">${icon('layers')}</span>Software Factory</div>
    <div class="breadcrumb">Planning <span aria-hidden="true">/</span> Resume a planning session</div>
    <div class="top-actions">${badge(`<span class="dot"></span>${esc(status)}`, state.connection === 'connected' ? '' : 'amber')}
      <label class="small">View from <select id="client" aria-label="Simulated client view">${['Codex', 'BB', 'Browser'].map(c => `<option ${c === state.client ? 'selected' : ''}>${c}</option>`).join('')}</select></label>
    </div></header>`;
}
function tabs() {
  return `<nav class="tabs" aria-label="Planning views">${[['questions', 'Questions'], ['history', 'History'], ['plan', 'Plan & handoff']].map(([key, label]) => button('view', label, `data-view="${key}" class="${state.view === key ? 'active' : ''}" aria-current="${state.view === key ? 'page' : 'false'}"`)).join('')}</nav>`;
}
function outline() {
  return `<aside class="outline"><div class="outline-head"><h2>Resume a planning session</h2><span class="muted small">Grill with Docs · sample feature</span></div>
    <nav aria-label="Decision outline">
      ${button('scope', `${icon('check')}<span><span class="label">Scope & constraints</span><small>Agreed · local, single user</small></span>`, 'class="outline-item"')}
      ${button('round', `${icon(Object.keys(state.answers).length >= 2 ? 'check' : 'circle')}<span><span class="label">Plan & review</span><small>${state.round === 1 ? 'Ready · 2 questions' : 'Answered · round 1'}</small></span>`, `data-round="1" class="outline-item ${state.round === 1 && state.view === 'questions' ? 'current' : ''}"`)}
      ${button('round', `${icon(state.round === 1 ? 'lock' : state.answers.check ? 'check' : 'circle')}<span><span class="label">Records & checks</span><small>${!state.answers.documents || !state.answers.review ? 'Waiting on round 1' : 'Ready after round 1'}</small></span>`, `data-round="2" class="outline-item ${state.round === 2 && state.view === 'questions' ? 'current' : ''}" ${!state.answers.documents || !state.answers.review ? 'disabled' : ''}`)}
      ${button('view', `${icon(state.run ? 'check' : 'file')}<span><span class="label">Plan & handoff</span><small>${state.run ? 'Implementation linked' : isReady() ? 'Ready for your approval' : 'Waiting on decisions'}</small></span>`, `data-view="plan" class="outline-item ${state.view === 'plan' ? 'current' : ''}"`)}
    </nav><hr class="outline-rule"><div class="session-note">The same planning record from every entry point.<br><strong>${esc(state.session)}</strong><br>Plan revision ${state.planRevision}
    <a href="https://github.com/pessini/software-factory/issues/10" target="_blank" rel="noreferrer">Open issue 10</a>
    <p class="top-gap">Wayfinder maps are reserved for a later milestone.</p></div></aside>`;
}
function questionCard(q) {
  const current = selection(q);
  const draft = Boolean(state.drafts[q.id]);
  const submitted = Boolean(state.answers[q.id]) && !state.stale.includes(q.id);
  const discussionOpen = state.openDiscussions.includes(q.id);
  return `<article class="question" id="question-${q.id}" aria-labelledby="heading-${q.id}">
    <div class="flex between wrap"><span class="muted small">${q.topic}</span>${badge(draft ? 'Draft · not submitted' : submitted ? 'Submitted' : state.stale.includes(q.id) ? 'Needs review' : 'Ready to answer', draft || state.stale.includes(q.id) ? 'amber' : submitted ? '' : 'neutral')}</div>
    <h2 id="heading-${q.id}">${q.title}</h2><p class="description">${q.description}</p>
    <fieldset style="border:0;padding:0;margin:0"><legend class="sr-only">${q.title}</legend>${q.options.map(([value, title, detail]) => `<label class="answer-option"><input type="radio" name="${q.id}" id="${q.id}-${value}" value="${value}" ${current?.[0] === value ? 'checked' : ''} ${!available() ? 'disabled' : ''}><span><strong>${title}</strong><small>${detail}</small></span></label>`).join('')}</fieldset>
    <p class="recommendation"><strong>Recommendation.</strong> ${q.recommendation}</p>
    <div class="question-footer">${button('discuss', `${icon('chat')} Discuss${state.discussions[q.id]?.length ? ` (${state.discussions[q.id].length})` : ''}`, `data-id="${q.id}" class="text-button" aria-expanded="${discussionOpen}"`)}<span class="muted">${q.source}</span></div>
    ${discussionOpen ? `<div class="discussion">${(state.discussions[q.id] || []).map(d => `<p><strong>${esc(d.author)}</strong><br>${esc(d.text)}</p>`).join('')}
      <label for="discussion-${q.id}">Ask about this decision</label><textarea id="discussion-${q.id}" data-discussion="${q.id}" placeholder="What would change if…">${esc(state.discussionDrafts[q.id] || '')}</textarea>
      ${button('send-discussion', 'Send discussion', `data-id="${q.id}" class="text-button" ${!available() ? 'disabled' : ''}`)}<div class="small muted">The response is a preset example. It does not submit an answer.</div></div>` : ''}
  </article>`;
}
function submitRow() {
  const count = draftCount();
  return `<div class="submit-row"><div><strong class="small" data-draft-count>${count} prepared answer${count === 1 ? '' : 's'}</strong><small>Only submission records your choices.</small></div>
    ${button('submit', `Submit ${count || ''} answer${count === 1 ? '' : 's'} ${icon('arrow')}`, `class="primary" ${count === 0 || !available() ? 'disabled' : ''}`)}</div>`;
}
function statusNotice() {
  return `${state.connection !== 'connected' ? `<div class="notice warning" role="status"><strong>${state.connection === 'disconnected' ? 'Disconnected.' : 'Recovery required.'}</strong> ${state.connection === 'disconnected' ? 'Your draft is still in this demo tab. Reconnect before submitting.' : 'The response to the start request was lost. Check the saved request before continuing.'}<div>${button('recover', state.connection === 'disconnected' ? 'Reconnect demo' : 'Check saved request', 'class="text-button"')}</div></div>` : ''}
    ${state.stale.length ? `<div class="notice warning">${state.stale.length} later decisions need another review. The document wording and approval must also be reviewed.</div>` : ''}
    ${state.notice ? `<div class="notice" role="status">${esc(state.notice)}</div>` : ''}`;
}
function questionContent(focused = false) {
  if (state.worker === 'working') return `<div class="empty-state"><h2>Answers recorded in the demo</h2><p class="muted">Preparing the next round from your choices…</p></div>`;
  const pending = pendingQuestions();
  const hasDrafts = draftCount() > 0;
  if (state.round === 2 && pending.length === 0 && !hasDrafts) return `<div class="empty-state stack"><span>${badge('Questions complete')}</span><h2>Your decisions are ready to review.</h2><p class="muted">Check the proposed documents, then approve the exact plan revision. Finishing these questions does not start implementation.</p>${button('view', 'Review plan & handoff', 'data-view="plan" class="primary"')}</div>`;
  const qs = questions();
  state.focusIndex = Math.min(state.focusIndex, qs.length - 1);
  return `<div class="round-meta"><h2>Round ${state.round} <span class="muted">/ 2 in this example</span></h2><span class="small muted">${pending.length} ready · revision ${state.questionRevision}</span></div>
    ${focused ? `<div class="focus-progress">${qs.map((q, i) => button('focus', `<span class="sr-only">Question ${i + 1}: ${q.topic}</span>`, `data-index="${i}" class="${i === state.focusIndex ? 'active' : ''}"`)).join('')}</div>${questionCard(qs[state.focusIndex])}<div class="flex between" style="margin-bottom:20px">${button('focus', 'Previous question', `data-index="${Math.max(0, state.focusIndex - 1)}" ${state.focusIndex === 0 ? 'disabled' : ''}`)}${button('focus', 'Next question', `data-index="${Math.min(qs.length - 1, state.focusIndex + 1)}" ${state.focusIndex === qs.length - 1 ? 'disabled' : ''}`)}</div>` : qs.map(questionCard).join('')}
    ${submitRow()}`;
}
function documentWording() {
  return {
    glossary: state.answers.record?.value === 'session'
      ? 'Planning session — the shared record of questions, submitted answers, document revisions, and progress. Browser, BB, and Codex read the same session.'
      : 'Approved plan — the exact revision of the scope, decisions, source documents, and completion checks that the user approves for implementation.',
    decision: state.answers.record?.value === 'snapshot'
      ? 'An implementation run uses a fixed copy of the approved plan. Later planning changes create another revision and cannot change the active run.'
      : 'The shared controller owns the saved planning session. The browser, BB, and Codex use the same controller operations and do not store separate copies of decisions.',
  };
}
function documentEdits() {
  if (!state.answers.documents) return `<div class="stack"><p class="muted small">Document wording appears after you submit the plan contents answer.</p><p class="small muted">Suggestions are kept separate from accepted records.</p></div>`;
  const accepted = state.documentsAcceptedFor === state.planRevision;
  const wording = documentWording();
  const includeAdr = state.answers.documents.value === 'glossary_adr';
  return `<div class="small muted">${accepted ? 'Accepted in this demo' : 'Proposed wording'} · plan r${state.planRevision}</div>
    <div class="document-edit"><h3>CONTEXT.md ${badge(accepted ? 'Accepted' : 'Proposed', accepted ? '' : 'amber')}</h3><div class="source-rev">sample revision ${accepted ? state.docRevision : `${state.docRevision} → ${state.docRevision + 1}`}</div><p class="diff-line">+ ${wording.glossary}</p></div>
    ${includeAdr ? `<div class="document-edit"><h3>docs/adr/001-planning.md</h3><div class="source-rev">${accepted ? 'Accepted demo document' : 'New file · proposed'}</div><p class="diff-line">+ ${wording.decision}</p></div>` : '<p class="small muted">No decision record is proposed for this feature.</p>'}
    <div class="doc-actions">${button('accept-docs', accepted ? 'Wording accepted in demo' : 'Accept this wording', `${accepted || !state.answers.check || state.stale.length || !available() ? 'disabled' : ''} class="${accepted ? '' : 'primary'}"`)}<p class="small muted" style="margin-top:9px">${state.answers.check ? 'This action changes the demo record only. It writes no repository files.' : 'Settle the remaining decisions before accepting the wording.'}</p></div>`;
}
function documents() {
  return `<aside class="right-panel" aria-label="Documents and sources"><div class="flex between"><h2>Working documents</h2>${icon('file')}</div>
    <nav class="tabs" aria-label="Document views">${button('document-tab', 'Sources', `data-tab="sources" class="${state.documentTab === 'sources' ? 'active' : ''}"`)}${button('document-tab', 'Proposed changes', `data-tab="changes" class="${state.documentTab === 'changes' ? 'active' : ''}"`)}</nav>
    ${state.documentTab === 'sources' ? `<div class="source"><h3>Issue 10</h3><p>Planning and implementation have separate approval steps. The user approves an exact plan revision.</p><div class="source-rev">Source: issue body · 22 Sep 2026</div><a href="https://github.com/pessini/software-factory/issues/10" target="_blank" rel="noreferrer">Read requirements</a></div>
    <div class="source"><h3>Saved interactions</h3><p>The harness already records questions, answers, and repeated-answer checks. The planning screens must use shared controller operations.</p><div class="source-rev">src/software_factory/interactions.py<br>reference revision 3f683b9 · issue 10</div><a href="https://github.com/pessini/software-factory/blob/3f683b9/src/software_factory/interactions.py" target="_blank" rel="noreferrer">View reference code</a></div>
    <div class="source"><h3>Decisions in this session</h3><p>${Object.keys(state.answers).length} submitted answers · ${Object.keys(state.drafts).length} unsent drafts</p><p>One local user. Shared questions and progress. Explicit approval before starting a run.</p></div>
    <div class="notice small">Recommendations are suggestions. Select and submit an answer to record your choice.</div>` : documentEdits()}</aside>`;
}
function historyView() {
  return `<div class="top-gap"><h2>Session history</h2><p class="muted small" style="margin-top:8px">Submitted choices and document actions. Unsent drafts appear only on the question.</p>
    ${[...state.history].reverse().map(entry => `<article class="history-entry"><time>${esc(entry.time)} · plan r${entry.revision}</time><h3>${esc(entry.title)}</h3><p class="muted">${esc(entry.detail)}</p></article>`).join('')}
    <div class="top-gap">${button('revisit', 'Revisit plan contents', !state.answers.documents || !available() ? 'disabled' : '')}<p class="small muted" style="margin-top:9px">Changing this answer marks later decisions for review.</p></div></div>`;
}
function snapshot() {
  return { session: state.session, revision: state.planRevision, goal: 'Resume one shared planning session and explicitly approve its implementation.',
    workspace: 'pessini/software-factory', answers: structuredClone(state.answers), documents: { revision: state.docRevision, ...documentWording(), includeAdr: state.answers.documents?.value === 'glossary_adr' },
    scope: ['One local user', 'Shared browser, BB, and Codex session', 'Grill questions and document review'],
    checks: ['Confirmed answers survive reopening and service restart.', 'A repeated request returns the same implementation run.', 'Changing an earlier decision blocks unresolved plan handoff.', 'Later edits cannot change an approved run snapshot.'],
    exclusions: ['Wayfinder editor', 'Public hosting', 'Extra delivery permissions'], runSettings: 'Existing workflow rules and permission settings',
  };
}
function approvalControls() {
  return `<div class="approval"><label><input type="checkbox" id="approve-plan" ${state.approval ? 'checked' : ''} ${!isReady() || !available() ? 'disabled' : ''}><span>I approve plan revision ${state.planRevision} and its document versions for this simulated run.</span></label>
    ${button('start', `Start implementation ${icon('arrow')}`, `class="primary" ${!isReady() || !state.approval || !available() ? 'disabled' : ''}`)}<p class="small muted" style="margin-top:10px">Demo only. No worker starts and no delivery permissions change.</p></div>`;
}
function planBlockers() {
  const blocked = [];
  if (!['documents', 'review', 'record', 'check'].every(id => state.answers[id])) blocked.push('Submit the remaining answers.');
  if (Object.keys(state.drafts).length) blocked.push('Submit your prepared answer changes.');
  if (state.stale.length) blocked.push('Review affected decisions.');
  if (state.documentsAcceptedFor !== state.planRevision) blocked.push('Accept the proposed document wording.');
  return blocked;
}
function planView() {
  const ready = isReady();
  const blocked = planBlockers();
  return `<div class="top-gap stack"><div class="flex between wrap"><h2>The plan you will approve</h2>${badge(`Revision ${state.planRevision} · ${ready ? 'ready for approval' : 'not ready'}`, ready ? '' : 'amber')}</div>
    <div class="plan-sheet"><h2>Resume a planning session</h2><p class="muted">Continue the same questions and decisions from a browser, BB, or Codex, then approve one implementation run.</p>
    <dl><dt>Session</dt><dd>${state.session}</dd><dt>Workspace</dt><dd>pessini/software-factory</dd><dt>Documents</dt><dd>${state.answers.documents?.value === 'glossary_adr' ? 'CONTEXT.md + docs/adr/001-planning.md' : state.answers.documents ? 'CONTEXT.md' : 'Awaiting your decision'} · sample revision ${state.docRevision}</dd><dt>Run settings</dt><dd>Existing workflow checks and delivery permissions</dd></dl>
    <h2>Scope & decisions</h2><p class="muted small">One person, one local service. Shared Grill questions, documents, and an explicit implementation handoff.</p>
    ${allQuestions().filter(q => state.answers[q.id]).map(q => `<div class="review-answer"><h3>${esc(state.answers[q.id].question)} ${state.stale.includes(q.id) ? badge('Needs review', 'amber') : ''}</h3><p>${esc(state.answers[q.id].label)}</p></div>`).join('')}
    <h2>Observable completion checks</h2><ul>${snapshot().checks.map(check => `<li>${check}</li>`).join('')}</ul>
    <h2>Assumptions & deferred work</h2><p class="muted small">The local service must be running. Controller separation is a prerequisite. Wayfinder editing and public hosting are deferred. Starting a run does not grant permission to push, merge, or deploy.</p>
    <h2>Document wording</h2>${documentEdits()}
    ${blocked.length ? `<div class="notice warning"><strong>Before implementation</strong><ul>${blocked.map(item => `<li>${item}</li>`).join('')}</ul>${button('view', 'Return to questions', 'data-view="questions"')}</div>` : ''}
    ${approvalControls()}
    </div>${state.run ? runPanel() : ''}</div>`;
}
function reviewSteps() { return ['Scope & limits', 'Recorded decisions', 'Completion checks', 'Document wording', 'Approve revision']; }
function focusPlan() {
  const step = state.reviewStep, parts = [
    `<h2>What work does this plan authorize?</h2><p class="top-gap">Resume one shared planning session, then start implementation only after explicit approval.</p><dl><dt>In scope</dt><dd>One local user; shared browser, BB, and Codex session; Grill questions and document review.</dd><dt>Deferred</dt><dd>Wayfinder editing and public hosting.</dd><dt>Assumption</dt><dd>The local service is running. Shared controller operations are available.</dd><dt>Workspace</dt><dd>pessini/software-factory</dd><dt>Permissions</dt><dd>Existing file, commit, push, merge, and deployment rules remain in force.</dd></dl>`,
    `<h2>Are these still the right decisions?</h2><p class="muted top-gap">Changing an earlier choice opens its dependent decisions for review.</p>${allQuestions().filter(q => state.answers[q.id]).map(q => `<div class="review-answer"><h3>${esc(state.answers[q.id].question)}</h3><p>${esc(state.answers[q.id].label)} ${state.stale.includes(q.id) ? badge('Needs review', 'amber') : ''}</p></div>`).join('')}${button('revisit', 'Revisit plan contents')}`,
    `<h2>How will we know it works?</h2><ul class="top-gap">${snapshot().checks.map(c => `<li>${c}</li>`).join('')}</ul><div class="notice">These are required checks for the real feature. The prototype only demonstrates their visible states.</div>`,
    `<h2>Does the wording match your intent?</h2><p class="muted top-gap">Accept these exact document changes for plan r${state.planRevision}.</p>${documentEdits()}`,
    `<h2>Approve one exact revision.</h2><dl><dt>Plan</dt><dd>r${state.planRevision} · ${state.session}</dd><dt>Documents</dt><dd>Sample revision ${state.docRevision} · ${state.answers.documents?.label || 'not yet selected'}</dd><dt>Scope</dt><dd>Resume a planning session</dd><dt>Start request</dt><dd>${state.session}:plan-r${state.planRevision}</dd></dl>${planBlockers().length ? `<div class="notice warning"><ul>${planBlockers().map(b => `<li>${b}</li>`).join('')}</ul></div>` : '<div class="notice">All required decisions and document wording are settled. Implementation is waiting for your approval.</div>'}${approvalControls()}`,
  ];
  return `<div class="review-step-heading"><span class="muted small">Review ${step + 1} of 5</span>${badge(`Plan r${state.planRevision}`, 'neutral')}</div><section class="plan-sheet focus-review">${parts[step]}</section><div class="flex between top-gap">${button('review-step', 'Previous section', `data-step="${Math.max(0, step - 1)}" ${step === 0 ? 'disabled' : ''}`)}${button('review-step', step === 4 ? 'Review complete' : `Next: ${reviewSteps()[step + 1]}`, `data-step="${Math.min(4, step + 1)}" ${step === 4 ? 'disabled' : ''} class="primary"`)}</div>${state.run ? runPanel() : ''}`;
}
function signoffPanel() {
  return `<section class="signoff"><h2>Before you start</h2><p class="muted small top-gap">You are approving the document on the left and its source versions.</p><div class="readiness-list">
    ${[['Required decisions', Object.keys(state.answers).length === 4 && !state.stale.length && !Object.keys(state.drafts).length], ['Document wording', state.documentsAcceptedFor === state.planRevision], ['Exact revision', true]].map(([name, done]) => `<div class="flex between"><span class="flex">${icon(done ? 'check' : 'circle')}${name}</span>${badge(name === 'Exact revision' ? `r${state.planRevision}` : done ? 'Settled' : 'Review needed', done ? '' : 'amber')}</div>`).join('')}
    </div><dl><dt>Plan</dt><dd>r${state.planRevision}</dd><dt>Documents</dt><dd>sample revision ${state.docRevision}</dd><dt>Workspace</dt><dd>pessini/software-factory</dd></dl>
    ${planBlockers().length ? `<div class="notice warning">${planBlockers().map(esc).join(' ')}</div>` : ''}${approvalControls()}<p class="small muted top-gap">Later edits create another revision. They cannot change this implementation run.</p></section>${state.run ? runPanel() : ''}`;
}
function runPanel() {
  const run = state.run;
  return `<section class="run"><h3>${run.status === 'completed' ? 'Implementation completed · sample result' : 'Implementation linked · simulated'}</h3><p><code>${run.id}</code> uses plan r${run.snapshot.revision}. ${run.snapshot.revision !== state.planRevision ? `Your current plan is r${state.planRevision}; the linked snapshot has not changed.` : 'Repeating this request returns the same run.'}</p>
    <div class="step-list">${['Validate', 'Build', 'Test', 'Review', 'Report'].map((s, i) => `<span class="${i === run.step ? 'current' : ''}">${s}</span>`).join('')}</div>
    <div class="flex wrap">${button('repeat', 'Repeat same request', !available() ? 'disabled' : '')}${button('advance-run', run.status === 'completed' ? 'View sample report' : 'Advance demo run', !available() ? 'disabled' : '')}${button('edit-plan', 'Edit plan after handoff', !available() ? 'disabled' : '')}</div>
    ${run.status === 'completed' ? '<p class="top-gap">Sample report: all example checks passed. No real code was changed, no checks were run, and no pull request was created.</p>' : ''}
    <details class="top-gap"><summary>Approved snapshot · plan r${run.snapshot.revision}</summary><pre class="state-json">${esc(JSON.stringify(run.snapshot, null, 2))}</pre></details></section>`;
}
function toolsPanel() {
  return `<details class="prototype-tools" id="demo-tools"><summary>Prototype controls & full state</summary><p class="muted top-gap">Use these controls to inspect the proposed states. Connection, worker, and cross-client behavior are simulated.</p>
    <div class="tool-grid">${button('reset', 'Reset example')}${button('ready-example', 'Load ready plan')}${button('disconnect', 'Disconnect')}${button('recover', 'Reconnect / recover')}${button('lost-response', state.simulateLostResponse ? 'Lost-response demo armed' : 'Lose next start response')}</div>
    <label class="small">Layout <select id="variant-select">${Object.entries(variants).map(([key, label]) => `<option value="${key}" ${variant === key ? 'selected' : ''}>${key} — ${label}</option>`).join('')}</select></label>
    <p class="small muted top-gap">Design question: which layout keeps questions, document effects, and approval clear?</p><pre class="state-json" id="full-state">${esc(JSON.stringify(stateView(), null, 2))}</pre></details>`;
}
function content(focused = false) { return `${statusNotice()}${state.view === 'questions' ? questionContent(focused) : state.view === 'history' ? historyView() : focused ? focusPlan() : planView()}${toolsPanel()}`; }
function VariantA() {
  return `<div class="workspace">${outline()}<main id="main" class="main-column"><div class="page-heading"><h1>${state.view === 'plan' ? 'Know what you’re approving.' : 'Let’s settle the details.'}</h1><p>${state.view === 'plan' ? 'Review the scope, decisions, and exact document versions before work starts.' : 'Review the recommendations. Prepare your answers. Submit when ready.'}</p></div>${tabs()}${content()}</main>${documents()}</div>`;
}
function VariantB() {
  return `<main id="main" class="focus-layout"><header class="focus-heading"><h1>${state.view === 'plan' ? 'A clear path to approval.' : 'One decision at a time.'}</h1><p>Resume a planning session · review each part before work starts.</p></header>${tabs()}<div class="focus-content top-gap"><nav class="focus-rail" aria-label="${state.view === 'plan' ? 'Plan review sections' : 'Planning views'}">${state.view === 'plan' ? reviewSteps().map((label, i) => button('review-step', `${i + 1}. ${label}`, `data-step="${i}" class="${state.reviewStep === i ? 'active' : ''}"`)).join('') : [['questions', 'The interview'], ['history', 'Past decisions'], ['plan', 'Review & approve']].map(([key, label]) => button('view', label, `data-view="${key}" class="${state.view === key ? 'active' : ''}"`)).join('')}<p class="small muted" style="padding:12px">Plan r${state.planRevision}<br>${Object.keys(state.answers).length} of 4 answers submitted</p></nav><section>${content(true)}<details class="focus-docs" id="focus-docs"><summary>Source documents</summary>${documents()}</details></section></div></main>`;
}
function manuscript() {
  return `<article class="manuscript"><h2>Resume a planning session.</h2>${badge(`Plan r${state.planRevision} · sample feature`, 'amber')}
    <p>A planning session follows the work across a browser, BB, and Codex. The same questions, answers, and documents remain available from each entry point.</p>
    <h3>The boundary</h3><p>Planning produces an agreed feature plan. The user reviews that plan before starting an implementation run.</p>
    <div class="annotation"><div class="flex between"><strong>Plan contents</strong>${badge(state.answers.documents ? 'Answered' : 'Open decision', state.answers.documents ? '' : 'amber')}</div><p class="small">${state.answers.documents ? esc(state.answers.documents.label) : 'Which documents need to travel with the approved plan?'}</p>${button(state.answers.documents ? 'revisit' : 'round', state.answers.documents ? 'Revisit this decision' : 'Answer in round 1', 'data-round="1" class="text-button"')}</div>
    <h3>When a decision changes</h3><p>${state.answers.review?.value === 'inline' ? 'Mark each affected question and document section in place. Review these items before approving another run.' : 'Collect affected answers and plan sections in one review. Each item must be settled before the plan is ready.'}</p>
    <h3>The approved copy</h3><p>The implementation run receives the exact plan revision the user approved. Later planning edits leave that copy unchanged.</p>
    ${state.view === 'plan' ? `<h3>Recorded decisions</h3>${allQuestions().filter(q => state.answers[q.id]).map(q => `<div class="review-answer"><strong class="small">${esc(state.answers[q.id].question)}</strong><p>${esc(state.answers[q.id].label)} ${state.stale.includes(q.id) ? badge('Needs review', 'amber') : ''}</p></div>`).join('')}<h3>Completion checks</h3><ul>${snapshot().checks.map(c => `<li>${c}</li>`).join('')}</ul><h3>Limits and assumptions</h3><p>One local user. The local service is running. Shared controller operations are available. Wayfinder editing and public hosting are deferred. Existing delivery permissions remain in force.</p><h3>Exact document wording</h3>${documentEdits()}` : ''}
    <div class="annotation"><strong>${state.stale.length ? 'Affected wording needs another review' : 'Document changes'}</strong><p class="small">${state.documentsAcceptedFor === state.planRevision ? 'The wording is accepted for the current demo revision.' : 'Proposed wording stays separate until you accept it.'}</p>${button('show-changes', 'Inspect proposed wording', 'class="text-button"')}</div>
    ${documents()}</article>`;
}
function VariantC() {
  return `<main id="main" class="document-layout"><header class="document-heading"><div><h1>Make the plan say what you mean.</h1><p>Read the feature plan. Resolve its open decisions. Approve one exact revision.</p></div>${badge(`Plan r${state.planRevision}`, 'neutral')}</header>${tabs()}<div class="document-columns ${state.view === 'plan' ? 'review-columns' : ''}">${manuscript()}<section>${state.view === 'plan' ? statusNotice() + signoffPanel() + toolsPanel() : content()}</section></div></main>`;
}
function PrototypeSwitcher() {
  // This app is only served by the development-only runner, which refuses production mode.
  return `<nav class="switcher" aria-label="Prototype layout switcher">${button('previous-variant', icon('left'), 'aria-label="Previous layout"')}<div class="switcher-label">${variant} — ${variants[variant]}<small>Compare 3 layouts · same demo state</small></div>${button('next-variant', icon('right'), 'aria-label="Next layout"')}<span class="keys">← / →</span></nav>`;
}
function render() {
  const activeId = document.activeElement?.id;
  const openDetails = [...document.querySelectorAll('details[open][id]')].map(el => el.id);
  app.innerHTML = `${header()}${({ A: VariantA, B: VariantB, C: VariantC })[variant]()}${PrototypeSwitcher()}`;
  openDetails.forEach(id => { const el = document.getElementById(id); if (el) el.open = true; });
  if (activeId) document.getElementById(activeId)?.focus({ preventScroll: true });
}
function updateVariant(key) {
  variant = key;
  const url = new URL(location.href); url.searchParams.set('variant', key); history.replaceState(null, '', url);
  render(); announce(`Layout ${key}: ${variants[key]}. Your demo answers are unchanged.`);
}
function cycleVariant(offset) {
  const keys = Object.keys(variants); updateVariant(keys[(keys.indexOf(variant) + offset + keys.length) % keys.length]);
}
function invalidate(ids) {
  state.stale = [...new Set([...state.stale, ...ids.filter(id => state.answers[id])])];
  ids.forEach(id => delete state.drafts[id]);
  state.documentsAcceptedFor = null; state.approval = false;
}
function submitAnswers() {
  if (!available() || !draftCount()) return;
  state.planRevision += 1;
  const qs = questions();
  for (const q of qs) {
    const value = state.drafts[q.id]; if (!value) continue;
    if (state.answers[q.id] && state.answers[q.id].value !== value) {
      if (q.id === 'documents') invalidate(['record', 'check']);
      if (q.id === 'review') invalidate(['check']);
    }
    state.answers[q.id] = { value, label: answerLabel(q, value), question: q.title, questionRevision: state.questionRevision };
    delete state.drafts[q.id]; state.stale = state.stale.filter(id => id !== q.id);
    recordEvent('Answer submitted', `${q.title} ${answerLabel(q, value)}`);
  }
  state.documentsAcceptedFor = null; state.approval = false;
  const roundComplete = qs.every(q => state.answers[q.id] && !state.stale.includes(q.id));
  if (roundComplete && state.round === 1) {
    state.worker = 'working'; showNotice('Answers recorded in this tab before preparing the next round.'); render();
    const workingState = state;
    setTimeout(() => {
      if (state !== workingState) return;
      state.round = 2; state.questionRevision += 1; state.focusIndex = 0; state.worker = 'waiting for you';
      showNotice(`Round 2 now follows your ${state.answers.documents.value === 'glossary_adr' ? 'glossary and decision records' : 'glossary-only'} choice.`); render();
    }, 550);
  } else { showNotice(roundComplete ? 'Questions complete. Review the proposed wording, then approve the plan.' : 'Prepared answers submitted. The remaining questions stay in this round.'); render(); }
}
function startRun() {
  if (!isReady() || !state.approval || !available()) return;
  const key = `${state.session}:plan-r${state.planRevision}`;
  if (state.handoffs[key]) { state.run = state.handoffs[key]; showNotice(`Existing request found: ${state.run.id}. No second run was created.`); return; }
  const run = { id: `demo-run-${String(Object.keys(state.handoffs).length + 1).padStart(3, '0')}`, request: key, snapshot: snapshot(), step: 0, status: 'validating' };
  state.request = key; state.handoffs[key] = run;
  recordEvent('Plan approved for simulated implementation', `Plan r${state.planRevision}; request ${key}.`);
  if (state.simulateLostResponse) {
    state.simulateLostResponse = false; state.connection = 'recovery required';
    showNotice('The demo saved a run, but its response was lost. Check the saved request to recover its ID.');
  } else { state.run = run; showNotice(`Implementation linked to ${run.id}. No real worker was started.`); }
}
const actions = {
  'previous-variant': () => cycleVariant(-1), 'next-variant': () => cycleVariant(1),
  view: el => { state.view = el.dataset.view; state.notice = ''; },
  scope: () => { state.view = 'history'; },
  round: el => { state.round = Number(el.dataset.round); state.view = 'questions'; state.focusIndex = 0; },
  focus: el => { state.focusIndex = Number(el.dataset.index); },
  'review-step': el => { state.reviewStep = Number(el.dataset.step); },
  'document-tab': el => { state.documentTab = el.dataset.tab; },
  'show-changes': () => { state.documentTab = 'changes'; showNotice('Proposed wording is shown in Working documents.'); },
  discuss: el => { const id = el.dataset.id; state.openDiscussions = state.openDiscussions.includes(id) ? state.openDiscussions.filter(q => q !== id) : [...state.openDiscussions, id]; },
  'send-discussion': el => {
    const id = el.dataset.id, text = state.discussionDrafts[id]?.trim();
    if (!text || !available()) return;
    state.discussions[id] ||= []; state.discussions[id].push({ author: 'You', text }, { author: 'Example reply · simulated', text: 'The source requires one shared session and explicit approval. Consider whether this choice changes the document wording or the checks. Your submitted answer is unchanged.' });
    state.discussionDrafts[id] = ''; showNotice('Example discussion added. No answer was submitted.');
  },
  submit: submitAnswers,
  'accept-docs': () => {
    if (!state.answers.check || state.stale.length || !available()) return;
    state.documentsAcceptedFor = state.planRevision; state.docRevision += 1;
    recordEvent('Document wording accepted in demo', `Sample document revision ${state.docRevision}, for plan r${state.planRevision}.`);
    showNotice('Wording accepted in this demo. Review the plan to approve implementation.');
  },
  revisit: () => { if (!available()) return; state.round = 1; state.view = 'questions'; state.focusIndex = 0; state.approval = false; showNotice('Select a different plan contents answer and submit it. Dependent decisions will then need review.'); },
  start: startRun,
  repeat: () => { if (!available() || !state.run) return; state.run = state.handoffs[state.run.request]; showNotice(`Same request, same run: ${state.run.id}. ${Object.keys(state.handoffs).length} demo run(s) exist.`); },
  'advance-run': () => { if (!available() || !state.run) return; state.run.step = Math.min(4, state.run.step + 1); state.run.status = state.run.step === 4 ? 'completed' : ['validating', 'building', 'testing', 'reviewing'][state.run.step]; showNotice(`Demo run: ${state.run.status}. This is a simulated result.`); },
  'edit-plan': () => { if (!available()) return; state.planRevision += 1; invalidate(['record', 'check']); state.view = 'questions'; state.round = 2; state.questionRevision += 1; showNotice('A new planning revision needs review. The active run still has its original snapshot.'); },
  reset: () => { state = initialState(); announce('Example reset.'); },
  'ready-example': () => {
    loadReviewExample(true);
    recordEvent('Ready-plan fixture loaded', 'All example answers and document wording accepted. User approval is still required.'); showNotice('Ready-plan example loaded. Review and approve it to simulate the handoff.');
  },
  disconnect: () => { state.connection = 'disconnected'; showNotice('Disconnected example. Drafts remain in this tab; submissions are disabled.'); },
  recover: () => { state.connection = 'connected'; if (state.request && state.handoffs[state.request]) { state.run = state.handoffs[state.request]; showNotice(`Recovered ${state.run.id} from the saved demo request. No second run was created.`); } else showNotice('Demo reconnected. Continue with the existing answers and drafts.'); },
  'lost-response': () => { state.simulateLostResponse = !state.simulateLostResponse; showNotice(state.simulateLostResponse ? 'The next new start request will simulate a lost response.' : 'Lost-response simulation disabled.'); },
};
app.addEventListener('click', event => {
  const el = event.target.closest('button[data-action]'); if (!el || el.disabled) return;
  actions[el.dataset.action]?.(el); render();
});
app.addEventListener('change', event => {
  const el = event.target;
  if (el.matches('input[type="radio"]')) { state.drafts[el.name] = el.value; state.approval = false; render(); announce('Answer prepared. Submit the round to record it.'); }
  if (el.id === 'approve-plan') { state.approval = el.checked; render(); }
  if (el.id === 'client') { state.client = el.value; showNotice(`Showing the same in-memory example from ${state.client}. Real client switching is not connected.`); render(); }
  if (el.id === 'variant-select') updateVariant(el.value);
});
app.addEventListener('input', event => {
  if (event.target.dataset.discussion) { state.discussionDrafts[event.target.dataset.discussion] = event.target.value; const pre = document.getElementById('full-state'); if (pre) pre.textContent = JSON.stringify(stateView(), null, 2); }
});
document.addEventListener('keydown', event => {
  if (event.target.closest('input, textarea, select, [contenteditable]') || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
  if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); cycleVariant(event.key === 'ArrowRight' ? 1 : -1); }
});
window.addEventListener('popstate', () => { variant = new URLSearchParams(location.search).get('variant') || 'A'; if (!variants[variant]) variant = 'A'; render(); });
function loadReviewExample(accepted = false) {
  state = initialState();
  for (const round of [1, 2]) for (const q of questions(round)) state.answers[q.id] = { value: q.options[0][0], label: q.options[0][1], question: q.title, questionRevision: round };
  state.round = 2; state.planRevision = 5; state.docRevision = accepted ? 2 : 1;
  state.documentsAcceptedFor = accepted ? 5 : null; state.view = 'plan';
  state.history.push({ title: 'Two rounds answered · sample records', detail: 'Glossary and decision records; grouped review; controller ownership; repeated start request.', revision: 5, time: 'Before this review · sample data' });
}
loadReviewExample();
render();
