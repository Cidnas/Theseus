"""Research-agent contract, generated-package validation, and approval staging."""

from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from .database import LearningStore
from .graph import validate_graph
from .tools import TOOL_NAMES


GENERATED_TOOLS_SOURCE = """\
from examples.learning_agent.tools import make_tools

TOOLS_SCHEMA_VERSION = 1


def bind_tools(database_path: str):
    return make_tools(database_path)
"""


@dataclass(frozen=True, slots=True)
class BuildRequest:
    goal: str
    audience: str
    depth: str
    source_material: str | None = None


@dataclass(frozen=True, slots=True)
class TopicPaths:
    root: Path
    slug: str

    @property
    def build_archive(self) -> Path:
        return self.root / ".learning-data" / "builds" / self.slug

    @property
    def topic(self) -> Path:
        return self.root / ".learning-data" / "topics" / self.slug

    @property
    def graph(self) -> Path:
        return self.topic / "graph.json"

    @property
    def manifest(self) -> Path:
        return self.topic / "manifest.json"

    @property
    def tools(self) -> Path:
        return self.topic / "tools.py"

    @property
    def database(self) -> Path:
        return self.topic / "learning.db"


def slugify(value: str) -> str:
    """Create a stable, human-readable topic identifier."""

    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:60]
    if not slug:
        raise ValueError("goal must contain at least one letter or number")
    return slug


def next_attempt_number(paths: TopicPaths) -> int:
    """Return a collision-free build attempt number after interruption/restart."""

    numbers = []
    if paths.build_archive.is_dir():
        for child in paths.build_archive.iterdir():
            match = re.fullmatch(r"attempt-(\d+)", child.name)
            if child.is_dir() and match:
                numbers.append(int(match.group(1)))
    return max(numbers, default=0) + 1


def builder_prompt(request: BuildRequest, feedback: str | None = None) -> str:
    """Return the strict prompt for the graph-building research agent."""

    source_section = (
        f"\nUSER-SUPPLIED SOURCE MATERIAL:\n{request.source_material}\n"
        if request.source_material
        else ""
    )
    feedback_section = f"\nOPERATOR FEEDBACK TO ADDRESS:\n{feedback}\n" if feedback else ""
    return f"""
Research and model the prerequisite structure for this learning goal.

GOAL: {request.goal}
AUDIENCE: {request.audience}
DEPTH: {request.depth}
{source_section}{feedback_section}
Use web research and pedagogically credible primary or authoritative sources where
possible. Reconcile disagreements instead of copying a single curriculum. Return
ONLY one JSON object with keys: research_markdown, sources, graph, tools_py.

Requirements:
- research_markdown: at least 600 characters explaining scope, decomposition,
  prerequisite choices, alternative routes, and source-driven decisions.
- sources: at least 3 objects with title, url, and relevance.
- graph: schema_version 1, goal object (title, audience, depth),
  goal_concept_id, and 15-40 concepts.
- Every concept has id (lower_snake_case), title, description, kind
  (atomic or chunk), mastery_criteria, components, and
  prerequisite_groups.
- mastery_criteria must be a non-empty JSON array of strings, for example:
  "mastery_criteria": ["Correctly classify five of six examples and explain why."]
  Never use a field named measurable_mastery_criteria.
- components are all required and express component_of relationships. An atomic
  concept has none; a chunk has at least one.
- prerequisite_groups is an OR-of-ANDs: each inner list is a complete route, and
  multiple inner lists are alternatives.
- The graph must be acyclic and every concept must connect to goal_concept_id.
- Keep atoms assessable through learner explanations, examples, or problem solving.
- tools_py must be EXACTLY this Python source, encoded as a JSON string:

{GENERATED_TOOLS_SOURCE}
""".strip()


def builder_repair_prompt(
    validation_error: str, *, previous_artifact: dict[str, Any] | None = None
) -> str:
    """Ask the same builder thread for a minimal repair, not fresh research."""

    artifact = (
        "\nThe prior artifact is included because this is a resumed CLI process:\n"
        + json.dumps(previous_artifact, ensure_ascii=False)
        if previous_artifact is not None
        else "\nUse the complete artifact from your immediately preceding response."
    )
    return f"""
Revise the previous JSON artifact; do not restart research or redesign the graph.
Preserve every valid source, research conclusion, concept, edge, and identifier.
Apply the smallest correction that fixes this validation failure, then check every
concept for the same schema mistake. Return the complete corrected JSON object and
nothing else.

VALIDATION FAILURE:
{validation_error}
{artifact}
""".strip()


