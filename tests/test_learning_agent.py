from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

from codeagent import CodexAppServer, final_text
from examples.learning_agent.build import (
    GENERATED_TOOLS_SOURCE,
    BuildRequest,
    TopicPaths,
    approve_candidate,
    archive_candidate,
    builder_prompt,
    builder_repair_prompt,
    next_attempt_number,
    normalize_candidate,
    parse_builder_response,
    recover_latest_candidate,
    validate_candidate,
    validate_generated_tools_source,
)
from examples.learning_agent.cli import _parser
from examples.learning_agent.cli_debug import DebugRenderer
from examples.learning_agent.database import LearningStore, StaleStateError
from examples.learning_agent.graph import (
    GraphValidationError,
    beta_metrics,
    prerequisite_routes,
    readiness,
    validate_graph,
)
from examples.learning_agent.runtime import LearningRuntime, ROLE_TOOLS


FAKE_SERVER = Path(__file__).with_name("fake_app_server.py")


def alternative_graph() -> dict[str, object]:
    return {
        "schema_version": 1,
        "goal": {
            "title": "Solve the target",
            "audience": "beginner",
            "depth": "introductory",
        },
        "goal_concept_id": "target",
        "concepts": [
            _concept("weak"),
            _concept("route_a"),
            _concept("route_b"),
            _concept("target", prerequisites=[["weak"], ["route_a", "route_b"]]),
        ],
    }


def chunk_graph() -> dict[str, object]:
    return {
        "schema_version": 1,
        "goal": {
            "title": "Use a composed skill",
            "audience": "beginner",
            "depth": "introductory",
        },
        "goal_concept_id": "target",
        "concepts": [
            _concept("part_a"),
            _concept("part_b"),
            _concept("combined", kind="chunk", components=["part_a", "part_b"]),
            _concept("target", prerequisites=[["combined"]]),
        ],
    }


def generated_graph(count: int = 15) -> dict[str, object]:
    concepts = []
    for index in range(count):
        prerequisites = [[f"concept_{index - 1}"]] if index else []
        concepts.append(_concept(f"concept_{index}", prerequisites=prerequisites))
    return {
        "schema_version": 1,
        "goal": {
            "title": "Generated goal",
            "audience": "adult beginner",
            "depth": "working knowledge",
        },
        "goal_concept_id": f"concept_{count - 1}",
        "concepts": concepts,
    }


def _concept(
    concept_id: str,
    *,
    kind: str = "atomic",
    components: list[str] | None = None,
    prerequisites: list[list[str]] | None = None,
) -> dict[str, object]:
    return {
        "id": concept_id,
        "title": concept_id.replace("_", " ").title(),
        "description": f"Understand and apply {concept_id}.",
        "kind": kind,
        "mastery_criteria": [f"Explain {concept_id} and apply it to an example."],
        "components": components or [],
        "prerequisite_groups": prerequisites or [],
    }


def plan_json(graph: dict[str, object]) -> str:
    goal_id = str(graph["goal_concept_id"])
    return json.dumps(
        {
            "goal_id": goal_id,
            "roadmap": "Close the shortest prerequisite gap, then integrate it.",
            "route": ["weak"],
            "steps": [
                {
                    "step_id": f"step_{index}",
                    "concept_ids": ["weak"],
                    "objective": f"Demonstrate objective {index}.",
                    "mode": "practice",
                    "strategy": "Elicit, teach, then retry with a fresh example.",
                    "probes": ["Explain the idea and solve one example."],
                    "completion_criteria": ["Produces a correct explanation and application."],
                }
                for index in range(1, 4)
            ],
        }
    )


def generated_plan_json(graph: dict[str, object]) -> str:
    return json.dumps(
        {
            "goal_id": graph["goal_concept_id"],
            "roadmap": "Build the first foundation through explanation and application.",
            "route": ["concept_0"],
            "steps": [
                {
                    "step_id": f"generated_step_{index}",
                    "concept_ids": ["concept_0"],
                    "objective": f"Demonstrate generated objective {index}.",
                    "mode": "practice",
                    "strategy": "Elicit an explanation, then ask for an application.",
                    "probes": ["Explain the concept and apply it to an example."],
                    "completion_criteria": [
                        "Produces a correct explanation and application."
                    ],
                }
                for index in range(1, 4)
            ],
        }
    )


