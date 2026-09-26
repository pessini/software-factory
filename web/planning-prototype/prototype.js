/* THROWAWAY: issue 10. Original sample-state behavior, redesigned presentation.
 * Static assets: Geist and Phosphor (licenses in assets/). No controller calls. */
const app = document.querySelector('#app');
const variants = { A: 'Brief', B: 'Review desk', C: 'Changes' };
const iconNames = { layers:'stack', file:'file-text', arrow:'arrow-right', left:'caret-left', right:'caret-right', chat:'chat-circle', lock:'lock-simple', close:'x', grid:'squares-four', history:'clock', scope:'book-open', checks:'list-checks', decisions:'git-branch', docs:'file-text', theme:'sun' };
const icon = name => `<svg class="icon" viewBox="0 0 256 256" fill="currentColor" aria-hidden="true"><use href="assets/phosphor.svg#${iconNames[name] || name}"></use></svg>`;
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
    notice: '', event: 0, reviewArea: 'scope', activeFile: 'glossary', fileMode: 'reading', drawer: null, revisiting: false,
  };
}
let state = initialState();
let theme = 'system';
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
function stateView() { return { variant, theme, ...state }; }

function questionCard(q) {
  const current = selection(q);
  const draft = Boolean(state.drafts[q.id]);
  const submitted = Boolean(state.answers[q.id]) && !state.stale.includes(q.id);
  const discussionOpen = state.openDiscussions.includes(q.id);
  return `<article class="question" id="question-${q.id}" aria-labelledby="heading-${q.id}">
    <div class="flex between wrap"><span class="question-topic">${icon('chat')}${q.topic}</span>${badge(draft ? 'Draft · not submitted' : submitted ? 'Submitted' : state.stale.includes(q.id) ? 'Needs review' : 'Ready to answer', draft || state.stale.includes(q.id) ? 'amber' : submitted ? '' : 'neutral')}</div>
    <h2 id="heading-${q.id}">${q.title}</h2><p class="description">${q.description}</p>
    <fieldset style="border:0;padding:0;margin:0"><legend class="sr-only">${q.title}</legend>${q.options.map(([value, title, detail]) => `<label class="answer-option"><input type="radio" name="${q.id}" id="${q.id}-${value}" value="${value}" ${current?.[0] === value ? 'checked' : ''} ${!available() ? 'disabled' : ''}><span><strong>${title}</strong><small>${detail}</small></span></label>`).join('')}</fieldset>
    <details class="recommendation" id="recommendation-${q.id}"><summary>Why this recommendation?</summary><p>${q.recommendation}</p></details>
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
  if (state.round === 2 && pending.length === 0 && !hasDrafts && !state.revisiting) return `<div class="empty-state stack"><span>${badge('Questions complete')}</span><h2>Your decisions are ready to review.</h2><p class="muted">Check the proposed documents, then approve the exact plan revision. Finishing these questions does not start implementation.</p>${button('view', 'Review plan & handoff', 'data-view="plan" class="primary"')}</div>`;
  const qs = questions();
  state.focusIndex = Math.min(state.focusIndex, qs.length - 1);
  return `<div class="round-meta"><h2>Round ${state.round} <span class="muted">/ 2 in this example</span></h2><span class="small muted">${pending.length} ready · revision ${state.questionRevision}</span></div>
    ${focused ? `<div class="focus-progress">${qs.map((q, i) => button('focus', `<span class="sr-only">Question ${i + 1}: ${q.topic}</span>`, `data-index="${i}" class="${i === state.focusIndex ? 'active' : ''}"`)).join('')}</div>${questionCard(qs[state.focusIndex])}<div class="flex between" style="margin-bottom:20px">${button('focus', 'Previous question', `data-index="${Math.max(0, state.focusIndex - 1)}" ${state.focusIndex === 0 ? 'disabled' : ''}`)}${button('focus', 'Next question', `data-index="${Math.min(qs.length - 1, state.focusIndex + 1)}" ${state.focusIndex === qs.length - 1 ? 'disabled' : ''}`)}</div>` : qs.map(questionCard).join('')}
    ${submitRow()}`;
}
function documentWording() {
  return {
    glossary: state.answers.record?.value === 'session'
      ? 'Planning session : the shared record of questions, submitted answers, document revisions, and progress. Browser, BB, and Codex read the same session.'
      : 'Approved plan : the exact revision of the scope, decisions, source documents, and completion checks that the user approves for implementation.',
    decision: state.answers.record?.value === 'snapshot'
      ? 'An implementation run uses a fixed copy of the approved plan. Later planning changes create another revision and cannot change the active run.'
      : 'The shared controller owns the saved planning session. The browser, BB, and Codex use the same controller operations and do not store separate copies of decisions.',
  };
}
function snapshot() {
  return { session: state.session, revision: state.planRevision, goal: 'Resume one shared planning session and explicitly approve its implementation.',
    workspace: 'pessini/software-factory', answers: structuredClone(state.answers), documents: { revision: state.docRevision, ...documentWording(), includeAdr: state.answers.documents?.value === 'glossary_adr' },
    scope: ['One local user', 'Shared browser, BB, and Codex session', 'Grill questions and document review'],
    checks: ['Confirmed answers survive reopening and service restart.', 'A repeated request returns the same implementation run.', 'Changing an earlier decision blocks unresolved plan handoff.', 'Later edits cannot change an approved run snapshot.'],
    exclusions: ['Wayfinder editor', 'Public hosting', 'Extra delivery permissions'], runSettings: 'Existing workflow rules and permission settings',
  };
}
function planBlockers() {
  const blocked = [];
  if (!['documents', 'review', 'record', 'check'].every(id => state.answers[id])) blocked.push('Submit the remaining answers.');
  if (Object.keys(state.drafts).length) blocked.push('Submit your prepared answer changes.');
  if (state.stale.length) blocked.push('Review affected decisions.');
  if (state.documentsAcceptedFor !== state.planRevision) blocked.push('Accept the proposed document wording.');
  return blocked;
}
function runPanel() {
  const run = state.run;
  return `<section class="run"><h3>${run.status === 'completed' ? 'Implementation completed · sample result' : 'Implementation linked · simulated'}</h3><p><code>${run.id}</code> uses plan r${run.snapshot.revision}. ${run.snapshot.revision !== state.planRevision ? `Your current plan is r${state.planRevision}; the linked snapshot has not changed.` : 'Repeating this request returns the same run.'}</p>
    <div class="step-list">${['Validate', 'Build', 'Test', 'Review', 'Report'].map((s, i) => `<span class="${i === run.step ? 'current' : ''}">${s}</span>`).join('')}</div>
    <div class="flex wrap">${button('repeat', 'Repeat same request', !available() ? 'disabled' : '')}${button('advance-run', run.status === 'completed' ? 'View sample report' : 'Advance demo run', !available() ? 'disabled' : '')}${button('edit-plan', 'Edit plan after handoff', !available() ? 'disabled' : '')}</div>
    ${run.status === 'completed' ? '<p class="top-gap">Sample report: all example checks passed. No real code was changed, no checks were run, and no pull request was created.</p>' : ''}
    <details class="top-gap"><summary>Approved snapshot · plan r${run.snapshot.revision}</summary><pre class="state-json">${esc(JSON.stringify(run.snapshot, null, 2))}</pre></details></section>`;
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
  state.revisiting = false;
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
  view: el => { state.view = el.dataset.view; state.notice = ''; state.drawer = null; },
  'review-area': el => { state.reviewArea = el.dataset.area; state.view = 'plan'; },
  'open-drawer': el => { state.drawer = el.dataset.drawer; },
  'open-file': el => { state.activeFile = el.dataset.file; state.drawer = 'documents'; },
  'edit-answer': el => { state.round = ['documents', 'review'].includes(el.dataset.id) ? 1 : 2; state.focusIndex = questions(state.round).findIndex(q => q.id === el.dataset.id); state.view = 'questions'; state.drawer = null; state.revisiting = true; showNotice('Prepare your new answer, then submit it to update the plan.'); },
  'close-drawer': () => { state.drawer = null; },
  'select-file': el => { state.activeFile = el.dataset.file; },
  'file-mode': el => { state.fileMode = el.dataset.mode; },
  'switch-layout': el => updateVariant(el.dataset.variant),
  'review-documents': () => { state.reviewArea = 'documents'; state.view = 'plan'; if (variant === 'A') state.drawer = 'documents'; },
  scope: () => { state.view = 'history'; },
  round: el => { state.round = Number(el.dataset.round); state.view = 'questions'; state.focusIndex = 0; },
  focus: el => { state.focusIndex = Number(el.dataset.index); },
  'review-step': el => { state.reviewStep = Number(el.dataset.step); },
  'document-tab': el => { state.documentTab = el.dataset.tab; },
  'show-changes': () => { state.drawer = 'documents'; },
  discuss: el => { const id = el.dataset.id; state.openDiscussions = state.openDiscussions.includes(id) ? state.openDiscussions.filter(q => q !== id) : [...state.openDiscussions, id]; },
  'send-discussion': el => {
    const id = el.dataset.id, text = state.discussionDrafts[id]?.trim();
    if (!text || !available()) return;
    state.discussions[id] ||= []; state.discussions[id].push({ author: 'You', text }, { author: 'Example reply · simulated', text: 'The source requires one shared session and explicit approval. Consider whether this choice changes the document wording or the checks. Your submitted answer is unchanged.' });
    state.discussionDrafts[id] = ''; showNotice('Example discussion added. No answer was submitted.');
  },
  submit: submitAnswers,
  'accept-docs': () => {
    if (!['documents', 'review', 'record', 'check'].every(id => state.answers[id]) || state.stale.length || !available()) return;
    if (Object.keys(state.drafts).length || state.documentsAcceptedFor === state.planRevision) return;
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
  if (el.dataset.action === 'open-drawer') drawerReturnFocus = el.dataset.drawer;
  if (el.dataset.action === 'open-file') drawerReturnFocus = `file:${el.dataset.file}`;
  actions[el.dataset.action]?.(el); render();
});
app.addEventListener('change', event => {
  const el = event.target;
  if (el.matches('input[type="radio"]')) { state.drafts[el.name] = el.value; state.approval = false; render(); announce('Answer prepared. Submit the round to record it.'); }
  if (el.id === 'approve-plan') { state.approval = el.checked; render(); }
  if (el.id === 'client') { state.client = el.value; showNotice(`Showing the same in-memory example from ${state.client}. Real client switching is not connected.`); render(); }
  if (el.id === 'variant-select') updateVariant(el.value);
  if (el.id === 'theme-select') { theme = el.value; render(); }
});
app.addEventListener('input', event => {
  if (event.target.dataset.discussion) { state.discussionDrafts[event.target.dataset.discussion] = event.target.value; const pre = document.getElementById('full-state'); if (pre) pre.textContent = JSON.stringify(stateView(), null, 2); }
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && state.drawer) { state.drawer = null; render(); return; }
  if (event.target.closest('input, textarea, select, [contenteditable]') || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
  if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); cycleVariant(event.key === 'ArrowRight' ? 1 : -1); }
});
window.addEventListener('popstate', () => { variant = new URLSearchParams(location.search).get('variant') || 'A'; if (!variants[variant]) variant = 'A'; render(); });
function loadReviewExample(accepted = false) {
  state = initialState();
  for (const round of [1, 2]) for (const q of questions(round)) state.answers[q.id] = { value: q.options[0][0], label: q.options[0][1], question: q.title, questionRevision: round };
  state.round = 2; state.planRevision = 5; state.docRevision = accepted ? 2 : 1; state.questionRevision = 2;
  state.documentsAcceptedFor = accepted ? 5 : null; state.view = 'plan';
  state.history.push({ title: 'Two rounds answered · sample records', detail: 'Glossary and decision records; grouped review; controller ownership; repeated start request.', revision: 5, time: 'Before this review · sample data' });
}
loadReviewExample();
render();
