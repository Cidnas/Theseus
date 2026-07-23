"""SQLite persistence and enforced learning-state transitions."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .graph import beta_metrics, readiness, relationships, validate_graph


JUDGMENTS = {"demonstrated", "partial", "contradicted", "uninformative"}
QUALITIES = {"direct", "indirect", "self_report"}
QUALITY_MULTIPLIERS = {"direct": 1.0, "indirect": 0.5, "self_report": 0.25}
JUDGMENT_DELTAS = {
    "demonstrated": (2.0, 0.0),
    "partial": (1.0, 1.0),
    "contradicted": (0.0, 2.0),
    "uninformative": (0.0, 0.0),
}


class StaleStateError(RuntimeError):
    """Raised when a planner writes against an obsolete learner revision."""


class LearningStore:
    """Persistent source of truth for one learner and one learning goal."""

    def __init__(self, database_path: str | Path) -> None:
        self.path = Path(database_path).expanduser().resolve()

    def initialize(
        self,
        graph: dict[str, Any],
        *,
        manifest: dict[str, Any] | None = None,
        builder_thread_id: str | None = None,
    ) -> None:
        """Create a new store, or verify that an existing store matches the graph."""

        normalized = validate_graph(graph)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mastery (
                    concept_id TEXT PRIMARY KEY,
                    alpha REAL NOT NULL CHECK (alpha > 0),
                    beta REAL NOT NULL CHECK (beta > 0)
                );
                CREATE TABLE IF NOT EXISTS evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    concept_id TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    elicitation_context TEXT NOT NULL,
                    plan_id INTEGER,
                    step_id TEXT,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'assessed')),
                    judgment TEXT,
                    quality TEXT,
                    rationale TEXT,
                    created_at TEXT NOT NULL,
                    assessed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    based_on_revision INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id INTEGER NOT NULL,
                    step_id TEXT NOT NULL,
                    requested_status TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                CREATE TABLE IF NOT EXISTS agent_sessions (
                    role TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    tools_json TEXT NOT NULL,
                    skills_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            existing = connection.execute(
                "SELECT value FROM metadata WHERE key = 'graph'"
            ).fetchone()
            graph_json = _json(normalized)
            if existing is not None and existing[0] != graph_json:
                raise ValueError("database already belongs to a different graph")
            connection.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES ('graph', ?)",
                (graph_json,),
            )
            connection.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES ('state_revision', '0')"
            )
            if manifest is not None:
                connection.execute(
                    "INSERT OR REPLACE INTO metadata(key, value) VALUES ('manifest', ?)",
                    (_json(manifest),),
                )
            for concept in normalized["concepts"]:
                connection.execute(
                    "INSERT OR IGNORE INTO mastery(concept_id, alpha, beta) VALUES (?, 1, 1)",
                    (concept["id"],),
                )
            if builder_thread_id:
                self._save_agent_session(
                    connection, "builder", builder_thread_id, [], []
                )

    def get_knowledge_graph(self) -> dict[str, Any]:
        """Return the normalized graph with explicit relationship records."""

        graph = self._metadata_json("graph")
        graph["relationships"] = relationships(graph)
        return graph

    def get_learner_model(self) -> dict[str, Any]:
        """Return beliefs, readiness, selected routes, and planning frontiers."""

        graph = self._metadata_json("graph")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT concept_id, alpha, beta FROM mastery ORDER BY concept_id"
            ).fetchall()
            revision = int(
                connection.execute(
                    "SELECT value FROM metadata WHERE key = 'state_revision'"
                ).fetchone()[0]
            )
        metrics: dict[str, dict[str, Any]] = {}
        effective: dict[str, float] = {}
        for row in rows:
            values = beta_metrics(row["alpha"], row["beta"])
            values.update({"alpha": row["alpha"], "beta": row["beta"]})
            metrics[row["concept_id"]] = values
            effective[row["concept_id"]] = values["effective_mastery"]
        for concept_id, values in metrics.items():
            values["readiness"] = readiness(graph, concept_id, effective)
        return {"state_revision": revision, "concepts": metrics}

    def submit_mastery_evidence(
        self, concept_id: str, evidence: str, elicitation_context: str
    ) -> dict[str, Any]:
        """Append pending evidence and associate it with the active plan step."""

        if not evidence.strip() or not elicitation_context.strip():
            raise ValueError("evidence and elicitation_context cannot be empty")
        with self._transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM mastery WHERE concept_id = ?", (concept_id,)
            ).fetchone() is None:
                raise KeyError(f"unknown concept: {concept_id}")
            plan_id, step_id = self._active_step(connection)
            cursor = connection.execute(
                """
                INSERT INTO evidence(
                    concept_id, evidence, elicitation_context, plan_id, step_id,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    concept_id,
                    evidence.strip(),
                    elicitation_context.strip(),
                    plan_id,
                    step_id,
                    _now(),
                ),
            )
            return {
                "evidence_id": cursor.lastrowid,
                "status": "pending",
                "plan_id": plan_id,
                "step_id": step_id,
            }

    def list_pending_mastery_evidence(self) -> list[dict[str, Any]]:
        """Return every unassessed evidence record with its concept criteria."""

        graph = self._metadata_json("graph")
        concepts = {concept["id"]: concept for concept in graph["concepts"]}
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM evidence WHERE status = 'pending' ORDER BY id"
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["mastery_criteria"] = concepts[row["concept_id"]]["mastery_criteria"]
            result.append(item)
        return result

    def assess_mastery_evidence(
        self, evidence_id: int, judgment: str, quality: str, rationale: str
    ) -> dict[str, Any]:
        """Assess evidence once and deterministically update its linked belief."""

        if judgment not in JUDGMENTS:
            raise ValueError(f"invalid judgment: {judgment}")
        if quality not in QUALITIES:
            raise ValueError(f"invalid quality: {quality}")
        if not rationale.strip():
            raise ValueError("rationale cannot be empty")
        with self._transaction() as connection:
            evidence_row = connection.execute(
                "SELECT * FROM evidence WHERE id = ?", (evidence_id,)
            ).fetchone()
            if evidence_row is None:
                raise KeyError(f"unknown evidence: {evidence_id}")
            if evidence_row["status"] != "pending":
                raise ValueError(f"evidence {evidence_id} has already been assessed")
            multiplier = QUALITY_MULTIPLIERS[quality]
            alpha_delta, beta_delta = JUDGMENT_DELTAS[judgment]
            connection.execute(
                """
                UPDATE mastery
                SET alpha = alpha + ?, beta = beta + ?
                WHERE concept_id = ?
                """,
                (
                    alpha_delta * multiplier,
                    beta_delta * multiplier,
                    evidence_row["concept_id"],
                ),
            )
            connection.execute(
                """
                UPDATE evidence
                SET status = 'assessed', judgment = ?, quality = ?, rationale = ?,
                    assessed_at = ?
                WHERE id = ?
                """,
                (judgment, quality, rationale.strip(), _now(), evidence_id),
            )
            revision = self._increment_revision(connection)
            belief = connection.execute(
                "SELECT alpha, beta FROM mastery WHERE concept_id = ?",
                (evidence_row["concept_id"],),
            ).fetchone()
        return {
            "evidence_id": evidence_id,
            "concept_id": evidence_row["concept_id"],
            "judgment": judgment,
            "quality": quality,
            "state_revision": revision,
            **dict(belief),
            **beta_metrics(belief["alpha"], belief["beta"]),
        }

    def get_learning_plan(self) -> dict[str, Any] | None:
        """Return the newest plan, including host-controlled IDs and status."""

        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM plans ORDER BY id DESC LIMIT 1").fetchone()
        return self._plan_from_row(row) if row is not None else None

    def save_learning_plan(self, plan_json: str, state_revision: int) -> dict[str, Any]:
        """Validate and save a new 3-5-step plan using optimistic concurrency."""

        try:
            raw_plan = json.loads(plan_json)
        except json.JSONDecodeError as error:
            raise ValueError("plan_json must contain valid JSON") from error
        graph = self._metadata_json("graph")
        plan = _validate_plan(raw_plan, graph)
        with self._transaction() as connection:
            current_revision = int(
                connection.execute(
                    "SELECT value FROM metadata WHERE key = 'state_revision'"
                ).fetchone()[0]
            )
            if state_revision != current_revision:
                raise StaleStateError(
                    f"plan used revision {state_revision}, current revision is {current_revision}"
                )
            connection.execute(
                "UPDATE plans SET status = 'superseded' WHERE status IN ('active', 'blocked')"
            )
            plan["steps"][0]["status"] = "active"
            for step in plan["steps"][1:]:
                step["status"] = "pending"
            cursor = connection.execute(
                """
                INSERT INTO plans(based_on_revision, status, plan_json, created_at)
                VALUES (?, 'active', ?, ?)
                """,
                (state_revision, _json(plan), _now()),
            )
            plan_id = int(cursor.lastrowid)
        return {
            "plan_id": plan_id,
            "status": "active",
            "based_on_state_revision": state_revision,
            **plan,
        }

    def signal_plan_checkpoint(
        self, plan_id: int, step_id: str, status: str, summary: str
    ) -> dict[str, Any]:
        """Request host evaluation for a completed or blocked active step."""

        if status not in {"completed", "blocked"}:
            raise ValueError("checkpoint status must be completed or blocked")
        if not summary.strip():
            raise ValueError("checkpoint summary cannot be empty")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM plans WHERE id = ?", (plan_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown plan: {plan_id}")
            if row["status"] != "active":
                raise ValueError("checkpoint plan is not active")
            plan = json.loads(row["plan_json"])
            step = next((item for item in plan["steps"] if item["step_id"] == step_id), None)
            if step is None or step["status"] != "active":
                raise ValueError("checkpoint step is not the active step")
            pending = connection.execute(
                "SELECT id FROM checkpoints WHERE plan_id = ? AND status = 'pending'",
                (plan_id,),
            ).fetchone()
            if pending is not None:
                raise ValueError("this plan already has a pending checkpoint")
            cursor = connection.execute(
                """
                INSERT INTO checkpoints(
                    plan_id, step_id, requested_status, summary, status, created_at
                ) VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (plan_id, step_id, status, summary.strip(), _now()),
            )
        return {"checkpoint_id": cursor.lastrowid, "status": "pending_evaluation"}

    def record_tutor_turn(
        self,
        plan_id: int,
        step_id: str,
        evidence: list[dict[str, str]],
        checkpoint: dict[str, str] | None,
    ) -> dict[str, Any]:
        """Atomically persist one structured tutor turn against the active step."""

        normalized_evidence: list[dict[str, str]] = []
        for item in evidence:
            if not isinstance(item, dict) or set(item) != {
                "concept_id",
                "evidence",
                "elicitation_context",
            }:
                raise ValueError("tutor evidence has an invalid shape")
            if not all(isinstance(value, str) and value.strip() for value in item.values()):
                raise ValueError("tutor evidence fields must be non-empty strings")
            normalized_evidence.append(
                {key: value.strip() for key, value in item.items()}
            )

        normalized_checkpoint: dict[str, str] | None = None
        if checkpoint is not None:
            if not isinstance(checkpoint, dict) or set(checkpoint) != {"status", "summary"}:
                raise ValueError("tutor checkpoint has an invalid shape")
            status = checkpoint.get("status")
            summary = checkpoint.get("summary")
            if not isinstance(status, str) or status not in {"completed", "blocked"}:
                raise ValueError("checkpoint status must be completed or blocked")
            if not isinstance(summary, str) or not summary.strip():
                raise ValueError("checkpoint summary cannot be empty")
            normalized_checkpoint = {"status": status, "summary": summary.strip()}

        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM plans WHERE id = ?", (plan_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown plan: {plan_id}")
            if row["status"] != "active":
                raise ValueError("tutor turn plan is not active")
            plan = json.loads(row["plan_json"])
            step = next(
                (item for item in plan["steps"] if item["step_id"] == step_id), None
            )
            if step is None or step["status"] != "active":
                raise ValueError("tutor turn step is not the active step")

            allowed_concepts = set(step["concept_ids"])
            created_at = _now()
            evidence_ids: list[int] = []
            for item in normalized_evidence:
                if item["concept_id"] not in allowed_concepts:
                    raise ValueError(
                        "tutor evidence concept is outside the active plan step"
                    )
                cursor = connection.execute(
                    """
                    INSERT INTO evidence(
                        concept_id, evidence, elicitation_context, plan_id, step_id,
                        status, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        item["concept_id"],
                        item["evidence"],
                        item["elicitation_context"],
                        plan_id,
                        step_id,
                        created_at,
                    ),
                )
                evidence_ids.append(int(cursor.lastrowid))

            checkpoint_id: int | None = None
            if normalized_checkpoint is not None:
                pending = connection.execute(
                    "SELECT id FROM checkpoints WHERE plan_id = ? AND status = 'pending'",
                    (plan_id,),
                ).fetchone()
                if pending is not None:
                    raise ValueError("this plan already has a pending checkpoint")
                cursor = connection.execute(
                    """
                    INSERT INTO checkpoints(
                        plan_id, step_id, requested_status, summary, status, created_at
                    ) VALUES (?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        plan_id,
                        step_id,
                        normalized_checkpoint["status"],
                        normalized_checkpoint["summary"],
                        created_at,
                    ),
                )
                checkpoint_id = int(cursor.lastrowid)

        return {"evidence_ids": evidence_ids, "checkpoint_id": checkpoint_id}

    def get_step_evidence(
        self, plan_id: int, step_id: str, *, limit: int = 12
    ) -> list[dict[str, Any]]:
        """Return compact recent evidence for host-provided tutor context."""

        if limit <= 0:
            raise ValueError("evidence limit must be positive")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT id, concept_id, evidence, status, judgment, quality, rationale
                FROM evidence WHERE plan_id = ? AND step_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (plan_id, step_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def pending_checkpoints(self) -> list[dict[str, Any]]:
        """Return host work items; this operation is intentionally not an agent tool."""

        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM checkpoints WHERE status = 'pending' ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    def resolve_checkpoint(self, checkpoint_id: int) -> dict[str, Any]:
        """Enforce the evidence gate and perform the next plan transition."""

        with self._transaction() as connection:
            checkpoint = connection.execute(
                "SELECT * FROM checkpoints WHERE id = ?", (checkpoint_id,)
            ).fetchone()
            if checkpoint is None:
                raise KeyError(f"unknown checkpoint: {checkpoint_id}")
            if checkpoint["status"] != "pending":
                raise ValueError("checkpoint has already been resolved")
            pending_count = connection.execute(
                """
                SELECT COUNT(*) FROM evidence
                WHERE plan_id = ? AND step_id = ? AND status = 'pending'
                """,
                (checkpoint["plan_id"], checkpoint["step_id"]),
            ).fetchone()[0]
            if pending_count:
                return {"status": "waiting_for_evaluator", "pending": pending_count}

            plan_row = connection.execute(
                "SELECT * FROM plans WHERE id = ?", (checkpoint["plan_id"],)
            ).fetchone()
            plan = json.loads(plan_row["plan_json"])
            step = next(
                item for item in plan["steps"] if item["step_id"] == checkpoint["step_id"]
            )
            if checkpoint["requested_status"] == "blocked":
                step["status"] = "blocked"
                plan_status = "blocked"
                outcome = "blocked"
            else:
                placeholders = ",".join("?" for _ in step["concept_ids"])
                counts = connection.execute(
                    f"""
                    SELECT COUNT(*) AS informative,
                           SUM(CASE WHEN quality = 'direct' THEN 1 ELSE 0 END) AS direct
                    FROM evidence
                    WHERE plan_id = ? AND step_id = ? AND status = 'assessed'
                      AND judgment != 'uninformative'
                      AND concept_id IN ({placeholders})
                    """,
                    (checkpoint["plan_id"], checkpoint["step_id"], *step["concept_ids"]),
                ).fetchone()
                informative = int(counts["informative"] or 0)
                direct = int(counts["direct"] or 0)
                if informative < 2 or direct < 1:
                    plan_status = "active"
                    outcome = "evidence_gate_failed"
                else:
                    step["status"] = "completed"
                    next_step = next(
                        (item for item in plan["steps"] if item["status"] == "pending"),
                        None,
                    )
                    if next_step is None:
                        plan_status = "completed"
                        outcome = "plan_completed"
                    else:
                        next_step["status"] = "active"
                        plan_status = "active"
                        outcome = "step_completed"
            connection.execute(
                "UPDATE plans SET status = ?, plan_json = ? WHERE id = ?",
                (plan_status, _json(plan), checkpoint["plan_id"]),
            )
            connection.execute(
                """
                UPDATE checkpoints
                SET status = 'resolved', result = ?, resolved_at = ? WHERE id = ?
                """,
                (outcome, _now(), checkpoint_id),
            )
        return {"status": outcome, "plan_status": plan_status}

    def save_agent_session(
        self, role: str, thread_id: str, tools: list[str], skills: list[str] | None = None
    ) -> None:
        """Persist a role's Codex thread and exact dynamic capabilities."""

        if role not in {"builder", "tutor", "evaluator", "planner"}:
            raise ValueError(f"invalid agent role: {role}")
        with self._transaction() as connection:
            self._save_agent_session(connection, role, thread_id, tools, skills or [])

    def get_agent_sessions(self) -> dict[str, dict[str, Any]]:
        """Return persisted role-to-thread bindings."""

        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT * FROM agent_sessions").fetchall()
        return {
            row["role"]: {
                "thread_id": row["thread_id"],
                "tools": json.loads(row["tools_json"]),
                "skills": json.loads(row["skills_json"]),
            }
            for row in rows
        }

    def _save_agent_session(
        self,
        connection: sqlite3.Connection,
        role: str,
        thread_id: str,
        tools: list[str],
        skills: list[str],
    ) -> None:
        connection.execute(
            """
            INSERT OR REPLACE INTO agent_sessions(
                role, thread_id, tools_json, skills_json, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (role, thread_id, _json(tools), _json(skills), _now()),
        )

    def _active_step(self, connection: sqlite3.Connection) -> tuple[int | None, str | None]:
        row = connection.execute(
            "SELECT id, plan_json FROM plans WHERE status = 'active' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None, None
        plan = json.loads(row["plan_json"])
        step = next((item for item in plan["steps"] if item["status"] == "active"), None)
        return (row["id"], step["step_id"]) if step else (row["id"], None)

    def _plan_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "plan_id": row["id"],
            "status": row["status"],
            "based_on_state_revision": row["based_on_revision"],
            **json.loads(row["plan_json"]),
        }

    def _metadata_json(self, key: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            raise RuntimeError(f"learning database is missing metadata: {key}")
        return json.loads(row["value"])

    def _increment_revision(self, connection: sqlite3.Connection) -> int:
        current = int(
            connection.execute(
                "SELECT value FROM metadata WHERE key = 'state_revision'"
            ).fetchone()[0]
        )
        revision = current + 1
        connection.execute(
            "UPDATE metadata SET value = ? WHERE key = 'state_revision'",
            (str(revision),),
        )
        return revision

    def _connect(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise RuntimeError(f"learning database is missing: {self.path}")
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def _validate_plan(raw: Any, graph: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("plan must be an object")
    concept_ids = {concept["id"] for concept in graph["concepts"]}
    if raw.get("goal_id") != graph["goal_concept_id"]:
        raise ValueError("plan goal_id does not match the graph")
    route = raw.get("route")
    if not isinstance(route, list) or any(item not in concept_ids for item in route):
        raise ValueError("plan route must contain known concept IDs")
    roadmap = raw.get("roadmap")
    if not isinstance(roadmap, str) or not roadmap.strip():
        raise ValueError("plan roadmap cannot be empty")
    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list) or not 3 <= len(raw_steps) <= 5:
        raise ValueError("plan must contain 3 to 5 steps")
    seen: set[str] = set()
    steps: list[dict[str, Any]] = []
    for raw_step in raw_steps:
        if not isinstance(raw_step, dict):
            raise ValueError("each plan step must be an object")
        step_id = raw_step.get("step_id")
        focus = raw_step.get("concept_ids")
        if not isinstance(step_id, str) or not step_id.strip() or step_id in seen:
            raise ValueError("step_id values must be unique non-empty strings")
        if not isinstance(focus, list) or not focus or any(
            item not in concept_ids for item in focus
        ):
            raise ValueError(f"step {step_id} needs known focus concept IDs")
        seen.add(step_id)
        fields: dict[str, str] = {}
        for field in ("objective", "mode", "strategy"):
            value = raw_step.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"step {step_id} needs {field}")
            fields[field] = value.strip()
        probes = raw_step.get("probes")
        criteria = raw_step.get("completion_criteria")
        if not _nonempty_strings(probes) or not _nonempty_strings(criteria):
            raise ValueError(f"step {step_id} needs probes and completion criteria")
        steps.append(
            {
                "step_id": step_id.strip(),
                "concept_ids": list(dict.fromkeys(focus)),
                **fields,
                "probes": probes,
                "completion_criteria": criteria,
                "evidence_requirements": {
                    "informative_signals": 2,
                    "minimum_direct_signals": 1,
                },
                "status": "pending",
            }
        )
    return {
        "goal_id": graph["goal_concept_id"],
        "roadmap": roadmap.strip(),
        "route": list(dict.fromkeys(route)),
        "steps": steps,
    }


def _nonempty_strings(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, str) and bool(item.strip()) for item in value)
    )


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _now() -> str:
    return datetime.now(UTC).isoformat()
