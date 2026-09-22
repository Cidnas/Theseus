# Editable submodule

Use this when you want to work on Theseus from an application repository.
For a normal installation, use the [README](../README.md#install).

From the application's root:

```bash
git submodule add https://github.com/Cidnas/Theseus.git packages/theseus
uv add --editable ./packages/theseus
git add .gitmodules packages/theseus pyproject.toml uv.lock
```

When cloning the application:

```bash
git clone --recurse-submodules <application-url>
cd <application-directory>
uv sync
```

Make shared-library changes inside `packages/theseus` on a new branch. Run its
tests, then commit and push there first. The application records that commit:

```bash
# Back in the application's root:
git add packages/theseus uv.lock
git commit -m "Update Theseus"
```

To select a specific commit, run `git fetch origin` and
`git checkout <commit-sha>` inside `packages/theseus`, then `uv sync` from the
application root. Keep prompts, business tools, and UI in the application.

[Copy-ready coding-agent instructions](consumer-agent-guidelines.md).
