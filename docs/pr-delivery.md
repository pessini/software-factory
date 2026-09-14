# Verified PR delivery — issue #7

The default remains a local `ready_for_review` handoff. To authorize publication,
save this reviewed policy in `.factory/factory.yaml` before starting a run:

```yaml
delivery:
  mode: pull_request
  repository: owner/repository
  base_branch: main
  work_type: feat
  issue: 7
  title: Handle empty input
  review_fixes: true
```

`pull_request` authorizes the controller to commit and push the verified changes
and create a regular PR. `review_fixes` also authorizes publication, replies,
thread resolution, and a summary for the review findings the operator selects.
These choices are saved for the run. Restart does not read new authorization from
the workspace policy. Agents still cannot commit, push, or change this policy.
The older `safety.auto_commit` and `safety.allow_push` fields remain false; they
do not grant agent permissions. Only the controller's delivery policy grants
publication permission. Merge and deployment remain outside the workflow.

For a completed manual run, pass a reviewed JSON file with the same delivery
fields to `software-factory deliver RUN_ID --policy delivery.json`. This saves
authorization for the actual verified result. Put that policy file outside the
target repository so it does not change the verified content. Subsequent calls
need only `software-factory deliver RUN_ID`. A saved publication destination
cannot be replaced by a later policy file.
An explicit later policy can add `review_fixes: true` when every other field
still matches. The existing publication authorization remains saved.

Use an authenticated GitHub CLI (`gh`) with access to the target repository and
a configured Git author. The initial checkout must start at the selected remote
base commit. If that base has moved before delivery begins, verify the updated
base in a new run. The first implementation supports GitHub.com repositories;
fork PRs and other GitHub hosts are not supported.

## What delivery verifies

The controller checks the saved handoff, reviewed inputs, files, index, and Git
references. It creates a separate temporary index from the verified bytes and
checks every resulting Git blob and file mode against those bytes. It does not
run hooks or content filters. Submodules and checkout transformations whose bytes
differ from the verified tree are rejected.

The controller saves the commit identity before changing the checkout. It creates
an issue and work-type branch, then updates the checkout and index to the same
files. File contents are not rewritten. A restart accepts only the recorded
before/after metadata states; unexpected user changes are preserved and stop
delivery. A verified no-change result does not create an empty PR.

Pushes use an explicit destination and an expected previous remote commit.
Review-fix pushes must also be forward changes from that commit. PRs are regular,
never drafts. Linked issues receive `Closes issue #NUMBER` in the body. The
controller can infer one issue from a GitHub issue URL in the original request
when it belongs to the configured repository; otherwise set `delivery.issue`.

Each remote operation is saved in the existing operation table before execution.
Recovery reads the remote branch, PR, comments, and thread state before doing
more work. PR and comment bodies have a saved operation marker. If a creation
request has an uncertain outcome and no matching object is visible, delivery
stops at `recovery_needed` rather than blindly repeating it. Inspect the operation
and GitHub result before further action. An absent branch push can repeat with
the same expected remote commit; thread resolution can repeat after checking
the current thread and commit.

## CI and results

The run's implementation status remains separate from `delivery.status`:

| Delivery status | Meaning |
| --- | --- |
| `manual` | Verified local handoff; publication is not authorized |
| `no_change` | The request was already satisfied; no PR needed |
| `delivered` | PR exists and all reported checks/statuses passed for its saved commit |
| `ci_pending` | At least one reported check/status is pending |
| `ci_failed` | At least one reported check/status failed |
| `ci_unavailable` | No checks/statuses are available; success is not assumed |
| `blocked` | Content, required evidence, or remote state does not match |
| `recovery_needed` | A remote result could not be confirmed |
| `fixes_incomplete` | The saved fix run needs input, recovery, or a correction |
| `stopping` / `cancelled` | Publication cancellation requested / observed |

CI includes all returned check-run pages and the latest legacy status for each
context. This reports GitHub's available checks; it does not impose a branch
protection policy or merge the PR. `deliver` refreshes CI for the exact saved
commit. The CLI returns a nonzero status for failed, pending, unavailable, or
incomplete delivery. The final report includes the PR, commit, CI identity, and
any error. Cancellation stops later mutations and controlled commands; completed
remote operations remain on GitHub. Use `resume` to inspect an interrupted stop.

## Selected review fixes

```text
software-factory review-threads RUN_ID
software-factory fix-reviews RUN_ID --thread THREAD_ID --thread ANOTHER_THREAD_ID
```

Select unresolved threads that GitHub still marks as current. Findings can have
been opened on an earlier PR commit; their original context remains saved and
the fix run must verify them against the current delivered content. The controller
saves their complete discussion and the original verified context. It starts one
ordinary saved workflow run for the batch with the same model settings, review
mode, commands, limits, and file protection. Each selected finding becomes a
required completion criterion. Existing product criteria remain required.

After checks and review prove those criteria, the controller commits the new
verified files, pushes to the existing PR, replies with the commit and evidence,
resolves the applied threads, and posts one summary. Unselected findings remain
open. A new discussion message or changed PR head stops replies/resolution until
the changed context is reviewed. Required local checks and review must pass;
remote CI failures remain visible and do not become a successful delivery result.

Restart uses the same fix run and saved remote operations. `resume RUN_ID` also
continues its incomplete fix batch. A blocking question belongs to the fix run
shown in `delivery.review_fix.run_id`; answer that run, then continue the batch.
Earlier implementation and fix evidence remain available. A report identifies
which saved run verified the PR's current commit.

## Test boundary

Integration tests use the public run/deliver/fix interfaces, real local Git
repositories, and a controlled GitHub adapter. Separate tests exercise the real
GitHub adapter through a controlled `gh` executable, including pagination and
literal multiline bodies. These checks do not publish to a real repository or
claim live-provider acceptance of PR delivery.

API references: [pull requests](https://docs.github.com/en/rest/pulls/pulls),
[check runs](https://docs.github.com/en/rest/checks/runs), and
[review comments](https://docs.github.com/en/rest/pulls/comments).
