# Issue 10: plan review prototype

**Throwaway code. Keep this directory on `feat/10-planning-ui-prototype`, out of main.**

Question: **Which layout helps a person review the exact plan before starting implementation?**

The user selected plan review as the main purpose, then asked for a clearer end-to-end
flow. The page now opens at the first Grill question for one concrete shared-session
request. It shows the selected focused-feature path, the later Wayfinder path, adaptive
question rounds, finishing the Grill, reviewing documents and scope, explicit approval,
and the simulated LangGraph run. The three layouts still compare how to review the plan.

## Run

From the repository root:

```sh
uv run --no-project python web/planning-prototype/serve.py
```

Open <http://127.0.0.1:4310/?variant=A>. Stop with `Ctrl+C`.
Use `--port 4311` if the port is occupied. No build, package install, model credentials,
or database are needed. Fonts and icons are bundled with their licenses.
The server binds only to `127.0.0.1` and refuses to run when `NODE_ENV=production`.

## Follow the example from the beginning

The sample request starts in Codex, continues in the browser and BB using session
`planning-010-demo`, then asks for approval before implementation starts.

The overview first shows the request, target workspace, shared session, client path, and
chosen approach. The plan is still provisional there; the Grill settles its decisions,
and final approval stays after document review.

1. In **Grill questions**, answer the two independent Round 1 cards and submit them.
   Draft choices do not count as decisions until submission.
2. Round 2 appears after the example agent response. Its questions depend on the Round 1
   choices. Submit those answers to settle the decision tree.
3. Select **Finish Grill · prepare plan**. This prepares the final document wording and
   opens **Plan & handoff**; it does not start implementation.
4. Review the scope, decisions, checks, and exact `CONTEXT.md` / decision-record wording.
5. Select **Approve plan & start implementation**. That explicit action approves the
   displayed plan and document revisions and saves one simulated LangGraph run. Use
   **Advance demo run** to see Validate → Build → Test → Review → Report.
6. Repeat the same request to see the same run ID. Edit the plan after handoff to see that
   the linked run keeps its approved snapshot.

The overview also shows the larger-effort path: Wayfinder starts from a named destination
and maps dependent decisions. A ticket discussion opens the same Grill. The installed
Wayfinder skill then hands a clear map to **to-spec → to-tickets → implement**. Issue 10
describes a shared saved handoff for an approved scope in a later UI milestone; that bridge
needs to line up with the current skill sequence. Wayfinder is not a working map editor in
this prototype.

| Layout | URL | Content order | Best use |
| --- | --- | --- | --- |
| A: Brief | `/?variant=A` | Outcome, pending files, expandable decisions, checks; approval beside the plan | Understand the whole plan and find the next action |
| B: Review desk | `/?variant=B` | Section navigation, focused reader, approval and context | Review each part in detail without a long document |
| C: Changes | `/?variant=C` | File navigation, proposed wording, reasons, review context; approval below | Inspect the exact document changes |

The bottom bar switches plan-review layouts. Left and right arrow keys also work when
focus is outside a form field. Switching preserves this tab's sample state and updates the
URL. Reloading resets to the first Grill round. Sources and demo controls open in side
panels. **Ready plan** skips the interview and loads final sample wording for comparing
the review layouts directly.
Use **Appearance and demo settings** to choose system, light, or dark appearance.

## Try the review

1. Follow the two question rounds, or load **Ready plan** from demo controls.
2. Read the scope, submitted decisions, checks, and constraints.
3. Inspect the proposed `CONTEXT.md` and decision-record wording. In A, open a file or
   select **Review wording**. In B, select **Document wording**. In C, use the file list
   and **Read / Changes** controls.
4. Read each proposed file and check its plan revision.
5. Select **Approve plan & start implementation**. This approves the displayed revisions
   and creates a simulated run with an immutable copy.
6. Select **Repeat same request**. The run ID stays the same.
7. Select **Edit plan after handoff**. The new revision needs review; the run's copy stays unchanged.

## Other cases

Open **Demo controls** in the bottom bar, or **Appearance and demo settings** in the header.
The panel also contains **Full prototype state**, updated after each action.

- **Changed answer:** expand an agreed decision and select **Revisit decision**. Prepared
  answers remain separate until submission. An earlier change can require later answers
  and document wording to be reviewed again.
- **Discussion:** select **Discuss** on a question and write a note. It is sent with your
  staged answers; the reply is a preset example and does not change your answer.
- **Lost response:** select **Lose next start response**, then approve and start a new
  request. **Check saved request** recovers the same simulated run.
- **Connection loss:** **Disconnect** disables submission. **Reconnect / recover** keeps
  existing answers and drafts in this tab.
- **Client view:** select Browser, BB, or Codex. These are labels on one sample session,
  not live connections.
- **Fresh interview:** clears the sample answers. Recommendations do not preselect answers.
- **Ready plan:** loads final sample wording, but still requires explicit approval.
- **Run progress:** **Advance demo run** shows sample stages and a sample report.

## Working recommendation and limits

**Start with A: Brief.** It shows what will be built, what still needs review, and the
next required action. B is useful for a longer plan; C is useful when exact wording
is the main decision. This recommendation replaces the earlier five-step Focus study.

**The user has not selected a winner.** No layout is promoted into production. All answers,
revisions, documents, run IDs, and results are sample data. Wayfinder editing is outside
this prototype. Durable storage, service restart, concurrent clients, and a real worker
handoff still need implementation under issue 10 and its prerequisite, issue 9.

The content audit, visual choices, and browser checks are in [DESIGN-NOTES.md](DESIGN-NOTES.md).
No automated test suite was added to this throwaway prototype.

## Sources

- [Issue 10: shared planning interface and handoff](https://github.com/pessini/software-factory/issues/10)
- [Issue 9: shared controller prerequisite](https://github.com/pessini/software-factory/issues/9)
- [Grill with UI reference project](https://github.com/jasonku09/grill-with-ui/tree/d6dbc7585315a3224ac92b9ba8d93d7b93db227f), whose staged answers, adaptive rounds, and explicit finish/send actions inform the flow.
- [Bundled font and icon sources](assets/sources.json)

The original sample behavior is retained. Geist and Phosphor assets are redistributed
with their licenses in `assets/`. No external font or icon request is made at runtime.
