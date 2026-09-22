# Theseus repository instructions

These instructions apply to the entire repository. Read `README.md`,
`CONTRIBUTING.md`, and `RELEASING.md` before making a public API, packaging,
state, protocol, or release change.

## Project boundary

- Keep this repository focused on reusable agent-backend infrastructure.
- Keep application prompts, business-specific tools, application databases,
  user interfaces, and example agents in their consuming projects.
- Add a feature here only when it is useful across projects or provides a
  general extension point.
- Do not vendor this package into a consuming application. Fix shared behavior
  here, release a version, and upgrade the application deliberately.

## Compatibility

- Treat exports from `theseus/__init__.py` as the public API.
- Preserve documented behavior unless a breaking change is explicitly
  approved and versioned.
- Keep backend protocol details behind the public Python interface.
- Preserve project-local state isolation, per-thread capability selection,
  thread resumption, and event routing between concurrent runs.
- Never move or recreate an existing release tag. Fix a released problem in a
  new version.

## State and credentials

- `.theseus/` is generated local runtime state. It can contain credentials,
  conversation history, skills, caches, and databases.
- Never commit, inspect unnecessarily, copy into fixtures, or delete
  `.theseus/` without explicit owner approval.
- Tests must use temporary directories and fake credentials or protocol data.
- Do not print tokens, authentication files, prompts, or conversation history.

## Testing

- For a bug fix, first add a focused regression test that demonstrates the
  externally visible failure.
- Test public behavior and safety boundaries, not private implementation
  details or application-specific workflows.
- Keep the default suite deterministic, local, and free of real model calls.
- Extend `tests/fake_app_server.py` only to model protocol behavior required by
  a core test. Do not add business-domain behavior to the fake.
- Run `python -m unittest discover -s tests -v` after relevant changes.
- Live tests are opt-in because they use real authentication and model calls.
  Run them only when the change requires real protocol verification and the
  owner has authorized it.
- Build and smoke-install the wheel before a release.

## Change discipline

- Keep changes small and explain why they belong in the shared package.
- Update documentation when public behavior or integration requirements
  change.
- Update `CHANGELOG.md` for user-visible changes.
- Do not bump versions, commit, tag, push, or publish unless the owner has
  approved that release step.
- Preserve unrelated local changes in a dirty working tree.
