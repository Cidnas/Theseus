"""Opt-in contract checks against a real, explicitly selected Codex model.

Usage reports contain numeric counters only, never prompts or model output.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from contextlib import aclosing, contextmanager
from pathlib import Path

from theseus import CodexAppServer, register_tools, run_parallel


@unittest.skipUnless(os.environ.get("THESEUS_LIVE_TEST") == "1", "live test disabled")
class LiveCodexAppServerTests(unittest.TestCase):
    def setUp(self):
        self.model = os.environ.get("THESEUS_LIVE_MODEL")
        source_home = os.environ.get("CODEX_AUTH_HOME")
        if not self.model or not source_home:
            self.fail("Live tests require THESEUS_LIVE_MODEL and CODEX_AUTH_HOME")
        directory = tempfile.TemporaryDirectory(prefix="theseus-live-")
        self.addCleanup(directory.cleanup)
        self.client = CodexAppServer(directory.name, timeout=120, cancel_timeout=5)
        self.addCleanup(self.client.close)
        self.client.import_auth(source_home)
        self.totals = {}

    def agent(self, **options):
        return self.client.agent(
            model=self.model, effort="low", sandbox="read-only", **options
        )

    @contextmanager
    def measured(self, purpose, agent):
        started = time.monotonic()
        measurement = {"usage": None, "status": "unconfirmed", "updates": 0}

        def observe(message):
            params = message.get("params", {})
            if message.get("method") == "thread/tokenUsage/updated":
                measurement["usage"] = params.get("tokenUsage")
                measurement["updates"] += 1
            if message.get("method") == "turn/completed":
                measurement["status"] = params["turn"]["status"]

        measurement["observe"] = observe
        try:
            yield measurement
        except BaseException as error:
            measurement["status"] = type(error).__name__
            raise
        finally:
            usage = measurement["usage"]
            total = usage.get("total") if usage else None
            previous = self.totals.get(agent.id, {})
            counters = (
                {
                    key: value - previous.get(key, 0)
                    for key, value in total.items()
                    if isinstance(value, int)
                }
                if total is not None
                else None
            )
            if total is not None:
                self.totals[agent.id] = total
            row = {
                "test": self._testMethodName,
                "purpose": purpose,
                "model": self.model,
                "effort": "low",
                "status": measurement["status"],
                "seconds": round(time.monotonic() - started, 3),
                "tokens": counters,
                "usage_updates": measurement["updates"],
            }
            report = os.environ.get("THESEUS_LIVE_REPORT")
            if report:
                with Path(report).open("a", encoding="utf-8") as file:
                    file.write(json.dumps(row) + "\n")

    def check_result(self, result, expected):
        # Boolean assertions deliberately avoid dumping live conversation data.
        self.assertTrue(result.status == "completed", "turn did not complete")
        self.assertTrue(result.text == expected, "unexpected final response")
        self.assertIsNotNone(result.usage, "token usage was not reported")

    def test_real_streaming(self):
        agent = self.agent()

        async def scenario():
            with self.measured("streamed text and structured result", agent) as meter:
                async with aclosing(
                    agent.stream_async("Reply with exactly: THESEUS_OK")
                ) as stream:
                    events = [event async for event in stream]
                result = events[-1].result
                if result is not None:
                    meter.update(usage=result.usage, status=result.status)
                self.assertTrue(any(e.kind == "text_delta" for e in events))
                self.assertEqual(sum(e.kind == "completed" for e in events), 1)
                self.check_result(result, "THESEUS_OK")

        asyncio.run(scenario())

    def test_real_structured_output_with_application_context(self):
        agent = self.agent()
        schema = {
            "type": "object",
            "properties": {
                "marker": {"type": "string"},
                "accepted": {"type": "boolean"},
            },
            "required": ["marker", "accepted"],
            "additionalProperties": False,
        }
        with self.measured(
            "JSON schema and trusted application context", agent
        ) as meter:
            result = agent.run(
                "Copy the marker from the integration record and set accepted to true.",
                output_schema=schema,
                additional_context={
                    "integration_record": '{"marker":"THESEUS_CONTEXT_OK"}'
                },
                on_event=meter["observe"],
            )
            self.assertTrue(result.status == "completed", "turn did not complete")
            try:
                parsed = json.loads(result.text or "{}")
            except ValueError:
                self.fail("response did not match JSON schema")
            self.assertTrue(
                parsed == {"marker": "THESEUS_CONTEXT_OK", "accepted": True}
            )

    def test_real_skill_tool_and_resume(self):
        calls = []
        self.client.add_tool(
            "integration_value",
            "Return the integration-test value.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            lambda arguments: calls.append(arguments) or "THESEUS_TOOL_OK",
        )
        self.client.add_skill(
            "integration-check",
            "Exercise the Theseus dynamic-tool bridge.",
            "Call integration_value exactly once and return only its text output.",
        )
        agent = self.agent(tools=["integration_value"], skills=["integration-check"])
        with self.measured("custom skill invokes Python dynamic tool", agent) as meter:
            result = agent.run("Use $integration-check now.", on_event=meter["observe"])
            self.check_result(result, "THESEUS_TOOL_OK")
            self.assertTrue(calls == [{}], "tool was not called exactly once")
        self.client.close()
        agent = self.client.resume(
            agent.id,
            tools=["integration_value"],
            skills=["integration-check"],
            model=self.model,
            effort="low",
        )
        with self.measured("resume persisted thread and rebind tool", agent) as meter:
            result = agent.run(
                "Use $integration-check again. Call the tool again, don't reuse its prior result.",
                on_event=meter["observe"],
            )
            self.check_result(result, "THESEUS_TOOL_OK")
            self.assertTrue(calls == [{}, {}], "resumed tool handler was not invoked")

    def test_real_concurrent_async_tools(self):
        async def scenario():
            entered = set()
            both = asyncio.Event()
            caller_loop = asyncio.get_running_loop()

            def make_handler(label):
                async def handler():
                    """Return this agent's test marker after its peer is ready."""
                    self.assertIs(asyncio.get_running_loop(), caller_loop)
                    entered.add(label)
                    if len(entered) == 2:
                        both.set()
                    await asyncio.wait_for(both.wait(), 60)
                    return label

                handler.__name__ = "marker_" + label.lower()
                return handler

            agents = []
            for label in ("ALPHA", "BETA"):
                names = register_tools(self.client, [make_handler(label)])
                agents.append(
                    await self.client.agent_async(
                        model=self.model,
                        effort="low",
                        sandbox="read-only",
                        tools=names,
                    )
                )
            with self.measured(
                "parallel agent ALPHA with isolated async tool", agents[0]
            ) as a:
                with self.measured(
                    "parallel agent BETA with isolated async tool", agents[1]
                ) as b:

                    def observe(message):
                        thread = message.get("params", {}).get("threadId")
                        if thread == agents[0].id:
                            a["observe"](message)
                        elif thread == agents[1].id:
                            b["observe"](message)

                    results = await run_parallel(
                        [
                            (
                                agents[0],
                                "Call marker_alpha exactly once; return only its output.",
                            ),
                            (
                                agents[1],
                                "Call marker_beta exactly once; return only its output.",
                            ),
                        ],
                        limit=2,
                        on_event=observe,
                    )
                    self.check_result(results[0], "ALPHA")
                    self.check_result(results[1], "BETA")
                    self.assertEqual(len(entered), 2)

        asyncio.run(scenario())

    def test_real_cancellation_and_reuse(self):
        async def scenario():
            entered, stopped = asyncio.Event(), asyncio.Event()

            async def wait_for_release():
                """Wait until the application releases this operation."""
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()

            names = register_tools(self.client, [wait_for_release])
            agent = await self.client.agent_async(
                model=self.model,
                effort="low",
                sandbox="read-only",
                tools=names,
            )
            with self.measured(
                "interrupt active turn and cancel async tool", agent
            ) as meter:
                task = asyncio.create_task(
                    agent.run_async(
                        "Call wait_for_release exactly once and wait for its response.",
                        on_event=meter["observe"],
                    )
                )
                try:
                    await asyncio.wait_for(entered.wait(), 60)
                    self.assertTrue(await agent.cancel_async())
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    await asyncio.wait_for(stopped.wait(), 5)
                    meter["status"] = "cancelled"
                finally:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            with self.measured(
                "reuse thread after confirmed interruption", agent
            ) as meter:
                result = await agent.run_async(
                    "Do not call tools. Reply with exactly: THESEUS_REUSED",
                    on_event=meter["observe"],
                )
                self.check_result(result, "THESEUS_REUSED")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
