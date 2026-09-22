# Theseus

Run Codex agents from Python. Give each agent its own tools, change its model,
stream responses, and run several agents through one app-server process.

## Install

Requires Python 3.12+, the `codex` CLI on your `PATH`, and a Codex login.

```bash
python -m pip install "git+https://github.com/Cidnas/Theseus.git"
```

Theseus is in development. Pin a commit with `@<commit-sha>` for a reproducible
install. The Codex app-server protocol is experimental.

## Run an agent

```python
from pathlib import Path
from theseus import CodexAppServer

client = CodexAppServer(".")
client.import_auth(Path.home() / ".codex")

with client:
    agent = client.agent(model="gpt-6-luna", effort="low")
    result = agent.run("Explain this project's structure.")
    print(result.text)
```

`import_auth()` copies your login into the project's private `.theseus/`
directory. Add `.theseus/` to your `.gitignore`. You can supply
`CODEX_ACCESS_TOKEN` instead of importing a login.

Use `client.models()` to see available models. Change settings with
`agent.configure(model="gpt-6-luna", effort="high")`.

## Give it a tool

Inside an open client, register a typed Python function and select it for an agent:

```python
from theseus import register_tools

def double(value: int) -> int:
    """Double an integer."""
    return value * 2

names = register_tools(client, [double])
agent = client.agent(model="gpt-6-luna", tools=names)
result = agent.run("Use double to calculate twice 21.")
```

Tools can be sync or async. Arguments are validated before the function runs.
Each agent gets only its selected tools. Python tools run with your application's
permissions; Codex's shell sandbox does not restrict them.

## Run agents together

```python
from theseus import run_parallel

async def analyze(client):
    api = await client.agent_async(model="gpt-6-luna", effort="low")
    tests = await client.agent_async(model="gpt-6-luna", effort="low")
    return await run_parallel([
        (api, "Review the public API."),
        (tests, "Review test coverage."),
    ], limit=2)
```

Results follow input order. Each agent supports one active run at a time.
Use `await agent.run_async(...)` for a single async run.

## More

- [Usage](docs/usage.md): streaming, results, resuming, skills, integrations, limits.
- [Editable submodule](docs/submodule-quickstart.md): work on Theseus from an application.
- [Architecture](docs/architecture.md) · [Contributing](CONTRIBUTING.md) · [Live test results](docs/live-verification.md)
