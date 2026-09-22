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

The Codex app-server protocol is experimental. This repository is an unreleased
development package (`0.1.0.dev0`). Pin an exact commit when integrating it.

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

## Public API

Create an agent, configure it, and read a structured result:

```python
from theseus import CodexAppServer

# Supply authentication explicitly before entering the context: import_auth()
# or CODEX_ACCESS_TOKEN. Theseus never copies authentication automatically.
with CodexAppServer("/path/to/project") as client:
    agent = client.agent(model="your-model", effort="medium")
    result = agent.run("Explain the project architecture.")
    print(result.text)
    print(result.status)
    agent.configure(effort="high")
```

`client.models()` discovers model IDs and supported reasoning efforts.
`agent.run(..., model=..., effort=..., summary=..., service_tier=...)` overrides
thread settings. As in Codex, supplied settings become defaults for subsequent
turns. `output_schema` applies only to the current turn. Use `configure()` to
set the next turn's defaults explicitly; configuring an active agent is rejected.

`RunResult` contains `thread_id`, `turn_id`, `text`, `status`, `error`, and
`usage` when the server provides it. Failed model turns return their status and
error; transport failures and timeouts raise typed exceptions. Raw messages
are available with `include_raw=True`, but are not retained by default.

For direct event access, `create_agent()` / `resume_agent()` return
thread IDs, `run()` / `run_async()` return raw messages, `on_event` receives raw
messages, and `final_text()` extracts the last completed agent message.

### Streaming and cancellation

```python
from contextlib import closing

with closing(agent.stream("Explain the project architecture.")) as events:
    for event in events:
        if event.kind == "text_delta":
            print(event.text, end="", flush=True)
        elif event.kind == "completed":
            result = event.result
```

Streams expose text deltas, completed messages, tool start/completion events,
and one final result. Close the iterator when stopping early; that interrupts
the run. Async callers use `stream_async()` with `contextlib.aclosing`.
`agent.cancel()` or `await agent.cancel_async()` also interrupts an active run.
Cancelling the task awaiting `run_async()` propagates cancellation to Codex.

### Async and multiple agents

```python
from theseus import CodexAppServer, run_parallel

async def analyze(project):
    async with CodexAppServer(project) as client:
        first = await client.agent_async(model="your-model", effort="low")
        second = await client.agent_async(model="your-model", effort="high")
        return await run_parallel([
            (first, "Inspect the public API."),
            (second, "Inspect test coverage."),
        ], limit=2)
```

`run_parallel()` preserves input order, bounds scheduling, and cancels siblings
on failure. Jobs must use distinct agents. Application-defined tools may
call another agent's `run_async()` to delegate; choose the child and its
capabilities in the application. Theseus does not supply business workflows
or grant child agents extra permissions. Each thread permits one active run.

## Tools and skills

`register_tools(client, functions)` adapts documented sync or async Python
functions. Parameters support `str`, `int`, `float`, `bool`, `None`, typed lists,
string-keyed dictionaries, unions/optionals, `Literal`, and descriptions through
`Annotated`. `add_tool()` accepts an explicit JSON Schema and dict-based handler.
JSON Schema validation runs before a handler executes; schema references must
be local. The `jsonschema` package is an installation dependency.

Pass the resulting names to `client.agent(tools=names)`. Each agent snapshots
its selected handlers and schemas: replacing a catalog entry affects new or
explicitly resumed agents, not an already-created agent. A tool selected for
one agent is not automatically available to another.

Synchronous callbacks execute in bounded worker threads. Async tools and
async event callbacks execute on the application's event loop for async runs,
or on the backend loop for synchronous runs. Use async methods when delegating
from an async tool. Synchronous event callbacks run in worker threads and must
be thread-safe. Callbacks must return promptly and must not close their own
client or cancel their own run; use the owning application for lifecycle control.

Tool callbacks run in the embedding Python process, with its permissions;
Codex's shell sandbox does not sandbox these functions. Tool authors own
side-effect authorization. Errors are returned to the model without exposing
arbitrary exception text. MCP integrations remain opt-in; shell operations
remain under Codex's sandbox and approval policy.

`add_skill()` installs a skill under the isolated Codex home. Skills persist
on disk. Dynamic Python handlers must be registered again after process restart.

## External integrations

New agents do not inherit external integrations by default. Theseus disables
account apps/connectors, plugins, tool suggestions, and configured MCP servers
for each new or resumed thread unless the application explicitly opts in.

To retain the integrations available through the effective Codex configuration:

