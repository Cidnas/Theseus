# Live test results

On 2026-09-22, all five live tests passed with `gpt-6-luna`, low effort,
Codex CLI `0.156.0`, and Python `3.12.3`. Total runtime: 29.5 seconds.

| Check | Result |
| --- | --- |
| Streaming | Text deltas and one final result |
| Structured output | Valid JSON using application context |
| Skill, tool, resumption | Python tool works before and after a server restart |
| Concurrent agents | Two async handlers overlap; results stay with their agents |
| Cancellation | Pending handler stops; the same thread accepts another turn |

Eight turns ran: seven completed, one deliberately interrupted. Completed turns
reported 136,998 input tokens (72,192 cached) and 171 output tokens. Resumed
thread totals were differenced to avoid counting the same usage twice.

The reported usage was approximately **$0.0073 at Standard API rates**, or
**0.1822 Standard Codex credits**, using the published rates on that date:
[API pricing](https://developers.openai.com/api/docs/pricing) and
[Codex pricing](https://learn.chatgpt.com/docs/pricing).
This is a pricing equivalent, not an account charge. The interrupted turn did
not report usage and is excluded. A tool turn can involve several model calls.

To reproduce, see [Contributing](../CONTRIBUTING.md#work-locally). Tests create
and clean up temporary state; existing project state is untouched.
