# Issue 10: plan review prototype

**Throwaway code. Keep this directory on `feat/10-planning-ui-prototype`, out of main.**

Question: **Which layout helps a person review the exact plan before starting implementation?**

The user selected plan review as the main purpose of this experiment. It opens with four
sample answers already submitted and document wording still waiting for acceptance.
The interview remains available to explore changed decisions.

## Run

From the repository root:

```sh
uv run --no-project python web/planning-prototype/serve.py
```

Open <http://127.0.0.1:4310/?variant=A>. Stop the server with `Ctrl+C`.
Use `--port 4311` if the port is occupied. No packages, model credentials, build, or database
are needed. The server binds only to `127.0.0.1` and serves this directory.
It refuses to run when `NODE_ENV=production`; it is not part of the production application.

| Layout | URL | Review approach | Trade-off |
| --- | --- | --- | --- |
| A — Workbench | `/?variant=A` | Topics left, full review center, sources right | Strong context; more scrolling and higher density |
| B — Focus | `/?variant=B` | Five review sections, followed by explicit approval | Less information at once; comparing sections takes another click |
| C — Document first | `/?variant=C` | Full feature plan beside a compact approval panel | Wording and approval are close; long plans require substantial reading |

Use the floating arrows, or the keyboard's left and right arrows, to compare layouts.
The keyboard shortcut leaves inputs, text fields, selects, and editable content alone.
The layout is recorded in the URL. Switching layouts preserves this tab's demo state.
Reloading retains the layout but resets the sample session.

## Try the main review

1. Read the scope, decisions, required checks, assumptions, and exclusions.
2. Inspect the proposed `CONTEXT.md` and decision-record wording.
3. Select **Accept this wording**. This accepts a demo record; no file is written.
4. Select the checkbox that names the exact plan revision.
5. Select **Start implementation**. The page creates a simulated run with a copied plan.
6. Select **Repeat same request**. The run ID stays the same.
7. Select **Edit plan after handoff**. The new revision needs review. The run's approved
   copy stays unchanged.

In B, document wording is section 4 and approval is section 5.
In C, document wording is in the plan; approval is in the adjacent panel.

## Other cases

- **Changed earlier decision:** revisit plan contents; select glossary-only and submit.
  Round 2 changes from a decision-record question to a glossary question. Later choices,
  document wording, and approval need review. Old submitted answers remain in history.
- **Prepared answers:** select options without submitting. They remain marked as drafts.
  They do not replace the submitted choices. Prepared changes block approval.
- **Discussion:** open Discuss on a question and send text. The reply is a preset example.
  It does not submit an answer.
- **Lost start response:** expand **Prototype controls & full state**, select **Lose next
  start response**, and start a newly approved revision. **Check saved request** recovers
  the simulated run instead of creating another one.
- **Connection loss:** select **Disconnect**. Drafts remain, and submission is disabled.
  **Reconnect / recover** returns to the existing in-memory state.
- **Same session from another client:** change **View from**. This is an illustrative label
  over the same in-memory record, not a real BB or Codex connection.
- **Fresh interview:** select **Reset example**. Recommendations are not selected for you.
- **Ready plan:** select **Load ready plan** to skip the sample interview and document
  acceptance. Explicit implementation approval is still required.
- **Run progress:** advance the demo run through validation, build, test, review, and a
  sample report. No real checks or pull request are produced.

The full relevant state is rendered under the prototype controls after each action.
The example covers only Grill. Wayfinder map editing remains outside this experiment.

## Verdict and limits

**Working recommendation: B for a deliberate review before implementation.** Its five
sections make scope, decisions, checks, documents, and final approval individually visible.
C is the stronger alternative when reviewing the actual wording is the main activity.
A offers more simultaneous context during the interview.

**No winning layout has been selected by the user.** This is a recommendation for the
comparison, not a validated product decision. No layout is promoted into production, and
no production controller, delivery policy, or main-branch implementation is changed.

The exercise makes the separate document-acceptance and plan-approval actions concrete.
It does not establish durable storage, restart recovery, concurrent-client behavior,
exactly-once worker execution, stale-command rejection, or a live LangGraph handoff.
Those remain implementation and verification work under issue 10 and its prerequisite.
All answers, revisions, document contents, run IDs, and run results are sample data.

## Verification

Browser checks use the actual local page and simulated records. No test suite was added,
as required by the prototype skill.

- JavaScript syntax check and Python runner compilation.
- Mechanical UI scan: no findings.
- Desktop layouts and narrow-window rendering.
- Document acceptance does not itself authorize implementation.
- Explicit approval creates one simulated run; repeating the request returns its ID.
- A lost response can be reconciled to the same simulated run.
- Later plan edits preserve the original run snapshot and block unresolved handoff.
- Changed plan contents produce the alternate glossary question in the next round.
- Layout and simulated-client changes retain the current tab's answers.

These are prototype checks, not completion evidence for the real feature.

## Sources

- [Issue 10: shared planning interface and handoff](https://github.com/pessini/software-factory/issues/10)
- [Issue 9: shared controller prerequisite](https://github.com/pessini/software-factory/issues/9)

The implementation is original prototype code. No source from the reference project was
copied. The issue's requirements informed the question cards, grouped submission, document
review, and handoff states.
