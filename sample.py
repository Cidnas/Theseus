"""Minimal client application that gives Codex access to a SQLite shop."""

from codeagent import CodexAppServer, final_text, register_tools
from examples.shop_agent.database import create_sample_database
from examples.shop_agent.tools import TOOLS


# Create the SQLite database and fill it with predictable sample data.
create_sample_database()

# Create a client whose Codex state belongs to this project.
codex = CodexAppServer(".")

# Explicitly reuse authentication from the user's normal Codex installation.
codex.import_auth("~/.codex")

# Register the shop's documented Python functions as Codex tools.
tool_names = register_tools(codex, TOOLS)

# Start a read-only agent, ask a shop question, and collect its response.
with codex:
    thread_id = codex.create_agent(
        tools=tool_names,
        sandbox="read-only",
        developer_instructions=(
            "Answer shop questions using only the registered shop tools. "
            "Use the fewest tools needed and never inspect the database directly."
        ),
    )
    messages = codex.run(
        "What did alice@example.com buy most recently, and are two more in stock?",
        thread_id,
    )

# Extract the final answer from the raw app-server messages.
print(final_text(messages))
