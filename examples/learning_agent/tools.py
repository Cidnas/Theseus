"""The fixed, capability-scoped tool contract used by generated topics."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .database import LearningStore


TOOL_NAMES = (
    "get_knowledge_graph",
    "get_learner_model",
    "submit_mastery_evidence",
    "list_pending_mastery_evidence",
    "assess_mastery_evidence",
    "get_learning_plan",
    "save_learning_plan",
    "signal_plan_checkpoint",
)

TUTOR_TOOLS: tuple[str, ...] = ()
EVALUATOR_TOOLS = (
    "get_knowledge_graph",
    "get_learner_model",
    "list_pending_mastery_evidence",
    "assess_mastery_evidence",
)
PLANNER_TOOLS = (
    "get_knowledge_graph",
    "get_learner_model",
    "get_learning_plan",
    "save_learning_plan",
)


def make_tools(database_path: str | Path) -> tuple[Callable[..., Any], ...]:
    """Bind the eight stable tool functions to one topic database."""

    store = LearningStore(database_path)

    def get_knowledge_graph() -> dict[str, Any]:
        """Return concepts, mastery criteria, composition edges, and prerequisite routes."""

        return store.get_knowledge_graph()

    def get_learner_model() -> dict[str, Any]:
        """Return current concept beliefs, uncertainty, readiness, routes, and frontier."""

        return store.get_learner_model()

    def submit_mastery_evidence(
        concept_id: str, evidence: str, elicitation_context: str
    ) -> dict[str, Any]:
        """Record an observed learner signal for one concept without grading it."""

        return store.submit_mastery_evidence(concept_id, evidence, elicitation_context)

    def list_pending_mastery_evidence() -> list[dict[str, Any]]:
        """List unassessed learner signals together with the linked mastery criteria."""

        return store.list_pending_mastery_evidence()

    def assess_mastery_evidence(
        evidence_id: int, judgment: str, quality: str, rationale: str
    ) -> dict[str, Any]:
        """Classify one pending signal using the fixed judgment and quality categories."""

        return store.assess_mastery_evidence(evidence_id, judgment, quality, rationale)

    def get_learning_plan() -> dict[str, Any] | None:
        """Return the current learning roadmap, active step, and completion state."""

        return store.get_learning_plan()

    def save_learning_plan(plan_json: str, state_revision: int) -> dict[str, Any]:
        """Save a validated 3-5-step plan against an exact learner-state revision."""

        return store.save_learning_plan(plan_json, state_revision)

    def signal_plan_checkpoint(
        plan_id: int, step_id: str, status: str, summary: str
    ) -> dict[str, Any]:
        """Request evidence evaluation when the active step is complete or blocked."""

        return store.signal_plan_checkpoint(plan_id, step_id, status, summary)

    return (
        get_knowledge_graph,
        get_learner_model,
        submit_mastery_evidence,
        list_pending_mastery_evidence,
        assess_mastery_evidence,
        get_learning_plan,
        save_learning_plan,
        signal_plan_checkpoint,
    )
