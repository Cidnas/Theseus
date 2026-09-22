from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

from theseus import CodexAppServer, CodexTimeoutError, final_text

FAKE_SERVER = Path(__file__).with_name("fake_app_server.py")


class ExecutionTests(unittest.TestCase):
    def test_completion_before_start_response_is_not_lost(self):
        with tempfile.TemporaryDirectory() as directory:
            with CodexAppServer(
                directory, server_command=(sys.executable, str(FAKE_SERVER))
            ) as client:
                thread = client.create_agent()
                messages = client.run(
                    "Complete before start response.", thread, timeout=0.3
                )
                self.assertEqual(final_text(messages), "early completion")

    def test_slow_tool_does_not_block_run_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            with CodexAppServer(
                directory, server_command=(sys.executable, str(FAKE_SERVER))
            ) as client:
                client.add_tool(
                    "slow",
                    "A slow callback.",
                    {"type": "object"},
                    lambda arguments: time.sleep(0.6),
                )
                thread = client.create_agent(tools=["slow"])
                started = time.monotonic()
                with self.assertRaises(CodexTimeoutError):
                    client.run("Use the tool.", thread, timeout=0.05)
                self.assertLess(time.monotonic() - started, 0.45)
