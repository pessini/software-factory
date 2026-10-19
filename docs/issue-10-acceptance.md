# Issue 10 verification: Grill planning and handoff (first release)

Verification date: 2026-10-19. The first-release Grill UI and the
planning-to-LangGraph handoff are implemented and passed one live end-to-end flow.
The later Wayfinder milestone is not implemented. See [planning](planning.md) for
usage and design, and the [compact live evidence](issue-10-planning-evidence.json).

## Live flow

Environment: BB 0.46.1, Apple Container 1.0.0, worker image 0.154.0, MCP Python
SDK 2.3.0, LangGraph 1.2.16, `gpt-5.6-luna / low` for every worker. The target was
a disposable repository with one helper, one test, and a README.

1. **Browser start.** `software-factory web` served the UI. The request was "Add a
   slugify function to text_utils.py that turns product names into URL slugs for
   product pages, with tests."
2. **Round 1.** The interviewer read the README and code, then mapped four topics.
   Three were ready, and one waited on the other three. It asked three questions,
   each with a recommendation and options, and proposed two glossary terms.
   - The first answer deliberately differed from the recommendation, and the
     saved decision matches the user's choice.
   - The second answer used **Ask about this** (`[discuss]`). The interviewer
     explained what the standard library can and cannot transliterate, kept that
     decision open, listed the affected decisions, and asked it again in round 2.
   - The third answer used the recommendation.
3. **Round 2** settled the remaining decision. The revision finished as `planned`
   with six completion checks, three decisions, three glossary terms, assumptions,
   exclusions, deferred items, document versions, and plan hash `f0f25bfd…`.
4. **Cross-interface reads.** The CLI `plan-status` and BB `/plan-status` showed the
   same session, revision, and hash. BB included the browser link.
5. **Approval.** In the browser, the exact-revision checkbox enabled **Start
   implementation**. Implementation run `fct_6e6ea07fc968` reached `ready_for_review`:
   - Baseline, Planner, Builder, checks, and Review each dispatched once.
   - The required criteria were the six plan checks plus `planning-glossary`.
   - The Builder implemented the planned decisions and wrote `CONTEXT.md` with the
     approved wording. Six unit tests pass.
   - No commit or push occurred.
6. **Repeated approval.** Approving again from the CLI returned the same run. The UI
   showed the implementation status and opened the final report.

The three planning worker operations (one create, two clarifications) each
dispatched once. The planning run never changed the target.

## Acceptance criteria (first release)

| Criterion | Status | Evidence |
| --- | --- | --- |
| Runs from this repository with local setup | Met | `software-factory web`; [planning](planning.md) |
| BB, Codex, browser open/control the same session | Met | Browser + CLI + BB live. Codex uses the same `factory_plan_*` MCP tools (transport test lists them) |
| No BB task storage or BB-only rules | Met | Planning runs use Factory storage and shared control |
| Cards, recommendations, discussion, grouped submit, history, document previews | Met | Live rounds; right-column glossary and decision records |
| Questions adapt and respect dependencies | Met | Waiting topic; re-asked decision after `[discuss]` |
| Only submissions record decisions; drafts distinct | Met | Browser drafts labelled; first answer overrode the recommendation |
| Glossary and decision records saved and traceable | Met | Plan snapshot per revision; `CONTEXT.md` written through the verified implementation run |
| Refresh, client switching, restart preserve progress | Met | Durable interactions and detached jobs; simulated tests plus live reads from three interfaces |
| Duplicate submissions do not repeat work; conflicts fail clearly | Met | Same-answer resubmission is a no-op; a different answer or a stale revision fails (tests) |
| Changed earlier answer flags affected decisions; blocks unresolved handoff | Met | `affected` list; only a `planned` revision with the exact hash can be approved (tests) |
| Inspect and approve the exact plan revision | Met | Hash-bound approval; wrong hash refused (test) |
| Handoff supplies scope and checks; records run ID | Met | Required criteria and handoff record (live) |
| Repeated or uncertain handoff does not duplicate | Met | Deterministic request ID; repeated approval returned the same run (live + test) |
| Later edits do not change the approved snapshot | Met | Revision test compares the earlier snapshot after revising |
| Existing protection, checks, review, delivery permissions | Met | Normal implementation run; no new permissions |
| Planning UI shows implementation status and final results | Met | Status pill, report dialog, PR link when delivery publishes |
| One full flow, recovery, cross-interface; simulated vs live separated | Met | This record (live); `tests/integration/test_planning.py` (simulated) |

Wayfinder criteria remain open for the later milestone.

## Automated checks

- `tests/integration/test_planning.py` (scripted workers) covers:
  - interview round trip and idempotent start;
  - answer validation;
  - exact-hash approval with exactly one launched implementation;
  - revision immutability, stale-question rejection, and revision history;
  - a waiting planning run not reserving the workspace.
- `tests/unit/test_planning_web.py` covers:
  - contract rules and handoff criteria;
  - quoted choices;
  - the token cookie flow;
  - host, origin, and content-type rejection.
