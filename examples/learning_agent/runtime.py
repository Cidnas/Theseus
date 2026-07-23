"""Four-agent orchestration over the deterministic learning state engine."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from codeagent import CodexAppServer, CodexProtocolError, final_text, register_tools

from .build import TopicPaths, load_generated_tools
from .database import LearningStore
from .tools import EVALUATOR_TOOLS, PLANNER_TOOLS, TOOL_NAMES, TUTOR_TOOLS


ROLE_TOOLS = {
    "tutor": TUTOR_TOOLS,
    "evaluator": EVALUATOR_TOOLS,
    "planner": PLANNER_TOOLS,
}

ROLE_INSTRUCTIONS = {
    "tutor": """
You are the learner-facing tutor. The learner sees only your final conversational
reply, never internal state or other agents. The host supplies the current active
learning step as trusted application context on every turn. Use that compact
context and the conversation itself; do not research, inspect files, or seek the
full graph, plan, or learner model.

Teach naturally, adapt to the learner's latest response, and ask useful questions.
A step may take unlimited conversational turns. Return the learner-facing reply
plus zero or more evidence records using the required output schema. Record only
criterion-relevant signals from the learner's latest message, linked to the
concept actually evidenced. Evidence must quote or precisely summarize what the
learner did; do not grade it. A candid inability to answer may be diagnostically
useful, but routine acknowledgments and conversational filler are not evidence.

Return a completed checkpoint only when the active step's observable completion
criteria and evidence requirements appear met across the conversation. Return a
blocked checkpoint only when the route is genuinely unsuitable or the learner
cannot progress, not merely because more teaching is needed. Do not begin a new
step in the same turn; the host will supply it on the next turn after validation.
The host and evaluator own persistence, grading, probabilities, and transitions.
Never mention schemas, agents, probabilities, hidden plans, or evidence machinery
in the learner-facing reply.
""",
    "evaluator": """
You are a strict evidence evaluator, not a tutor or planner. Use
list_pending_mastery_evidence and assess every pending record exactly once against
the linked concept's mastery criteria. You may choose only:
- judgment: demonstrated, partial, contradicted, uninformative
- quality: direct, indirect, self_report

Direct means the learner actually explained, solved, produced, or applied
something. Indirect is a weaker behavioral inference. A claim about one's own
knowledge is self_report. Provide a concise criterion-grounded rationale. Never
invent numeric updates; deterministic host code owns probabilities.
""",
    "planner": """
You are the learning planner. Inspect the graph, learner model, and previous plan.
Choose the shortest viable prerequisite route exposed by readiness and restrict
focus to its available frontier. Build a roadmap plus one plan of 3-5 ordered
steps. A step is an objective and teaching strategy, never a scripted turn count;
it may take unlimited conversation turns.