```python
thread_id = client.create_agent(inherit_integrations=True)
```

To expose only application-selected integrations, pass a Codex configuration
fragment using the `apps`, `mcp_servers`, `plugins`, or `tool_suggest` sections:

```python
thread_id = client.create_agent(
    integration_config={
        "apps": {
            "notion": {"enabled": True},
        },
        "mcp_servers": {
            "project_docs": {
                "url": "https://docs.example.com/mcp",
                "enabled": True,
            },
        },
    }
)
```

When inheritance is disabled, the `apps._default.enabled = false` policy remains
in place, so an app must be named explicitly. Supplying an `apps`, `plugins`, or
`tool_suggest` section enables the corresponding Codex feature automatically.
An app still needs its normal authorization; configuration does not perform an
OAuth or installation flow.

## Threads, limits, and resumption

Persist `agent.id` and its selected capabilities in the consuming application.
After restart, rebuild the tool catalog and call
`client.resume(thread_id, tools=names, skills=..., integration_config=...)`.
Codex restores the thread's original dynamic-tool definitions from its history;
Theseus rebinds selected Python handlers and enforces their local allowlist.
Tools omitted during resumption are denied even if Codex still advertises their
persisted definitions. Use a new thread when changing tool names or schemas.

`additional_context` supplies trusted application context separately from the
user prompt. Do not use it for untrusted tool output or messages from other
agents without accounting for that trust boundary.

Constructor controls:

| Option | Default | Behavior |
| --- | --- | --- |
| `timeout` | 120 seconds | Whole-run deadline, including startup/resumption |
| `cancel_timeout` | 2 seconds | Additional interruption/cleanup grace |
| `max_concurrent_runs` | 32 | Excess runs raise `CodexBusyError` |
| `max_concurrent_tools` | 16 | Excess tool calls return an error to the model |
| `tool_timeout` | 120 seconds | Maximum wait for a tool result |
| `event_buffer` | 4096 | Pending protocol events per active run |

A slow consumer that exceeds an event buffer fails its run rather than causing
unbounded queue growth. Streams have their own configurable `buffer_size`.
Raw-message runs and `include_raw=True` intentionally retain all collected events.

Timeouts and cancellation send `turn/interrupt` and wait for terminal confirmation.
If confirmation fails, the thread cannot be reused until the client is closed
and restarted. Other threads continue. A cancelled synchronous run raises
`CodexCancelledError`; an async run raises `asyncio.CancelledError`.

Python cannot forcibly terminate an arbitrary running synchronous callback.
A timed-out callback may continue side effects; its slot remains occupied until
it returns. Async cancellation is cooperative. A callback that suppresses
cancellation can delay cleanup. Use process isolation in the consuming application
when tool execution needs hard termination.

## Authentication and state

State is stored under `<project>/.theseus` by default, separate from the
normal Codex home. The directory may contain credentials and conversation
history and must not be committed.

Authentication is never copied implicitly. Call `import_auth()` before
starting the app-server, or provide `CODEX_ACCESS_TOKEN` in the environment.

## Current limitations

- Codex's app-server and dynamic-tool protocol remain experimental.
- Tool definitions cannot be replaced on a resumed thread with the current protocol.
- Applications persist thread IDs, selected capabilities, and orchestration policy.
- Hard cancellation of arbitrary Python callbacks requires external process isolation.
- Theseus is verified with a protocol fake, the local CLI's generated
  schema, and five authenticated GPT-6 Luna integration tests. Live verification
  remains opt-in; see [the verification report](docs/live-verification.md).

See [docs/architecture.md](docs/architecture.md) for module boundaries,
protocol behavior, and benchmark methodology.

## Development

The core tests use a local protocol fake and make no model calls:

```bash
python -m unittest discover -s tests -v
```

The optional live app-server tests use local Codex authentication and make real
model calls:

```bash
THESEUS_LIVE_TEST=1 THESEUS_LIVE_MODEL=gpt-6-luna \
  CODEX_AUTH_HOME="$HOME/.codex" THESEUS_LIVE_REPORT=/tmp/theseus-live-usage.jsonl \
  python -m unittest tests.test_live -v
```

The model must be selected explicitly. The five live checks cover streaming,
structured output/application context, skill/tool execution and resumption,
concurrent async tools, and cancellation followed by thread reuse. Each uses
temporary isolated state. The optional report appends numeric token usage and
timing per attempted turn, without prompts, responses, or credentials. Token
totals are differenced across resumed turns to avoid counting history twice;
an interrupted turn may not report usage. This report is not a billing receipt.

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
