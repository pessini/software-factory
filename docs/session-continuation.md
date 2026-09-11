# Saved sessions and blocking questions

Issue #5 uses the existing ACP session and FactoryEngine interfaces. The run
store holds conversation updates, run identities, questions, and answers beside
the workflow checkpoints. Loading a session replays saved ACP updates before the
load response. It does not start or resume a workflow.

## Commands

- The first work request starts a run.
- `/status` (or `status`) shows the current run and any unanswered question.
- `/continue` (or `continue`) resumes the current unfinished run. A completed run
  returns its saved result.
- `/retry` retries recovery of a run in `recovery_needed`. It retains saved
  operations and failed-attempt history. Final failures cannot be retried.
- `/new REQUEST` explicitly starts another run after the current run ends. The
  new run must acquire workspace ownership and pass the existing repository checks.
- `/answer INTERACTION_ID ANSWER` answers a single question. For multiple
  questions, ANSWER is a JSON object that maps each question ID to its answer.
- `/cancel` stops the current run, including a run waiting for an answer.

Other messages in an existing session do not start implementation or answer a
question. The session shows the available commands. An answer must name its
interaction. Repeating the same answer is harmless; changing a saved answer is
rejected. A cancellation prevents later answers from starting work.

## Time limits

`total_run_timeout_seconds` limits work time, including recovery time. A saved
blocking question pauses the remaining run and operation time until its answer
is accepted. Restart does not reset either allowance.

`overall_run_timeout_seconds` limits elapsed time from run creation, including
time waiting for the user. It defaults to seven days. Expiry is enforced when
the controller next resumes or accepts input, and while a live worker waits.
There is no background controller after the application exits.

## Compatibility

Older session records can load. Where no conversation was saved, the session
shows available run events and the saved result. Runs without execution and
ownership records remain available for inspection and cannot resume safely.

ACP plan entries only support pending, in progress, and completed. Titles show
waiting, failed, cancelled, and unused states explicitly. Tool calls use unique
event identities and retain the matching identity for completion updates.

The protected worker answers the pending provider request on its existing
connection. It checks the provider thread, turn, and request identities. It saves
delivery intent before sending the answer. A lost worker connection is a recovery
case; it does not authorize another provider turn or a repeated response. The
response format follows the installed provider schema and the
[App Server input protocol](https://learn.chatgpt.com/docs/app-server#toolrequestuserinput).

CLI examples:

```sh
software-factory inspect RUN_ID
software-factory answer RUN_ID INTERACTION_ID '{"question_id":"the answer"}'
software-factory resume RUN_ID
software-factory retry RUN_ID
software-factory cancel RUN_ID
```
