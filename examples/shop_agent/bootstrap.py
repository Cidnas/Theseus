"""Connect the shop's plain functions to the codeagent runtime."""

from codeagent import CodexAppServer, register_tools as register_functions

from .tools import TOOLS


def register_tools(codex: CodexAppServer) -> tuple[str, ...]:
    """Register the shop's selected functions with this app-server client."""

    return register_functions(codex, TOOLS)
