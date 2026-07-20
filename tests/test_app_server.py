from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from codeagent import CodexAppServer, final_text


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
                raw = client.run("Use the tool.", thread_id)

            self.assertEqual(calls, [{"value": 7}])
            self.assertEqual(raw[-1]["method"], "turn/completed")
            payload = json.loads(final_text(raw) or "{}")
            self.assertEqual(payload["codexHome"], str(project / ".codex-agent"))
            self.assertEqual(
                payload["threadParams"]["dynamicTools"][0]["name"], "double"
            )
            skill_config = payload["threadParams"]["config"]["skills"]["config"]
            self.assertIn({"path": str(selected), "enabled": True}, skill_config)
            self.assertIn({"path": str(unselected), "enabled": False}, skill_config)
            self.assertEqual(
                payload["toolResponse"]["result"],
                {"contentItems": [{"type": "inputText", "text": "14"}], "success": True},
            )
            self.assertEqual(
                (project / ".codex-agent/skills/selected-skill/references/example.txt").read_text(),
                "example",
            )

    def test_unknown_thread_is_resumed_before_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            with self.make_client(project) as client:
                raw = client.run("Continue.", "existing-thread")
            payload = json.loads(final_text(raw) or "{}")
            self.assertEqual(payload["threadParams"]["threadId"], "existing-thread")
            self.assertEqual(payload["threadParams"]["dynamicTools"], [])

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

    def test_agent_can_resume_after_client_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            client = self.make_client(project)
            thread_id = client.create_agent()
            client.close()
            try:
                raw = client.run("Continue after restart.", thread_id)
            finally:
                client.close()
            payload = json.loads(final_text(raw) or "{}")
            self.assertEqual(payload["threadParams"]["threadId"], thread_id)

    def test_different_agents_can_run_in_parallel_without_mixing_messages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)

            async def run_agents() -> tuple[
                str,
                str,
                list[dict[str, object]],
                list[dict[str, object]],
            ]:
                async with self.make_client(project) as client:
                    thread_a = client.create_agent()
                    thread_b = client.create_agent()
                    messages_a, messages_b = await asyncio.gather(
                        client.run_async("Parallel A", thread_a),
                        client.run_async("Parallel B", thread_b),
                    )
                    return thread_a, thread_b, messages_a, messages_b

            thread_a, thread_b, messages_a, messages_b = asyncio.run(run_agents())

            self.assertEqual(final_text(messages_a), "Parallel A")
            self.assertEqual(final_text(messages_b), "Parallel B")
            for thread_id, messages in (
                (thread_a, messages_a),
                (thread_b, messages_b),
            ):
                for message in messages:
                    params = message.get("params")
                    if isinstance(params, dict) and "threadId" in params:
                        self.assertEqual(params["threadId"], thread_id)

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

    def test_unknown_capability_is_rejected_before_starting_thread(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            with self.make_client(project) as client:
                with self.assertRaisesRegex(KeyError, "unknown tool"):
                    client.create_agent(tools=["missing"])


@unittest.skipUnless(os.environ.get("CODEAGENT_LIVE_TEST") == "1", "live test disabled")
class LiveCodexAppServerTests(unittest.TestCase):
    def test_real_prompt_round_trip(self) -> None:
        source_home = os.environ.get("CODEX_AUTH_HOME")
        if not source_home:
            self.skipTest("CODEX_AUTH_HOME is not set")
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            client = CodexAppServer(project, timeout=180)
            client.import_auth(source_home)
            with client:
                thread_id = client.create_agent(sandbox="read-only")
                raw = client.run("Reply with exactly: CODEAGENT_OK", thread_id)
            self.assertEqual(
                final_text(raw),
                "CODEAGENT_OK",
                msg=json.dumps(raw, indent=2, sort_keys=True),
            )

    def test_real_custom_skill_and_tool(self) -> None:
        source_home = os.environ.get("CODEX_AUTH_HOME")
        if not source_home:
            self.skipTest("CODEX_AUTH_HOME is not set")
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            calls: list[dict[str, object]] = []
            client = CodexAppServer(project, timeout=180)
            client.import_auth(source_home)
            client.add_tool(
                "integration_value",
                "Return the fixed integration-test value.",
                {"type": "object", "properties": {}, "additionalProperties": False},
                lambda arguments: calls.append(arguments) or "CODEAGENT_TOOL_OK",
            )
            client.add_skill(
                "integration-check",
                "Exercise the codeAgent dynamic-tool bridge.",
                "Call integration_value exactly once and return only the tool's text output.",
            )
            with client:
                thread_id = client.create_agent(
                    tools=["integration_value"],
                    skills=["integration-check"],
                    sandbox="read-only",
                )
                raw = client.run("Use $integration-check now.", thread_id)
            self.assertEqual(calls, [{}], msg=json.dumps(raw, indent=2, sort_keys=True))
            self.assertEqual(
                final_text(raw),
                "CODEAGENT_TOOL_OK",
                msg=json.dumps(raw, indent=2, sort_keys=True),
            )


if __name__ == "__main__":
    unittest.main()
