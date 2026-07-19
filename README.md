# codeAgent

`codeAgent` is a small Python module for embedding the local Codex app-server in
another application. Each client owns a project-local `.codex-agent` home, so
its configuration, sessions, skills, and runtime state stay separate from the
host machine's normal Codex home.

Application tools are ordinary typed Python functions. They do not import
`codeagent`, construct schemas, or know that Codex will call them:

```python
# my_application/tools/shop.py

def get_item_price(item: str) -> float:
    """Return the shop price for an item."""

    prices = {"coffee": 3.50, "notebook": 6.25, "pen": 1.20}
    return prices[item]
```

The application's tools package explicitly collects the functions it wants to
make available:

```python
# my_application/tools/__init__.py

from .shop import get_item_price

TOOLS = [get_item_price]
```

At startup, the generic adapter inspects those functions, generates their JSON
schemas, adapts app-server argument dictionaries to normal keyword arguments,
and registers them in the local catalog:

For now, tool parameters must be annotated with `str`, `int`, `float`, or
`bool`. A docstring supplies the description shown to Codex.

```python
from codeagent import CodexAppServer, final_text, register_tools
from my_application.tools import TOOLS

codex = CodexAppServer(".")
register_tools(codex, TOOLS)

with codex:
    thread_id = codex.create_agent(
        tools=["get_item_price"],
        sandbox="read-only",
    )
    raw_messages = codex.run(
        "According to the shop tool, how much does a notebook cost?",
        thread_id,
    )
    print(final_text(raw_messages))
```

Registration only prepares the local catalog; it does not require a running
thread or send anything to Codex. The `tools` argument to `create_agent()` is a
separate allowlist. When that thread starts, only the selected registered
functions are sent as Codex `dynamicTools`.

Skills are installed persistently under the isolated Codex home. Dynamic tool
handlers are Python callables, so the application registers them again whenever
it starts a new process.

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