def valid_candidate() -> dict[str, object]:
    return {
        "research_markdown": "R" * 650,
        "sources": [
            {
                "title": f"Authoritative source {index}",
                "url": f"https://example.com/source-{index}",
                "relevance": "Supports the decomposition and prerequisite order.",
            }
            for index in range(3)
        ],
        "graph": generated_graph(),
        "tools_py": GENERATED_TOOLS_SOURCE,
    }


class KnowledgeGraphTests(unittest.TestCase):
    def test_shortest_route_rewards_one_small_gap(self) -> None:
        graph = validate_graph(alternative_graph())
        result = readiness(
            graph,
            "target",
            {"weak": 0.72, "route_a": 0.80, "route_b": 0.80},
        )
        self.assertEqual(result["route"], ["weak"])
        self.assertAlmostEqual(result["route_cost"], 0.28)
        self.assertGreater(result["value"], 0.75)

    def test_chunk_is_flattened_to_distinct_atomic_components(self) -> None:
        graph = validate_graph(chunk_graph())
        self.assertEqual(
            prerequisite_routes(graph, "target"),
            [frozenset({"part_a", "part_b"})],
        )

    def test_no_prerequisites_means_full_readiness(self) -> None:
        graph = validate_graph(alternative_graph())
        self.assertEqual(
            readiness(graph, "weak", {})["value"],
            1.0,
        )

    def test_frontier_advances_to_goal_after_route_is_satisfied(self) -> None:
        graph = validate_graph(alternative_graph())
        result = readiness(
            graph,
            "target",
            {"weak": 0.80, "route_a": 0.10, "route_b": 0.10, "target": 0.20},
        )
        self.assertEqual(result["route"], ["weak"])
        self.assertEqual(result["frontier"], ["target"])

    def test_graph_rejects_cycles_disconnected_nodes_and_missing_criteria(self) -> None:
        cycle = alternative_graph()
        cycle["concepts"][0]["prerequisite_groups"] = [["target"]]
        with self.assertRaisesRegex(GraphValidationError, "cycle"):
            validate_graph(cycle)

        disconnected = alternative_graph()
        disconnected["concepts"].append(_concept("island"))
        with self.assertRaisesRegex(GraphValidationError, "disconnected"):
            validate_graph(disconnected)

        no_criteria = alternative_graph()
        no_criteria["concepts"][0]["mastery_criteria"] = []
        with self.assertRaisesRegex(GraphValidationError, "mastery criteria"):
            validate_graph(no_criteria)

    def test_beta_metrics_include_uncertainty_and_conservative_mastery(self) -> None:
        metrics = beta_metrics(3.0, 1.0)
        self.assertEqual(metrics["probability"], 0.75)
        self.assertGreater(metrics["uncertainty"], 0)
        self.assertLess(metrics["effective_mastery"], metrics["probability"])


