/* Throwaway presentation study. Native HTML controls, custom product styling.
 * DESIGN_VARIANCE 6 / MOTION_INTENSITY 3 / VISUAL_DENSITY 4.
 * No marketing hero or decorative imagery: the review itself is the product. */
const reviewAreas = [
  ['scope', 'Scope & limits', 'What the work includes', 'scope'],
  ['decisions', 'Decisions', 'The choices behind the plan', 'decisions'],
  ['checks', 'Completion checks', 'How success will be verified', 'checks'],
  ['documents', 'Document wording', 'Review the proposed changes', 'docs'],
];
let drawerReturnFocus = null;
const decisionsSettled = () => ['documents', 'review', 'record', 'check'].every(id => state.answers[id]) && !state.stale.length && !Object.keys(state.drafts).length && discussionDraftCount() === 0;
const docCount = () => state.answers.documents?.value === 'glossary_adr' ? 2 : state.answers.documents ? 1 : 0;

function brand() { return `<div class="brand"><span class="brand-mark">${icon('layers')}</span><span>Software Factory</span></div>`; }
function sessionHeading() {
  return `<div class="session-heading"><div class="title-line"><h1>Plan the shared interface</h1><span class="revision">plan r${state.planRevision}</span></div><p>See the destination and approach first. Grill questions make the plan concrete; review and approve the exact handoff after the answers settle.</p></div>`;
}
function workflowOverview() {
  const handoff = state.request && state.handoffs[state.request];
  const runDone = handoff?.status === 'completed';
  const steps = [
    ['Destination + approach', 'Request received · focused feature → Grill with Docs', 'done'],
    ['Adaptive Grill rounds', state.grillFinished ? 'Finished; decisions recorded' : decisionsSettled() ? 'Answers settled · finish the Grill' : `Round ${state.round} · answer and submit`, state.grillFinished ? 'done' : 'active'],
    ['Review plan + documents', state.grillFinished ? `Plan r${state.planRevision} · docs r${state.docRevision}` : 'Waiting for the Grill', state.grillFinished ? 'active' : 'waiting'],
    ['Approve + start handoff', handoff ? 'One implementation request saved' : isReady() ? `Ready to approve r${state.planRevision}` : 'Waiting for plan review', handoff ? 'done' : isReady() ? 'active' : 'waiting'],
    ['LangGraph workflow', runDone ? 'Sample report ready' : handoff ? `Run ${handoff.id} · ${handoff.status}` : 'Validate → build → test → review → report', runDone ? 'done' : handoff ? 'active' : 'waiting'],
  ];
  const next = state.run?.status === 'completed' ? 'The sample run is complete. Its report belongs to the approved plan snapshot.'
    : state.run ? 'The approved plan is now a simulated LangGraph run. Advance the steps below to see its report.'
    : handoff && !state.run ? 'The request was saved, but its response was lost. Recover the saved run before continuing.'
    : state.grillFinished ? 'The Grill is finished. Review the exact plan and document versions. Approve plan and start implementation performs the handoff.'
    : decisionsSettled() ? 'All current questions are answered. Finish the Grill to prepare the reviewable plan.'
    : 'Answer the current round and submit it. The next round appears after the agent uses those answers.';
  return `<section class="flow-map" aria-label="Example flow from request to implementation">
    <div class="flow-request"><div><span class="eyebrow">Example request · started in Codex</span><p>“${esc(sampleCase.request)}”</p></div><div class="flow-session"><span>Session</span><strong>${esc(sampleCase.session)}</strong><span>Workspace · ${esc(sampleCase.project)}</span><span>Continue in Browser or BB</span></div></div>
    <div class="flow-path-choice"><article class="selected"><span>Focused feature · this example</span><strong>Grill with Docs</strong><small>Adaptive question rounds; the glossary and decisions evolve with your answers.</small></article><article><span>Large or uncertain effort · later</span><strong>Wayfinder</strong><small>Name the destination, map dependent decisions, and open a Grill discussion for a ticket when needed.</small></article></div>
    <ol class="flow-steps">${steps.map(([title, detail, status], index) => `<li class="${status}"><span class="flow-number">${index + 1}</span><div><strong>${title}</strong><small>${detail}</small></div></li>`).join('')}</ol>
    <div class="flow-footer"><p>${icon('info')}<span>${next}</span></p><details open><summary>How does the larger-effort path continue?</summary><p>After the decision map clears, the installed Wayfinder flow hands to <strong>to-spec → to-tickets → implement</strong>. Issue 10 proposes a shared saved handoff for an approved scope in a later UI milestone; that bridge still needs to match the current skill flow.</p></details></div>
  </section>`;
}
function mainNavigation(className = '') {
  return `<nav class="main-navigation ${className}" aria-label="Planning views">${[['questions', 'Grill questions', 'chat'], ['plan', 'Plan & handoff', 'book-open'], ['history', 'History', 'history']].map(([key, text, glyph]) => button('view', `${icon(glyph)}<span>${text}</span>${key === 'questions' && state.stale.length ? `<span class="count">${state.stale.length}</span>` : ''}`, `data-view="${key}" class="${state.view === key ? 'selected' : ''}" ${state.view === key ? 'aria-current="page"' : ''}`)).join('')}</nav>`;
}
function utilityButtons() {
  return `<div class="utilities">${button('open-drawer', `${icon('file')}<span>Sources</span>`, 'data-drawer="sources" class="quiet" aria-label="Sources"')}${button('open-drawer', icon('sliders-horizontal'), 'data-drawer="settings" class="icon-button" aria-label="Appearance and demo settings"')}</div>`;
}
function pageHeader(withBrand = false) {
  return `<header class="page-header">${withBrand ? brand() : '<div class="breadcrumb">Planning <span>/</span> <a href="https://github.com/pessini/software-factory/issues/10" target="_blank" rel="noreferrer">Issue 10</a></div>'}${withBrand ? mainNavigation('horizontal') : ''}${utilityButtons()}</header>`;
}
function sidebar() {
  return `<aside class="app-sidebar">${brand()}<div class="workspace-name">${icon('code')}<span>${esc(sampleCase.project)}</span></div>${mainNavigation()}<div class="sidebar-context"><span class="small muted">Current session</span><strong>${esc(sampleCase.session)}</strong><span class="small muted">${esc(sampleCase.route)}</span></div><div class="sidebar-bottom"><span class="small muted">Local planning workspace</span><a href="https://github.com/pessini/software-factory/issues/10" target="_blank" rel="noreferrer">View issue 10 ${icon('arrow-up-right')}</a></div></aside>`;
}
function sectionHeading(title, subtitle, right = '') {
  return `<div class="section-heading"><div><h2>${title}</h2>${subtitle ? `<p>${subtitle}</p>` : ''}</div>${right}</div>`;
}
function scopeContent(compact = false) {
  return `<div class="scope-content"><p class="scope-intro">${esc(sampleCase.request)} This plan keeps one saved session across ${sampleCase.clients.join(', ')} and sends work to the existing implementation workflow only after explicit approval.</p><div class="scope-columns"><section><h3>${icon('check-circle')}Included</h3><ul><li>One local user and one shared session</li><li>Adaptive Grill questions with saved answers and document history</li><li>Review the exact plan and document revisions before handoff</li><li>One linked implementation run with existing checks and permissions</li></ul></section><section><h3>${icon('clock')}Deferred</h3><ul><li>Wayfinder map editor in this first milestone</li><li>Public or multi-user hosting</li><li>Extra delivery permissions or autonomous start</li></ul></section></div>${compact ? '' : `<details class="disclosure" id="scope-assumptions"><summary><span>Assumptions & constraints</span>${icon('caret-down')}</summary><div class="disclosure-body"><p>The local service must be running. The shared controller owns the saved session and continuation.</p><p>Existing file, commit, push, merge, and deployment permissions remain in force.</p><p>Target repository: <strong>${esc(sampleCase.project)}</strong></p></div></details>`}</div>`;
}
function decisionList(compact = false) {
  const answered = allQuestions().filter(q => state.answers[q.id]);
  if (!answered.length) return emptyMessage('No decisions submitted', 'Answer the current round to build the plan.', 'questions');
  return `<div class="decision-list ${compact ? 'compact' : ''}">${answered.map(q => {
    const answer = state.answers[q.id], stale = state.stale.includes(q.id);
    const detail = q.options.find(o => o[0] === answer.value)?.[2];
    return `<details class="decision-row" id="decision-${q.id}"><summary><span class="row-icon ${stale ? 'pending' : ''}">${icon(stale ? 'warning-circle' : 'check')}</span><span><span class="row-label">${q.topic}</span><strong>${esc(answer.label)}</strong></span>${stale ? badge('Review needed', 'pending') : icon('caret-down')}</summary><div class="disclosure-body"><p>${esc(answer.question)}</p><p>${esc(detail || 'The question changed. Review the earlier answer against the new scope.')}</p><div class="row-meta">Submitted in question revision ${answer.questionRevision}</div>${button('edit-answer', `${icon('note-pencil')} Revisit decision`, `data-id="${q.id}" class="quiet"`)}</div></details>`;
  }).join('')}</div>`;
}
function checksContent() {
  const labels = ['Continue after reopening', 'One run for each approved request', 'Review the effects of a changed answer', 'Keep the approved plan unchanged'];
  return `<div class="check-list">${snapshot().checks.map((text, index) => `<details class="check-row" id="completion-${index}"><summary>${icon('checks')}<span>${labels[index]}</span>${icon('caret-down')}</summary><div class="disclosure-body"><p>${text}</p><span class="row-meta">Required during implementation. Not verified by this prototype.</span></div></details>`).join('')}</div><p class="helper">These define completion. They are not results from a live implementation run.</p>`;
}
function fileRecords() {
  if (!state.answers.documents) return [];
  const wording = documentWording();
  const records = [{ id: 'glossary', path: 'CONTEXT.md', title: state.answers.record?.value === 'session' ? 'Define the planning session' : 'Define the approved plan', kind: 'Glossary addition', text: wording.glossary,
    why: 'Keep the meaning of approval clear wherever the session is opened.' }];
  if (state.answers.documents.value === 'glossary_adr') records.push({ id: 'decision', path: 'docs/adr/001-planning.md', title: state.answers.record?.value === 'snapshot' ? 'Preserve the approved snapshot' : 'Keep one owner for the session', kind: 'New decision record', text: wording.decision,
    why: 'Record the reason for the choice so future changes preserve the agreed behavior.' });
  return records;
}
function filePreview(file, mode = 'reading') {
  return `<section class="file-preview"><header><span class="file-path">${icon('file')}${file.path}</span><span class="file-meta">${state.grillFinished ? `For approval · doc r${state.docRevision}` : `Working draft · doc r${state.docRevision}`}</span></header><div class="${mode === 'changes' ? 'diff-body' : 'reading-body'}">${mode === 'changes' ? `<div class="diff-context"><span>@@</span>${file.id === 'glossary' ? 'Glossary / new definition' : 'Decision / new record'}</div><div class="diff-addition"><span aria-hidden="true">+</span><div><h3>${file.title}</h3><p>${file.text}</p></div></div>` : `<span class="document-type">${file.kind}</span><h3>${file.title}</h3><p>${file.text}</p>`}</div></section>`;
}
function documentsContent() {
  const files = fileRecords();
  if (state.drawer === 'documents') files.sort((a, b) => Number(b.id === state.activeFile) - Number(a.id === state.activeFile));
  if (!files.length) return emptyMessage('No document changes yet', 'Choose the plan contents in the question round first.', 'questions');
  return `<div class="document-review"><div class="review-intro"><p>${state.grillFinished ? 'Review these exact document versions as part of the implementation plan.' : 'This working draft changes as answers are submitted. Finish the Grill to prepare its final review version.'}</p>${badge(state.grillFinished ? 'Ready for review' : 'Working draft', state.grillFinished ? '' : 'pending')}</div>${files.map(file => filePreview(file)).join('')}<div class="document-accept"><p class="helper">The <strong>Approve plan and start implementation</strong> action approves the displayed plan and document revisions together. No file is written by this prototype.</p></div></div>`;
}
function reviewItems() {
  return `<div class="review-items"><div>${icon(decisionsSettled() ? 'check-circle' : 'circle')}<span>Grill decisions</span><strong>${state.stale.length ? `${state.stale.length} to revisit` : `${Object.keys(state.answers).length} of 4 settled`}</strong></div><div>${icon(state.grillFinished ? 'check-circle' : 'circle')}<span>Plan documents</span><strong>${state.grillFinished ? `Doc r${state.docRevision} ready` : 'Draft in progress'}</strong></div><div>${icon(state.approval ? 'check-circle' : 'circle')}<span>Plan approval</span><strong>${state.approval ? 'Confirmed' : 'Waiting for you'}</strong></div></div>`;
}
function approvalCard() {
  const nextAction = !decisionsSettled()
    ? button('view', `Continue Grill ${icon('arrow')}`, 'data-view="questions" class="secondary"')
    : !state.grillFinished
      ? button('finish-grill', `Finish Grill ${icon('arrow')}`, 'class="secondary"')
      : button('review-documents', `Review wording ${icon('arrow')}`, 'class="secondary"');
  return `<section class="approval-card" aria-label="Implementation approval"><div class="approval-icon">${icon('lock')}</div><h2>${state.run ? 'Implementation linked' : isReady() ? 'Ready for your approval' : 'Before work begins'}</h2><p class="muted">${state.run ? `The approved copy belongs to ${state.run.id}.` : 'You decide when this plan becomes an implementation request.'}</p>${reviewItems()}<div class="approval-version"><span>Plan revision</span><strong>r${state.planRevision}</strong></div>
    ${state.run ? `<div class="next-action"><span>Saved handoff</span><p>${state.run.id} · ${state.run.status}</p>${button('view', 'View run report', 'data-view="plan" class="secondary"')}</div>` : !isReady() ? `<div class="next-action"><span>Next action</span><p>${planBlockers()[0]}</p>${nextAction}</div>` : approvalControls()}
    <div class="approval-footnote">${icon('lock')}<span>Later edits cannot change the approved copy.</span></div></section>`;
}
function approvalControls() {
  return `<div class="approval-controls"><p>Approves plan r${state.planRevision} with document r${state.docRevision} and creates one implementation request.</p>${button('start', `Approve plan & start implementation ${icon('arrow')}`, `class="primary" ${!isReady() || !available() ? 'disabled' : ''}`)}<p class="helper">This is the explicit handoff. The existing workflow owns validation, build, tests, review, and delivery permissions.</p></div>`;
}
function revisionNote() { return `<div class="revision-note">${icon('clock')}<div><strong>From two Grill rounds</strong><p>${state.grillFinished ? 'Review the final wording and scope here, then approve the exact revisions to hand them off.' : decisionsSettled() ? 'All answers are settled. Finish the Grill to prepare the final plan.' : 'Submit each round; dependent questions appear after their prerequisites are settled.'}</p></div></div>`; }
function briefPlan() {
  return `<div class="brief-columns"><div class="brief-content"><section class="outcome-section">${sectionHeading('What we will build', 'The agreed boundary of this feature.')}${scopeContent(true)}</section><section class="attention-section">${sectionHeading(state.grillFinished ? 'Plan documents ready' : 'Working document draft', state.grillFinished ? `Review the versions included in plan r${state.planRevision}.` : decisionsSettled() ? 'The decisions are settled. Finish the Grill to prepare final wording.' : 'Draft wording follows the answers you submit.', badge(`${docCount()} files`, 'neutral'))}<div class="file-list">${fileRecords().map(file => button('open-file', `<span class="file-list-icon">${icon('file')}</span><span><strong>${file.path}</strong><small>${file.title}</small></span><span class="file-list-state">${state.grillFinished ? 'For approval' : 'Draft'}</span>${icon('right')}`, `data-file="${file.id}"`)).join('') || '<p class="helper">Submit the first question round to propose documents.</p>'}</div></section><section class="decisions-section">${sectionHeading('Agreed decisions', 'Open a decision to inspect the reason or revisit it.')}${decisionList(true)}</section><details class="section-disclosure" id="brief-checks"><summary><span>${icon('checks')}Completion checks</span><span class="muted">4 required</span>${icon('caret-down')}</summary><div>${checksContent()}</div></details><details class="section-disclosure" id="brief-constraints"><summary><span>${icon('info')}Assumptions & constraints</span>${icon('caret-down')}</summary><div><p>The shared local service must be running. The implementation uses existing controller operations and delivery permissions.</p></div></details></div><aside class="brief-aside">${approvalCard()}${revisionNote()}${button('open-drawer', `${icon('file')} Inspect source material ${icon('arrow-up-right')}`, 'data-drawer="sources" class="source-shortcut"')}</aside></div>`;
}
function VariantA() {
  return `<div class="app-shell">${sidebar()}<div class="workspace">${pageHeader()}<main id="main" class="brief-page">${sessionHeading()}${statusNotice()}${workflowOverview()}${state.view === 'plan' ? briefPlan() : supportingPage()}${state.view === 'plan' && state.run ? runPanel() : ''}</main></div></div>`;
}
function sectionNavigation() {
  return `<nav class="section-navigation" aria-label="Plan sections">${reviewAreas.map(([key, label, description, glyph]) => button('review-area', `${icon(glyph)}<span><strong>${label}</strong><small>${description}</small></span>${key === 'documents' && docCount() ? `<span class="count">${docCount()}</span>` : icon('right')}`, `data-area="${key}" class="${state.reviewArea === key ? 'selected' : ''}" ${state.reviewArea === key ? 'aria-current="page"' : ''}`)).join('')}</nav>`;
}
function areaContent() {
  const area = state.reviewArea;
  return area === 'scope' ? scopeContent() : area === 'decisions' ? decisionList() : area === 'checks' ? checksContent() : documentsContent();
}
function VariantB() {
  const area = reviewAreas.find(([key]) => key === state.reviewArea) || reviewAreas[0];
  return `<div class="desk-shell">${pageHeader(true)}<main id="main" class="desk-page">${sessionHeading()}${statusNotice()}${workflowOverview()}${state.view !== 'plan' ? supportingPage() : `<div class="desk-columns"><aside class="desk-navigation"><h2>Review the plan</h2><p class="helper">Move freely between sections.</p>${sectionNavigation()}<div class="review-count"><strong>${state.grillFinished ? 'Plan ready for review' : decisionsSettled() ? 'Finish Grill to prepare plan' : 'Questions still need attention'}</strong><p>Approving the exact plan and documents starts the handoff.</p></div></aside><section class="desk-reader"><header class="reader-heading"><span class="reader-icon">${icon(area[3])}</span><h2>${area[1]}</h2>${state.reviewArea === 'documents' ? badge(state.grillFinished ? 'Ready for review' : 'Working draft', state.grillFinished ? '' : 'pending') : badge('In plan', 'neutral')}</header><div class="reader-body">${areaContent()}</div><footer class="reader-footer"><span>Plan r${state.planRevision}</span>${button('review-area', `${state.reviewArea === 'documents' ? 'Back to scope' : 'Next section'} ${icon('arrow')}`, `data-area="${reviewAreas[(reviewAreas.findIndex(([key]) => key === state.reviewArea) + 1) % reviewAreas.length][0]}" class="quiet"`)}</footer></section><aside class="desk-aside">${approvalCard()}<div class="context-note"><h3>Why this matters</h3><p>${state.reviewArea === 'documents' ? 'Start includes the exact wording and versions shown here.' : state.reviewArea === 'checks' ? 'These checks become the evidence required to complete the work.' : 'Each entry point uses one shared record. Approval applies to the whole plan.'}</p>${button('open-drawer', 'Open sources', 'data-drawer="sources" class="text-button"')}</div></aside></div>`}${state.view === 'plan' && state.run ? runPanel() : ''}</main></div>`;
}
function VariantC() {
  const files = fileRecords();
  const file = files.find(f => f.id === state.activeFile) || files[0];
  return `<div class="changes-shell">${pageHeader(true)}<main id="main" class="changes-page">${sessionHeading()}${statusNotice()}${workflowOverview()}${state.view !== 'plan' ? supportingPage() : `<div class="changes-workspace"><aside class="file-navigation"><div class="file-navigation-heading"><h2>Plan documents</h2><span>${files.length}</span></div>${files.map(f => button('select-file', `${icon('file')}<span><strong>${f.path}</strong><small>${f.kind}</small></span>`, `data-file="${f.id}" class="${file?.id === f.id ? 'selected' : ''}" ${file?.id === f.id ? 'aria-current="page"' : ''}`)).join('')}<div class="file-navigation-note">${icon('lock')}<p>These versions are included in the plan review.</p></div>${button('open-drawer', `${icon('book-open')} Read the whole plan ${icon('arrow-up-right')}`, 'data-drawer="plan" class="whole-plan-link"')}</aside><section class="change-reader"><header class="change-toolbar"><span>${state.grillFinished ? 'Final plan wording' : 'Working draft wording'}</span><div class="segmented" role="group" aria-label="Document presentation">${button('file-mode', 'Read', `data-mode="reading" aria-pressed="${state.fileMode === 'reading'}"`)}${button('file-mode', 'Changes', `data-mode="changes" aria-pressed="${state.fileMode === 'changes'}"`)}</div></header>${file ? filePreview(file, state.fileMode) : emptyMessage('No proposed documents', 'Submit the first round to create a document proposal.', 'questions')}<div class="document-reason"><h3>Why this is in the plan</h3><p>${file?.why || 'Documents follow the decisions you submit.'}</p><div class="reason-source">${icon('decisions')}<span>${file?.id === 'decision' ? esc(state.answers.record?.label) : esc(state.answers.documents?.label || 'Awaiting a decision')}</span>${button('edit-answer', 'Revisit', `data-id="${file?.id === 'decision' ? 'record' : 'documents'}" class="text-button"`)}</div></div><footer class="change-reader-footer"><span>${icon('file')}Document r${state.docRevision}</span>${file && files.length > 1 ? button('select-file', `Other document ${icon('right')}`, `data-file="${file.id === 'glossary' ? 'decision' : 'glossary'}" class="quiet"`) : ''}</footer></section><aside class="change-context"><h2>Review context</h2><p class="helper">${esc(sampleCase.route)}</p><details open class="context-disclosure" id="change-scope"><summary>Scope ${icon('caret-down')}</summary><p>One local user. One saved session shared across Codex, the browser, and BB. Approval includes exact plan and document revisions.</p></details><details class="context-disclosure" id="change-checks"><summary>Completion checks ${icon('caret-down')}</summary>${checksContent()}</details><details class="context-disclosure" id="change-exclusions"><summary>Deferred work ${icon('caret-down')}</summary><p>Wayfinder map editing, public hosting, and additional delivery permissions.</p></details>${button('open-drawer', `${icon('file')} Open sources`, 'data-drawer="sources" class="quiet" aria-label="Sources"')}<div class="change-accept"><h3>${state.grillFinished ? 'Ready for explicit approval' : 'Plan not ready yet'}</h3><p class="helper">Plan r${state.planRevision} includes ${files.length} ${files.length === 1 ? 'document' : 'documents'} at doc r${state.docRevision}. The action below approves this complete set.</p></div></aside></div><section class="handoff-strip"><div><span class="handoff-icon">${icon('lock')}</span><div><h2>Approve plan r${state.planRevision}</h2><p>${isReady() ? `Includes document r${state.docRevision}.` : planBlockers()[0]}</p></div></div>${isReady() ? approvalControls() : `<div class="handoff-pending">${badge('Waiting for review', 'neutral')}<p>Finish the Grill and review its plan first.</p></div>`}</section>`}${state.view === 'plan' && state.run ? runPanel() : ''}</main></div>`;
}
function emptyMessage(title, text, view) { return `<div class="empty-state"><span class="empty-icon">${icon('book-open')}</span><h2>${title}</h2><p>${text}</p>${button('view', 'Open questions', `data-view="${view}" class="secondary"`)}</div>`; }
function historyView() {
  return `<section class="activity-list">${sectionHeading('How the plan developed', 'Submitted choices and accepted changes. Drafts are kept separate.')}${[...state.history].reverse().map(entry => `<article class="activity-item"><span class="activity-icon">${icon(entry.title.includes('Document') ? 'file' : 'history')}</span><div><div class="activity-meta"><time>${esc(entry.time)}</time><span>Plan r${entry.revision}</span></div><h3>${esc(entry.title)}</h3><p>${esc(entry.detail)}</p></div></article>`).join('')}</section>`;
}
function supportingPage() {
  return state.view === 'history' ? `<div class="supporting-layout">${historyView()}<aside class="context-note"><h3>A record of your choices</h3><p>Only submitted answers change the plan. Revisit a decision to prepare a new answer.</p>${button('revisit', 'Revisit plan contents', 'class="secondary"')}</aside></div>` : `<div class="supporting-layout"><section>${questionContent(variant === 'B')}</section><aside class="question-context"><h2>The decision tree</h2><p class="helper">Only questions with settled prerequisites are ready.</p>${button('round', `${icon('decisions')}<span>Plan & review<small>First question round</small></span>`, 'data-round="1" class="tree-item"')}${button('round', `${icon(state.answers.documents && state.answers.review ? 'decisions' : 'lock')}<span>Records & checks<small>${state.answers.documents && state.answers.review ? 'Ready after round 1' : 'Waiting on round 1'}</small></span>`, `data-round="2" class="tree-item" ${!state.answers.documents || !state.answers.review ? 'disabled' : ''}`)}<div class="context-note"><h3>Documents follow your answers</h3><p>Recommendations are suggestions. Submit your choices before they change the plan.</p>${button('open-drawer', 'Open sources', 'data-drawer="sources" class="text-button"')}</div></aside></div>`;
}
function sourceContent() {
  return `<div class="sources-list"><article>${icon('book-open')}<h3>Shared planning interface</h3><p>Issue 10 defines the session, Grill-first flow, document review, and explicit handoff requirements.</p><a href="https://github.com/pessini/software-factory/issues/10" target="_blank" rel="noreferrer">Read issue 10 ${icon('arrow-up-right')}</a></article><article>${icon('chat')}<h3>Grill with UI reference</h3><p>The reference uses question cards, staged answers and discussions, adaptive rounds, and an explicit Finish Grill action. This example applies that sequence to the shared local session.</p><a href="https://github.com/jasonku09/grill-with-ui/tree/d6dbc7585315a3224ac92b9ba8d93d7b93db227f" target="_blank" rel="noreferrer">View the pinned reference ${icon('arrow-up-right')}</a></article><article>${icon('code')}<h3>Saved interactions</h3><p>The issue references the existing answer records and repeated-answer checks.</p><code>src/software_factory/interactions.py</code><span class="helper">Reference revision 3f683b9</span><a href="https://github.com/pessini/software-factory/blob/3f683b9/src/software_factory/interactions.py" target="_blank" rel="noreferrer">View reference code ${icon('arrow-up-right')}</a></article><article>${icon('lock')}<h3>About this prototype</h3><p>All answers, documents, revisions, and runs are sample data. Refresh resets the demo. No repository files or model workers are changed.</p></article></div>`;
}
function settingsContent() {
  return `<div class="settings-form"><label for="theme-select">Appearance</label><select id="theme-select">${['system','light','dark'].map(value => `<option value="${value}" ${theme === value ? 'selected' : ''}>${value[0].toUpperCase() + value.slice(1)}</option>`).join('')}</select><p class="helper">System follows your device. This choice stays in the tab.</p><label for="client">Simulated client view</label><select id="client">${['Codex','BB','Browser'].map(value => `<option ${state.client === value ? 'selected' : ''}>${value}</option>`).join('')}</select><p class="helper">All three labels use the same sample session.</p><h3>Explore a state</h3><div class="tool-grid">${button('reset', 'Fresh interview')}${button('ready-example', 'Ready plan')}${button('disconnect', 'Disconnect')}${button('recover', 'Reconnect / recover')}${button('lost-response', state.simulateLostResponse ? 'Lost response armed' : 'Lose next start response')}</div><details id="full-state-details" class="disclosure"><summary>Full prototype state ${icon('caret-down')}</summary><pre id="full-state" class="state-json">${esc(JSON.stringify(stateView(), null, 2))}</pre></details></div>`;
}
function drawer() {
  if (!state.drawer) return '';
  const titles = { sources:'Source material', settings:'Appearance & demo controls', documents:'Review document wording', plan:'The complete plan' };
  let content = state.drawer === 'sources' ? sourceContent() : state.drawer === 'settings' ? settingsContent() : state.drawer === 'documents' ? documentsContent() : `${sectionHeading('Scope & limits')}${scopeContent()}${sectionHeading('Recorded decisions')}${decisionList()}${sectionHeading('Completion checks')}${checksContent()}`;
  return `<dialog class="drawer ${state.drawer === 'documents' ? 'document-drawer' : ''}" id="review-drawer" aria-labelledby="drawer-title"><header><h2 id="drawer-title">${titles[state.drawer]}</h2>${button('close-drawer', icon('close'), 'class="icon-button" aria-label="Close panel" autofocus')}</header><div class="drawer-body">${content}</div></dialog>`;
}
function PrototypeSwitcher() {
  return `<footer class="prototype-dock"><div class="prototype-label">${icon('flask')}<strong>Prototype</strong><span>Sample data</span></div><nav aria-label="Prototype layout switcher">${button('previous-variant', icon('left'), 'class="icon-button" aria-label="Previous layout"')}<div class="layout-options">${Object.entries(variants).map(([key, name]) => button('switch-layout', `<span>${key}</span>${name}`, `data-variant="${key}" class="${variant === key ? 'selected' : ''}" aria-pressed="${variant === key}"`)).join('')}</div>${button('next-variant', icon('right'), 'class="icon-button" aria-label="Next layout"')}</nav><div class="prototype-dock-tools">${button('open-drawer', `${icon('sliders-horizontal')}<span>Demo controls</span>`, 'data-drawer="settings" class="quiet"')}</div></footer>`;
}
function render() {
  const active = document.activeElement, activeId = active?.id;
  if (!state.drawer && active?.dataset.drawer) drawerReturnFocus = active.dataset.drawer;
  const openDetails = [...document.querySelectorAll('details[open][id]')].map(el => el.id);
  document.documentElement.dataset.theme = theme;
  app.innerHTML = `${({ A: VariantA, B: VariantB, C: VariantC })[variant]()}${PrototypeSwitcher()}${drawer()}`;
  openDetails.forEach(id => { const el = document.getElementById(id); if (el) el.open = true; });
  const dialog = document.getElementById('review-drawer');
  if (dialog) { dialog.showModal(); dialog.addEventListener('cancel', event => { event.preventDefault(); state.drawer = null; render(); }); }
  if (activeId) document.getElementById(activeId)?.focus({ preventScroll: true });
  else if (!dialog && drawerReturnFocus) { document.querySelector(drawerReturnFocus.startsWith('file:') ? `[data-action="open-file"][data-file="${drawerReturnFocus.slice(5)}"]` : `[data-drawer="${drawerReturnFocus}"]`)?.focus({ preventScroll: true }); drawerReturnFocus = null; }
}
