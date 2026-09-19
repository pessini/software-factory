# Factory controls for BB

This panel lets a user set a shared provider, model, and effort, then override
plan, build, repair, review, and revision. It reads models from Factory's protected
native worker. Each change is saved through the shared Factory controls. The
selected BB conversation is mapped to a Factory session so conversations in the
same workspace retain separate settings. The panel supports workspaces on the
BB server machine. Set the same `FACTORY_HOME` for the BB agent and panel command.

Install the Python project with `uv tool install --editable .` from the repository
root. Register Software Factory as described in `docs/macos-runbook.md`.
Then run these commands from this plugin directory:

```sh
npm ci
bb plugin build
bb plugin install . --yes
```

In a Software Factory task, open the right panel, select **New tab**, then
**Factory**. Choose shared settings or step overrides. Check the resolved value
beside each step, then send the work request in the task. Select **Reload choices**
to read saved values after a reload. Active runs reject settings changes.

The plugin uses the executable from BB's registered `software-factory` agent.
For a custom launcher, set **Software Factory executable** in plugin settings
to the Python project's `software-factory` executable.

Validation:

```sh
npx tsc --noEmit
bb plugin build
```

The Python tests cover settings resolution, session persistence, and child
execution choices. See `docs/bb-settings-compatibility.md` in the repository for
the historical live BB 0.42.1 check. Current verification and remaining live
checks are recorded in `docs/issue-9-acceptance.md`.
The generated BB UI components remain available for panel maintenance.
