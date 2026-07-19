from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from codeagent import CodexAppServer, final_text, register_tools


FAKE_SERVER = Path(__file__).with_name("fake_app_server.py")


class ToolAdapterTests(unittest.TestCase):
    def make_client(self, project: Path) -> CodexAppServer:
        return CodexAppServer(
            project,
            server_command=(sys.executable, str(FAKE_SERVER)),
            timeout=5,
        )

    def test_plain_function_is_adapted_and_called_with_keyword_arguments(self) -> None:
        calls: list[int] = []

        def double(value: int) -> int:
            """Double an integer."""

            calls.append(value)
            return value * 2

        with tempfile.TemporaryDirectory() as directory:
            with self.make_client(Path(directory)) as client:
                names = register_tools(client, [double])
                thread_id = client.create_agent(tools=names, sandbox="read-only")
                raw = client.run("Use the tool.", thread_id)

        payload = json.loads(final_text(raw) or "{}")
        spec = payload["threadParams"]["dynamicTools"][0]
        self.assertEqual(names, ("double",))
        self.assertEqual(calls, [7])
        self.assertEqual(spec["name"], "double")
        self.assertEqual(spec["description"], "Double an integer.")
        self.assertEqual(
            spec["inputSchema"],
            {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        )

    def test_tool_function_requires_documentation_and_parameter_types(self) -> None:
        def undocumented(value: int) -> int:
            return value

        def untyped(value):
            """Return the value."""

            return value

        with tempfile.TemporaryDirectory() as directory:
            client = self.make_client(Path(directory))
            with self.assertRaisesRegex(ValueError, "requires a docstring"):
                register_tools(client, [undocumented])
            with self.assertRaisesRegex(TypeError, "requires a type annotation"):
                register_tools(client, [untyped])


if __name__ == "__main__":
    unittest.main()
