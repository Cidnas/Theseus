# Theseus

`Theseus` is a small Python library for embedding the local Codex app-server
in an application. It manages the app-server process, isolated project state,
Codex threads, dynamic Python tools, skills, streamed events, and concurrent
runs.

The library deliberately does not contain application agents. Each consuming
project owns its prompts, tools, skills, persistence, API, and user interface.

## Requirements

- Python 3.12 or newer
- The `codex` CLI available on `PATH`
- Codex authentication imported from another Codex home or supplied through
  `CODEX_ACCESS_TOKEN`

The Codex app-server protocol is experimental. Applications should pin a
`theseus` release and upgrade intentionally.

## Installation

For a consuming project that may also contribute fixes, add this repository as
a Git submodule and install it editably:

```bash
git submodule add \
  git@github.com:Cidnas/Theseus.git \
  packages/theseus

uv add --editable ./packages/theseus
```

The consuming repository records the exact package commit. Clone it later with:

```bash
git clone --recurse-submodules <consumer-repository-url>
uv sync
```

When changing the package, commit and push inside `packages/theseus` first.
Then commit the updated submodule pointer in the consuming repository.
See [docs/submodule-quickstart.md](docs/submodule-quickstart.md) for the complete
add, clone, edit, push, and upgrade workflow.

For a deployment that does not need an editable source checkout, depend
directly on an immutable release tag:

```toml
dependencies = [
    "theseus @ git+ssh://git@github.com/Cidnas/Theseus.git@<release-tag>",
]
```

Do not make production projects depend on an unpinned branch such as `main`.

### Migrating from 0.1.0

Update imports from `codeagent` to `theseus`. Before starting Theseus in an
existing project, preserve its local credentials and thread history by moving
the private state directory:

```bash
mv .codex-agent .theseus
```

## Public API

The package exports:

- `CodexAppServer` for lifecycle, thread, tool, skill, and turn operations
- `register_tools()` for adapting documented, typed Python functions
- `final_text()` for extracting the last completed agent message from a turn
- `__version__` for the installed library version
- Typed app-server exceptions for protocol, timeout, and process failures

A normal application creates one `CodexAppServer`, explicitly supplies
authentication, registers its tools and skills, creates or resumes a thread,
and calls `run()` or `run_async()`.

## Tools and skills

Tools can be registered directly with `add_tool()`. `register_tools()` provides
a smaller adapter for ordinary synchronous Python functions whose parameters
use `str`, `int`, `float`, or `bool` annotations. A thread receives only the
registered tool names selected when it is created or resumed.

`add_skill()` installs a skill under the isolated Codex home. Skills persist on
disk. Dynamic tools live in the embedding Python process and must be registered
again after that process restarts.

## Threads and turns

`create_agent()` returns a Codex thread ID. Applications are responsible for
persisting that ID together with the names of the tools and skills assigned to
it. After a process restart, rebuild the tool catalog and call
`resume_agent()` before the next turn.

`run()` returns the raw app-server messages collected through
`turn/completed`. It also supports:

- `on_event` for progress, telemetry, or UI streaming
- `output_schema` for a schema-constrained final response
- `additional_context` for trusted application context kept separate from the
  user's prompt

`run_async()` makes the blocking operation awaitable. Different threads may
run concurrently, but one thread can have only one active run.

## Authentication and state

State is stored under `<project>/.theseus` by default, separate from the
normal Codex home. The directory may contain credentials and conversation
history and must not be committed.

Authentication is never copied implicitly. Call `import_auth()` before
starting the app-server, or provide `CODEX_ACCESS_TOKEN` in the environment.

## Current limitations

- The Codex app-server protocol may change.
- Adapted tool parameters support only `str`, `int`, `float`, and `bool`.
- Adapted tool functions must be synchronous.
- A thread can have only one active run.
- Cancelling `run_async()` does not interrupt the underlying Codex turn.
- Applications must persist thread IDs and capability selections themselves.

## Development

The core tests use a local protocol fake and make no model calls:

```bash
python -m unittest discover -s tests -v
```

The optional live app-server tests use local Codex authentication and make real
model calls:

```bash
THESEUS_LIVE_TEST=1 CODEX_AUTH_HOME="$HOME/.codex" \
  python -m unittest tests.test_app_server.LiveCodexAppServerTests -v
```

See [RELEASING.md](RELEASING.md) for versioning and release policy.

## Coding-agent guidance

Repository-wide development rules live in [AGENTS.md](AGENTS.md) and the full
contribution contract lives in [CONTRIBUTING.md](CONTRIBUTING.md). Because an
installed dependency cannot automatically control coding agents working in a
different repository, consuming projects should copy the instruction block in
[docs/consumer-agent-guidelines.md](docs/consumer-agent-guidelines.md) into
their own root `AGENTS.md`. When a coding agent works inside the package
submodule, this repository's own `AGENTS.md` applies from that separate Git
root.