class LearningStoreTests(unittest.TestCase):
    def make_store(self, directory: str) -> LearningStore:
        store = LearningStore(Path(directory) / "learning.db")
        store.initialize(alternative_graph())
        return store

    def test_evaluator_categories_apply_exact_beta_updates_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            direct = store.submit_mastery_evidence("weak", "Solved it", "Fresh problem")
            result = store.assess_mastery_evidence(
                direct["evidence_id"], "demonstrated", "direct", "Correct solution."
            )
            self.assertEqual((result["alpha"], result["beta"]), (3.0, 1.0))
            self.assertEqual(result["state_revision"], 1)
            with self.assertRaisesRegex(ValueError, "already been assessed"):
                store.assess_mastery_evidence(
                    direct["evidence_id"], "demonstrated", "direct", "Duplicate."
                )

            indirect = store.submit_mastery_evidence("weak", "Some reasoning", "Discussion")
            result = store.assess_mastery_evidence(
                indirect["evidence_id"], "partial", "indirect", "Incomplete reasoning."
            )
            self.assertEqual((result["alpha"], result["beta"]), (3.5, 1.5))

            report = store.submit_mastery_evidence("weak", "I know it", "Self report")
            result = store.assess_mastery_evidence(
                report["evidence_id"], "contradicted", "self_report", "Claim conflicts."
            )
            self.assertEqual((result["alpha"], result["beta"]), (3.5, 2.0))

            unhelpful = store.submit_mastery_evidence("weak", "Maybe", "No probe")
            result = store.assess_mastery_evidence(
                unhelpful["evidence_id"], "uninformative", "direct", "No usable signal."
            )
            self.assertEqual((result["alpha"], result["beta"]), (3.5, 2.0))

    def test_checkpoint_gate_requires_two_informative_and_one_direct(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            plan = store.save_learning_plan(plan_json(alternative_graph()), 0)
            first = store.submit_mastery_evidence("weak", "Explanation", "Direct question")
            second = store.submit_mastery_evidence("weak", "Related transfer", "Follow-up")
            store.assess_mastery_evidence(
                first["evidence_id"], "demonstrated", "direct", "Meets criterion."
            )
            store.assess_mastery_evidence(
                second["evidence_id"], "partial", "indirect", "Useful transfer signal."
            )
            checkpoint = store.signal_plan_checkpoint(
                plan["plan_id"], "step_1", "completed", "Criteria appear satisfied."
            )
            outcome = store.resolve_checkpoint(checkpoint["checkpoint_id"])
            self.assertEqual(outcome["status"], "step_completed")
            current = store.get_learning_plan()
            self.assertEqual(current["steps"][0]["status"], "completed")
            self.assertEqual(current["steps"][1]["status"], "active")

    def test_failed_gate_keeps_step_active_and_does_not_replan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            plan = store.save_learning_plan(plan_json(alternative_graph()), 0)
            evidence = store.submit_mastery_evidence("weak", "One answer", "One probe")
            store.assess_mastery_evidence(
                evidence["evidence_id"], "demonstrated", "direct", "One good signal."
            )
            checkpoint = store.signal_plan_checkpoint(
                plan["plan_id"], "step_1", "completed", "Try completion."
            )
            outcome = store.resolve_checkpoint(checkpoint["checkpoint_id"])
            self.assertEqual(outcome["status"], "evidence_gate_failed")
            current = store.get_learning_plan()
            self.assertEqual(current["plan_id"], plan["plan_id"])
            self.assertEqual(current["steps"][0]["status"], "active")

    def test_structured_tutor_turn_persists_evidence_and_checkpoint_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            plan = store.save_learning_plan(plan_json(alternative_graph()), 0)
            result = store.record_tutor_turn(
                plan["plan_id"],
                "step_1",
                [
                    {
                        "concept_id": "weak",
                        "evidence": "The learner explained the idea.",
                        "elicitation_context": "An explicit explanation prompt.",
                    }
                ],
                {"status": "completed", "summary": "Criteria appear satisfied."},
            )
            self.assertEqual(len(result["evidence_ids"]), 1)
            self.assertIsNotNone(result["checkpoint_id"])
            self.assertEqual(
                store.get_step_evidence(plan["plan_id"], "step_1")[0]["evidence"],
                "The learner explained the idea.",
            )
            self.assertEqual(len(store.pending_checkpoints()), 1)

    def test_structured_tutor_turn_rolls_back_if_evidence_leaves_active_step(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            plan = store.save_learning_plan(plan_json(alternative_graph()), 0)
            with self.assertRaisesRegex(ValueError, "outside the active plan step"):
                store.record_tutor_turn(
                    plan["plan_id"],
                    "step_1",
                    [
                        {
                            "concept_id": "weak",
                            "evidence": "Valid first record.",
                            "elicitation_context": "First probe.",
                        },
                        {
                            "concept_id": "route_a",
                            "evidence": "Invalid second record.",
                            "elicitation_context": "Second probe.",
                        },
                    ],
                    None,
                )
            self.assertEqual(store.get_step_evidence(plan["plan_id"], "step_1"), [])

    def test_checkpoint_waits_for_evaluator_and_blocked_allows_early_replan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            plan = store.save_learning_plan(plan_json(alternative_graph()), 0)
            store.submit_mastery_evidence("weak", "Pending answer", "Probe")
            checkpoint = store.signal_plan_checkpoint(
                plan["plan_id"], "step_1", "completed", "Evaluate first."
            )
            self.assertEqual(
                store.resolve_checkpoint(checkpoint["checkpoint_id"])["status"],
                "waiting_for_evaluator",
            )

        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            plan = store.save_learning_plan(plan_json(alternative_graph()), 0)
            checkpoint = store.signal_plan_checkpoint(
                plan["plan_id"], "step_1", "blocked", "This route is unsuitable."
            )
            self.assertEqual(
                store.resolve_checkpoint(checkpoint["checkpoint_id"])["status"],
                "blocked",
            )
            self.assertEqual(store.get_learning_plan()["status"], "blocked")

    def test_stale_plan_is_rejected_after_evidence_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            evidence = store.submit_mastery_evidence("weak", "Answer", "Question")
            store.assess_mastery_evidence(
                evidence["evidence_id"], "partial", "direct", "Mixed result."
            )
            with self.assertRaises(StaleStateError):
                store.save_learning_plan(plan_json(alternative_graph()), 0)

    def test_plan_has_no_turn_budget_and_persists_agent_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            plan = store.save_learning_plan(plan_json(alternative_graph()), 0)
            self.assertNotIn("turn", json.dumps(plan).lower())
            store.save_agent_session("tutor", "thread-tutor", ["get_learning_plan"])
            reloaded = LearningStore(store.path)
            self.assertEqual(
                reloaded.get_agent_sessions()["tutor"]["thread_id"], "thread-tutor"
            )

    def test_evidence_is_append_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            evidence = store.submit_mastery_evidence("weak", "Answer", "Question")
            store.assess_mastery_evidence(
                evidence["evidence_id"], "demonstrated", "direct", "Correct."
            )
            with sqlite3.connect(store.path) as connection:
                row = connection.execute(
                    "SELECT evidence, status FROM evidence WHERE id = ?",
                    (evidence["evidence_id"],),
                ).fetchone()
            self.assertEqual(row, ("Answer", "assessed"))


class GeneratedPackageTests(unittest.TestCase):
    def test_build_attempt_numbers_survive_interruption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = TopicPaths(Path(directory), "generated-goal")
            (paths.build_archive / "attempt-01").mkdir(parents=True)
            (paths.build_archive / "attempt-03").mkdir()
            self.assertEqual(next_attempt_number(paths), 4)

    def test_common_mastery_criteria_alias_is_normalized_without_model_retry(self) -> None:
        candidate = valid_candidate()
        for concept in candidate["graph"]["concepts"]:
            criterion = concept.pop("mastery_criteria")[0]
            concept["measurable_mastery_criteria"] = criterion
        normalized = normalize_candidate(candidate)
        validated = validate_candidate(normalized)
        self.assertTrue(
            all(
                isinstance(concept["mastery_criteria"], list)
                for concept in validated["graph"]["concepts"]
            )
        )

    def test_saved_failed_response_can_be_recovered_after_cli_restart(self) -> None:
        candidate = valid_candidate()
        for concept in candidate["graph"]["concepts"]:
            concept["measurable_mastery_criteria"] = concept.pop(
                "mastery_criteria"
            )[0]
        with tempfile.TemporaryDirectory() as directory:
            paths = TopicPaths(Path(directory), "generated-goal")
            attempt = paths.build_archive / "attempt-01"
            attempt.mkdir(parents=True)
            (attempt / "raw-response.txt").write_text(
                json.dumps(candidate), encoding="utf-8"
            )
            recovered = recover_latest_candidate(paths)
        self.assertIsNotNone(recovered)
        self.assertEqual(len(recovered["graph"]["concepts"]), 15)

    def test_repair_prompt_requires_minimal_same_thread_revision(self) -> None:
        prompt = builder_repair_prompt(
            "concept x mastery_criteria must be a list"
        )
        self.assertIn("do not restart research", prompt)
        self.assertIn("smallest correction", prompt)
        self.assertIn("immediately preceding response", prompt)

    def test_candidate_contract_and_subprocess_validation(self) -> None:
        candidate = validate_candidate(valid_candidate())
        with tempfile.TemporaryDirectory() as directory:
            paths = TopicPaths(Path(directory), "generated-goal")
            attempt = archive_candidate(paths, candidate, 1)
            validation = json.loads(
                (attempt / "validation.json").read_text(encoding="utf-8")
            )
            self.assertTrue(validation["safe_adapter"])
            self.assertEqual(len(validation["tool_names"]), 8)

    def test_unsafe_or_modified_generated_code_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "fixed audited"):
            validate_generated_tools_source("import os\nos.system('echo unsafe')\n")
        with self.assertRaisesRegex(ValueError, "fixed audited"):
            validate_generated_tools_source(
                GENERATED_TOOLS_SOURCE.replace("make_tools(database_path)", "()")
            )

    def test_builder_rejects_graph_outside_compact_size(self) -> None:
        candidate = valid_candidate()
        candidate["graph"] = generated_graph(14)
        with self.assertRaisesRegex(GraphValidationError, "between 15 and 40"):
            validate_candidate(candidate)

    def test_runtime_agents_receive_only_their_role_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = TopicPaths(root, "generated-goal")
            candidate = validate_candidate(valid_candidate())
            attempt = archive_candidate(paths, candidate, 1)
            approve_candidate(
                paths,
                attempt,
                BuildRequest("Generated goal", "adult beginner", "working knowledge"),
                "thread-builder",
            )
            client = CodexAppServer(
                root,
                server_command=(sys.executable, str(FAKE_SERVER)),
                timeout=5,
            )
            first_threads: dict[str, str]
            with client:
                runtime = LearningRuntime(root, paths, client)
                runtime.prepare()
                first_threads = dict(runtime.threads)
                for role, expected in ROLE_TOOLS.items():
                    raw = client.run("Inspect capability selection.", runtime.threads[role])
                    payload = json.loads(final_text(raw) or "{}")
                    actual = tuple(
                        tool["name"] for tool in payload["threadParams"]["dynamicTools"]
                    )
                    self.assertEqual(actual, expected)

            restarted_client = CodexAppServer(
                root,
                server_command=(sys.executable, str(FAKE_SERVER)),
                timeout=5,
            )
            with restarted_client:
                restarted = LearningRuntime(root, paths, restarted_client)
                restarted.prepare()
                self.assertEqual(restarted.threads, first_threads)
                self.assertEqual(
                    set(restarted.threads),
                    {"builder", "tutor", "evaluator", "planner"},
                )
                for role, expected in ROLE_TOOLS.items():
                    raw = restarted_client.run(
                        "Inspect restored capabilities.", restarted.threads[role]
                    )
                    payload = json.loads(final_text(raw) or "{}")
                    actual = tuple(
                        tool["name"] for tool in payload["threadParams"]["dynamicTools"]
                    )
                    self.assertEqual(actual, expected)

    def test_runtime_replaces_a_saved_role_thread_with_no_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = TopicPaths(root, "generated-goal")
            candidate = validate_candidate(valid_candidate())
            attempt = archive_candidate(paths, candidate, 1)
            approve_candidate(
                paths,
                attempt,
                BuildRequest("Generated goal", "adult beginner", "working knowledge"),
                "thread-builder",
            )
            store = LearningStore(paths.database)
            missing_thread = "missing-rollout-tutor"
            store.save_agent_session(
                "tutor", missing_thread, list(ROLE_TOOLS["tutor"])
            )
            progress: list[str] = []
            client = CodexAppServer(
                root,
                server_command=(sys.executable, str(FAKE_SERVER)),
                timeout=5,
            )
            with client:
                runtime = LearningRuntime(root, paths, client, on_progress=progress.append)
                runtime.prepare()
                messages = runtime._run_role("tutor", "Continue the active lesson.")

            replacement = runtime.threads["tutor"]
            self.assertNotEqual(replacement, missing_thread)
            self.assertEqual(
                store.get_agent_sessions()["tutor"]["thread_id"], replacement
            )
            self.assertIsNotNone(final_text(messages))
            self.assertTrue(
                any("not resumable" in message for message in progress), progress
            )

    def test_runtime_migrates_legacy_tutor_and_uses_one_structured_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = TopicPaths(root, "generated-goal")
            graph = generated_graph()
            candidate = validate_candidate(valid_candidate())
            attempt = archive_candidate(paths, candidate, 1)
            approve_candidate(
                paths,
                attempt,
                BuildRequest("Generated goal", "adult beginner", "working knowledge"),
                "thread-builder",
            )
            store = LearningStore(paths.database)
            store.save_learning_plan(generated_plan_json(graph), 0)
            legacy_thread = "legacy-tutor-thread"
            store.save_agent_session(
                "tutor",
                legacy_thread,
                [
                    "get_knowledge_graph",
                    "get_learner_model",
                    "submit_mastery_evidence",
                    "get_learning_plan",
                    "signal_plan_checkpoint",
                ],
            )
            progress: list[str] = []
            client = CodexAppServer(
                root,
                server_command=(sys.executable, str(FAKE_SERVER)),
                timeout=5,
            )
            with client:
                runtime = LearningRuntime(root, paths, client, on_progress=progress.append)
                answer = runtime.tutor_turn("A relevant learner answer.")

            self.assertEqual(answer, "Fast structured tutor reply.")
            self.assertNotEqual(runtime.threads["tutor"], legacy_thread)
            saved = store.get_agent_sessions()["tutor"]
            self.assertEqual(saved["tools"], [])
            evidence = store.get_step_evidence(1, "generated_step_1")
            self.assertEqual(len(evidence), 1)
            self.assertEqual(evidence[0]["concept_id"], "concept_0")
            self.assertTrue(any("fast turn protocol" in item for item in progress))


class CliDebugTests(unittest.TestCase):
    def test_debug_flag_is_available_for_build_and_chat(self) -> None:
        build = _parser().parse_args(
            [
                "build",
                "--goal",
                "Goal",
                "--audience",
                "Audience",
                "--depth",
                "Depth",
                "--debug",
            ]
        )
        chat = _parser().parse_args(["chat", "goal", "--debug"])
        self.assertTrue(build.debug)
        self.assertTrue(chat.debug)

    def test_non_tty_debug_renderer_emits_stable_phase_lines(self) -> None:
        stream = io.StringIO()
        with DebugRenderer(True, stream) as debug:
            debug.phase("Planner is working")
            debug.note("Plan step is ready")
        self.assertEqual(
            stream.getvalue().splitlines(),
            ["[debug] Planner is working", "[debug] Plan step is ready"],
        )

    def test_disabled_debug_renderer_is_silent(self) -> None:
        stream = io.StringIO()
        with DebugRenderer(False, stream) as debug:
            debug.phase("Hidden")
            debug.note("Also hidden")
        self.assertEqual(stream.getvalue(), "")

    def test_tty_spinner_shows_backend_activity_and_elapsed_time(self) -> None:
        class TtyBuffer(io.StringIO):
            def isatty(self) -> bool:
                return True

        stream = TtyBuffer()
        with DebugRenderer(True, stream) as debug:
            debug.phase("Researching")
            debug.model_event(
                "builder",
                {"method": "item/completed", "params": {"item": {"type": "webSearch"}}},
            )
            time.sleep(0.15)
            debug.idle()
        rendered = stream.getvalue()
        self.assertIn("Researching", rendered)
        self.assertIn("1 events", rendered)
        self.assertIn("builder completed webSearch", rendered)


@unittest.skipUnless(
    os.environ.get("CODEAGENT_LEARNING_LIVE_TEST") == "1",
    "real learning-agent test disabled",
)
class LiveLearningAgentTests(unittest.TestCase):
    """Exercise all four roles through the real Codex app-server and models."""

    def test_real_builder_planner_tutor_and_evaluator(self) -> None:
        source_home = os.environ.get("CODEX_AUTH_HOME", "~/.codex")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = BuildRequest(
                goal="Understand binary search well enough to explain and trace it",
                audience="adult beginner who knows basic lists and comparisons",
                depth="conceptual understanding plus hand-tracing small examples",
            )
            paths = TopicPaths(root, "binary-search-live-test")
            client = CodexAppServer(root, timeout=600)
            client.import_auth(source_home)
            with client:
                print("\n[live 1/7] Starting real graph-builder model", flush=True)
                builder_thread = client.create_agent(
                    sandbox="workspace-write",
                    approval_policy="never",
                    developer_instructions=(
                        "Research the requested curriculum carefully and return only "
                        "the exact structured artifact requested by the user."
                    ),
                )
                feedback = None
                candidate = None
                builder_errors: list[str] = []
                prompt = builder_prompt(request)
                for attempt_number in range(1, 4):
                    print(
                        f"[live 2/7] Researching and generating graph "
                        f"(attempt {attempt_number})",
                        flush=True,
                    )
                    response = final_text(
                        client.run(
                            prompt,
                            builder_thread,
                            timeout=600,
                        )
                    )
                    try:
                        candidate = validate_candidate(
                            parse_builder_response(response or "")
                        )
                        break
                    except Exception as error:
                        builder_errors.append(f"attempt {attempt_number}: {error}")
                        print(f"[live] Builder validation failed: {error}", flush=True)
                        feedback = f"Validation failed: {error}. Return a corrected artifact."
                        prompt = builder_repair_prompt(feedback)
                self.assertIsNotNone(
                    candidate,
                    "real builder did not produce a valid package: "
                    + " | ".join(builder_errors),
                )
                print("[live 3/7] Validating and approving generated package", flush=True)
                attempt = archive_candidate(paths, candidate, 1)
                approve_candidate(paths, attempt, request, builder_thread)

                runtime = LearningRuntime(
                    root,
                    paths,
                    client,
                    lambda message: print(f"[live] {message}", flush=True),
                )
                print("[live 4/7] Running real planner model", flush=True)
                plan = runtime.ensure_active_plan()
                self.assertIn(len(plan["steps"]), range(3, 6))

                print("[live 5/7] Running real tutor model", flush=True)
                tutor_reply = runtime.tutor_turn(
                    "I am ready. Start the active step with a concise explanation "
                    "and then ask me one diagnostic question."
                )
                self.assertTrue(tutor_reply.strip())

                current_plan = runtime.store.get_learning_plan()
                active = next(
                    step for step in current_plan["steps"] if step["status"] == "active"
                )
                graph = runtime.store.get_knowledge_graph()
                concept_by_id = {item["id"]: item for item in graph["concepts"]}
                focus = active["concept_ids"][0]
                criterion = concept_by_id[focus]["mastery_criteria"][0]
                first = runtime.store.submit_mastery_evidence(
                    focus,
                    f'Direct learner response correctly explained {focus!r} and '
                    f"gave a worked example satisfying: {criterion}",
                    "Verbatim response to an explicit explanation-and-example probe.",
                )
                second = runtime.store.submit_mastery_evidence(
                    focus,
                    f"On a fresh case, the learner independently applied {focus!r} "
                    f"and justified each step, satisfying: {criterion}",
                    "Direct transfer problem with no hints.",
                )
                self.assertNotEqual(first["evidence_id"], second["evidence_id"])
                checkpoint = runtime.store.signal_plan_checkpoint(
                    current_plan["plan_id"],
                    active["step_id"],
                    "completed",
                    "Two direct demonstrations are ready for independent evaluation.",
                )
                self.assertEqual(checkpoint["status"], "pending_evaluation")
                print("[live 6/7] Running real evidence-evaluator model", flush=True)
                outcomes = runtime.process_checkpoints()
                self.assertEqual(runtime.store.list_pending_mastery_evidence(), [])
                self.assertGreaterEqual(
                    runtime.store.get_learner_model()["state_revision"], 2
                )
                self.assertIn(
                    outcomes[0]["status"],
                    {"step_completed", "plan_completed", "evidence_gate_failed"},
                )
                print("[live 7/7] Real four-agent flow completed", flush=True)


if __name__ == "__main__":
    unittest.main()
