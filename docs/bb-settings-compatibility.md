# BB model controls: issue 4

Checked on 2026-09-10 with installed BB 0.42.1.

## Environment and discovery

The packaged CLI is `node /Applications/bb.app/Contents/Resources/app.asar.unpacked/node_modules/bb-app/dist/bb.js`.
The app was installed but stopped. Opening it restored the local connection.
`settings version --json` reports 0.42.1. The existing project is
`proj_amf2zi4zwv`, with environment `env_m5qxyjyyzn`. `environment show`
confirms this repository's canonical path and project ID.

`provider list --environment <id> --json` returns provider availability and
permission modes. `provider models <provider> --environment <id> --json`
returns each model's `model`, `supportedReasoningEfforts`,
`defaultReasoningEffort`, and `isDefault`. Use `model` for dispatch, not `id`.
The Codex catalog reports different effort sets for different models.
The factory must exclude itself to prevent recursive discovery and execution.
Providers that only allow full access cannot run factory steps.

## UI decision, recorded before panel implementation

The installed ACP adapter's `findAcpModelConfigOption` selects the first option
with category `model` (or ID `model`). `findAcpThoughtLevelConfigOption` selects
the first option with category `thought_level`. Discovery and configuration
consume those two options. There is no generic surface for the five independent
factory step options in this adapter. The native model menu alone cannot express
the required settings. Use a small Factory panel, backed by the same Python
session-settings implementation as ACP `session/set_config_option`.

The live checks below test this panel decision.

## Existing execution limit

Issue 3 remains open. The live adapter rejects execution because it cannot yet
enforce role isolation, protect controller storage, or match output to a saved
execution identity. Settings work must not remove these checks. A model listing
or successful panel save does not prove a supported end-to-end factory run.

## Live results

The Factory Controls plugin built with BB 0.42.1 / Plugin SDK 0.4.47 and loaded
with status `running`. The BB right-panel **New tab → Factory** action renders
Preset, Shared, Plan, Build, Repair, Review, and Revision controls. Each step
shows its resolved choice.

A disposable BB ACP task (`thr_x2nn3z2ps9`, environment `env_wirkyvdem7`, workspace
`/tmp/factory-issue4-probe`) was used for the UI check. BB required an initial
prompt to create the task. The initial run stopped at the empty repository's
missing HEAD, without any model child starting. A fixture commit then supplied
a valid repository for further checks.

The UI saved **codex / GPT-5.6-Luna / low** for Repair and
**codex / GPT-6-Astra / high** for Review. A full BB page reload retained both
selections. Other steps retained their inherited choices. The first UI check
found an undefined optional JSON field in the panel request; the request now
omits absent fields and the repeated check passed.

A separate disposable stdio ACP process used live environment model discovery.
`initialize`, `session/new`, `session/set_config_option`, and `session/load`
succeeded. A process restart returned the same seven controls and the saved
repair choice. Automated protocol coverage repeats this sequence with a
controlled external provider.

The next disposable run (`fct_0dba0a13d0ae`) saved the UI's Repair and Review
choices in both requested settings and effective settings. It stopped at the
existing isolation check with no child dispatch. Review subsequently moved
that protection check ahead of run-time model discovery; automated tests
confirm that an unsupported live adapter cannot invoke even the BB CLI.

Review also found and fixed session targeting and connection cleanup errors.
Tests now load an older session and change it through the panel, disconnect
during the first ACP update, and list providers with different role permissions.

The final `doctor` check passed Python, Git, the packaged BB command, workspace,
policy, the configured test command, parent context, SQLite storage, the live
BB connection and environment, and provider options. It reported the expected
failure for child isolation. The disposable task was stopped and archived
after these checks.

Workflow tests verify that different saved models and efforts reach plan,
build, repair, review, and revision. Repair and revision create separate saved
child conversations when their settings differ from Build. An unsupported
model/effort pair fails before child creation. Actual provider execution remains
unverified on live BB because the existing isolation and execution-identity
adapter is incomplete; no live child-execution pass is claimed.
