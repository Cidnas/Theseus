# Using codeAgent as an editable submodule

This setup keeps `codeagent` inside the consuming project while preserving it
as a separate Git repository. The consuming project records the exact package
commit, and Python imports directly from the editable checkout.

## Add it to a new project

From the consuming project's root:

```bash
git submodule add \
  git@github.com:Cidnas/codexAgent.git \
  packages/codeagent

uv add --editable ./packages/codeagent
```

Commit the setup in the consuming project:

```bash
git add .gitmodules packages/codeagent pyproject.toml uv.lock
git commit -m "Add codeagent backend"
```

Application code can now import the package normally:

```python
from codeagent import CodexAppServer, final_text, register_tools
```

## Clone a consuming project

Initialize the submodule while cloning:

```bash
git clone --recurse-submodules <consumer-repository-url>
cd <consumer-repository>
uv sync
```

For an existing clone whose submodule directory is empty:

```bash
git submodule update --init --recursive
uv sync
```

## Edit the shared package

Enter the submodule and confirm that Git is operating on the package
repository:

```bash
cd packages/codeagent
git rev-parse --show-toplevel
git remote -v
git switch -c fix/<short-description>
```

Edit the package and run its deterministic tests:

```bash
python -m unittest discover -s tests -v
```

Commit and push from inside the package repository:

```bash
git add .
git commit -m "Fix <problem>"
git push -u origin fix/<short-description>
```

Merge and release that package change through the package repository. Then
update the consuming project to the released commit or tag:

```bash
git fetch --tags
git checkout v0.1.1
cd ../..
uv sync
git add packages/codeagent uv.lock
git commit -m "Update codeagent to v0.1.1"
```

## Remember the two repositories

- Commands run at the consuming-project root use the consuming project's
  `origin`.
- Commands run inside `packages/codeagent` use the package repository's
  `origin`.
- Push the package commit before committing its updated pointer in the
  consuming project.
- Do not edit `.venv/site-packages`; the editable installation already imports
  from `packages/codeagent`.
- Clone with `--recurse-submodules`, or initialize submodules before running
  `uv sync`.
