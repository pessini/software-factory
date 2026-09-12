# Model settings

Use the BB **Factory** panel to choose settings before sending a work request.
Open the task's right panel, select **New tab**, then **Factory**. Each change is
saved. The controls list only combinations reported by the task's environment.
Install the panel using `bb-plugin-factory-controls/README.md`.

## Project defaults and presets

Version 1 role settings remain valid. Planner supplies Plan; Builder supplies
Build, Repair, and Revision; Reviewer supplies Review in single mode. In focused
mode, Reviewer supplies the starting model and effort for Request Review, Code
Review, Security Review, and Judge. These four roles always use read-only worker
access and `auto` permission mode. A model selection cannot grant full access.

Optional `settings` and `presets` entries use this structure:

```yaml
settings:
  shared:
    provider: your-provider
    model: a-model-from-bb
    reasoning_level: an-effort-for-that-model
  steps:
    repair:
      model: another-model-from-bb
presets:
  focused:
    steps:
      review:
        reasoning_level: an-effort-for-that-model
```

Model IDs and efforts in this example are placeholders. Get real choices from
`bb provider models <provider> --environment <environment> --json` or the panel.

Resolution starts with the old role setting, then applies project settings,
the selected preset, and session choices, in that order. In each layer, shared
values apply first and step values apply next. Changing a provider clears the
inherited model and effort. Changing a model clears inherited effort. Explicit
values in the same choice still apply. Omitted values then use that model's
reported defaults. Unsupported explicit choices fail; there is no fallback.

## Saved runs and sessions

At run creation, `config.json` saves the requested session choices, all active
effective choices, the catalog used, settings version, prompt version, and the
complete project configuration. Resume reads this saved file. The current YAML
cannot change the run's settings. Existing repository protection still blocks
unapproved changes to protected files after setup; preserving settings does not
remove that protection.
Runs created before model settings were saved must be started again. Resume
rejects a missing snapshot before it can create a child.

ACP session choices are stored in SQLite beside the run records. New sessions
in the same parent task inherit the last saved selections. Existing session
configuration is retained on reload. Changes and run starts are checked against
the stored active run. To use a changed project configuration, start a fresh
parent task. Mid-run setting changes are outside this version's scope.

Each completed model step records the requested and provider-reported choice in
its events. A provider-reported mismatch blocks the run. If the provider cannot
report a choice, the event says `unverified`.

`doctor` checks the actual BB environment, models, command executables, and
SQLite storage. It also reports the existing child-isolation limit. See
`bb-settings-compatibility.md` for live evidence and limitations.

## Focused review settings

Set `review_mode: focused` in the project configuration before starting a new
session. The separate step keys are `request_review`, `code_review`,
`security_review`, and `judge`. They use the same shared defaults, presets,
session controls, validation, and saved snapshot as other steps. The old `review`
key applies only to single mode. Move its choices to the four focused steps when
changing mode. The panel shows the steps active in the saved session configuration.
Resume keeps the saved review mode and choices. It does not consult a new catalog
or silently select another model.
