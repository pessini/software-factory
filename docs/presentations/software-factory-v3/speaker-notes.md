# Speaker notes · Agentic Software Factory

Developer team presentation. Suggested duration: 25–30 minutes, including discussion.

## 01. What personal project are you working on that involves AI?

Invite two or three people to respond. Ask each person to name a project and the problem it solves. Introduce Agentic Software Factory later. Suggested time: 60 to 90 seconds.

## 02. Challenge: Tell me about an AI trend I don't know about

Listen to examples from the team. Where relevant, connect them to the move from individual prompts to processes with tools, memory, evaluation, and human participation. Use this as a transition, not a statistical forecast.

## 03. How do you see the role of future professionals on AI teams?

Collect responses before moving on. Use the team’s words to connect the professional role to process design: ask useful questions, define criteria, review evidence, and decide when to proceed.

## 04. Workflow: from request to execution

Start with the development workflow. During planning, a scoped request goes through Grill; a larger effort can use Wayfinder to organize decisions into tickets. The team reviews scope, criteria, and documents before approving a version. This path is simulated in the prototype. Passing the approved plan to the controller still needs implementation. The existing harness runs bootstrap, baseline, Planner, Builder, tests, review, and finalization. Test failures trigger repair and another test run. Blocking review findings trigger implementation changes, testing, and another review. The output is ready_for_review. This overview introduces the flow; later slides explain the methods and execution graph. Distinguish the Planner’s internal technical planning from the Grill session before approval.

## 05. Grilling: decision rounds with dependencies

The skill organizes the conversation as a decision tree. In the original skill, the model identifies the frontier through judgment, without a deterministic dependency engine. The frontier contains questions that can be asked without assuming pending answers. Ask the whole frontier in one round and wait for responses. Dependent questions come later. When an environmental fact is missing, the skill directs the agent to delegate investigation to a subagent; only branches that depend on that fact wait. The session ends when no open decisions or silent assumptions remain and the person confirms shared understanding. Finishing the interview does not start implementation.

## 06. How an answer unlocks the next round

Explain the example from left to right. We can discuss recovery after deciding who owns the session. We can define the handoff snapshot after deciding when approval happens. If an earlier answer changes, dependent decisions need another review. This preserves the skill’s adaptive behavior rather than turning it into a fixed form.

## 07. Prototype: questions, answers, and discussion

Show how the prototype supports rounds, recommendations, and discussion. The agent and its responses are simulated. The browser is not connected to the controller. Browser, BB, and Codex are labels for views of a sample session, not live connections. This page does not generate arbitrary new rounds through a model. The screenshot was captured for this presentation.

## 08. Plan review and version approval

In the prototype, Finish Grill prepares the plan. Implementation starts only after Approve plan & start implementation. The approved snapshot stays immutable even if the plan changes later. Repeating the same request returns the same simulated run. The study compares three ways to review the same content. In the local version, domain-modeling maintains terminology in CONTEXT.md and records relevant decisions in ADRs. These files are proposed and simulated in the interface.

## 09. Wayfinder: decisions and dependencies

Wayfinder, also by Matt Pocock, supports work larger than a single session. First name the destination. Then explore decisions broadly, create a map issue, and add child tickets with precise questions. Each ticket represents a decision or investigation, not a slice of implementation. The map is an index containing the destination, notes, decisions so far, areas not yet specified, and out-of-scope work. Its frontier contains open, unblocked, unclaimed tickets. A precise question can have a ticket even while blocked. Questions that cannot yet be formulated stay in the fog. This diagram explains the method; it is not a screenshot of an implemented feature.

## 10. Ticket types and how they are resolved

Research invokes the research skill. Prototype requires human participation to evaluate the artifact. Grilling invokes grilling and domain-modeling. Task completes a prerequisite for a decision. Before starting, a session claims the ticket so other sessions do not pick it. On resolution, record the answer in a comment, close the ticket, and add a summary with a link to the map. New tickets emerge as the fog becomes precise. Normally, a session resolves one ticket; research is the exception. The issue tracker is authoritative, with local Markdown as a fallback. Native tracker dependencies make the frontier visible.

