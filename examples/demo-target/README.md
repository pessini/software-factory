# Factory demo target

Copy this directory outside the factory engine repository, initialize it as a
Git repository, and verify it before opening it in BB:

```bash
cp -R examples/demo-target /tmp/factory-demo-target
cd /tmp/factory-demo-target
git init
uv sync
uv run pytest -q
software-factory repo-check
git add .
git commit -m "Create software factory demo target"
```

Then open it in BB and submit the contents of `REQUEST.md` to the
**Software Factory** provider.
