"""Stable exceptions shared by the public API and private backend."""


class CodexAppServerError(RuntimeError):
    """Base error for the app-server client."""


class CodexProtocolError(CodexAppServerError):
    """The server returned an invalid message or rejected a request."""


class CodexTimeoutError(CodexAppServerError):
    """An operation exceeded its deadline."""


class CodexServerExited(CodexAppServerError):
    """The server connection closed before the operation finished."""


class CodexBusyError(CodexAppServerError):
    """A thread is occupied or the configured concurrency limit was reached."""


class CodexCancelledError(CodexAppServerError):
    """A run was explicitly cancelled before a turn could start."""
