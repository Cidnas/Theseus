# Architecture

One client owns one Codex app-server process and a background asyncio loop.
Agents share that process; each keeps its own thread, tools, and event queue.
Both sync and async APIs use the same backend.

```text
Application → Agent / CodexAppServer → Backend → Transport → Codex
                                        ↓
                                  Python tool handlers
```

| Module | Owns |
| --- | --- |
| `agent.py` | Results, streams, parallel runs |
| `app_server.py` | Client setup, tools, skills, configuration |
| `_backend.py` | Threads, turns, routing, cancellation |
| `_transport.py` | JSON-RPC and process lifecycle |
| `_runtime.py` | Sync/async bridge |
| `_tools.py`, `tool_adapter.py` | Validation and tool execution |
| `_config.py`, `_helpers.py` | Integration settings and local state |

The transport reader never executes application callbacks. Async handlers run
on the application's loop for async calls; sync handlers use bounded workers.
Turn IDs keep concurrent responses separate. Cancellation waits for confirmation
before allowing another run on that thread.

Codex persists thread and skill data under the project's `.theseus/` directory.
Applications store thread IDs and rebuild their Python tool handlers on restart.
The public API is exported from `theseus/__init__.py`; underscored modules are
internal.

To measure local transport overhead without model calls:

```bash
python benchmarks/transport.py --runs 200
```

For end-to-end checks, see [live tests](live-verification.md).