def parse_builder_response(text: str) -> dict[str, Any]:
    """Parse a builder response, tolerating one surrounding Markdown fence."""

    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            value = "\n".join(lines[1:-1])
            if value.lstrip().startswith("json\n"):
                value = value.lstrip()[5:]
    try:
        candidate = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("builder did not return one valid JSON object") from error
    if not isinstance(candidate, dict):
        raise ValueError("builder response must be a JSON object")
    return candidate


def validate_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    """Validate research depth, sources, graph shape, and generated code."""

    candidate = normalize_candidate(candidate)
    research = candidate.get("research_markdown")
    sources = candidate.get("sources")
    tools_source = candidate.get("tools_py")
    if not isinstance(research, str) or len(research.strip()) < 600:
        raise ValueError("research_markdown must contain at least 600 characters")
    if not isinstance(sources, list) or len(sources) < 3:
        raise ValueError("at least three research sources are required")
    for source in sources:
        if not isinstance(source, dict) or any(
            not isinstance(source.get(field), str) or not source[field].strip()
            for field in ("title", "url", "relevance")
        ):
            raise ValueError("each source needs title, url, and relevance")
        if not source["url"].startswith(("https://", "http://")):
            raise ValueError("source URLs must use http or https")
    graph = validate_graph(candidate.get("graph", {}), min_concepts=15, max_concepts=40)
    validate_generated_tools_source(tools_source)
    return {
        "research_markdown": research.strip(),
        "sources": sources,
        "graph": graph,
        "tools_py": tools_source,
    }


def normalize_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    """Canonically repair unambiguous, representation-only model mistakes."""

    normalized = copy.deepcopy(candidate)
    graph = normalized.get("graph")
    if not isinstance(graph, dict) or not isinstance(graph.get("concepts"), list):
        return normalized
    for concept in graph["concepts"]:
        if not isinstance(concept, dict):
            continue
        criteria = concept.get("mastery_criteria")
        if criteria is None and "measurable_mastery_criteria" in concept:
            criteria = concept.pop("measurable_mastery_criteria")
        if isinstance(criteria, str) and criteria.strip():
            concept["mastery_criteria"] = [criteria.strip()]
    return normalized


def recover_latest_candidate(paths: TopicPaths) -> dict[str, Any] | None:
    """Revalidate saved raw output so a restarted CLI can avoid new research."""

    if not paths.build_archive.is_dir():
        return None
    attempts = sorted(paths.build_archive.glob("attempt-*"), reverse=True)
    for attempt in attempts:
        raw_path = attempt / "raw-response.txt"
        if not raw_path.is_file():
            continue
        try:
            return validate_candidate(
                parse_builder_response(raw_path.read_text(encoding="utf-8"))
            )
        except (ValueError, GraphValidationError):
            continue
    return None


def validate_generated_tools_source(source: Any) -> None:
    """Accept only the inert, audited adapter template before importing it."""

    if not isinstance(source, str):
        raise ValueError("tools_py must be a string")
    try:
        ast.parse(source)
    except SyntaxError as error:
        raise ValueError("tools_py is not valid Python") from error
    if source.strip() != GENERATED_TOOLS_SOURCE.strip():
        raise ValueError("tools_py does not match the fixed audited tool adapter")


def archive_candidate(
    paths: TopicPaths, candidate: dict[str, Any], attempt: int
) -> Path:
    """Preserve a complete builder attempt for development observability."""

    attempt_dir = paths.build_archive / f"attempt-{attempt:02d}"
    if attempt_dir.exists():
        raise FileExistsError(f"build attempt already exists: {attempt_dir}")
    attempt_dir.mkdir(parents=True, exist_ok=False)
    (attempt_dir / "research.md").write_text(
        candidate["research_markdown"] + "\n", encoding="utf-8"
    )
    _write_json(attempt_dir / "sources.json", candidate["sources"])
    _write_json(attempt_dir / "graph.json", candidate["graph"])
    (attempt_dir / "tools.py").write_text(candidate["tools_py"], encoding="utf-8")
    validation = validate_generated_package(attempt_dir)
    _write_json(attempt_dir / "validation.json", validation)
    return attempt_dir


