# Usage

The examples below use an open `CodexAppServer` client and an agent created as
shown in the [README](../README.md). Async code can use
`async with CodexAppServer(project) as client` after supplying authentication.

## Results and settings

```python
result = agent.run("Summarize the changes.", timeout=60)
if result.status == "completed":
    print(result.text)

agent.configure(model="gpt-6-luna", effort="high")
```

Results contain `text`, `status`, `error`, `usage`, `thread_id`, and `turn_id`.
Model failures return a failed status; transport errors and deadlines raise
exceptions. Token usage is present only when Codex reports it.

You can also pass `model`, `effort`, `summary`, and `service_tier` to `run()`.
These settings persist for later turns. `output_schema` applies to one turn:

```python
result = agent.run(
    "Summarize the project in one sentence.",
    output_schema={
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
        "additionalProperties": False,
    },
)
```

`result.text` contains the JSON. `additional_context={"source": "text"}` supplies
trusted application context; don't put untrusted tool output there.

## Streaming

```python
from contextlib import closing

with closing(agent.stream("Explain the project.")) as events:
    for event in events:
        if event.kind == "text_delta":
            print(event.text, end="", flush=True)
        elif event.kind == "completed":
            result = event.result
```

Events also include `message`, `tool_started`, and `tool_completed`.
Async callers use `stream_async()` with `contextlib.aclosing`.
Closing a stream early interrupts its turn. From the owning application, use
`agent.cancel()` or `await agent.cancel_async()` to interrupt an active run.
Cancelling a task awaiting `run_async()` also cancels the Codex turn.

## Resume a conversation

Save `agent.id` and its selected tools, skills, and integration settings.
After restarting your application, use the same project directory, register
its tools again, then resume:

```python
agent = client.resume(saved_thread_id, tools=names)
result = agent.run("Continue where we left off.")
```

Codex keeps the thread's tool definitions. Use a new agent when changing tool
names or schemas. Omitted tools cannot execute, even if still visible to Codex.

## Tools and skills

`register_tools()` accepts documented functions with typed parameters: scalars,
lists, string-keyed dictionaries, unions, `Literal`, and `Annotated` descriptions.
For an explicit JSON Schema, use `client.add_tool(name, description, schema, handler)`.
Schema references must be local. Each agent snapshots its chosen handlers.

Async tools run on the caller's event loop during async runs; sync tools run in
worker threads. Use async APIs when calling another agent from a tool. Callbacks
must return promptly; don't close the client or cancel their own run from inside
one. A timed-out sync function may keep running and holds its slot until it exits.

```python
client.add_skill(
    "brief-replies",
    "Keep responses short.",
    "Answer in one paragraph unless the user asks for more detail.",
)
agent = client.agent(skills=["brief-replies"])
```

Skills persist under `.theseus/skills`. Python handlers must be re-registered
after a process restart.

## MCP and other integrations

Apps, MCP servers, plugins, and tool suggestions are off by default.
Use `client.agent(inherit_integrations=True)` to inherit Codex configuration,
or select integrations explicitly:

```python
agent = client.agent(integration_config={
    "mcp_servers": {
        "docs": {"url": "https://docs.example.com/mcp", "enabled": True},
    },
})
```

The accepted sections are `apps`, `mcp_servers`, `plugins`, and `tool_suggest`.
Configuring an integration doesn't authenticate it. For shell access, agent
creation defaults to `sandbox="workspace-write"` and `approval_policy="never"`.
Use `sandbox="read-only"` when writes aren't needed.

## Limits and errors

Set limits on `CodexAppServer(...)`:

| Setting | Default |
| --- | ---: |
| `timeout` | 120 seconds per run |
| `tool_timeout` | 120 seconds per tool |
| `cancel_timeout` | 2 seconds for interruption cleanup |
| `max_concurrent_runs` | 32 |
| `max_concurrent_tools` | 16 |
| `event_buffer` | 4,096 pending events per run |

A full run pool raises `CodexBusyError`; a deadline raises `CodexTimeoutError`.
Cancelled sync runs raise `CodexCancelledError`; async runs raise
`asyncio.CancelledError`. If interruption cannot be confirmed, close and restart
the client before reusing that thread. Python cancellation cannot undo side
effects or forcibly stop sync callbacks.

Streams have their own `buffer_size` (default 256). Buffers are bounded; sync
stream overflow fails the run, while async streams wait for the consumer.

## Raw events

Use `agent.run(..., include_raw=True)` to retain protocol events in the result.
For direct access, `client.create_agent()` returns a thread ID and
`client.run(prompt, thread_id)` returns raw messages. `on_event` receives each
raw message; `final_text(messages)` extracts the final text.
