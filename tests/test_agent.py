from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
import unittest
from contextlib import aclosing, closing
from pathlib import Path
from typing import Annotated, Literal

from theseus import (
    CodexAppServer,
    CodexBusyError,
    CodexCancelledError,
    CodexProtocolError,
    CodexServerExited,
    CodexTimeoutError,
    register_tools,
    run_parallel,
)

FAKE = Path(__file__).with_name("fake_app_server.py")


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.client = CodexAppServer(
            self.directory.name,
            server_command=(sys.executable, str(FAKE)),
            timeout=3,
            cancel_timeout=0.4,
        )

    def tearDown(self):
        self.client.close()
        self.directory.cleanup()

    def test_simple_results_and_configuration(self):
        agent = self.client.agent(model="model-1", effort="low")
        result = agent.run("Inspect.", model="model-2", effort="high")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.thread_id, agent.id)
        self.assertEqual(result.raw_events, ())
        self.assertEqual(json.loads(result.text)["turnParams"]["effort"], "high")
        # Overrides become thread defaults, matching the upstream protocol.
        self.assertEqual(
            json.loads(agent.run("Inspect.").text)["turnParams"]["model"], "model-2"
        )
        agent.configure(model="model-1", effort="low")
        self.assertEqual(
            json.loads(agent.run("Inspect.").text)["turnParams"]["effort"], "low"
        )

    def test_model_discovery_collects_all_pages(self):
        models = self.client.models()
        self.assertEqual([model.id for model in models], ["model-1", "model-2"])
        self.assertEqual(models[0].reasoning_efforts, ("low", "high"))

    def test_stale_events_are_excluded_from_results_and_callbacks(self):
        agent = self.client.agent()
        observed = []
        result = agent.run(
            "Stale turn events.", include_raw=True, on_event=observed.append
        )
        self.assertEqual(tuple(observed), result.raw_events)
        self.assertNotIn("stale message", json.dumps(observed))
        self.assertNotIn("turn-stale", json.dumps(observed))

    def test_sync_stream_has_deltas_and_one_final_result(self):
        agent = self.client.agent()
        with closing(agent.stream("Stream deltas.")) as stream:
            events = list(stream)
        self.assertEqual(
            "".join(event.text for event in events if event.kind == "text_delta"),
            "hello world",
        )
        self.assertEqual(events[-1].result.text, "hello world")
        self.assertEqual(sum(event.kind == "completed" for event in events), 1)

    def test_async_stream_and_native_async_tool(self):
        async def double(value: int):
            """Double a value asynchronously."""
            await asyncio.sleep(0)
            return value * 2

        async def scenario():
            plain = await self.client.agent_async()
            async with aclosing(plain.stream_async("Stream deltas.")) as stream:
                events = [event async for event in stream]
            self.assertEqual(events[-1].result.text, "hello world")
            tools = register_tools(self.client, [double])
            worker = await self.client.agent_async(tools=tools)
            result = await worker.run_async("Use tool.")
            self.assertEqual(
                json.loads(result.text)["toolResponse"]["result"]["contentItems"][0][
                    "text"
                ],
                "14",
            )

        asyncio.run(scenario())

    def test_structured_function_inputs(self):
        calls = []

        def combine(
            values: list[int],
            mode: Literal["sum", "count"],
            metadata: dict[str, str],
            label: Annotated[str | None, "Optional label"] = None,
        ):
            """Combine a collection."""
            calls.append((values, mode, metadata, label))
            return sum(values)

        names = register_tools(self.client, [combine])
        result = self.client.agent(tools=names).run("Structured tool arguments.")
        self.assertTrue(json.loads(result.text)["toolResponse"]["result"]["success"])
        self.assertEqual(calls, [([1, 2], "sum", {"tag": "test"}, None)])

    def test_invalid_arguments_never_reach_function(self):
        calls = []

        def tool(value: int):
            """Accept only an integer."""
            calls.append(value)

        agent = self.client.agent(tools=register_tools(self.client, [tool]))
        result = agent.run("Invalid tool arguments.")
        self.assertFalse(json.loads(result.text)["toolResponse"]["result"]["success"])
        self.assertEqual(calls, [])

    def test_tool_schema_cannot_fetch_external_resources(self):
        with self.assertRaisesRegex(ValueError, "local definitions"):
            self.client.add_tool(
                "tool",
                "A tool.",
                {"$ref": "https://example.test/schema"},
                lambda _: None,
            )

    def test_capability_snapshot_survives_catalog_replacement(self):
        schema = {"type": "object"}
        self.client.add_tool("tool", "Old tool.", schema, lambda _: "old")
        agent = self.client.agent(tools=["tool"])
        self.client.add_tool("tool", "New tool.", schema, lambda _: "new")
        result = agent.run("Use tool.")
        self.assertEqual(
            json.loads(result.text)["toolResponse"]["result"]["contentItems"][0][
                "text"
            ],
            "old",
        )

    def test_parallel_results_keep_input_order(self):
        async def scenario():
            a = await self.client.agent_async()
            b = await self.client.agent_async()
            results = await run_parallel(
                [(a, "Parallel A"), (b, "Parallel B")], limit=2
            )
            self.assertEqual(
                [result.text for result in results], ["Parallel A", "Parallel B"]
            )
            with self.assertRaises(ValueError):
                await run_parallel([(a, "One"), (a, "Two")])

        asyncio.run(scenario())

    def test_async_tool_can_delegate_to_another_agent(self):
        child = self.client.agent()

        async def delegate(value: int):
            """Delegate an independent operation."""
            return (await child.run_async("Nested run.")).status

        parent = self.client.agent(tools=register_tools(self.client, [delegate]))
        result = parent.run("Use tool.")
        self.assertEqual(
            json.loads(result.text)["toolResponse"]["result"]["contentItems"][0][
                "text"
            ],
            "completed",
        )

    def test_task_cancellation_interrupts_server_and_allows_next_turn(self):
        async def scenario():
            agent = await self.client.agent_async()
            started = threading.Event()

            def event(message):
                if message.get("method") == "turn/started":
                    started.set()

            task = asyncio.create_task(agent.run_async("Hold turn.", on_event=event))
            while not started.is_set():
                await asyncio.sleep(0.001)
            with self.assertRaises(CodexBusyError):
                await agent.run_async("Another turn.")
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            result = await agent.run_async("Inspect.")
            self.assertEqual(len(json.loads(result.text)["interrupts"]), 1)

        asyncio.run(scenario())

    def test_explicit_cancel_works_from_sync_caller(self):
        agent = self.client.agent()
        started = threading.Event()
        outcome = []

        def run():
            try:
                agent.run(
                    "Hold turn.",
                    on_event=lambda event: (
                        started.set() if event.get("method") == "turn/started" else None
                    ),
                )
            except CodexCancelledError:
                outcome.append("cancelled")

        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(started.wait(2))
        self.assertTrue(agent.cancel())
        worker.join(2)
        self.assertEqual(outcome, ["cancelled"])
        self.assertFalse(agent.cancel())
        self.assertEqual(agent.run("Inspect.").status, "completed")

    def test_deadline_includes_delayed_start_response(self):
        agent = self.client.agent()
        with self.assertRaises(CodexTimeoutError):
            agent.run("Delay start response.", timeout=0.04)
        self.assertEqual(len(json.loads(agent.run("Inspect.").text)["interrupts"]), 1)

    def test_unconfirmed_interruption_blocks_reuse(self):
        agent = self.client.agent()
        with self.assertRaises(CodexTimeoutError):
            agent.run("Ignore interrupt.", timeout=0.04)
        with self.assertRaises(CodexBusyError):
            agent.run("Must not start.")
        self.client.close()
        self.assertEqual(agent.run("After restart.").status, "completed")

    def test_callback_failure_interrupts_run(self):
        agent = self.client.agent()

        def fail(message):
            if message.get("method") == "turn/started":
                raise ValueError("consumer failed")

        with self.assertRaisesRegex(ValueError, "consumer failed"):
            agent.run("Hold turn.", on_event=fail)
        self.assertEqual(agent.run("Next turn.").status, "completed")

    def test_server_failure_is_typed_and_client_can_restart(self):
        agent = self.client.agent()
        with self.assertRaises(CodexServerExited):
            agent.run("Exit server.")
        self.assertEqual(agent.run("After restart.").status, "completed")

    def test_invalid_protocol_does_not_include_raw_payload(self):
        agent = self.client.agent()
        with self.assertRaises(CodexProtocolError) as error:
            agent.run("Invalid JSON.")
        self.assertNotIn("not json", str(error.exception))

    def test_nonfinite_deadlines_rejected(self):
        agent = self.client.agent()
        for value in (float("nan"), float("inf"), 0, -1):
            with self.assertRaises(ValueError):
                agent.run("No turn.", timeout=value)

    def test_async_stream_early_close_interrupts_its_run(self):
        async def scenario():
            agent = await self.client.agent_async()
            async with aclosing(agent.stream_async("Stream then hold.")) as stream:
                event = await anext(stream)
                self.assertEqual(event.text, "started")
            result = await agent.run_async("Inspect.")
            self.assertEqual(len(json.loads(result.text)["interrupts"]), 1)

        asyncio.run(scenario())

    def test_sync_stream_early_close_interrupts_its_run(self):
        agent = self.client.agent()
        with closing(agent.stream("Stream then hold.")) as stream:
            self.assertEqual(next(stream).text, "started")
        self.assertEqual(len(json.loads(agent.run("Inspect.").text)["interrupts"]), 1)

    def test_run_limit_rejects_without_starting_another_turn(self):
        self.client.close()
        self.client = CodexAppServer(
            self.directory.name,
            server_command=(sys.executable, str(FAKE)),
            max_concurrent_runs=1,
        )

        async def scenario():
            a, b = await self.client.agent_async(), await self.client.agent_async()
            async with aclosing(a.stream_async("Stream then hold.")) as stream:
                await anext(stream)
                with self.assertRaises(CodexBusyError):
                    await b.run_async("Must not start.")
            self.assertEqual((await b.run_async("Inspect.")).status, "completed")

        asyncio.run(scenario())

    def test_timed_out_sync_tool_keeps_slot_until_function_exits(self):
        self.client.close()
        self.client = CodexAppServer(
            self.directory.name,
            server_command=(sys.executable, str(FAKE)),
            max_concurrent_tools=1,
            tool_timeout=0.03,
        )
        release = threading.Event()
        calls = []

        def blocked(value: int):
            """Wait for explicit release."""
            calls.append(value)
            release.wait(2)

        agent = self.client.agent(tools=register_tools(self.client, [blocked]))
        try:
            first = json.loads(agent.run("Use tool.").text)["toolResponse"]["result"]
            second = json.loads(agent.run("Use tool.").text)["toolResponse"]["result"]
            self.assertFalse(first["success"])
            self.assertIn("timed out", first["contentItems"][0]["text"])
            self.assertIn("concurrency limit", second["contentItems"][0]["text"])
            self.assertEqual(calls, [7])
        finally:
            release.set()

    def test_slow_async_tool_does_not_block_other_agents(self):
        entered, release = threading.Event(), threading.Event()

        async def slow(value: int):
            """Wait while another agent makes progress."""
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.001)
            return value

        async def scenario():
            a = await self.client.agent_async(tools=register_tools(self.client, [slow]))
            b = await self.client.agent_async()
            task = asyncio.create_task(a.run_async("Use tool."))
            try:
                while not entered.is_set():
                    await asyncio.sleep(0.001)
                self.assertEqual(
                    (await b.run_async("Inspect.", timeout=1)).status, "completed"
                )
            finally:
                release.set()
            self.assertEqual((await task).status, "completed")

        asyncio.run(scenario())

    def test_cancelled_async_tool_receives_cancellation(self):
        entered, stopped = threading.Event(), threading.Event()

        async def slow(value: int):
            """Wait until the turn is cancelled."""
            entered.set()
            try:
                await asyncio.sleep(10)
            finally:
                stopped.set()

        async def scenario():
            agent = await self.client.agent_async(
                tools=register_tools(self.client, [slow])
            )
            task = asyncio.create_task(agent.run_async("Use tool."))
            while not entered.is_set():
                await asyncio.sleep(0.001)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(stopped.is_set())

        asyncio.run(scenario())

    def test_failed_parallel_run_cancels_siblings(self):
        async def scenario():
            a, b = await self.client.agent_async(), await self.client.agent_async()
            with self.assertRaises(ValueError):
                await run_parallel([(a, "Hold turn."), (b, "")], limit=2)
            self.assertEqual((await a.run_async("Inspect.")).status, "completed")

        asyncio.run(scenario())

    def test_resume_without_rebinding_tool_denies_persisted_tool_calls(self):
        calls = []
        self.client.add_tool(
            "tool",
            "A selected tool.",
            {"type": "object"},
            lambda args: calls.append(args),
        )
        agent = self.client.agent(tools=["tool"])
        self.client.close()
        resumed = self.client.resume(agent.id)
        result = resumed.run("Use persisted tool.")
        self.assertFalse(json.loads(result.text)["toolResponse"]["result"]["success"])
        self.assertEqual(calls, [])

    def test_async_tools_and_callbacks_use_callers_loop(self):
        async def scenario():
            caller = asyncio.get_running_loop()
            observed = []

            async def tool(value: int):
                """Record the application event loop."""
                observed.append(asyncio.get_running_loop())
                return value

            async def callback(message):
                observed.append(asyncio.get_running_loop())

            agent = await self.client.agent_async(
                tools=register_tools(self.client, [tool])
            )
            await agent.run_async("Use tool.", on_event=callback)
            self.assertTrue(observed)
            self.assertTrue(all(loop is caller for loop in observed))

        asyncio.run(scenario())

    def test_rejected_start_does_not_quarantine_idle_thread(self):
        agent = self.client.agent()
        with self.assertRaises(CodexProtocolError):
            agent.run("Reject turn.")
        self.assertEqual(agent.run("Retry.").status, "completed")

    def test_async_callable_tool_and_sanitized_failure(self):
        class Tool:
            async def __call__(self, arguments):
                raise ValueError("private application detail")

        self.client.add_tool("tool", "An async callable.", {"type": "object"}, Tool())
        agent = self.client.agent(tools=["tool"])
        output = json.loads(agent.run("Use tool.").text)["toolResponse"]["result"]
        self.assertFalse(output["success"])
        self.assertIn("ValueError", str(output))
        self.assertNotIn("private application detail", str(output))
