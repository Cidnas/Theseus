"""Minimal client application that gives Codex access to one shop tool."""

from codeagent import CodexAppServer, final_text, register_tools
from examples.shop_agent.tools import get_item_price


# Create a client whose Codex state belongs to this project.
codex = CodexAppServer(".")

# Explicitly reuse authentication from the user's normal Codex installation.
codex.import_auth("~/.codex")

# Describe and register the typed Python function as a Codex tool.
tool_names = register_tools(codex, [get_item_price])

# Start a read-only agent, ask it to use the tool, and collect its response.
with codex:
    thread_id = codex.create_agent(
        tools=tool_names,
        sandbox="read-only",
    )
    messages = codex.run(
        "According to the shop tool, how much does a notebook cost?",
        thread_id,
    )

# Extract the final answer from the raw app-server messages.
print(final_text(messages))