def validate_generated_package(directory: Path) -> dict[str, Any]:
    """Validate the staged adapter again in an isolated Python subprocess."""

    tools_path = directory / "tools.py"
    validate_generated_tools_source(tools_path.read_text(encoding="utf-8"))
    project_root = Path(__file__).resolve().parents[2]
    probe = (
        "import importlib.util,json,sys;"
        f"sys.path.insert(0,{str(project_root)!r});"
        f"p={str(tools_path)!r};"
        "s=importlib.util.spec_from_file_location('generated_learning_tools',p);"
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
        "fs=m.bind_tools('/tmp/codeagent-contract-probe.db');"
        "print(json.dumps([f.__name__ for f in fs]))"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        cwd=project_root,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(f"generated tool subprocess failed: {completed.stderr.strip()}")
    names = json.loads(completed.stdout)
    if tuple(names) != TOOL_NAMES:
        raise ValueError("generated adapter returned the wrong tool contract")
    return {
        "safe_adapter": True,
        "tool_names": names,
        "subprocess_returncode": completed.returncode,
    }


def approve_candidate(
    paths: TopicPaths,
    attempt_dir: Path,
    request: BuildRequest,
    builder_thread_id: str,
) -> TopicPaths:
    """Promote one validated attempt into an immutable runtime topic package."""

    if paths.topic.exists():
        raise FileExistsError(f"topic already exists: {paths.slug}")
    paths.topic.mkdir(parents=True, exist_ok=False)
    shutil.copy2(attempt_dir / "graph.json", paths.graph)
    shutil.copy2(attempt_dir / "tools.py", paths.tools)
    graph = json.loads(paths.graph.read_text(encoding="utf-8"))
    manifest = {
        "schema_version": 1,
        "slug": paths.slug,
        "goal": request.goal,
        "audience": request.audience,
        "depth": request.depth,
        "builder_thread_id": builder_thread_id,
        "graph_sha256": hashlib.sha256(paths.graph.read_bytes()).hexdigest(),
        "tools_sha256": hashlib.sha256(paths.tools.read_bytes()).hexdigest(),
        "approved_attempt": attempt_dir.name,
    }
    _write_json(paths.manifest, manifest)
    LearningStore(paths.database).initialize(
        graph, manifest=manifest, builder_thread_id=builder_thread_id
    )
    return paths


def load_generated_tools(paths: TopicPaths) -> tuple[Any, ...]:
    """Revalidate, import, and bind an approved topic's generated adapter."""

    validate_generated_package(paths.topic)
    spec = importlib.util.spec_from_file_location(
        f"learning_topic_{paths.slug.replace('-', '_')}", paths.tools
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import generated tools: {paths.tools}")
    module = importlib.util.module_from_spec(spec)
    _execute_module(spec.loader, module)
    functions = tuple(module.bind_tools(str(paths.database)))
    if tuple(function.__name__ for function in functions) != TOOL_NAMES:
        raise RuntimeError("approved topic has an invalid tool contract")
    return functions


def load_topic(project_root: Path, slug_or_goal: str) -> TopicPaths:
    """Resolve and integrity-check an approved topic package."""

    slug = slug_or_goal if (project_root / ".learning-data/topics" / slug_or_goal).is_dir() else slugify(slug_or_goal)
    paths = TopicPaths(project_root.resolve(), slug)
    if not all(path.is_file() for path in (paths.graph, paths.manifest, paths.tools, paths.database)):
        raise FileNotFoundError(f"approved topic not found: {slug}")
    manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
    if hashlib.sha256(paths.graph.read_bytes()).hexdigest() != manifest["graph_sha256"]:
        raise ValueError("approved graph failed its integrity check")
    if hashlib.sha256(paths.tools.read_bytes()).hexdigest() != manifest["tools_sha256"]:
        raise ValueError("approved tool adapter failed its integrity check")
    return paths


def _execute_module(loader: Any, module: ModuleType) -> None:
    loader.exec_module(module)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