## 11. Planning workflows: Grill and Wayfinder

Wayfinder plans by default. The map is complete when the route is clear and no decisions remain before reaching the destination. Only explicit effort notes can override that rule. Local research describes the next steps as to-spec, to-tickets, and implement. This sequence is planning context recorded in the project, not an automatic call from the Wayfinder skill. The harness integration must preserve the approved scope and exact documents. A Wayfinder decision ticket could open a Grill session in a future integration.

## 12. Controller, interfaces, and workers

Interfaces send commands and display records. The controller owns the run ID, workers, progress, and limits. LangGraph determines transitions. Python validates data and runs checks. Native workers use the Codex connection, with container protection in the verified macOS setup. Closing an interface does not cancel a run. Persistent state supports recovery after a service restart under reconciliation rules. MCP is the tool connection, not a model or web interface. CLI and BB were verified with real models. Full Codex desktop integration remains future work.

## 13. Execution workflow and correction loops

The actual graph starts with bootstrap and baseline. It then runs Planner, Builder, tests, review, and finalization. Repairs and revisions return to testing. Single mode preserves the single Reviewer in older configurations; focused mode uses Request, Code, and Security with a read-only Judge. The opt-in risk_based mode can include Documentation Review. When a budget is exhausted or a terminal problem occurs, execution ends in failure while preserving records. Finalization checks the exact version covered by the evidence. ready_for_review is not human approval. An optional delivery policy can authorize commit, push, PR creation, and CI tracking. Merge and deployment remain outside the workflow.

## 14. Persistence, recovery, and validation

Recovery does not simply resend the same prompt. The controller checks spawn intents, operations, and existing results. If the situation is uncertain, it requires recovery. Quality commands come from reviewed argv arrays in configuration, not commands invented by a model. One writer is allowed per workspace. Cancellation is confirmed only after the corresponding workers exit. These properties make the process inspectable and limit the effects of failures. Do not describe them as a guarantee of correct code or absolute security.

## 15. Pending integration: planning and execution

This is the pending integration work. Planning already has a demonstrable but simulated experience. The harness already makes real changes. The dashed line marks the integration still to build. The browser should send answers and approvals to the controller, which validates, saves, and schedules the next step. Tracker ticket identity stays distinct from session and run identity. Execution should receive the approved scope and should not start automatically when the interview finishes.

## 16. Current implementation status

Present implementation status and test evidence separately for each part. CLI and BB passed real workflows. The MCP server exists and inspected a real run, but cross-client BB/MCP continuation was tested with simulated workers. Codex desktop integration is deferred. The prototype resets on page reload and does not launch real workers. Wayfinder has no functional map editor or browser tracker integration yet. Router and external providers are also deferred. September evidence is historical, not a new certification performed for these slides.

## 17. Work areas for the team

Discuss how to divide work across development, product, and quality. Contributions can start without code: run the prototype, compare layouts, report confusion, or bring a scoped real task. For engineering, the next piece is persistent planning sessions and a real handoff. For quality, prioritize connection failures, duplicate submissions, and stale revisions. A later step is Wayfinder linked to the tracker. These are proposed work areas, not a roadmap with promised dates.

## 18. Contribute to Agentic Software Factory

Close with a discussion of the team’s work. Choose a small use case, run the prototype, record problems, and propose a change. For implementation, the next integration is saving planning rounds and versions in the controller and starting a run from the approved plan. Use issue 10 to organize the discussion. Connect the invitation to contribute with a concrete first task.

## 19. References and evidence

These sources support the presentation. Matt Pocock’s references are pinned to a commit for review. Project URLs point to the prototype branch because it remains an experiment outside main. The decision tree and map are illustrative examples. The interface screenshots show the actual local prototype captured during preparation. This presentation does not publish issues, start Grilling or Wayfinder sessions, or run model workers.

