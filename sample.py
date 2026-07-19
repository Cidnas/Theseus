from codeagent import CodexAppServer, final_text
from examples.shop_agent.bootstrap import register_tools


codex = CodexAppServer(".")
codex.import_auth("~/.codex")
register_tools(codex)

with codex:
    thread_id = codex.create_agent(
        tools=["get_item_price"],
        sandbox="read-only",
    )
    raw_messages = codex.run(
        "According to the shop tool, how much does a notebook cost?",
        thread_id,
    )

    print(final_text(raw_messages))
