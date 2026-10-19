# Codex Router: capability and limitation research

Research date: 2026-09-18. Source snapshot: [`5e1b49e6e3fea1ada57f0e7fa6ca7612e9d17300`](https://github.com/duolahypercho/codex-router/tree/5e1b49e6e3fea1ada57f0e7fa6ca7612e9d17300), package version 0.6.0. Source was downloaded and read without installing or executing the router. No provider requests were sent. Findings describe the source and its authors' claims, not an independently verified working installation.

The supplied [website](https://codex-router.com/) identifies itself as community documentation adapted from the repository. It is not OpenAI documentation. The repository is the stronger source for implementation details. The snapshot's [package manifest](https://github.com/duolahypercho/codex-router/blob/5e1b49e6e3fea1ada57f0e7fa6ca7612e9d17300/package.json) supplies the version; the [Python dependency lock](https://github.com/duolahypercho/codex-router/blob/5e1b49e6e3fea1ada57f0e7fa6ca7612e9d17300/requirements/python.txt) pins LiteLLM 1.96.0.

## What it changes

Codex Router adds external models to a generated Codex model catalog and routes model requests to them. Some routes use LiteLLM to translate Responses requests into provider protocols. Codex still owns commands, permissions, MCP tools, skills, and task state. Native GPT traffic can keep its normal backend. Therefore, this router does not itself make LangGraph the workflow controller or turn Codex into a passive user interface. [Architecture](https://github.com/duolahypercho/codex-router/blob/5e1b49e6e3fea1ada57f0e7fa6ca7612e9d17300/docs/HOW-IT-WORKS.md).

**Design conclusion:** It can sit below a Codex worker as an optional model transport. A separate LangGraph service can still own job state and business decisions. Connecting BB and Codex to that same service requires application adapters; model routing alone does not supply them.

## Limitations that matter for a durable workflow

| Area | Verified source behavior | Consequence for this project |
| --- | --- | --- |
| Tool calls | Namespace tools are flattened for some routes and restored on the way back. Custom tools and tool schemas receive provider-specific changes. The router relays calls; the client executes them. [Request translation](https://github.com/duolahypercho/codex-router/blob/5e1b49e6e3fea1ada57f0e7fa6ca7612e9d17300/src/router.mjs#L3460) | A model working for plain text is insufficient evidence. Test file edits, tool results, and follow-up turns on each selected route. |
| Schema differences | Moonshot routes require special handling of references, union types, and recursive definitions. [Moonshot route rules](https://github.com/duolahypercho/codex-router/blob/5e1b49e6e3fea1ada57f0e7fa6ca7612e9d17300/src/moonshot-schema-routes.mjs) | The same tool schema can behave differently across providers. |
| Structured final output | An adapter converts `response_format` JSON modes into Responses `text.format`, including schema fields. [Format conversion](https://github.com/duolahypercho/codex-router/blob/5e1b49e6e3fea1ada57f0e7fa6ca7612e9d17300/src/openai-adapters.mjs#L209) | This proves format translation, not strict output enforcement on every route. Keep local result validation and test the factory's exact schema. |
| Reasoning history | Model-family and provider rules decide how prior reasoning is carried through later calls. [Reasoning rules](https://github.com/duolahypercho/codex-router/blob/5e1b49e6e3fea1ada57f0e7fa6ca7612e9d17300/src/chat-reasoning.mjs) | Identical effort labels do not establish identical behavior. Include several tool turns in tests. |
| Provider-specific settings | The router removes reasoning fields for keyless routes and applies other route-specific tool and request repairs. [Request normalization](https://github.com/duolahypercho/codex-router/blob/5e1b49e6e3fea1ada57f0e7fa6ca7612e9d17300/src/router.mjs#L3648) | A universal model configuration is not enough; record the exact provider/model route and settings. |
| Vision | The vision bridge is enabled by default. It can ask another configured model to read an image and substitute text. [Default settings](https://github.com/duolahypercho/codex-router/blob/5e1b49e6e3fea1ada57f0e7fa6ca7612e9d17300/src/vision-bridge-state.mjs) | The selected text model may not receive the image. Record which reader ran and include its cost and failure state. |
| Automatic model changes | Quota-related failover is enabled by default and can choose another enabled route. [Failover state and selection](https://github.com/duolahypercho/codex-router/blob/5e1b49e6e3fea1ada57f0e7fa6ca7612e9d17300/src/model-failover.mjs) | For reproducible runs, disable automatic changes or define an explicit allowed chain and record the actual model. |

The route compatibility test checks basic answers, streaming, function calls, returned tool data, and compaction. Its cheap subagent probe covers only streaming and tool calling. These checks are useful but do not certify the factory's output contracts, side effects, cancellation, or container configuration. [Compatibility test source](https://github.com/duolahypercho/codex-router/blob/5e1b49e6e3fea1ada57f0e7fa6ca7612e9d17300/src/compatibility-test.mjs).

The default-enabled behavior applies when there is no saved setting. A valid saved `enabled: false` remains off; missing configuration and a deliberate opt-out must not be confused. [Failover settings](https://github.com/duolahypercho/codex-router/blob/5e1b49e6e3fea1ada57f0e7fa6ca7612e9d17300/src/model-failover.mjs#L308), [Vision settings](https://github.com/duolahypercho/codex-router/blob/5e1b49e6e3fea1ada57f0e7fa6ca7612e9d17300/src/vision-bridge-state.mjs#L98).

## Compaction does not preserve the entire job

External providers cannot create OpenAI's opaque native compaction payload. The router creates its own `kcr2` checkpoint from selected visible transcript entries. This is intentionally lossy, bounded, and encoded as JSON rather than encrypted. Native OpenAI compaction remains opaque. A conversation already compacted by OpenAI is not automatically portable to an external model. [Compaction design](https://github.com/duolahypercho/codex-router/blob/5e1b49e6e3fea1ada57f0e7fa6ca7612e9d17300/docs/HOW-IT-WORKS.md#transport-and-compaction).

**Design conclusion:** Store requirements, approvals, step state, artifact locations, and completed actions in LangGraph's durable state. A model transcript or router summary should not be the only record of whether a release, patch, or approval occurred. Changing the selected model is not the same operation as migrating a durable workflow.

## Native Codex subagents have an extra dependency

For routed native subagents, the router can need an authenticated OpenAI relay to turn an opaque delegated task into readable input. That path requires ChatGPT sign-in and can fail on native authentication or quota errors. Login-free routing does not remove this dependency. [Subagent relay](https://github.com/duolahypercho/codex-router/blob/5e1b49e6e3fea1ada57f0e7fa6ca7612e9d17300/docs/HOW-IT-WORKS.md#transport-and-compaction).

The subagent guide defines five checks: streaming, function calling, encrypted relay, returned marker, and a second turn in the same child. It also permits explicit operator selection to enable a route. [Subagent guide](https://github.com/duolahypercho/codex-router/blob/5e1b49e6e3fea1ada57f0e7fa6ca7612e9d17300/docs/SUBAGENT-CERTIFICATION.md).

**Documentation mismatch:** One architecture passage says local settings cannot promote an unverified route. The actual `applyMultiAgentSettings()` source promotes explicitly selected routes, and `all` promotes every non-hidden route. Thus `multiAgentVersion: v2` alone is not proof of a completed test. Inspect the accepted evidence or perform the exact route test. [Implementation](https://github.com/duolahypercho/codex-router/blob/5e1b49e6e3fea1ada57f0e7fa6ca7612e9d17300/src/multi-agent-state.mjs#L194).

**Design conclusion:** LangGraph can avoid this specific native-subagent dependency by starting independent worker tasks and owning their coordination. That is separate from using Codex's built-in child-agent mechanism. It does not remove ordinary worker authentication, billing, or output requirements.

## Credentials, local networking, and containers

The documented service layout binds to `127.0.0.1`. Requests require a local caller secret, distinct from the internal service secret and provider credentials. Generated Codex configuration contains that caller secret in a URL, so configuration and error output can be sensitive. The security guide expressly advises against exposing the listeners on a shared network or changing them to `0.0.0.0`. These controls do not protect against hostile code running as the same OS user. [Security model](https://github.com/duolahypercho/codex-router/blob/5e1b49e6e3fea1ada57f0e7fa6ca7612e9d17300/SECURITY.md).

**Design conclusion:** Installing the router for the desktop user does not automatically configure a worker with a separate `CODEX_HOME`. Nor does host loopback become container loopback. A container-based worker needs an explicit, authenticated connection design and generated worker configuration. Do not treat a wider listener binding as a routine setup fix. The parent project report must verify the worker's actual mounts, networking, and secret handling.

Provider API keys and subscriptions are separate account arrangements. Some routes use existing official CLI login state; others use separately billed API keys. The repository's experimental client-agent bridges are distinct from model routes and currently withhold filesystem/terminal capabilities and reject permission requests by default. Those bridges do not establish a complete interchangeable coding backend. [Authentication and bridge documentation](https://github.com/duolahypercho/codex-router/blob/5e1b49e6e3fea1ada57f0e7fa6ca7612e9d17300/README.md).

## Costs and version compatibility

Model calls, compaction, image reading, and native delegation relays can consume different quotas. Source documents automatic fallback to other configured routes; a selected model name is therefore not a complete cost record. **Proposed control:** Include requested route, actual route, image reader, fallback reason, and usage in the factory run log. Treat provider invoices and account dashboards as the final spending record.

**Evidence limit:** A successful app-server result under a requested model name does not, by itself, prove the router kept that model for the full run. Verify the actual route from router records when fallback is possible. This research did not establish a complete app-server-to-router usage correlation contract.

The troubleshooting guide describes version-sensitive model catalogs and requires app restart to reload them. It warns that the router can resolve a different CLI version from the one the user runs. The test suite contains catalog fixtures for Codex 0.154.0, but this is not an end-to-end compatibility guarantee for that version. [Troubleshooting](https://github.com/duolahypercho/codex-router/blob/5e1b49e6e3fea1ada57f0e7fa6ca7612e9d17300/docs/TROUBLESHOOTING.md), [Version fixtures](https://github.com/duolahypercho/codex-router/blob/5e1b49e6e3fea1ada57f0e7fa6ca7612e9d17300/test/native-account-catalog.test.mjs#L380).

**Recommendation:** Treat the router as an optional, version-pinned worker transport. First make LangGraph's run API independent of BB and Codex. Then test one external provider against the factory's exact worker contract, with automatic fallback disabled. A successful plain-text prompt or model picker entry is not sufficient evidence for production use.