Call save_learning_plan exactly once with the current state_revision and a JSON
object shaped as:
{
  "goal_id": "graph goal_concept_id",
  "roadmap": "short rationale and direction",
  "route": ["concept_id"],
  "steps": [{
    "step_id": "stable_short_id",
    "concept_ids": ["concept_id"],
    "objective": "observable learning objective",
    "mode": "diagnose|teach|practice|integrate",
    "strategy": "adaptive instructional approach",
    "probes": ["suggested elicitation or practice probe"],
    "completion_criteria": ["observable criterion"]
  }]
}
Do not include statuses or numeric mastery updates; the host owns them. Every
step needs at least one probe and completion criterion. Return a short internal
confirmation after the tool succeeds.
""",
}


class LearningRuntime:
    """Create/resume role agents and drive hidden checkpoint transitions."""

    def __init__(
        self,
        project_root: str | Path,
        topic: TopicPaths,
        codex: CodexAppServer,
        on_progress: Callable[[str], None] | None = None,
        on_model_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.topic = topic
        self.codex = codex
        self.store = LearningStore(topic.database)
        self.on_progress = on_progress
        self.on_model_event = on_model_event
        self.threads: dict[str, str] = {}
        self._registered = False

    def prepare(self) -> None:
        """Register generated tools and restore or create all runtime agents."""

        if self._registered:
            return
        self._report("Validating the approved topic and registering its tools")
        tool_names = register_tools(self.codex, load_generated_tools(self.topic))
        if set(tool_names) != set(TOOL_NAMES):
            raise RuntimeError("generated topic did not register the eight-tool contract")
        sessions = self.store.get_agent_sessions()
        self._report("Restoring persisted builder, tutor, evaluator, and planner threads")
        builder = sessions.get("builder")
        if builder:
            self.codex.resume_agent(
                builder["thread_id"], tools=builder["tools"], skills=builder["skills"]
            )
            self.threads["builder"] = builder["thread_id"]
        for role, selected in ROLE_TOOLS.items():
            saved = sessions.get(role)
            if saved:
                if tuple(saved["tools"]) != tuple(selected):
                    if role != "tutor":
                        raise RuntimeError(f"persisted {role} capability set is invalid")
                    self._report("Upgrading the saved tutor to the fast turn protocol")
                    thread_id = self._create_role_agent(role)
                else:
                    thread_id = self.codex.resume_agent(
                        saved["thread_id"], tools=selected, skills=saved["skills"]
                    )
            else:
                thread_id = self._create_role_agent(role)
            self.threads[role] = thread_id
        self._registered = True
        self._report("All agent threads are ready")

    def ensure_active_plan(self) -> dict[str, Any]:
        """Create the initial plan or refresh a completed/blocked plan."""

        self.prepare()
        plan = self.store.get_learning_plan()
        if plan is None:
            return self.run_planner("Create the initial learning plan.")
        if plan["status"] in {"completed", "blocked"}:
            return self.run_planner(
                f"Replace plan {plan['plan_id']} because it is {plan['status']}."
            )
        self._report_active_step(plan)
        return plan

    def tutor_turn(self, learner_message: str) -> str:
        """Run one unrestricted tutor turn, then process any hidden checkpoint."""

        if not learner_message.strip():
            raise ValueError("learner message cannot be empty")
        plan = self.ensure_active_plan()
        active = next(step for step in plan["steps"] if step["status"] == "active")
        self._report(f"Tutor model is working on step {active['step_id']}")
        messages = self._run_role(
            "tutor",
            learner_message,
            output_schema=_tutor_turn_schema(active["concept_ids"]),
            additional_context={
                "active_learning_step": self._tutor_context(plan, active)
            },
        )
        turn = _parse_tutor_turn(messages)
        persisted = self.store.record_tutor_turn(
            plan["plan_id"],
            active["step_id"],
            turn["evidence"],
            turn["checkpoint"],
        )
        evidence_count = len(persisted["evidence_ids"])
        if evidence_count:
            self._report(f"Recorded {evidence_count} learner evidence signal(s)")
        if persisted["checkpoint_id"] is not None:
            self._report(f"Tutor requested a checkpoint for step {active['step_id']}")
        self.process_checkpoints()
        return turn["reply"]

    def process_checkpoints(self) -> list[dict[str, Any]]:
        """Evaluate checkpoint evidence and replan only at allowed boundaries."""

        outcomes: list[dict[str, Any]] = []
        for checkpoint in self.store.pending_checkpoints():
            self._report(
                f"Evaluator is reviewing evidence for step {checkpoint['step_id']}"
            )
            self.run_evaluator(checkpoint)
            self._report(f"Applying the evidence gate for step {checkpoint['step_id']}")
            outcome = self.store.resolve_checkpoint(checkpoint["id"])
            if outcome["status"] == "waiting_for_evaluator":
                raise RuntimeError("evaluator left checkpoint evidence unassessed")
            outcomes.append(outcome)
            self._report(f"Checkpoint result: {outcome['status'].replace('_', ' ')}")
            if outcome["status"] in {"plan_completed", "blocked"}:
                self.run_planner(
                    "Create a new plan after " + outcome["status"].replace("_", " ") + "."
                )
            else:
                current = self.store.get_learning_plan()
                if current is not None:
                    self._report_active_step(current)
        return outcomes

    def run_evaluator(self, checkpoint: dict[str, Any]) -> None:
        """Ask the evaluator to exhaust the pending evidence queue."""

        self.prepare()
        pending = self.store.list_pending_mastery_evidence()
        if not pending:
            return
        prompt = (
            f"Evaluate all pending evidence for checkpoint {checkpoint['id']} "
            f"on plan {checkpoint['plan_id']} step {checkpoint['step_id']}. "
            "Call the assessment tool once for every record, including records "
            "that are uninformative. Do not stop while any record remains pending."
        )
        self._run_role("evaluator", prompt)
        remaining = self.store.list_pending_mastery_evidence()
        if remaining:
            raise RuntimeError(
                "evaluator did not assess evidence IDs: "
                + ", ".join(str(item["id"]) for item in remaining)
            )

    def run_planner(self, reason: str) -> dict[str, Any]:
        """Run the planner and verify that it persisted a fresh active plan."""

        self.prepare()
        revision = self.store.get_learner_model()["state_revision"]
        previous = self.store.get_learning_plan()
        previous_id = previous["plan_id"] if previous else 0
        self._report("Planner model is creating the next 3-5-step learning plan")
        prompt = (
            f"{reason} Current learner state_revision is {revision}. Inspect all "
            "available state with your tools, then persist the new plan."
        )
        self._run_role("planner", prompt)
        plan = self.store.get_learning_plan()
        if (
            plan is None
            or plan["plan_id"] <= previous_id
            or plan["status"] != "active"
            or plan["based_on_state_revision"] != revision
        ):
            raise RuntimeError("planner did not persist a fresh plan")
        self._report_active_step(plan)
        return plan

    def _create_role_agent(self, role: str) -> str:
        selected = ROLE_TOOLS[role]
        thread_id = self.codex.create_agent(
            tools=selected,
            sandbox="read-only",
            approval_policy="never",
            developer_instructions=ROLE_INSTRUCTIONS[role].strip(),
        )
        self.store.save_agent_session(role, thread_id, list(selected))
        self.threads[role] = thread_id
        return thread_id

    def _run_role(
        self,
        role: str,
        prompt: str,
        *,
        output_schema: Mapping[str, Any] | None = None,
        additional_context: Mapping[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        try:
            return self.codex.run(
                prompt,
                self.threads[role],
                on_event=self._event_handler(role),
                output_schema=output_schema,
                additional_context=additional_context,
            )
        except CodexProtocolError as error:
            if "no rollout found for thread id" not in str(error).lower():
                raise
            old_thread_id = self.threads[role]
            self._report(
                f"Saved {role} thread is not resumable; creating a replacement"
            )
            new_thread_id = self._create_role_agent(role)
            self._report(
                f"Replaced {role} thread {old_thread_id} with {new_thread_id}"
            )
            return self.codex.run(
                prompt,
                new_thread_id,
                on_event=self._event_handler(role),
                output_schema=output_schema,
                additional_context=additional_context,
            )

    def _tutor_context(
        self, plan: dict[str, Any], active: dict[str, Any]
    ) -> str:
        step = {
            key: active[key]
            for key in (
                "step_id",
                "concept_ids",
                "objective",
                "mode",
                "strategy",
                "probes",
                "completion_criteria",
                "evidence_requirements",
            )
        }
        context = {
            "plan_id": plan["plan_id"],
            "step": step,
            "prior_evidence": self.store.get_step_evidence(
                plan["plan_id"], active["step_id"]
            ),
        }
        return json.dumps(context, separators=(",", ":"), ensure_ascii=False)

    def _report_active_step(self, plan: dict[str, Any]) -> None:
        active = next(
            (step for step in plan["steps"] if step["status"] == "active"),
            None,
        )
        if active is None:
            self._report(f"Plan {plan['plan_id']} has status {plan['status']}")
            return
        index = plan["steps"].index(active) + 1
        self._report(
            f"Plan {plan['plan_id']}, step {index}/{len(plan['steps'])}: "
            f"{active['step_id']} — {active['objective']}"
        )

    def _report(self, message: str) -> None:
        if self.on_progress is not None:
            self.on_progress(message)

    def _event_handler(self, role: str) -> Callable[[dict[str, Any]], None] | None:
        if self.on_model_event is None:
            return None
        return lambda event: self.on_model_event(role, event)


def _tutor_turn_schema(concept_ids: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "reply": {"type": "string", "minLength": 1},
            "evidence": {
                "type": "array",
                "maxItems": 6,
                "items": {
                    "type": "object",
                    "properties": {
                        "concept_id": {"type": "string", "enum": concept_ids},
                        "evidence": {"type": "string", "minLength": 1},
                        "elicitation_context": {"type": "string", "minLength": 1},
                    },
                    "required": [
                        "concept_id",
                        "evidence",
                        "elicitation_context",
                    ],
                    "additionalProperties": False,
                },
            },
            "checkpoint": {
                "anyOf": [
                    {"type": "null"},
                    {
                        "type": "object",
                        "properties": {
                            "status": {
                                "type": "string",
                                "enum": ["completed", "blocked"],
                            },
                            "summary": {"type": "string", "minLength": 1},
                        },
                        "required": ["status", "summary"],
                        "additionalProperties": False,
                    },
                ]
            },
        },
        "required": ["reply", "evidence", "checkpoint"],
        "additionalProperties": False,
    }


def _parse_tutor_turn(messages: list[dict[str, Any]]) -> dict[str, Any]:
    raw = final_text(messages)
    if raw is None:
        raise RuntimeError("tutor completed without a structured response")
    try:
        turn = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError("tutor returned invalid structured JSON") from error
    if not isinstance(turn, dict) or set(turn) != {"reply", "evidence", "checkpoint"}:
        raise RuntimeError("tutor structured response has an invalid shape")
    if not isinstance(turn["reply"], str) or not turn["reply"].strip():
        raise RuntimeError("tutor completed without a learner-facing reply")
    if not isinstance(turn["evidence"], list) or len(turn["evidence"]) > 6:
        raise RuntimeError("tutor structured response has invalid evidence")
    checkpoint = turn["checkpoint"]
    if checkpoint is not None and not isinstance(checkpoint, dict):
        raise RuntimeError("tutor structured response has an invalid checkpoint")
    turn["reply"] = turn["reply"].strip()
    return turn


def plan_as_pretty_json(plan: dict[str, Any]) -> str:
    """Small debugging helper intentionally unused by the learner-facing CLI."""

    return json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=False)
