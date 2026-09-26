# Issue 10: plan review prototype

**Throwaway code. Keep this directory on `feat/10-planning-ui-prototype`, out of main.**

Question: **Which layout helps a person review the exact plan before starting implementation?**

The user selected plan review as the main purpose, then asked for more beautiful and
organized layouts and a new arrangement of the content. The page opens with four sample
answers submitted and document wording waiting for acceptance. The interview remains
available to explore changed decisions.

## Run

From the repository root:

```sh
uv run --no-project python web/planning-prototype/serve.py
```

Open <http://127.0.0.1:4310/?variant=A>. Stop with `Ctrl+C`.
Use `--port 4311` if the port is occupied. No build, package install, model credentials,
or database are needed. Fonts and icons are bundled with their licenses.
The server binds only to `127.0.0.1` and refuses to run when `NODE_ENV=production`.

| Layout | URL | Content order | Best use |
| --- | --- | --- | --- |
| A: Brief | `/?variant=A` | Outcome, pending files, expandable decisions, checks; approval beside the plan | Understand the whole plan and find the next action |
| B: Review desk | `/?variant=B` | Section navigation, focused reader, approval and context | Review each part in detail without a long document |
| C: Changes | `/?variant=C` | File navigation, proposed wording, reasons, review context; approval below | Inspect the exact document changes |

The bottom bar switches layouts. Left and right arrow keys also work when focus is
outside a form field. Switching preserves this tab's sample state and updates the URL.
Reloading resets the sample session. Sources and demo controls open in side panels.
Use **Appearance and demo settings** to choose system, light, or dark appearance.

## Try the review

1. Read the scope, submitted decisions, checks, and constraints.
2. Inspect the proposed `CONTEXT.md` and decision-record wording. In A, open a file or
   select **Review wording**. In B, select **Document wording**. In C, use the file list
   and **Read / Changes** controls.
3. Select **Accept wording**. Acceptance covers the complete displayed file set. No file is written.
4. Select the checkbox that names the exact plan and document revision.
5. Select **Start implementation**. A simulated run stores a copy of the plan.
6. Select **Repeat same request**. The run ID stays the same.
7. Select **Edit plan after handoff**. The new revision needs review; the run's copy stays unchanged.

## Other cases

Open **Demo controls** in the bottom bar, or **Appearance and demo settings** in the header.
The panel also contains **Full prototype state**, updated after each action.

- **Changed answer:** expand an agreed decision and select **Revisit decision**. Prepared
  answers remain separate until submission. An earlier change can require later answers
  and document wording to be reviewed again.
- **Discussion:** select **Discuss** on a question. The reply is a preset example;
  it does not submit an answer.
- **Lost response:** select **Lose next start response**, then approve and start a new
  request. **Check saved request** recovers the same simulated run.
- **Connection loss:** **Disconnect** disables submission. **Reconnect / recover** keeps
  existing answers and drafts in this tab.
- **Client view:** select Browser, BB, or Codex. These are labels on one sample session,
  not live connections.
- **Fresh interview:** clears the sample answers. Recommendations do not preselect answers.
- **Ready plan:** loads accepted sample wording, but still requires explicit approval.
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
- [Bundled font and icon sources](assets/sources.json)

The original sample behavior is retained. Geist and Phosphor assets are redistributed
with their licenses in `assets/`. No external font or icon request is made at runtime.
