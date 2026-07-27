# Changelog

All notable changes to `theseus` are recorded here. Releases follow the
versioning policy in [RELEASING.md](RELEASING.md).

## Unreleased

### Changed

- Renamed the project, Python distribution, and import package from
  `codeagent` to `Theseus`/`theseus`.
- Changed the default private runtime-state directory from `.codex-agent/` to
  `.theseus/`. Existing consumers should move the directory before starting
  Theseus to preserve authentication and thread history.

## 0.1.0 - 2026-07-26

### Added

- A Python client for embedding the Codex app-server in applications.
- Isolated project-local authentication, configuration, skills, and thread
  state.
- Thread creation and resumption with per-thread tools and skills.
- Structured output schemas and trusted application context.
- Event callbacks and concurrent runs across different threads.
- Adaptation of documented, typed Python functions into dynamic agent tools.
- A public package version, contributor contract, coding-agent instructions,
  consumer-project guidance, and a documented release process.
