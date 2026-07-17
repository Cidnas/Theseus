# codeAgent

`codeAgent` is a small Python module for embedding the local Codex app-server in
another application. Each client owns a project-local `.codex-agent` home, so
its configuration, sessions, skills, and runtime state stay separate from the
host machine's normal Codex home.

The public workflow is intentionally small:

```python
from codeagent import CodexAppServer, final_text


def lookup_ticket(arguments: dict[str, object]) -> dict[str, object]:
    return {"id": arguments["id"], "status": "open"}


with CodexAppServer(".") as codex:
    codex.add_tool(
        "lookup_ticket",
        "Look up a ticket by id.",
        {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
        lookup_ticket,
    )
    codex.add_skill(
        "ticket-triage",
        "Triage a ticket and recommend the next action.",
        "Inspect the ticket, identify the owner, and propose the smallest next step.",
    )

    thread_id = codex.create_agent(
        tools=["lookup_ticket"],
        skills=["ticket-triage"],
        sandbox="read-only",
    )
    raw_messages = codex.run("Triage ticket APP-42.", thread_id)
    print(final_text(raw_messages))
```

`run()` returns the unmodified JSON messages received for the turn, ending with
`turn/completed`. The `final_text()` helper is optional and extracts the last
completed agent message.

## Authentication

The isolated home does not silently reuse host credentials. Authenticate it
directly by running Codex with `CODEX_HOME` set to `<project>/.codex-agent`, use
`CODEX_ACCESS_TOKEN`, or explicitly copy an existing login:

```python
codex = CodexAppServer(".")
codex.import_auth("~/.codex")
with codex:
    thread_id = codex.create_agent(sandbox="read-only")
```

`.codex-agent/` is ignored by Git because it may contain credentials and session
history.

## Tests

The normal suite uses a protocol-level fake server and does not make network or
model calls:

```bash
python -m unittest discover -s tests -v
```

An opt-in live round-trip test exercises the installed Codex app-server:

```bash
CODEAGENT_LIVE_TEST=1 CODEX_AUTH_HOME="$HOME/.codex" \
  python -m unittest tests.test_app_server.LiveCodexAppServerTests -v
```
