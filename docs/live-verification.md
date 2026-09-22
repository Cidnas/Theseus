# Live verification: 2026-09-22

All five live integration tests passed using **`gpt-6-luna`, low reasoning
effort**, through the real Codex app-server (`0.156.0`, Python `3.12.3`). The
suite took 29.479 seconds, including process startup and cleanup. Eight turns
were attempted: seven completed and one was deliberately interrupted. Tool
turns include multiple model requests, so eight turns does not mean eight
underlying inference requests.

## What was verified

| Test | Actual behavior verified |
| --- | --- |
| Streaming | Real text deltas, exactly one final structured result, reported usage |
| Structured output | JSON conforms to the requested schema and uses trusted application context |
| Skill, tool, resume | Custom skill calls a Python handler; after server restart, the same thread calls the rebound handler again |
| Concurrent tools | Two agents invoke different async handlers; a barrier requires both handlers to run concurrently; callbacks execute on the application's event loop and results stay with their own agents |
| Cancellation/reuse | Interrupt a turn while its async handler is pending, confirm handler cancellation, then successfully run another turn on that thread |

No library defect was exposed by this live run. The default suite also passed
all 50 deterministic tests, with the five live tests skipped unless explicitly
enabled. Those tests exercise reproducible failure cases as well as state,
capability, and routing boundaries; passing them alone does not establish live
protocol compatibility.

## Measured usage and cost limits

| Turn purpose | Input tokens (including cached) | Cached input | Output | Standard API equivalent, USD |
| --- | ---: | ---: | ---: | ---: |
| Interrupt pending tool | Not reported | Not reported | Not reported | Unknown |
| Reuse interrupted thread | 12,460 | 12,032 | 9 | $0.00016762 |
| Concurrent agent ALPHA | 24,758 | 12,032 | 27 | $0.00140642 |
| Concurrent agent BETA | 24,758 | 12,032 | 27 | $0.00140642 |
| Skill invokes dynamic tool | 25,010 | 12,032 | 47 | $0.00144162 |
| Resume and invoke tool again | 25,345 | 24,064 | 30 | $0.00038374 |
| Streaming | 12,305 | 0 | 8 | $0.00123450 |
| Structured output/context | 12,362 | 0 | 23 | $0.00124770 |
| **Reported total** | **136,998** | **72,192** | **171** | **$0.00728802** |

The reported input includes 64,806 uncached tokens. Cache-write and reasoning
output counters were zero. Resumed-thread counters were differenced against the
previous turn's cumulative totals; historical usage is not counted twice.

The estimate uses the published Standard short-context GPT-6 Luna rates:
$0.10 per million uncached input tokens, $0.01 per million cached input tokens,
and $0.50 per million output tokens. Source:
[OpenAI API pricing](https://developers.openai.com/api/docs/pricing), checked
2026-09-22. The reported tokens also correspond to approximately **0.1822
Standard Codex credits** at the published token rates in
[Codex pricing](https://learn.chatgpt.com/docs/pricing).

**This is a pricing equivalent for reported usage, not a verified account
charge or complete cost total.** The interrupted turn emitted no usage counters
before cancellation, so its usage is not accounted for. Authentication came
from the owner's existing Codex login; no invoice or billing ledger was
queried. Included subscription usage, purchased credits, speed, and account
terms determine actual billing. No model substitution or paid retry was used.

All generated runtime state was confined to temporary test directories and
cleaned up. Existing project state was untouched. The test report contains only
test names, purpose labels, the selected model/effort, status, timings, and token
counts; it contains no credentials, prompts, or model responses.
