"""Knowledge-graph validation and deterministic route calculations."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from itertools import product
from typing import Any


CONCEPT_ID = re.compile(r"^[a-z][a-z0-9_]*$")
MASTERY_CUTOFF = 0.70


class GraphValidationError(ValueError):
    """Raised when a generated prerequisite graph violates the v1 contract."""


def validate_graph(
    graph: Mapping[str, Any], *, min_concepts: int = 1, max_concepts: int = 40
) -> dict[str, Any]:
    """Validate and normalize a v1 prerequisite graph.

    ``prerequisite_groups`` is an OR-of-ANDs: each inner list is one complete
    prerequisite route. ``components`` is always an AND relationship and
    represents explicit ``component_of`` edges into a larger chunk.
    """

    if graph.get("schema_version") != 1:
        raise GraphValidationError("schema_version must be 1")
    concepts_value = graph.get("concepts")
    if not isinstance(concepts_value, list):
        raise GraphValidationError("concepts must be a list")
    if not min_concepts <= len(concepts_value) <= max_concepts:
        raise GraphValidationError(
            f"graph must contain between {min_concepts} and {max_concepts} concepts"
        )

    goal = graph.get("goal")
    if not isinstance(goal, dict):
        raise GraphValidationError("goal must be an object")
    for field in ("title", "audience", "depth"):
        if not isinstance(goal.get(field), str) or not goal[field].strip():
            raise GraphValidationError(f"goal.{field} must be a non-empty string")

    concepts: dict[str, dict[str, Any]] = {}
    for raw in concepts_value:
        if not isinstance(raw, dict):
            raise GraphValidationError("each concept must be an object")
        concept_id = raw.get("id")
        if not isinstance(concept_id, str) or not CONCEPT_ID.fullmatch(concept_id):
            raise GraphValidationError(f"invalid concept id: {concept_id!r}")
        if concept_id in concepts:
            raise GraphValidationError(f"duplicate concept id: {concept_id}")
        title = raw.get("title")
        description = raw.get("description")
        kind = raw.get("kind")
        criteria = raw.get("mastery_criteria")
        components = raw.get("components", [])
        groups = raw.get("prerequisite_groups", [])
        if not isinstance(title, str) or not title.strip():
            raise GraphValidationError(f"concept {concept_id} needs a title")
        if not isinstance(description, str) or not description.strip():
            raise GraphValidationError(f"concept {concept_id} needs a description")
        if kind not in {"atomic", "chunk"}:
            raise GraphValidationError(f"concept {concept_id} has invalid kind")
        if not _string_list(criteria):
            raise GraphValidationError(
                f"concept {concept_id} needs measurable mastery criteria"
            )
        if not _string_list(components, allow_empty=True):
            raise GraphValidationError(f"concept {concept_id} has invalid components")
        if kind == "atomic" and components:
            raise GraphValidationError(f"atomic concept {concept_id} cannot have components")
        if kind == "chunk" and not components:
            raise GraphValidationError(f"chunk {concept_id} must have components")
        if not isinstance(groups, list) or any(
            not _string_list(group) for group in groups
        ):
            raise GraphValidationError(
                f"concept {concept_id} has invalid prerequisite_groups"
            )
        concepts[concept_id] = {
            "id": concept_id,
            "title": title.strip(),
            "description": description.strip(),
            "kind": kind,
            "mastery_criteria": [criterion.strip() for criterion in criteria],
            "components": list(dict.fromkeys(components)),
            "prerequisite_groups": [list(dict.fromkeys(group)) for group in groups],
        }

    goal_id = graph.get("goal_concept_id")
    if goal_id not in concepts:
        raise GraphValidationError("goal_concept_id must reference a concept")
    for concept in concepts.values():
        references = concept["components"] + [
            item for group in concept["prerequisite_groups"] for item in group
        ]
        for reference in references:
            if reference not in concepts:
                raise GraphValidationError(
                    f"concept {concept['id']} references unknown concept {reference}"
                )
            if reference == concept["id"]:
                raise GraphValidationError(f"concept {reference} references itself")

    _reject_cycles(concepts)
    _require_connected(concepts, goal_id)
    return {
        "schema_version": 1,
        "goal": {field: goal[field].strip() for field in ("title", "audience", "depth")},
        "goal_concept_id": goal_id,
        "concepts": list(concepts.values()),
    }


def relationships(graph: Mapping[str, Any]) -> list[dict[str, str]]:
    """Return explicit edge records for clients that consume graph data."""

    edges: list[dict[str, str]] = []
    for concept in graph["concepts"]:
        for component in concept.get("components", []):
            edges.append(
                {"type": "component_of", "source": component, "target": concept["id"]}
            )
        for group_index, group in enumerate(concept.get("prerequisite_groups", [])):
            for prerequisite in group:
                edges.append(
                    {
                        "type": "prerequisite_of",
                        "source": prerequisite,
                        "target": concept["id"],
                        "group": str(group_index),
                    }
                )
    return edges


def prerequisite_routes(
    graph: Mapping[str, Any], concept_id: str
) -> list[frozenset[str]]:
    """Expand every viable route into a deduplicated set of atomic concepts."""

    concepts = {concept["id"]: concept for concept in graph["concepts"]}
    if concept_id not in concepts:
        raise KeyError(f"unknown concept: {concept_id}")
    memo: dict[tuple[str, bool], list[frozenset[str]]] = {}

    def expand(current_id: str, include_atomic: bool) -> list[frozenset[str]]:
        key = (current_id, include_atomic)
        if key in memo:
            return memo[key]
        current = concepts[current_id]
        groups = current.get("prerequisite_groups", []) or [[]]
        base_dependencies = list(current.get("components", []))
        routes: list[frozenset[str]] = []
        for group in groups:
            dependencies = base_dependencies + list(group)
            choices = [expand(item, True) for item in dependencies]
            combinations = product(*choices) if choices else [()]
            for combination in combinations:
                atoms: set[str] = set()
                for child_route in combination:
                    atoms.update(child_route)
                if include_atomic and current["kind"] == "atomic":
                    atoms.add(current_id)
                routes.append(frozenset(atoms))
        result = _deduplicate_routes(routes)
        memo[key] = result
        return result

    current = concepts[concept_id]
    if not current.get("components") and not current.get("prerequisite_groups"):
        return []
    return expand(concept_id, False)


def readiness(
    graph: Mapping[str, Any], concept_id: str, effective_mastery: Mapping[str, float]
) -> dict[str, Any]:
    """Calculate shortest-route readiness and the currently available frontier."""

    routes = prerequisite_routes(graph, concept_id)
    if not routes:
        return {"value": 1.0, "route": [], "route_cost": 0.0, "frontier": []}

    ranked = sorted(
        (
            (
                sum(1.0 - _clamp(effective_mastery.get(item, 0.0)) for item in route),
                sorted(route),
            )
            for route in routes
        ),
        key=lambda item: (item[0], item[1]),
    )
    cost, route = ranked[0]
    if all(effective_mastery.get(atom, 0.0) >= MASTERY_CUTOFF for atom in route):
        frontier = (
            [concept_id]
            if effective_mastery.get(concept_id, 0.0) < MASTERY_CUTOFF
            else []
        )
        return {
            "value": 1.0 / (1.0 + cost),
            "route": route,
            "route_cost": cost,
            "frontier": frontier,
        }
    frontier: list[str] = []
    for atom in route:
        if effective_mastery.get(atom, 0.0) >= MASTERY_CUTOFF:
            continue
        own_routes = prerequisite_routes(graph, atom)
        if not own_routes:
            frontier.append(atom)
            continue
        own_cost, own_route = min(
            (
                (
                    sum(
                        1.0 - _clamp(effective_mastery.get(item, 0.0))
                        for item in candidate
                    ),
                    candidate,
                )
                for candidate in own_routes
            )
            ,
            key=lambda item: (item[0], sorted(item[1])),
        )
        del own_cost
        if all(effective_mastery.get(item, 0.0) >= MASTERY_CUTOFF for item in own_route):
            frontier.append(atom)
    return {
        "value": 1.0 / (1.0 + cost),
        "route": route,
        "route_cost": cost,
        "frontier": frontier,
    }


def beta_metrics(alpha: float, beta: float) -> dict[str, float]:
    """Return the probability, uncertainty, and conservative mastery estimate."""

    probability = alpha / (alpha + beta)
    uncertainty = math.sqrt(
        (alpha * beta) / (((alpha + beta) ** 2) * (alpha + beta + 1.0))
    )
    return {
        "probability": probability,
        "uncertainty": uncertainty,
        "effective_mastery": max(0.0, probability - 1.645 * uncertainty),
    }


def _string_list(value: Any, *, allow_empty: bool = False) -> bool:
    return (
        isinstance(value, list)
        and (allow_empty or bool(value))
        and all(isinstance(item, str) and bool(item.strip()) for item in value)
    )


def _reject_cycles(concepts: Mapping[str, Mapping[str, Any]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(concept_id: str) -> None:
        if concept_id in visiting:
            raise GraphValidationError(f"dependency cycle includes {concept_id}")
        if concept_id in visited:
            return
        visiting.add(concept_id)
        concept = concepts[concept_id]
        dependencies = list(concept["components"]) + [
            item for group in concept["prerequisite_groups"] for item in group
        ]
        for dependency in dependencies:
            visit(dependency)
        visiting.remove(concept_id)
        visited.add(concept_id)

    for concept_id in concepts:
        visit(concept_id)


def _require_connected(concepts: Mapping[str, Mapping[str, Any]], goal_id: str) -> None:
    neighbors: dict[str, set[str]] = {concept_id: set() for concept_id in concepts}
    for concept in concepts.values():
        dependencies = list(concept["components"]) + [
            item for group in concept["prerequisite_groups"] for item in group
        ]
        for dependency in dependencies:
            neighbors[concept["id"]].add(dependency)
            neighbors[dependency].add(concept["id"])
    reached = {goal_id}
    stack = [goal_id]
    while stack:
        current = stack.pop()
        for neighbor in neighbors[current] - reached:
            reached.add(neighbor)
            stack.append(neighbor)
    if reached != set(concepts):
        missing = ", ".join(sorted(set(concepts) - reached))
        raise GraphValidationError(f"concepts disconnected from goal: {missing}")


def _deduplicate_routes(routes: Sequence[frozenset[str]]) -> list[frozenset[str]]:
    return list(dict.fromkeys(routes))


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
