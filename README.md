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
`turn/completed`; `final_text()` extracts the last completed agent message. Pass
`on_event=callback` to observe those messages as they arrive for UI progress,
telemetry, or debugging without coupling that presentation to the runtime.
Pass `output_schema={...}` when downstream code needs a schema-constrained final
message, and `additional_context={"source": "..."}` to attach trusted
application context without mixing it into the user's text.
After rebuilding the in-memory tool catalog in a new Python process, call
`resume_agent(thread_id, tools=[...], skills=[...])` before the next `run()` to
restore the thread's capability selections.

## Tools and skills

`register_tools()` derives a tool's name, description, and input schema from
its function name, docstring, and annotations. It only adds the function to an
in-memory catalog. `create_agent(tools=[...])` selects which registered tools
Codex can use in that thread.

For larger applications, keep tool functions in normal application modules and
collect the exposed functions in a `TOOLS` list. See
[`examples/shop_agent`](examples/shop_agent).

## SQLite shop example

[`sample.py`](sample.py) creates a small SQLite shop database, registers six
focused tools, and asks Codex a question that connects a customer, their latest
order, its product, and current inventory:

```bash
python sample.py
```

The generated database lives at `.sample-data/shop.db`. Schema creation, seed
data, and query helpers stay under `examples/shop_agent`, keeping the client
script focused on the public workflow.

## Prerequisite-graph tutor example

[`examples/learning_agent`](examples/learning_agent) is a serious first version
of a persistent adaptive tutor. A research agent generates a compact knowledge
graph for a goal, an operator approves it, a planner creates 3-5 learning steps,
a tutor teaches and gathers evidence, and a separate evaluator classifies that
evidence. Numeric mastery updates and plan transitions are deterministic host
code rather than model judgment.

Build and review a topic:

```bash
python -m examples.learning_agent build \
  --goal "Understand linear regression" \
  --audience "adult beginner with basic arithmetic" \
  --depth "able to fit, interpret, and diagnose a simple model"
```

The builder keeps research, sources, and validation logs in
`.learning-data/builds/`. Generated Python is constrained to a fixed adapter,
validated in a subprocess, and imported only after the operator types
`approve`. Approved runtime packages deliberately exclude the research context.
Long model operations print their current phase—research, validation, agent
restoration, planning, tutoring, evaluation, gate resolution, or replanning.

Continue the learner-only conversation with:

```bash
python -m examples.learning_agent chat understand-linear-regression
```

Add `--debug` to either command to show the current build phase or learning-plan
step with a spinner, elapsed time, backend event count, and last activity. After
60 seconds without a new app-server event it displays `QUIET`; the command's
`--timeout` remains the hard failure boundary.

```bash
python -m examples.learning_agent build \
  --goal "Understand linear regression" \
  --audience "adult beginner" \
  --depth "working practical knowledge" \
  --debug

python -m examples.learning_agent chat understand-linear-regression --debug
```

The animation is strictly a CLI adapter. The graph, persistence, agents, and
state transitions have no terminal dependency; a frontend can construct
`LearningRuntime` directly and optionally consume its progress and raw model-event
callbacks. `CodexAppServer.run()` also exposes the generic `on_event` callback.

Each plan has 3-5 steps, but a step has no turn budget. A completion checkpoint
requires two evaluator-accepted informative signals about its focus concepts,
including at least one direct demonstration. The planner runs again only when
all steps pass that gate or the current route is explicitly blocked. Graph,
beliefs, plans, evidence, and all four Codex thread IDs persist in SQLite.

Tutor turns use a single structured model completion. The host supplies only the
active step and its recent evidence as application context, then atomically
persists the tutor's evidence and optional checkpoint decision. The tutor does
not reread the full graph, plan, or learner model on ordinary conversational
turns; evaluator and planner agents still perform the deeper state work at the
checkpoint boundaries where it affects learning decisions.

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
- Applications are responsible for persisting thread IDs and passing their
  tool and skill selections to `resume_agent()` after a process restart.

## Tests

The default suite uses a local protocol fake and makes no model calls:

```bash
python -m unittest discover -s tests -v
```

The optional live suite uses local Codex authentication and makes real model
calls:

```bash
CODEAGENT_LIVE_TEST=1 CODEX_AUTH_HOME="$HOME/.codex" \
  python -m unittest \
    tests.test_app_server.LiveCodexAppServerTests \
    tests.test_shop_agent_live.LiveShopAgentTests -v
```

The slower learning-system integration test runs real builder, planner, tutor,
and evaluator model turns and reports seven progress phases:

```bash
CODEAGENT_LEARNING_LIVE_TEST=1 CODEX_AUTH_HOME="$HOME/.codex" \
  python -m unittest \
    tests.test_learning_agent.LiveLearningAgentTests -v
```
