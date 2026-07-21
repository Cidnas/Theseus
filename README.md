# codeAgent

`codeAgent` embeds the local Codex app-server in a Python application. It adds
project-local Codex state, per-thread tools and skills, thread resumption, and
concurrent runs.

## Requirements

- Python 3.12 or newer.
- The `codex` CLI available on `PATH`.
- Codex authentication imported from another Codex home or supplied through
  `CODEX_ACCESS_TOKEN`.

## Quickstart

Tools are ordinary typed Python functions with no dependency on `codeagent`:

```python
from codeagent import CodexAppServer, final_text, register_tools


def get_item_price(item: str) -> float:
    """Return the shop price for an item."""

    return {"coffee": 3.50, "notebook": 6.25}[item]


codex = CodexAppServer(".")
codex.import_auth("~/.codex")
tool_names = register_tools(codex, [get_item_price])

with codex:
    thread_id = codex.create_agent(
        tools=tool_names,
        sandbox="read-only",
    )
    messages = codex.run("How much is a notebook?", thread_id)

print(final_text(messages))
```

`create_agent()` returns a Codex thread ID. Reuse that ID with `run()` to
continue the conversation. `run()` returns the raw messages collected through
`turn/completed`; `final_text()` extracts the last completed agent message.

## Tools and skills

`register_tools()` derives a tool's name, description, and input schema from
its function name, docstring, and annotations. It only adds the function to an
in-memory catalog. `create_agent(tools=[...])` selects which registered tools
Codex can use in that thread.

For larger applications, keep tool functions in normal application modules and
collect the exposed functions in a `TOOLS` list. See
[`examples/shop_agent`](examples/shop_agent).

`add_skill(name, description, instructions, resources=...)` installs a skill
under the isolated Codex home. Select installed skills for a thread with
`create_agent(skills=[...])`. Skills persist on disk; the in-memory tool catalog
must be rebuilt when the Python application restarts.

## Concurrent runs

`run()` is synchronous. Use `run_async()` with `asyncio.gather()` to run
different Codex threads concurrently:

```python
import asyncio


async def run_both(codex: CodexAppServer):
    async with codex:
        thread_a = codex.create_agent()
        thread_b = codex.create_agent()

        return await asyncio.gather(
            codex.run_async("Inspect the API.", thread_a),
            codex.run_async("Inspect the tests.", thread_b),
        )
```

`run_async()` moves each blocking `run()` into a worker thread. One app-server
reader routes request responses by request `id` and turn events by `threadId`,
so concurrent runs do not consume each other's messages.

## Authentication and state

By default, state is stored under `<project>/.codex-agent`, separate from the
normal Codex home. The directory is ignored by Git because it can contain
credentials and conversation history.

Authentication is not copied automatically. Call `import_auth()` before
starting the app-server, as shown above, or set `CODEX_ACCESS_TOKEN` for a
non-interactive environment.

## Current limitations

- Codex app-server is experimental, so its protocol may change.
- Tool parameters must use `str`, `int`, `float`, or `bool` annotations.
- Tool functions must be synchronous; `async def` tools are not supported.
- A Codex thread can have only one active run.
- Cancelling `run_async()` does not send `turn/interrupt` to Codex.
- A new `CodexAppServer` object can resume a thread by ID, but it does not
  automatically recover that thread's tool and skill selections.

## Tests

The default suite uses a local protocol fake and makes no model calls:

```bash
python -m unittest discover -s tests -v
```

The optional live suite uses local Codex authentication and makes real model
calls:

```bash
CODEAGENT_LIVE_TEST=1 CODEX_AUTH_HOME="$HOME/.codex" \
  python -m unittest tests.test_app_server.LiveCodexAppServerTests -v
```
