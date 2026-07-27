# Coding-agent guidance for consuming projects

Coding agents only discover instructions in the repository where they are
working. Installing `theseus` does not automatically apply this repository's
`AGENTS.md` to an application repository.

Copy the following section into the consuming project's root `AGENTS.md` and
adapt the paths or commands that are specific to that project.

## Copy-ready instruction block

```markdown
## Shared agent backend

This project uses `theseus` as a versioned dependency from
https://github.com/Cidnas/Theseus, checked out as the Git submodule
`packages/theseus` and installed as an editable dependency.

- Treat `packages/theseus` as a separate upstream Git repository. Never patch
  `site-packages` or copy its source into the consuming application's modules.
- Before committing or pushing package changes, enter `packages/theseus` and
  confirm its repository root and remote with `git rev-parse --show-toplevel`
  and `git remote -v`.
- Commit and push shared-package changes inside `packages/theseus` first.
  Then return to the consuming repository and commit its updated submodule
  pointer.
- Keep this project's prompts, tools, business rules, persistence, API, and UI
  in this repository.
- If shared behavior is missing or broken, reproduce and fix it inside the
  submodule, release a new package version, and update the consuming repository
  to the released submodule commit.
- Keep the submodule pinned to an intentional commit, preferably an immutable
  release tag for production.
- Treat `.theseus/` as private generated runtime state. Never commit it,
  inspect it unnecessarily, place its contents in tests, or delete it without
  explicit owner approval.
- Persist thread IDs and their selected tool and skill names in the
  application's own storage when conversations must survive restarts.
- After a process restart, rebuild the in-memory tool catalog and explicitly
  resume saved threads with their capabilities.
- Do not start two simultaneous runs on the same thread. Use separate threads
  for concurrent work.
- Do not run live model tests or import authentication unless the task requires
  it and the owner has authorized real model use.
```

## Why this belongs in each application

The library can document its contract, but it cannot force a coding agent in a
different checkout to read documentation inside an installed dependency. The
application's own `AGENTS.md` is the reliable place to establish these editing,
state, testing, and upgrade rules.
