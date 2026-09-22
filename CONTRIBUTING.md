# Contributing

Keep Theseus focused on reusable agent infrastructure. Application prompts,
business tools, databases, and UI belong in the applications using it.

## Work locally

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
```

For fixes, add a regression test that demonstrates the failure, then make the
change. Test public behavior; use temporary state and fake protocol data.
Update the relevant docs and `CHANGELOG.md` when behavior changes.

The default suite makes no model calls. To run the live checks explicitly:

```bash
THESEUS_LIVE_TEST=1 THESEUS_LIVE_MODEL=gpt-6-luna \
  CODEX_AUTH_HOME="$HOME/.codex" THESEUS_LIVE_REPORT=/tmp/theseus-live-usage.jsonl \
  python -m unittest tests.test_live -v
```

Live tests use your login and consume model usage. The optional report appends
per-turn timing and token counts without conversation content.

Preserve public API behavior, per-agent tool selection, state isolation, and
concurrent event routing. Never commit credentials or `.theseus/`, or use real
runtime state as fixtures. Don't inspect or delete private state without approval.

See [AGENTS.md](AGENTS.md) for coding-agent instructions and
[RELEASING.md](RELEASING.md) for publishing. For changes made from a consuming
project, use an [editable submodule](docs/submodule-quickstart.md).
