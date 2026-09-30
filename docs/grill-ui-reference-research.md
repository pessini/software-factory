# Grill interface reference research

Checked on 2026-09-18. Reference: `jasonku09/grill-with-ui`, commit
`d6dbc7585315a3224ac92b9ba8d93d7b93db227f`. This note separates source facts from proposed
design choices. It does not assess the full Wayfinder workflow.

## What the reference provides

The project is a skill folder with a Node 20+ server and one HTML page. It has no runtime
package dependencies or build step. The README explicitly includes Codex CLI and IDE usage,
in addition to Claude Code and other agents. It requires shell command access. The visible
interface contains question cards, recommendations, discussions, deferred questions, and a
button that submits several prepared answers together. It can also show an optional
generated prototype or diagram.
[README](https://github.com/jasonku09/grill-with-ui/blob/d6dbc7585315a3224ac92b9ba8d93d7b93db227f/README.md)

The browser keeps prepared answers and drafts in `localStorage`. It reads `/state` once per
second. Pressing Send posts an action list to `/send`. Each submission stays marked as
pending until the agent records its sequence number in `agent.handled`. The visual appears
in an iframe with `sandbox="allow-scripts"`; a version change reloads it. The page includes
recovery messages when the server disappears or the agent takes too long.
[Page source](https://github.com/jasonku09/grill-with-ui/blob/d6dbc7585315a3224ac92b9ba8d93d7b93db227f/page.html#L170-L312)

## How information moves

1. The agent writes questions and replies to `state.json`.
2. The server supplies that state to the browser.
3. The browser submits actions; the server appends a numbered line to `events.jsonl` and
   prints the same line to standard output.
4. An agent-side listener reads the submission. The agent updates the state and records the
   last handled sequence number.

The server has no model connection. Its routes serve the page, state, events, and visual, or
accept a submission. It binds to `127.0.0.1`. Session files live under
`~/.grill-with-ui/sessions/` by default. The repository's common Git directory determines
the project identity, so related worktrees share the same session group. `pending` replays
events after the last handled sequence number. The server retries its saved port on
restart and can choose another free port.
[Server source](https://github.com/jasonku09/grill-with-ui/blob/d6dbc7585315a3224ac92b9ba8d93d7b93db227f/server.mjs#L52-L189)

## Codex and process lifetime

The reference's skill instructions use Claude's persistent Monitor when available. For
Codex and other agents without that tool, they start a detached server and run `wait` in
the foreground. That command checks the event file every 250 ms; its default timeout is
480 seconds. It returns a submission or a timeout result, after which the agent must
continue the loop. Finishing includes an explicit step to stop the server by process ID.
[Wait instructions](https://github.com/jasonku09/grill-with-ui/blob/d6dbc7585315a3224ac92b9ba8d93d7b93db227f/SKILL.md#L253-L261),
[Wait implementation](https://github.com/jasonku09/grill-with-ui/blob/d6dbc7585315a3224ac92b9ba8d93d7b93db227f/server.mjs#L191-L205)

**Design implication:** this is suitable for a live agent conversation, but the browser
server does not itself start a new Codex turn. A reliable continuation after an agent stops
would need another process that can resume the agent through an available supported
interface. Do not equate a saved browser submission with a successful agent continuation.

**Lifecycle limitation:** the README says that nothing runs after the agent session ends,
but the detached-server path relies on explicit cleanup. The server source handles stop
signals and does not contain a parent-process lifetime check. An interrupted conversation
can therefore leave the server running. This is a source-code finding, not a test result.
[Server shutdown](https://github.com/jasonku09/grill-with-ui/blob/d6dbc7585315a3224ac92b9ba8d93d7b93db227f/server.mjs#L184-L189),
[Finish instructions](https://github.com/jasonku09/grill-with-ui/blob/d6dbc7585315a3224ac92b9ba8d93d7b93db227f/SKILL.md#L228-L261)

## Document support and reuse

The skill asks the agent to read relevant code or documents before asking questions that
those sources can answer. It produces a Markdown design document at completion. Its
question schema includes options, recommendations, dependencies, answers, and discussions.
It has no dedicated fields for source citations, document sections, document revisions, or
proposed document edits. There is no document upload or document-edit route in the server.
Thus it supplies an interview interface, rather than a full document review interface.
[Interview method](https://github.com/jasonku09/grill-with-ui/blob/d6dbc7585315a3224ac92b9ba8d93d7b93db227f/SKILL.md#L113-L131),
[Output and schema](https://github.com/jasonku09/grill-with-ui/blob/d6dbc7585315a3224ac92b9ba8d93d7b93db227f/SKILL.md#L228-L304),
[Server routes](https://github.com/jasonku09/grill-with-ui/blob/d6dbc7585315a3224ac92b9ba8d93d7b93db227f/server.mjs#L144-L170)

The project uses the MIT license. Copying or adapting substantial parts requires keeping
the copyright and permission notice. The license names Jason Ku, copyright 2026.
[License](https://github.com/jasonku09/grill-with-ui/blob/d6dbc7585315a3224ac92b9ba8d93d7b93db227f/LICENSE)

## Proposed fit for this harness

These are design proposals, not features supplied by the reference:

- Reuse the question cards, recommendations, per-question discussions, prepared answers,
  explicit Send action, and saved session history for the first grill interface.
- Keep the browser and its local service independent of BB, Codex, or LangGraph. A small
  connection module can pass submitted actions to whichever agent runs the session.
- Give the service clear commands for publishing questions, reading answers, and completing
  a session. Validate submissions and track their sequence numbers in code. This reduces
  dependence on an agent rewriting the entire state file correctly.
- For `grill-me-with-docs`, extend each question with source file, section, revision, and
  short supporting text. Show the related document beside the question. Keep accepted
  decisions separate from proposed document changes until the workflow applies them.
- Let a later Wayfinder interface reuse session identity, submissions, history, status,
  and document references. Give its stages their own pages and data structures after its
  actual skills have been reviewed. Do not force every workflow into question cards.
- Start with an active-session wait mode if a small experiment is enough. Before adding
  unattended continuation, define which process resumes the agent and which process owns
  start, stop, recovery, and cleanup.

The existing reference is a useful starting point for a local, single-user application.
Its HTTP handlers have no authentication or origin checks and only minimal submission
validation. Keep it local for an experiment; review those handlers before reusing them for
a remotely accessible or shared service.
[HTTP implementation](https://github.com/jasonku09/grill-with-ui/blob/d6dbc7585315a3224ac92b9ba8d93d7b93db227f/server.mjs#L140-L184)

## Research scope

Read the pinned README, skill instructions, server, page, and license through GitHub's
read-only API. Firecrawl was installed and authenticated, but its status request could not
reach the service from the restricted shell. GitHub API reads succeeded with network
permission. No packages were installed, no upstream code was executed, and no browser or
agent lifecycle tests were run. Claims about Codex compatibility above describe the
reference's documented adapter, not an independent end-to-end compatibility test.
