"""Command-line entry points for building a topic and learning through chat."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from codeagent import CodexAppServer, final_text

from .build import (
    BuildRequest,
    TopicPaths,
    approve_candidate,
    archive_candidate,
    builder_prompt,
    load_topic,
    next_attempt_number,
    parse_builder_response,
    slugify,
    validate_candidate,
)
from .cli_debug import DebugRenderer
from .runtime import LearningRuntime


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    """Dispatch the learning example's build and chat commands."""

    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "build":
            return _build(arguments)
        if arguments.command == "chat":
            return _chat(arguments)
    except (KeyboardInterrupt, EOFError):
        print("\nStopped.")
        return 130
    except Exception as error:
        parser.exit(1, f"error: {error}\n")
    return 0


def _build(arguments: argparse.Namespace) -> int:
    debug = DebugRenderer(arguments.debug)
    source_material = None
    if arguments.source:
        source_material = Path(arguments.source).expanduser().read_text(encoding="utf-8")
    request = BuildRequest(
        goal=arguments.goal.strip(),
        audience=arguments.audience.strip(),
        depth=arguments.depth.strip(),
        source_material=source_material,
    )
    paths = TopicPaths(PROJECT_ROOT, slugify(request.goal))
    if paths.topic.exists():
        raise FileExistsError(
            f"topic '{paths.slug}' already exists; use chat instead of rebuilding it"
        )

    codex = CodexAppServer(PROJECT_ROOT, timeout=arguments.timeout)
    _configure_auth(codex, arguments.auth_home)
    paths.build_archive.mkdir(parents=True, exist_ok=True)
    with debug, codex:
        debug.phase("Build phase 1/5: starting the graph-builder agent")
        thread_id = codex.create_agent(
            sandbox="workspace-write",
            approval_policy="never",
            developer_instructions=(
                "You are a curriculum knowledge-graph research agent. Research deeply, "
                "obey the response schema exactly, use authoritative sources, and never "
                "pretend that a source supports a relationship it does not support."
            ),
        )
        feedback: str | None = None
        attempt = next_attempt_number(paths) - 1
        attempts_this_run = 0
        while True:
            attempt += 1
            attempts_this_run += 1
            debug.phase(
                f"Build phase 1/5: researching the graph (attempt {attempt})"
            )
            messages = codex.run(
                builder_prompt(request, feedback),
                thread_id,
                timeout=arguments.timeout,
                on_event=(
                    (lambda event: debug.model_event("builder", event))
                    if arguments.debug
                    else None
                ),
            )
            raw = final_text(messages) or ""
            try:
                debug.phase(
                    f"Build phase 2/5: validating graph and code (attempt {attempt})"
                )
                candidate = validate_candidate(parse_builder_response(raw))
                attempt_dir = archive_candidate(paths, candidate, attempt)
            except Exception as error:
                _archive_invalid(paths, attempt, raw, error)
                debug.note(f"Validation failed: {error}")
                if attempts_this_run >= 3:
                    raise RuntimeError(
                        f"builder failed validation three times; inspect {paths.build_archive}"
                    ) from error
                feedback = f"Automated validation failed: {error}. Correct every issue."
                continue

            debug.idle()
            _print_candidate(candidate, paths.slug, attempt)
            debug.note("Build phase 3/5: waiting for approval or revision feedback")
            decision = input(
                "Type 'approve' to promote this graph, or enter revision feedback: "
            ).strip()
            if decision.lower() in {"approve", "approved", "a"}:
                debug.phase("Build phase 4/5: promoting topic and starting role agents")
                approve_candidate(paths, attempt_dir, request, thread_id)
                runtime = LearningRuntime(
                    PROJECT_ROOT,
                    paths,
                    codex,
                    debug.phase if arguments.debug else None,
                    debug.model_event if arguments.debug else None,
                )
                runtime.prepare()
                runtime.ensure_active_plan()
                debug.note("Build phase 5/5: topic and initial plan are ready")
                print(
                    f"Approved '{paths.slug}'. Start with: python -m "
                    f"examples.learning_agent chat {paths.slug}"
                )
                return 0
            if not decision:
                print("Not approved; staged research was retained for review.")
                return 1
            feedback = decision
            attempts_this_run = 0


def _chat(arguments: argparse.Namespace) -> int:
    debug = DebugRenderer(arguments.debug)
    with debug:
        debug.phase("Loading and validating the approved topic")
        paths = load_topic(PROJECT_ROOT, arguments.topic)
        manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
        codex = CodexAppServer(PROJECT_ROOT, timeout=arguments.timeout)
        _configure_auth(codex, arguments.auth_home)
        with codex:
            runtime = LearningRuntime(
                PROJECT_ROOT,
                paths,
                codex,
                debug.phase if arguments.debug else None,
                debug.model_event if arguments.debug else None,
            )
            runtime.prepare()
            runtime.ensure_active_plan()
            debug.idle()
            print(f"Tutor ready: {manifest['goal']} (Ctrl-D to exit)")
            while True:
                try:
                    learner_message = input("You: ")
                except EOFError:
                    print()
                    return 0
                if not learner_message.strip():
                    continue
                try:
                    answer = runtime.tutor_turn(learner_message)
                finally:
                    debug.idle()
                print(f"Tutor: {answer}")


def _configure_auth(codex: CodexAppServer, auth_home: str | None) -> None:
    if os.environ.get("CODEX_ACCESS_TOKEN"):
        return
    target = codex.codex_home / "auth.json"
    if target.is_file():
        return
    codex.import_auth(auth_home or os.environ.get("CODEX_AUTH_HOME", "~/.codex"))


def _archive_invalid(paths: TopicPaths, attempt: int, raw: str, error: Exception) -> None:
    directory = paths.build_archive / f"attempt-{attempt:02d}"
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "raw-response.txt").write_text(raw, encoding="utf-8")
    (directory / "validation.json").write_text(
        json.dumps(
            {"valid": False, "error_type": type(error).__name__, "error": str(error)},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _print_candidate(candidate: dict[str, Any], slug: str, attempt: int) -> None:
    graph = candidate["graph"]
    chunks = sum(concept["kind"] == "chunk" for concept in graph["concepts"])
    print(f"\nBuild candidate '{slug}', attempt {attempt}")
    print(f"Concepts: {len(graph['concepts'])} ({chunks} chunks)")
    print(f"Goal node: {graph['goal_concept_id']}")
    print("Sources:")
    for source in candidate["sources"]:
        print(f"- {source['title']}: {source['url']}")
    print(f"Full research and validation: .learning-data/builds/{slug}/attempt-{attempt:02d}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m examples.learning_agent",
        description="Build and run a persistent prerequisite-graph tutor.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="research and approve a new topic graph")
    build.add_argument("--goal", required=True)
    build.add_argument("--audience", required=True)
    build.add_argument("--depth", required=True)
    build.add_argument("--source", help="optional UTF-8 source-material file")
    build.add_argument("--auth-home")
    build.add_argument("--timeout", type=float, default=600.0)
    build.add_argument(
        "--debug",
        action="store_true",
        help="show agent phases with an elapsed-time loading animation",
    )
    chat = subparsers.add_parser("chat", help="continue a learner conversation")
    chat.add_argument("topic", help="approved topic slug or original goal")
    chat.add_argument("--auth-home")
    chat.add_argument("--timeout", type=float, default=300.0)
    chat.add_argument(
        "--debug",
        action="store_true",
        help="show plan steps and hidden-agent phases with a loading animation",
    )
    return parser
