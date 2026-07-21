"""Behavioral integration test for real Codex tool selection."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from codeagent import CodexAppServer, final_text, register_tools
from examples.shop_agent.database import create_sample_database
from examples.shop_agent.tools import TOOLS


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(
    os.environ.get("CODEAGENT_LIVE_TEST") == "1",
    "real Codex test disabled",
)
class LiveShopAgentTests(unittest.TestCase):
    """Verify that real Codex chooses the smallest useful chain of shop tools."""

    def test_real_codex_chooses_only_the_tools_needed_for_the_question(self) -> None:
        """Codex should follow IDs through order history without redundant searches."""

        create_sample_database()
        codex = CodexAppServer(PROJECT_ROOT, timeout=180)
        codex.import_auth(os.environ.get("CODEX_AUTH_HOME", "~/.codex"))
        tool_names = register_tools(codex, TOOLS)

        with codex:
            thread_id = codex.create_agent(
                tools=tool_names,
                sandbox="read-only",
                developer_instructions=(
                    "Use only the registered shop tools for shop data. "
                    "Use the minimum number of tools needed, never inspect files or "
                    "the database directly, and return JSON only when requested."
                ),
            )
            messages = codex.run(
                "Customer alice@example.com wants to buy 2 more units of the only "
                "product in her most recent order. Return JSON with product, "
                "requested_quantity, units_in_stock, and can_fulfill. Do not guess.",
                thread_id,
            )

        called_tools = [
            message["params"]["tool"]
            for message in messages
            if message.get("method") == "item/tool/call"
            and isinstance(message.get("params"), dict)
        ]
        self.assertEqual(
            called_tools,
            [
                "find_customer",
                "list_customer_orders",
                "get_order_items",
                "check_inventory",
            ],
            msg=json.dumps(messages, indent=2, sort_keys=True),
        )

        answer = json.loads(final_text(messages) or "")
        self.assertEqual(answer["product"], "Hardcover Notebook")
        self.assertEqual(answer["requested_quantity"], 2)
        self.assertEqual(answer["units_in_stock"], 5)
        self.assertIs(answer["can_fulfill"], True)


if __name__ == "__main__":
    unittest.main()
