from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from theseus import CodexAppServer, final_text

FAKE_SERVER = Path(__file__).with_name("fake_app_server.py")


class CodexAppServerTests(unittest.TestCase):
    def make_client(self, project: Path) -> CodexAppServer:
        return CodexAppServer(
            project,
            server_command=(sys.executable, str(FAKE_SERVER)),
            timeout=5,
        )

    def test_run_returns_raw_messages_and_routes_registered_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            calls: list[dict[str, object]] = []
            observed_events: list[dict[str, object]] = []
            with self.make_client(project) as client:
                client.add_tool(
                    "double",
                    "Double an integer.",
                    {
                        "type": "object",
                        "properties": {"value": {"type": "integer"}},
                        "required": ["value"],
                    },
                    lambda arguments: calls.append(arguments) or arguments["value"] * 2,
                )
                selected = client.add_skill(
                    "selected-skill",
                    "A selected test skill.",
                    "Use this skill during the test.",
                    resources={"references/example.txt": "example"},
                )
                unselected = client.add_skill(
                    "unselected-skill",
                    "An unselected test skill.",
                    "Do not enable this skill.",
                )

                thread_id = client.create_agent(
                    tools=["double"], skills=["selected-skill"], sandbox="read-only"
                )
                raw = client.run(
                    "Use the tool.", thread_id, on_event=observed_events.append
                )

            self.assertEqual(calls, [{"value": 7}])
            self.assertEqual(observed_events, raw)
            self.assertEqual(raw[-1]["method"], "turn/completed")
            payload = json.loads(final_text(raw) or "{}")
            self.assertEqual(payload["codexHome"], str(project / ".theseus"))
            self.assertEqual(
                payload["threadParams"]["dynamicTools"][0]["name"], "double"
            )
            skill_config = payload["threadParams"]["config"]["skills"]["config"]
            self.assertIn({"path": str(selected), "enabled": True}, skill_config)
            self.assertIn({"path": str(unselected), "enabled": False}, skill_config)
            self.assertEqual(
                payload["toolResponse"]["result"],
                {
                    "contentItems": [{"type": "inputText", "text": "14"}],
                    "success": True,
                },
            )
            self.assertEqual(
                (
                    project / ".theseus/skills/selected-skill/references/example.txt"
                ).read_text(),
                "example",
            )

    def test_agent_integrations_are_isolated_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.make_client(Path(directory)) as client:
                thread_id = client.create_agent()
                raw = client.run("Inspect configuration.", thread_id)

        config = json.loads(final_text(raw) or "{}")["threadParams"]["config"]
        self.assertEqual(
            config["features"],
            {"apps": False, "plugins": False, "tool_suggest": False},
        )
        self.assertEqual(config["apps"], {"_default": {"enabled": False}})
        self.assertEqual(config["mcp_servers"], {})
        self.assertEqual(config["plugins"], {})

    def test_agent_can_inherit_integrations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.make_client(Path(directory)) as client:
                thread_id = client.create_agent(inherit_integrations=True)
                raw = client.run("Inspect configuration.", thread_id)

        config = json.loads(final_text(raw) or "{}")["threadParams"]["config"]
        self.assertNotIn("features", config)
        self.assertNotIn("apps", config)
        self.assertNotIn("mcp_servers", config)
        self.assertNotIn("plugins", config)

    def test_agent_can_use_only_explicit_integrations(self) -> None:
        integrations = {
            "apps": {"notion": {"enabled": True}},
            "mcp_servers": {
                "project_docs": {
                    "url": "https://docs.example.test/mcp",
                    "enabled": True,
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            with self.make_client(Path(directory)) as client:
                thread_id = client.create_agent(integration_config=integrations)
                raw = client.run("Inspect configuration.", thread_id)

        config = json.loads(final_text(raw) or "{}")["threadParams"]["config"]
        self.assertEqual(
            config["features"],
            {"apps": True, "plugins": False, "tool_suggest": False},
        )
        self.assertEqual(
            config["apps"],
            {
                "_default": {"enabled": False},
                "notion": {"enabled": True},
            },
        )
        self.assertEqual(config["mcp_servers"], integrations["mcp_servers"])
        self.assertEqual(config["plugins"], {})

    def test_agent_rejects_non_integration_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.make_client(Path(directory)) as client:
                with self.assertRaisesRegex(ValueError, "unsupported integration"):
                    client.create_agent(
                        integration_config={"sandbox_mode": "danger-full-access"}
                    )

    def test_run_passes_structured_output_and_application_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            schema = {
                "type": "object",
                "properties": {"reply": {"type": "string"}},
                "required": ["reply"],
                "additionalProperties": False,
            }
            with self.make_client(project) as client:
                thread_id = client.create_agent()
                raw = client.run(
                    "Use the supplied context.",
                    thread_id,
                    output_schema=schema,
                    additional_context={"active_step": "Step context"},
                )

            turn = json.loads(final_text(raw) or "{}")["turnParams"]
            self.assertEqual(turn["outputSchema"], schema)
            self.assertEqual(
                turn["additionalContext"],
                {"active_step": {"kind": "application", "value": "Step context"}},
            )

    def test_run_rejects_invalid_application_context_before_starting_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.make_client(Path(directory)) as client:
                thread_id = client.create_agent()
                with self.assertRaisesRegex(ValueError, "non-empty strings"):
                    client.run("Prompt", thread_id, additional_context={"": "value"})
                with self.assertRaisesRegex(TypeError, "values must be strings"):
                    client.run(  # type: ignore[arg-type]
                        "Prompt", thread_id, additional_context={"source": 3}
                    )

    def test_registered_tool_is_rejected_when_not_attached_to_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            called = False

            def unavailable(_: dict[str, object]) -> str:
                nonlocal called
                called = True
                return "must not run"

            with self.make_client(project) as client:
                client.add_tool(
                    "unavailable",
                    "A tool that is registered but not attached.",
                    {"type": "object"},
                    unavailable,
                )
                thread_id = client.create_agent()
                raw = client.run("Request an unavailable tool.", thread_id)

            payload = json.loads(final_text(raw) or "{}")
            self.assertFalse(called)
            self.assertFalse(payload["toolResponse"]["result"]["success"])
            self.assertIn(
                "unknown or unavailable tool",
                payload["toolResponse"]["result"]["contentItems"][0]["text"],
            )

    def test_resume_reattaches_tools_and_integration_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            first = self.make_client(project)
            first.add_tool(
                "double",
                "Double an integer.",
                {"type": "object", "properties": {"value": {"type": "integer"}}},
                lambda arguments: arguments["value"] * 2,
            )
            thread_id = first.create_agent(tools=["double"], inherit_integrations=True)
            first.close()

            calls: list[dict[str, object]] = []
            second = self.make_client(project)
            second.add_tool(
                "double",
                "Double an integer.",
                {"type": "object", "properties": {"value": {"type": "integer"}}},
                lambda arguments: calls.append(arguments) or arguments["value"] * 2,
            )
            integrations = {
                "mcp_servers": {"docs": {"url": "https://example.test/mcp"}}
            }
            second.resume_agent(
                thread_id, tools=["double"], integration_config=integrations
            )
            try:
                raw = second.run("Use the restored tool.", thread_id)
            finally:
                second.close()

            payload = json.loads(final_text(raw) or "{}")
            self.assertEqual(calls, [{"value": 7}])
            self.assertEqual(
                payload["toolResponse"]["result"]["contentItems"][0]["text"], "14"
            )
            config = payload["threadParams"]["config"]
            self.assertEqual(config["mcp_servers"], integrations["mcp_servers"])
            self.assertFalse(config["features"]["apps"])

    def test_skill_resource_cannot_escape_skill_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            client = self.make_client(project)
            with self.assertRaises(ValueError):
                client.add_skill(
                    "unsafe-skill",
                    "Unsafe test.",
                    "This must fail.",
                    resources={"../outside.txt": "no"},
                )

    def test_unknown_capability_is_rejected_on_create_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            with self.make_client(project) as client:
                for operation in (
                    client.create_agent,
                    lambda **kw: client.resume_agent("thread-1", **kw),
                ):
                    with self.subTest(operation=operation.__name__):
                        with self.assertRaisesRegex(KeyError, "unknown tool"):
                            operation(tools=["missing"])


if __name__ == "__main__":
    unittest.main()
